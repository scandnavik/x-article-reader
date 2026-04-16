#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

BROWSER_USE_SHIM = (
    "import asyncio, sys; "
    "loop=asyncio.new_event_loop(); "
    "asyncio.set_event_loop(loop); "
    "from browser_use.skill_cli.main import main; "
    "sys.exit(main())"
)

PLAYWRIGHT_ARTICLE_SHIM = r'''
from playwright.sync_api import sync_playwright
import json
import sys

url = sys.argv[1]

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 2200})
    page.goto(url, wait_until="domcontentloaded", timeout=120000)
    page.wait_for_timeout(10000)
    payload = page.evaluate("""() => ({
        url: location.href,
        title: document.title,
        text: document.body.innerText,
        headings: Array.from(document.querySelectorAll('h1,h2,h3'))
          .map((node) => node.innerText)
          .filter(Boolean),
        anchors: Array.from(document.querySelectorAll('a'))
          .slice(0, 20)
          .map((node) => node.innerText)
          .filter(Boolean)
    })""")
    browser.close()

print(json.dumps(payload, ensure_ascii=False))
'''.strip()

STATUS_END_MARKERS = [
    "\n想要發佈自己的文章嗎？",
    "\nWant to publish your own article?",
    "\nX 的新手？",
    "\nNew to X?",
]

ARTICLE_START_MARKERS = [
    "Don’t miss what’s happening\nPeople on X are the first to know.\nLog in\nSign up\n",
    "Don't miss what's happening\nPeople on X are the first to know.\nLog in\nSign up\n",
    "別錯過正在發生的新鮮事\nX 使用者總是搶先得知新消息。\n登入\n註冊\n",
]

ARTICLE_END_MARKERS = [
    "\nWant to publish your own Article?",
    "\n想要發佈自己的文章嗎？",
    "\nUpgrade to Premium",
]


def main() -> int:
    configure_stdio_utf8()
    args = parse_args()
    input_info = parse_input(args.input)

    if args.thread:
        if input_info["kind"] != "status":
            fail("--thread 模式目前只支援 status URL 或 tweet ID。")
        result = read_thread_input(input_info, args)
    elif input_info["kind"] == "status":
        result = read_status_input(input_info, args)
    else:
        result = read_article_input(input_info, args)

    rendered = (
        json.dumps(result, ensure_ascii=False, indent=2)
        if args.json
        else render_markdown(result)
    )

    out_target = args.out
    if not out_target and args.thread and result.get("thread"):
        out_target = default_thread_out_path(result)

    if out_target:
        out_path = Path(out_target).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(f"{rendered}\n", encoding="utf-8")
        print(f"已輸出到 {out_path}", file=sys.stderr)

    sys.stdout.write(f"{rendered}\n")
    return 0


def configure_stdio_utf8() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (LookupError, ValueError):
            continue


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read public X Articles from status or article URLs with fallback behavior."
    )
    parser.add_argument("input", help="X status URL, X article URL, or a numeric tweet ID")
    parser.add_argument("--json", action="store_true", help="Emit structured JSON")
    parser.add_argument("--out", help="Write output to a file")
    parser.add_argument("--no-browser", action="store_true", help="Skip browser-based body extraction")
    parser.add_argument("--keep-session", action="store_true", help="Keep browser_use sessions open")
    parser.add_argument(
        "--disable-status-browser",
        action="store_true",
        help="Skip the status-page browser extraction step",
    )
    parser.add_argument(
        "--disable-direct-article",
        action="store_true",
        help="Skip the direct article Playwright step",
    )
    parser.add_argument(
        "--thread",
        action="store_true",
        help="Expand the conversation and keep only self-replies from the original author",
    )
    parser.add_argument(
        "--max-scrolls",
        type=int,
        default=25,
        help="Max scroll rounds when expanding a thread (default: 25)",
    )
    return parser.parse_args()


def parse_input(raw_input: str) -> dict[str, Any]:
    raw_input = raw_input.strip()
    if re.fullmatch(r"\d+", raw_input):
        return {"kind": "status", "tweetId": raw_input, "rawInput": raw_input}

    try:
        parsed = urlparse(raw_input)
    except ValueError as exc:
        fail(f"輸入不是有效網址：{exc}")

    if parsed.scheme not in {"http", "https"}:
        fail("目前只支援 http 或 https 連結。")

    if not re.search(r"(^|\.)x\.com$|(^|\.)twitter\.com$", parsed.hostname or "", re.I):
        fail("目前只支援 x.com 或 twitter.com 的內容。")

    article_match = re.match(
        r"^/(?:(?P<handle>[^/]+)/)?article/(?P<id>\d+)",
        parsed.path,
        re.I,
    )
    if article_match:
        handle = article_match.group("handle")
        return {
            "kind": "article",
            "articleId": article_match.group("id"),
            "articleUrl": f"https://x.com{parsed.path}",
            "authorHint": None if not handle or handle.lower() == "i" else handle,
            "rawInput": raw_input,
        }

    status_match = re.search(r"/status/(\d+)", parsed.path, re.I)
    if not status_match:
        fail("網址裡找不到 status ID 或 article ID。")

    return {"kind": "status", "tweetId": status_match.group(1), "rawInput": raw_input}


def read_status_input(input_info: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    tweet_id = input_info["tweetId"]
    syndication = fetch_tweet_result(tweet_id)
    canonical_status_url = build_status_url(tweet_id, syndication)
    expanded_tweet_text = expand_tweet_urls(
        syndication.get("text", ""),
        syndication.get("entities", {}).get("urls", []),
    )
    user = syndication.get("user", {})
    article_meta = syndication.get("article") or {}
    article_id = article_meta.get("rest_id")
    article_title = article_meta.get("title")
    article_preview = article_meta.get("preview_text")
    author_handle = user.get("screen_name")

    result: dict[str, Any] = {
        "source": "x.com",
        "input": input_info.get("rawInput", tweet_id),
        "tweetId": tweet_id,
        "statusUrl": canonical_status_url,
        "articleUrl": f"https://x.com/i/article/{article_id}" if article_id else None,
        "author": {
            "name": user.get("name"),
            "handle": author_handle,
        },
        "createdAt": syndication.get("created_at"),
        "lang": syndication.get("lang"),
        "stats": {
            "favorites": syndication.get("favorite_count"),
            "replies": syndication.get("conversation_count"),
        },
        "tweetText": expanded_tweet_text,
        "warnings": [],
    }

    if not article_id:
        return result

    article_urls = build_article_urls(author_handle, article_id)
    attempts: list[dict[str, Any]] = []
    body = None
    method = None
    direct_details = None

    if not args.no_browser and not args.disable_status_browser:
        session_name = f"xread-status-{tweet_id}-{int(time.time())}"
        try:
            visible_text = extract_visible_text_with_browser_use(
                canonical_status_url,
                session_name,
                keep_session=args.keep_session,
                min_chars=2000,
            )
            body = clean_status_article_text(visible_text, article_title)
            attempts.append(
                {
                    "name": "status_page_visible_text",
                    "success": bool(body),
                    "chars": len(body or ""),
                    "url": canonical_status_url,
                }
            )
            if body:
                method = "status_page_visible_text"
        except Exception as exc:  # noqa: BLE001
            attempts.append(
                {
                    "name": "status_page_visible_text",
                    "success": False,
                    "url": canonical_status_url,
                    "error": str(exc),
                }
            )

    if not body and not args.no_browser and not args.disable_direct_article:
        for candidate_url in article_urls:
            try:
                payload = fetch_direct_article_with_playwright(candidate_url)
                cleaned = clean_direct_article_text(payload.get("text", ""))
                attempts.append(
                    {
                        "name": "playwright_article_page_visible_text",
                        "success": bool(cleaned.get("body")),
                        "chars": len(cleaned.get("body") or ""),
                        "url": candidate_url,
                    }
                )
                if cleaned.get("body"):
                    body = cleaned["body"]
                    method = "playwright_article_page_visible_text"
                    direct_details = cleaned
                    break
            except Exception as exc:  # noqa: BLE001
                attempts.append(
                    {
                        "name": "playwright_article_page_visible_text",
                        "success": False,
                        "url": candidate_url,
                        "error": str(exc),
                    }
                )

    if not body:
        if args.no_browser:
            result["warnings"].append("已停用瀏覽器擷取，只保留公開摘要。")
            method = "preview_only"
        else:
            result["warnings"].append("正文沒有成功抓到，已退回摘要模式。")
            method = method or "preview_only"

    result["article"] = {
        "id": article_id,
        "title": direct_details.get("title") if direct_details and direct_details.get("title") else article_title,
        "previewText": article_preview,
        "body": body,
        "extraction": {
            "method": method,
            "success": bool(body),
            "attempts": attempts,
        },
    }

    if direct_details:
        if not result["author"]["name"] and direct_details.get("authorName"):
            result["author"]["name"] = direct_details["authorName"]
        if not result["author"]["handle"] and direct_details.get("authorHandle"):
            result["author"]["handle"] = direct_details["authorHandle"]

    return result


def read_article_input(input_info: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    article_id = input_info["articleId"]
    author_hint = input_info.get("authorHint")
    attempts: list[dict[str, Any]] = []
    warnings: list[str] = []
    body = None
    cleaned_details = None
    method = None

    candidate_urls = []
    if author_hint:
        candidate_urls.append(f"https://x.com/{author_hint}/article/{article_id}")
    candidate_urls.append(f"https://x.com/i/article/{article_id}")

    if args.no_browser:
        warnings.append("直接 article 連結目前需要瀏覽器提取；已略過正文抓取。")
        method = "preview_only"
    else:
        for candidate_url in dedupe_list(candidate_urls):
            try:
                payload = fetch_direct_article_with_playwright(candidate_url)
                cleaned = clean_direct_article_text(payload.get("text", ""))
                attempts.append(
                    {
                        "name": "playwright_article_page_visible_text",
                        "success": bool(cleaned.get("body")),
                        "chars": len(cleaned.get("body") or ""),
                        "url": candidate_url,
                    }
                )
                if cleaned.get("body"):
                    body = cleaned["body"]
                    cleaned_details = cleaned
                    method = "playwright_article_page_visible_text"
                    break
            except Exception as exc:  # noqa: BLE001
                attempts.append(
                    {
                        "name": "playwright_article_page_visible_text",
                        "success": False,
                        "url": candidate_url,
                        "error": str(exc),
                    }
                )

    if not body:
        warnings.append("直接 article 連結沒有成功整理出正文；若有原始 status URL，改貼 status 會更穩。")
        method = method or "preview_only"

    return {
        "source": "x.com",
        "input": input_info["articleUrl"],
        "tweetId": None,
        "statusUrl": None,
        "articleUrl": input_info["articleUrl"],
        "author": {
            "name": cleaned_details.get("authorName") if cleaned_details else None,
            "handle": cleaned_details.get("authorHandle") if cleaned_details else author_hint,
        },
        "createdAt": None,
        "lang": None,
        "stats": {
            "favorites": None,
            "replies": None,
        },
        "tweetText": None,
        "warnings": warnings,
        "article": {
            "id": article_id,
            "title": cleaned_details.get("title") if cleaned_details else None,
            "previewText": cleaned_details.get("previewText") if cleaned_details else None,
            "body": body,
            "extraction": {
                "method": method,
                "success": bool(body),
                "attempts": attempts,
            },
        },
    }


COOKIES_PATH = Path.home() / ".x-article-reader" / "cookies.json"

PLAYWRIGHT_THREAD_SHIM = r'''
from playwright.sync_api import sync_playwright
import json
import sys
import time
from pathlib import Path

url = sys.argv[1]
cookies_path = Path(sys.argv[2])
max_scrolls = int(sys.argv[3])

SAME_SITE_MAP = {
    "no_restriction": "None",
    "none": "None",
    "lax": "Lax",
    "strict": "Strict",
    "unspecified": None,
}


def normalize_cookies(raw):
    out = []
    for c in raw:
        name = c.get("name")
        value = c.get("value")
        domain = c.get("domain")
        if not name or value is None or not domain:
            continue
        item = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": c.get("path") or "/",
            "httpOnly": bool(c.get("httpOnly")),
            "secure": bool(c.get("secure")),
        }
        expires = c.get("expires")
        if expires is None:
            expires = c.get("expirationDate")
        if isinstance(expires, (int, float)) and expires > 0:
            item["expires"] = float(expires)
        same_site = c.get("sameSite")
        if isinstance(same_site, str):
            mapped = SAME_SITE_MAP.get(same_site.lower(), same_site)
            if mapped in {"None", "Lax", "Strict"}:
                item["sameSite"] = mapped
        out.append(item)
    return out


EXTRACT_JS = """
() => {
  const nodes = Array.from(document.querySelectorAll('article[data-testid=\"tweet\"]'));
  const items = nodes.map((node) => {
    const timeAnchor = node.querySelector('a[href*=\"/status/\"] time');
    const href = timeAnchor ? timeAnchor.parentElement.getAttribute('href') : null;
    const datetime = timeAnchor ? timeAnchor.getAttribute('datetime') : null;
    const m = href ? href.match(/^\\/([^/]+)\\/status\\/(\\d+)/) : null;
    const textNode = node.querySelector('[data-testid=\"tweetText\"]');
    const text = textNode ? textNode.innerText : '';
    let photoCount = 0;
    try { photoCount = node.querySelectorAll('[data-testid=\"tweetPhoto\"]').length; } catch (e) {}
    return {
      handle: m ? m[1] : null,
      statusId: m ? m[2] : null,
      datetime: datetime,
      text: text,
      hasPhoto: photoCount > 0
    };
  }).filter((i) => i.handle && i.statusId && i.text);
  const seen = new Set();
  const unique = [];
  for (const it of items) {
    if (seen.has(it.statusId)) continue;
    seen.add(it.statusId);
    unique.push(it);
  }
  return unique;
}
"""

raw_cookies = []
if cookies_path.exists():
    try:
        raw = json.loads(cookies_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "cookies" in raw:
            raw = raw["cookies"]
        if isinstance(raw, list):
            raw_cookies = normalize_cookies(raw)
    except Exception as exc:
        print(json.dumps({"error": f"cookie load failed: {exc}"}), flush=True)
        sys.exit(2)

collected = {}
warnings = []

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        viewport={"width": 1440, "height": 2400},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ),
    )
    if raw_cookies:
        try:
            context.add_cookies(raw_cookies)
        except Exception as exc:
            warnings.append(f"add_cookies failed: {exc}")
    page = context.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=120000)
    page.wait_for_timeout(4500)

    last_height = 0
    stable = 0
    expand_js = """
    () => {
      const links = Array.from(document.querySelectorAll('[data-testid=\"tweet-text-show-more-link\"]'));
      let clicked = 0;
      for (const link of links) {
        try { link.click(); clicked += 1; } catch (e) {}
      }
      return clicked;
    }
    """
    for _ in range(max_scrolls):
        try:
            page.evaluate(expand_js)
            page.wait_for_timeout(600)
        except Exception as exc:
            warnings.append(f"expand failed: {exc}")
        try:
            batch = page.evaluate(EXTRACT_JS)
        except Exception as exc:
            warnings.append(f"evaluate failed: {exc}")
            batch = []
        for item in batch or []:
            sid = item.get("statusId")
            if sid and sid not in collected:
                collected[sid] = item
        height = page.evaluate("document.body.scrollHeight") or 0
        if height == last_height:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        last_height = height
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1800)

    browser.close()

print(json.dumps({
    "tweets": list(collected.values()),
    "warnings": warnings,
    "cookiesLoaded": len(raw_cookies),
}, ensure_ascii=False))
'''.strip()


def read_thread_input(input_info: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    tweet_id = input_info["tweetId"]
    syndication = fetch_tweet_result(tweet_id)
    canonical_status_url = build_status_url(tweet_id, syndication)
    user = syndication.get("user", {})
    author_handle = user.get("screen_name")
    if not author_handle:
        fail("無法從公開資料判定原推作者 handle。")

    warnings: list[str] = []
    tweets: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []

    if args.no_browser:
        fail("--thread 需要瀏覽器擷取，無法與 --no-browser 同用。")

    if not COOKIES_PATH.exists():
        warnings.append(
            f"沒找到 {COOKIES_PATH}；X 對匿名用戶只顯示原推，self-reply 會抓不到。"
            " 請用 Cookie-Editor 等擴充套件從已登入的 Chrome 匯出 x.com cookies 到上述路徑。"
        )

    try:
        thread_payload = expand_thread_with_playwright(
            canonical_status_url,
            max_scrolls=args.max_scrolls,
        )
        tweets = thread_payload.get("tweets") or []
        cookies_loaded = thread_payload.get("cookiesLoaded") or 0
        for warning in thread_payload.get("warnings") or []:
            warnings.append(warning)
        attempts.append(
            {
                "name": "playwright_thread_scroll",
                "success": bool(tweets),
                "count": len(tweets),
                "cookiesLoaded": cookies_loaded,
                "url": canonical_status_url,
            }
        )
    except Exception as exc:  # noqa: BLE001
        attempts.append(
            {
                "name": "playwright_thread_scroll",
                "success": False,
                "url": canonical_status_url,
                "error": str(exc),
            }
        )
        warnings.append(f"展開對話失敗：{exc}")

    author_lower = author_handle.lower()
    self_tweets = [t for t in tweets if (t.get("handle") or "").lower() == author_lower]

    if not self_tweets:
        warnings.append("沒有抓到原作者的貼文節點，可能需要登入 session 或頁面尚未完全載入。")

    ordered = order_thread_tweets(self_tweets, root_id=tweet_id)

    return {
        "source": "x.com",
        "input": input_info.get("rawInput", tweet_id),
        "tweetId": tweet_id,
        "statusUrl": canonical_status_url,
        "articleUrl": None,
        "author": {
            "name": user.get("name"),
            "handle": author_handle,
        },
        "createdAt": syndication.get("created_at"),
        "lang": syndication.get("lang"),
        "stats": {
            "favorites": syndication.get("favorite_count"),
            "replies": syndication.get("conversation_count"),
        },
        "tweetText": expand_tweet_urls(
            syndication.get("text", ""),
            syndication.get("entities", {}).get("urls", []),
        ),
        "warnings": warnings,
        "thread": {
            "rootId": tweet_id,
            "count": len(ordered),
            "totalCapturedInPage": len(tweets),
            "tweets": ordered,
            "extraction": {
                "method": "thread_scroll_expand",
                "success": bool(ordered),
                "attempts": attempts,
            },
        },
    }


def expand_thread_with_playwright(url: str, *, max_scrolls: int) -> dict[str, Any]:
    completed = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-c",
            PLAYWRIGHT_THREAD_SHIM,
            url,
            str(COOKIES_PATH),
            str(max_scrolls),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
    )
    if completed.returncode != 0:
        message = (completed.stdout + completed.stderr).strip() or f"playwright thread exit {completed.returncode}"
        raise RuntimeError(message)
    try:
        return json.loads(completed.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise RuntimeError(f"無法解析 thread 輸出：{exc}\n{completed.stdout[:500]}") from exc


def order_thread_tweets(tweets: list[dict[str, Any]], *, root_id: str) -> list[dict[str, Any]]:
    def sort_key(item: dict[str, Any]) -> tuple[int, str, str]:
        is_root = 0 if item.get("statusId") == root_id else 1
        datetime_value = item.get("datetime") or ""
        status_id = item.get("statusId") or ""
        return (is_root, datetime_value, status_id)

    return sorted(tweets, key=sort_key)


def fetch_tweet_result(tweet_id: str) -> dict[str, Any]:
    url = f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&token=x"
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        fail(f"公開貼文資料讀取失敗：HTTP {exc.code}")
    except URLError as exc:
        fail(f"公開貼文資料讀取失敗：{exc.reason}")

    if not isinstance(payload, dict) or not payload.get("id_str"):
        fail("公開貼文資料格式不完整。")

    return payload


def build_status_url(tweet_id: str, syndication: dict[str, Any]) -> str:
    handle = syndication.get("user", {}).get("screen_name")
    if handle:
        return f"https://x.com/{handle}/status/{tweet_id}"
    return f"https://x.com/i/status/{tweet_id}"


def expand_tweet_urls(text: str, urls: list[dict[str, Any]]) -> str:
    output = text
    for item in urls:
        short = item.get("url")
        expanded = item.get("expanded_url")
        if short and expanded:
            output = output.replace(short, expanded)
    return output


def build_article_urls(handle: str | None, article_id: str) -> list[str]:
    urls = []
    if handle:
        urls.append(f"https://x.com/{handle}/article/{article_id}")
    urls.append(f"https://x.com/i/article/{article_id}")
    return dedupe_list(urls)


def extract_visible_text_with_browser_use(
    url: str,
    session_name: str,
    *,
    keep_session: bool,
    min_chars: int,
) -> str:
    run_browser_use(session_name, ["open", url], timeout=180)
    try:
        visible_length = poll_visible_length(session_name, min_chars=min_chars, timeout_seconds=20)
        return read_visible_text_chunks(session_name, visible_length)
    finally:
        if not keep_session:
            try:
                run_browser_use(session_name, ["close"], timeout=30)
            except Exception:  # noqa: BLE001
                pass


def poll_visible_length(session_name: str, *, min_chars: int, timeout_seconds: int) -> int:
    started_at = time.time()
    while time.time() - started_at < timeout_seconds:
        raw = run_browser_use(
            session_name,
            ["eval", "document.body.innerText.length"],
            timeout=60,
        )
        visible_length = int(strip_browser_result(raw).strip() or "0")
        if visible_length >= min_chars:
            return visible_length
        time.sleep(1.2)
    raise RuntimeError("等待頁面正文載入逾時。")


def read_visible_text_chunks(session_name: str, visible_length: int) -> str:
    chunks = []
    chunk_size = 6000
    for start in range(0, visible_length, chunk_size):
        end = min(start + chunk_size, visible_length)
        raw = run_browser_use(
            session_name,
            ["eval", f"document.body.innerText.slice({start}, {end})"],
            timeout=60,
        )
        chunks.append(strip_browser_result(raw))
    return "".join(chunks)


def run_browser_use(session_name: str, args: list[str], *, timeout: int) -> str:
    command = [
        sys.executable,
        "-X",
        "utf8",
        "-c",
        BROWSER_USE_SHIM,
        "-s",
        session_name,
        *args,
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
    )
    if completed.returncode != 0:
        message = (completed.stdout + completed.stderr).strip() or f"browser_use exit {completed.returncode}"
        raise RuntimeError(message)
    return completed.stdout.strip()


def fetch_direct_article_with_playwright(article_url: str) -> dict[str, Any]:
    completed = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-c",
            PLAYWRIGHT_ARTICLE_SHIM,
            article_url,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
    )
    if completed.returncode != 0:
        message = (completed.stdout + completed.stderr).strip() or f"playwright exit {completed.returncode}"
        raise RuntimeError(message)
    return json.loads(completed.stdout)


def strip_browser_result(text: str) -> str:
    return re.sub(r"^result:\s*", "", text.replace("\r", ""), flags=re.U)


def clean_status_article_text(visible_text: str, article_title: str | None) -> str | None:
    if not visible_text:
        return None

    text = visible_text.replace("\r", "").strip()
    if article_title:
        title_index = text.find(article_title)
        if title_index >= 0:
            text = text[title_index:]

    for marker in STATUS_END_MARKERS:
        marker_index = text.find(marker)
        if marker_index >= 0:
            text = text[:marker_index].strip()
            break

    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        return None

    cleaned_lines = [lines[0]]
    index = 1
    while index < len(lines) and is_metric_line(lines[index]):
        index += 1
    cleaned_lines.extend(lines[index:])

    cleaned_text = "\n".join(cleaned_lines).strip()
    return cleaned_text or None


def clean_direct_article_text(visible_text: str) -> dict[str, Any]:
    if not visible_text:
        return {
            "title": None,
            "previewText": None,
            "body": None,
            "authorName": None,
            "authorHandle": None,
        }

    text = visible_text.replace("\r", "").strip()
    for marker in ARTICLE_START_MARKERS:
        if text.startswith(marker):
            text = text[len(marker):].strip()
            break

    for marker in ARTICLE_END_MARKERS:
        marker_index = text.find(marker)
        if marker_index >= 0:
            text = text[:marker_index].strip()
            break

    if "This page is not supported." in text or "此頁面不受支援。" in text:
        return {
            "title": None,
            "previewText": None,
            "body": None,
            "authorName": None,
            "authorHandle": None,
        }

    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        return {
            "title": None,
            "previewText": None,
            "body": None,
            "authorName": None,
            "authorHandle": None,
        }

    title = lines[0]
    author_name = None
    author_handle = None
    index = 1

    if index < len(lines) and not is_direct_article_meta_line(lines[index]):
        author_name = lines[index]
        index += 1

    if index < len(lines) and lines[index].startswith("@"):
        author_handle = lines[index].lstrip("@")
        index += 1

    while index < len(lines) and is_direct_article_meta_line(lines[index]):
        index += 1

    content_lines = [title, *lines[index:]]
    preview_text = lines[index] if index < len(lines) else None
    body = "\n".join(content_lines).strip() or None

    return {
        "title": title,
        "previewText": preview_text,
        "body": body,
        "authorName": author_name,
        "authorHandle": author_handle,
    }


def is_metric_line(line: str) -> bool:
    return bool(re.fullmatch(r"(·|[0-9][0-9.,]*([萬千KMB])?|[0-9][0-9.,]*\s*次查看)", line, re.U))


def is_direct_article_meta_line(line: str) -> bool:
    month_date = re.fullmatch(r"[A-Z][a-z]{2}\s+\d{1,2}(,\s+\d{4})?", line)
    zh_date = re.fullmatch(r"\d{4}年\d{1,2}月\d{1,2}日", line)
    return (
        line == "·"
        or line.lower() in {"follow", "following"}
        or is_metric_line(line)
        or bool(re.fullmatch(r"[0-9]+[smhdwy]", line, re.I))
        or bool(month_date)
        or bool(zh_date)
    )


def render_markdown(result: dict[str, Any]) -> str:
    if result.get("thread"):
        return render_thread_markdown(result)
    lines = []
    article = result.get("article") or {}
    title = article.get("title")
    lines.append(f"# {title}" if title else "# X 貼文")
    lines.append("")
    lines.append("## 來源")
    lines.append(f"- 作者：{render_author(result.get('author') or {})}")
    lines.append(f"- 時間：{result.get('createdAt') or '未知'}")
    lines.append(
        f"- 連結：{result.get('statusUrl') or result.get('articleUrl') or result.get('input')}"
    )
    lines.append(f"- 喜歡：{nullable_number((result.get('stats') or {}).get('favorites'))}")
    lines.append(f"- 回覆：{nullable_number((result.get('stats') or {}).get('replies'))}")

    preview_text = article.get("previewText")
    if preview_text:
        lines.extend(["", "## 摘要", preview_text])

    body = article.get("body")
    if body:
        lines.extend(["", "## 正文"])
        body_lines = body.split("\n")
        content = "\n".join(body_lines[1:]).strip() if title and body_lines[0] == title else body
        lines.append(content)
    else:
        lines.extend(["", "## 內文", result.get("tweetText") or "(無文字內容)"])

    warnings = result.get("warnings") or []
    if warnings:
        lines.append("")
        lines.append("## 備註")
        for warning in warnings:
            lines.append(f"- {warning}")

    return "\n".join(lines)


def default_thread_out_path(result: dict[str, Any]) -> str:
    author = result.get("author") or {}
    handle = (author.get("handle") or "unknown").lower()
    tweet_id = result.get("tweetId") or "unknown"
    return str(
        Path.home()
        / "ai-outputs"
        / "01-內容生產"
        / "00-靈感收集"
        / "x-captures"
        / f"{handle}-{tweet_id}.md"
    )


def render_thread_frontmatter(result: dict[str, Any]) -> list[str]:
    author = result.get("author") or {}
    handle = author.get("handle") or ""
    name = author.get("name") or handle or "unknown"
    thread = result.get("thread") or {}
    tweets = thread.get("tweets") or []
    first_text = (tweets[0].get("text") if tweets else "") or ""
    title_snippet = first_text.strip().replace("\n", " ")[:60] or f"{name} thread"
    title_snippet = title_snippet.replace('"', "'")
    created = result.get("createdAt") or ""
    today = time.strftime("%Y-%m-%d")
    tags = ["x-thread", "x-capture"]
    if handle:
        tags.append(f"author-{handle.lower()}")

    lines = ["---"]
    lines.append(f'title: "{title_snippet}"')
    lines.append("domain: x-captures")
    lines.append("status: seed")
    lines.append(f"tags: {json.dumps(tags, ensure_ascii=False)}")
    lines.append(f'source: "{result.get("statusUrl") or result.get("input") or ""}"')
    lines.append(f'author: "{name}"')
    if handle:
        lines.append(f'handle: "@{handle}"')
    if created:
        lines.append(f'posted_at: "{created}"')
    lines.append(f"captured: {today}")
    lines.append(f"tweet_count: {thread.get('count') or 0}")
    lines.append("---")
    lines.append("")
    return lines


def render_thread_markdown(result: dict[str, Any]) -> str:
    lines: list[str] = render_thread_frontmatter(result)
    author = result.get("author") or {}
    author_label = render_author(author)
    lines.append(f"# {author_label} 的 thread")
    lines.append("")
    lines.append("## 來源")
    lines.append(f"- 作者：{author_label}")
    lines.append(f"- 連結：{result.get('statusUrl') or result.get('input')}")
    thread = result.get("thread") or {}
    lines.append(f"- 作者本人貼文數：{thread.get('count') or 0}")
    lines.append(f"- 頁面總節點數：{thread.get('totalCapturedInPage') or 0}")
    lines.append("")
    tweets = thread.get("tweets") or []
    for index, item in enumerate(tweets):
        if index > 0:
            lines.append("")
            lines.append("---")
            lines.append("")
        marker = "原推" if item.get("statusId") == thread.get("rootId") else f"#{index + 1}"
        datetime_value = item.get("datetime") or "?"
        lines.append(f"### {marker}  ·  {datetime_value}")
        status_id = item.get("statusId")
        handle = item.get("handle")
        if status_id and handle:
            lines.append(f"https://x.com/{handle}/status/{status_id}")
        body_text = item.get("text") or ""
        if body_text:
            lines.append("")
            lines.append(body_text)
        if item.get("hasPhoto"):
            lines.append("")
            lines.append("_(含圖片)_")

    warnings = result.get("warnings") or []
    if warnings:
        lines.append("")
        lines.append("## 備註")
        for warning in warnings:
            lines.append(f"- {warning}")

    return "\n".join(lines)


def render_author(author: dict[str, Any]) -> str:
    name = author.get("name")
    handle = author.get("handle")
    if name and handle:
        return f"{name} (@{handle})"
    return name or handle or "未知"


def nullable_number(value: Any) -> str:
    return "未知" if value is None else str(value)


def dedupe_list(items: list[str]) -> list[str]:
    output = []
    seen = set()
    for item in items:
        if item not in seen:
            output.append(item)
            seen.add(item)
    return output


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    raise SystemExit(main())

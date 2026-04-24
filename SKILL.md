---
name: "x-article-reader"
description: "Use when the user shares an X status or X article URL and wants the article text, a clean Markdown export, or a low-token way to read or summarize a public X Article. Also supports --thread mode to capture the author's full self-reply chain under a status. This is especially important when a status is only a wrapper around a long article."
---

# X Article Reader

Use this skill when the user gives you:
- an `x.com/.../status/...` link that is really pointing at an X Article
- an `x.com/<handle>/article/<id>` link
- a status URL where the author has a self-reply thread underneath (use `--thread`)
- a request to read, export, or summarize a public X Article without relying on the official X API

This skill is designed for public X content on this machine. It does not require an official X API key. `--thread` mode requires a logged-in cookie jar (see below).

## What the bundled script does

The script in `scripts/x_article_reader.py` uses a 3-step strategy:

1. Read public metadata from the syndication endpoint to get the article title and preview.
2. If the input is a status URL and the user wants full text, open the status page in a browser session and extract the visible article text.
3. If full text still cannot be extracted, fall back to the preview instead of failing hard.

For direct article URLs, the script uses a Playwright-based browser pass.

## Preferred commands

On Windows:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/x_article_reader.ps1 "<x-url>" --json
```

On macOS or Linux:

```bash
bash scripts/x_article_reader.sh "<x-url>" --json
```

If you only need the cheap preview fallback:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/x_article_reader.ps1 "<status-url>" --json --no-browser
```

## Output modes

- Default output: Markdown, suitable for direct reading or summarization.
- `--json`: structured output with metadata, warnings, extraction method, and article body when available.
- `--out <file>`: write the result to a file.

## Fallback rules

1. If the input is a status URL, prefer the status-page extraction path.
2. If browser extraction fails, keep the article title and preview from the public metadata endpoint.
3. If the user only provided an `x.com/i/article/...` link and extraction fails, report that the link is not directly supported and ask for the original status URL when possible.

## Thread mode (`--thread`)

Captures the original tweet plus every self-reply from the same author under the same status, with long-tweet "Show more" auto-expanded.

```bash
python scripts/x_article_reader.py "<status-url>" --thread
```

Behavior:
- Output Markdown is auto-saved to `~/ai-outputs/01-內容生產/00-靈感收集/x-captures/<handle>-<statusid>.md` when `--out` is not supplied.
- Frontmatter includes `title`, `domain: research` (fallback; user may reclassify to `ai-agent` / `claude-code` by topic), `source`, `author`, `handle`, `posted_at`, `captured`, `tweet_count`.
- Replies from other users are filtered out; only the original author's posts are kept.

### Cookie jar setup (required once)

X serves only the root tweet to anonymous visitors. To read self-replies the script injects cookies from an already-logged-in browser session.

1. In your daily Chrome (where x.com is logged in), install a cookie export extension such as Cookie-Editor.
2. Open `https://x.com`, click the extension, export cookies as JSON.
3. Save the JSON to `~/.x-article-reader/cookies.json` (create the folder if missing).

The file is read every run. Rotate when the session expires (symptom: `--thread` returns only the root tweet).

## Harness

This skill includes a live harness:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_harness.ps1
```

The harness writes reports under:

```text
~/ai-outputs/04-技能驗證/x-article-reader
```

Use it after changing the script. The harness checks:
- full extraction from a real status-wrapped article
- preview-only fallback
- direct article extraction on a real author/article URL

## Validation checklist

Before reporting success:
- run the harness at least once
- confirm the status test returns real article body text
- confirm the preview fallback test returns preview text without crashing
- confirm the direct article test returns article body text


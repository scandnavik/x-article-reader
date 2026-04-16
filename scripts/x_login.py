#!/usr/bin/env python3
"""Headed login helper. Opens a persistent Chromium profile and waits for the
user to finish logging in to X. Cookies are saved under ~/.x-article-reader/profile.
Run once; subsequent --thread runs will reuse the saved session.
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE_DIR = Path.home() / ".x-article-reader" / "profile"


def main() -> int:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Profile dir: {PROFILE_DIR}", file=sys.stderr)
    print("Opening X login. Finish login in the browser, then press Enter here to save and close.", file=sys.stderr)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            channel="chrome",
            headless=False,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://x.com/login", wait_until="domcontentloaded", timeout=120000)

        try:
            input()
        except EOFError:
            pass

        context.close()

    print("Session saved.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

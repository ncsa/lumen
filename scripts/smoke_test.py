#!/usr/bin/env python
"""Crawl every reachable in-app link and probe known edge cases for dead pages.

Catches broken links, unhandled 404/500s, and pages that crash. Run against a
local dev instance (see scripts/README.md). Exits non-zero if any problem is found.

    BASE_URL=http://localhost:5001 uv run python scripts/smoke_test.py
"""
import os
import sys
from collections import deque
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE_URL", "http://localhost:5001").rstrip("/")
HOST = urlparse(BASE).netloc

# Direct probes for URLs not reachable by normal links — these must return a
# friendly themed page (or a real 404), never an unstyled crash.
EDGE_PROBES = [
    "/models/does-not-exist",
    "/clients/999999",
    "/admin/users/999999/profile",
    "/help/nonexistent-page",
    "/profile/client/999999",
]


def norm(u):
    u = u.split("#")[0]
    return u if u == BASE + "/" else u.rstrip("/")


def launch(pw):
    """Launch chromium, falling back to a cached Chrome-for-Testing if needed."""
    try:
        return pw.chromium.launch()
    except Exception:
        exe = os.environ.get("CHROME_PATH")
        if exe:
            return pw.chromium.launch(executable_path=exe)
        raise


def main():
    problems = []
    with sync_playwright() as pw:
        browser = launch(pw)
        page = browser.new_context().new_page()
        page.goto(BASE + "/devlogin", wait_until="domcontentloaded")

        seen, queue = set(), deque([BASE + "/", norm(page.url)])
        while queue:
            url = norm(queue.popleft())
            if url in seen or "/logout" in url:
                continue
            seen.add(url)
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=20000)
                status = resp.status if resp else 0
            except Exception as e:
                problems.append((url, f"ERROR {type(e).__name__}"))
                continue
            if status >= 400:
                problems.append((url, status))
            for h in page.eval_on_selector_all("a[href]", "els => els.map(e => e.getAttribute('href'))"):
                if not h or h.split(":")[0] in ("mailto", "tel", "javascript"):
                    continue
                full = urljoin(BASE, h)
                if urlparse(full).netloc == HOST and norm(full) not in seen:
                    queue.append(norm(full))

        crawled = len(seen)

        for path in EDGE_PROBES:
            resp = page.goto(BASE + path, wait_until="domcontentloaded")
            body = page.content()
            if "Traceback (most recent call last)" in body or "werkzeug.exceptions" in body:
                problems.append((path, "RAW CRASH PAGE"))
            elif resp and resp.status >= 500:
                problems.append((path, resp.status))
        browser.close()

    print(f"Crawled {crawled} pages + {len(EDGE_PROBES)} edge probes.")
    if problems:
        print(f"\n{len(problems)} PROBLEM(S):")
        for url, why in problems:
            print(f"  [{why}] {url}")
        sys.exit(1)
    print("No dead pages found. ✓")


if __name__ == "__main__":
    main()

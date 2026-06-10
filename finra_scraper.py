"""
FINRA beginner-resource scraper (TLS-impersonation edition).

FINRA.org sits behind Cloudflare bot management. A headless browser is
hard-blocked by its automation fingerprint, but the pages are plain
server-rendered HTML, so the reliable way through is to impersonate a real
Chrome's TLS/HTTP-2 fingerprint with curl_cffi and behave like a polite human:
slow, jittered, sequential requests that STOP the moment Cloudflare challenges
us (so we never hammer an IP into a block).

It crawls FINRA's investor-education sections breadth-first starting from a set
of beginner-friendly hubs, extracts each page's main body, converts it to clean
Markdown, and writes one .md file per page with YAML front matter.

Usage:
    uv run finra_scraper.py                 # full crawl (resumes; skips done)
    uv run finra_scraper.py --max-pages 15  # quick test
    uv run finra_scraper.py --delay 5       # extra-polite 5s base delay
    uv run finra_scraper.py --proxy http://user:pass@host:port

Safeguards that protect your IP:
  * Chrome TLS/JA3 impersonation so requests look like a real browser.
  * Sequential only — never parallel; base delay + random jitter between hits.
  * Hard stop on the FIRST Cloudflare block (403/429/503 or challenge HTML):
    the crawl aborts instead of retrying into a deeper block.
  * Resume: pages already saved on disk are skipped, so re-runs are cheap.
"""

from __future__ import annotations

import argparse
import random
import re
import sys
import time
from collections import deque
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests
from markdownify import markdownify as md

BASE = "https://www.finra.org"

# Seed pages: the beginner-friendly hubs. The crawler discovers the individual
# articles/topics underneath these on its own.
SEEDS = [
    "/investors/learn-to-invest",
    "/investors/investing/investing-basics",
    "/investors/investing/investment-products",
    "/investors/investing/investment-accounts",
    "/investors/investing/working-with-investment-professional",
    "/investors/investing/investing-basics/smart-investing-courses",
    "/investors/insights",
    "/investors/tools-and-calculators",
    "/investors/free-investor-publications",
    "/investors/insights/tips-new-investors",
]

# The Investor Insights feed is paginated; seed every page so we capture the
# full back-catalogue of articles (page=0 is the same as the bare URL).
SEEDS += [f"/investors/insights?page={n}" for n in range(0, 17)]

# Only follow links whose path starts with one of these prefixes.
ALLOWED_PREFIXES = (
    "/investors/investing",
    "/investors/insights",
    "/investors/learn-to-invest",
    "/investors/tools-and-calculators",
    "/investors/free-investor-publications",
    "/investors/protect-your-money",
    "/investors/personal-finance",
)

# Never crawl these (binary downloads, external tools, account flows, etc.).
SKIP_SUBSTRINGS = (
    "/login", "/user", "brokercheck", "fundanalyzer", "morningstar",
    "mailto:", "tel:", "javascript:", "/sites/default/files",
    ".pdf", ".xls", ".csv", ".zip", ".doc", ".jpg", ".png", ".gif",
)

OUT_DIR = Path("finra_resources")


class CloudflareBlocked(Exception):
    """Raised the moment we detect a Cloudflare challenge, to stop the crawl."""


def is_allowed(path: str) -> bool:
    return any(path.startswith(p) for p in ALLOWED_PREFIXES)


def should_skip(url: str) -> bool:
    low = url.lower()
    return any(s in low for s in SKIP_SUBSTRINGS)


def normalize(href: str, current_url: str) -> str | None:
    """Resolve a link to an absolute, fragment-free finra.org URL we may crawl."""
    if not href or should_skip(href) or href.startswith("#"):
        return None
    absolute = urljoin(current_url, href)
    absolute, _ = urldefrag(absolute)
    parsed = urlparse(absolute)
    if parsed.netloc not in ("www.finra.org", "finra.org"):
        return None
    if not is_allowed(parsed.path):
        return None
    # Keep ?page= query (pagination) but drop other tracking params.
    query = ""
    if parsed.query.startswith("page="):
        query = "?" + parsed.query.split("&")[0]
    return f"{BASE}{parsed.path}{query}"


def slugify(url: str) -> str:
    """Turn a URL into a safe, descriptive filename."""
    parsed = urlparse(url)
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    # Drop the leading "investors" for brevity; keep the section for context.
    if parts and parts[0] == "investors":
        parts = parts[1:]
    slug = "__".join(parts) or "index"
    if parsed.query:
        slug += "__" + parsed.query.replace("=", "-")
    return re.sub(r"[^a-zA-Z0-9_-]", "-", slug)


def looks_like_block(status: int, html: str) -> bool:
    """Detect a Cloudflare challenge/block so we can stop before we get banned."""
    if status in (403, 429, 503):
        return True
    head = html[:4000].lower()
    return (
        "just a moment" in head
        or "attention required" in head
        or "cf-challenge" in head
        or "/cdn-cgi/challenge-platform" in head
    )


def extract_content(html: str, url: str) -> dict | None:
    """Pull the title, body markdown, intro, and outbound links from a page."""
    soup = BeautifulSoup(html, "lxml")

    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else (
        soup.title.get_text(strip=True) if soup.title else url
    )

    # The real content lives in the Drupal "main-content" region. Strip the
    # chrome (nav, sidebar, footer, breadcrumbs, share widgets) before convert.
    main = (
        soup.select_one(".main-content")
        or soup.select_one('[role="main"]')
        or soup.select_one(".layout__region--middle")
        or soup.body
    )
    if main is None:
        return None

    for sel in [
        "nav", "header", "footer", ".region-sidebar-second", ".sidebar",
        ".breadcrumb", ".region-breadcrumb", ".social-share", ".addtoany",
        "script", "style", ".visually-hidden", ".pager", ".tabs",
        ".block-region-author", ".block-region-date",
        # We emit our own title heading + category, so drop the page's copies
        # to avoid a duplicated title at the top of every file.
        "h1", ".article--teaser-category", ".region-primary-title",
    ]:
        for tag in main.select(sel):
            tag.decompose()

    # Harvest outbound links BEFORE deciding whether the page is worth saving —
    # hub/listing pages have thin prose but are how we discover real articles.
    links = []
    for a in main.find_all("a", href=True):
        nxt = normalize(a["href"], url)
        if nxt:
            links.append(nxt)

    markdown = md(str(main), heading_style="ATX", strip=["img"]).strip()
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)

    intro = ""
    for line in markdown.splitlines():
        line = line.strip()
        if len(line) > 60 and not line.startswith("#"):
            intro = line
            break

    return {
        "title": title,
        "markdown": markdown,
        # Thin pages are still crawled for their links, but only saved as a
        # resource if they carry real prose.
        "save": len(markdown) >= 200,
        "intro": intro,
        "links": links,
    }


def yaml_escape(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def output_path(url: str) -> Path:
    return OUT_DIR / f"{slugify(url)}.md"


def write_markdown(url: str, data: dict) -> Path:
    OUT_DIR.mkdir(exist_ok=True)
    path = output_path(url)
    section = urlparse(url).path.strip("/").split("/")
    section = section[2] if len(section) > 2 else (section[-1] if section else "")
    front = [
        "---",
        f"title: {yaml_escape(data['title'])}",
        f"source_url: {url}",
        f"section: {section}",
        f"description: {yaml_escape(data['intro'][:300])}",
        f"scraped_at: {time.strftime('%Y-%m-%d')}",
        "---",
        "",
    ]
    path.write_text("\n".join(front) + f"# {data['title']}\n\n{data['markdown']}\n",
                    encoding="utf-8")
    return path


HEADERS = {
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def fetch(session, url: str, timeout: int) -> str:
    """GET a page with Chrome impersonation; raise CloudflareBlocked on a wall."""
    resp = session.get(url, headers=HEADERS, timeout=timeout,
                       impersonate="chrome", allow_redirects=True)
    html = resp.text or ""
    if looks_like_block(resp.status_code, html):
        raise CloudflareBlocked(f"status={resp.status_code} at {url}")
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}")
    return html


def crawl(max_pages: int, base_delay: float, proxy: str | None) -> None:
    queue: deque[str] = deque()
    seen: set[str] = set()
    for s in SEEDS:
        u = f"{BASE}{s}"
        if u not in seen:
            seen.add(u)
            queue.append(u)

    proxies = {"http": proxy, "https": proxy} if proxy else None
    session = cffi_requests.Session(proxies=proxies)

    saved = skipped = visited = 0
    try:
        while queue and visited < max_pages:
            url = queue.popleft()

            # Resume support: don't re-fetch a page we already have on disk.
            if output_path(url).exists():
                skipped += 1
                # Still need its links to reach deeper pages on a fresh run,
                # but to keep re-runs cheap we trust the prior crawl's coverage.
                continue

            visited += 1
            try:
                html = fetch(session, url, timeout=30)
            except CloudflareBlocked as e:
                print(f"\n!! Cloudflare block detected: {e}", file=sys.stderr)
                print("!! Stopping now to avoid burning this IP. Re-run later "
                      "(it resumes) or pass --proxy.", file=sys.stderr)
                break
            except Exception as e:  # noqa: BLE001
                print(f"  [error] {url}: {e}", file=sys.stderr)
                time.sleep(base_delay)
                continue

            data = extract_content(html, url)
            if data:
                for link in data["links"]:
                    if link not in seen:
                        seen.add(link)
                        queue.append(link)
                if data["save"]:
                    out = write_markdown(url, data)
                    saved += 1
                    print(f"[{saved:3}] {out.name}  <- {url}")
                else:
                    print(f"  [hub] {url}  ({len(data['links'])} links)",
                          file=sys.stderr)
            else:
                print(f"  [skip:empty] {url}", file=sys.stderr)

            # Polite, human-ish pacing with jitter. Index varies the delay so
            # the cadence is not a tell-tale fixed interval.
            time.sleep(base_delay + random.uniform(0.4, 1.8))
    finally:
        session.close()

    print(f"\nDone. Visited {visited}, saved {saved}, skipped {skipped} "
          f"(already had). Output in ./{OUT_DIR}/")


def main() -> None:
    ap = argparse.ArgumentParser(description="Scrape FINRA beginner resources.")
    ap.add_argument("--max-pages", type=int, default=400,
                    help="hard cap on pages to fetch this run (default 400)")
    ap.add_argument("--delay", type=float, default=3.0,
                    help="base politeness delay between requests, seconds")
    ap.add_argument("--proxy", default=None,
                    help="proxy URL, e.g. http://user:pass@host:port")
    args = ap.parse_args()
    crawl(args.max_pages, base_delay=args.delay, proxy=args.proxy)


if __name__ == "__main__":
    main()

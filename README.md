# FINRA Beginner-Resource Scraper

Crawls FINRA.org's investor-education sections and saves each page as clean
**Markdown** with YAML front matter (title, source URL, section, description).

## Why not a headless browser?

FINRA sits behind **Cloudflare bot management**. A headless browser (Playwright/
Chrome) is *hard-blocked* by its automation fingerprint — you get
`Attention Required` no matter how you dress it up. The pages themselves are
plain **server-rendered HTML** (no JavaScript needed), so the reliable path is to
impersonate a real Chrome's TLS/HTTP-2 fingerprint with
[`curl_cffi`](https://github.com/lexiforest/curl_cffi) and behave like a polite
human. This scraper does exactly that.

## What it scrapes

Starts from the beginner-friendly hubs and crawls outward, staying inside the
investor-education sections:

- Investing Basics (financial foundations, risk, asset allocation, fees, ...)
- Investment Products (stocks, bonds, mutual funds, ETPs, options, crypto, ...)
- Investment Accounts
- Working With an Investment Professional
- Investor Insights articles (full paginated back-catalogue)
- Tools & Calculators, Free Investor Publications, Tips for New Investors

## Usage

```bash
uv run finra_scraper.py                 # full crawl (resumes; skips done)
uv run finra_scraper.py --max-pages 15  # quick test
uv run finra_scraper.py --delay 5       # extra-polite 5s base delay
uv run finra_scraper.py --proxy http://user:pass@host:port
```

Output lands in `./finra_resources/`, one `.md` file per page.

## IP safety (read before running)

The scraper is built to **not** burn an IP:

- **Chrome TLS/JA3 impersonation** so requests look like a real browser.
- **Sequential only**, never parallel, with a base delay (default 3s) + random
  jitter between every request.
- **Hard stop on the first Cloudflare block** — if any request returns a
  challenge (403/429/503 or a `Just a moment` / `Attention Required` page) the
  crawl aborts immediately and tells you, instead of retrying into a deeper ban.
- **Resume** — pages already saved on disk are skipped, so a re-run after a
  cooldown is cheap and won't re-hammer the site.

If you hit a block, wait for the IP to cool down (or pass `--proxy`) and re-run;
it picks up where it left off.

> Note: the IP used to develop this got temporarily flagged by Cloudflare during
> testing. Run the real crawl from a fresh IP, keep `--delay` at 3s or higher,
> and let the hard-stop protect you. A full crawl is only a few hundred polite,
> spaced-out requests.

## Files

- `finra_scraper.py` — the scraper.
- `finra_resources/` — output Markdown (created on first run).

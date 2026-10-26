# Market Digest

A free, static, mobile-friendly digest of Indian stock-market news: quarterly results and guidance, order wins, M&A, policy, big movers and the global snapshot. Built every weekday at 8:10 AM IST by GitHub Actions, and on demand.

**Live site:** enable GitHub Pages (Settings → Pages → Source: *Deploy from a branch*, branch `main`, folder `/ (root)`). The site is then at `https://<owner>.github.io/market-digest/`.

## How it works
- `scripts/build_digest.py` pulls RSS feeds (ET, Moneycontrol, Business Standard, Mint, RBI, SEBI, PIB), BSE corporate announcements and free price data (Yahoo Finance), classifies items with Groq's free API (keyword rules if no key), and writes `data/<run-id>.json` + `data/index.json`.
- `.github/workflows/digest.yml` runs it on a schedule and via **Run workflow** (choose mode: full / movers / orders / results / policy / custom, plus a movers threshold), then commits the data.
- `index.html` reads those JSON files. No server, no login, no tracking.

## Setup (one time)
1. Create the repo and push this folder.
2. Settings → Secrets and variables → Actions → **New repository secret**: `GROQ_API_KEY` (free key from console.groq.com). Optional variable `GROQ_MODEL`.
3. Settings → Pages → deploy from branch `main`, root.
4. Edit `config.js` so `repo` is your `owner/repo` (powers the Run now button).
5. Actions → Market Digest → **Run workflow** to build the first real digest.

## Run on demand from your phone
Open the site → **Run now** → **Open runner** → Run workflow. The GitHub mobile app works too (Actions tab).

## Legal posture
Facts in our own words with source names and links; no article text is copied; feeds are used as published for syndication; the BSE announcements endpoint is read once per run at human-like rates; no buy/sell/target language anywhere. Disclaimer in the footer.

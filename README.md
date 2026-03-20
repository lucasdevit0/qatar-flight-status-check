# Qatar Airways Travel Alert Scraper

This repository runs a GitHub Actions workflow that checks the Qatar Airways travel alerts page twice daily, stores newly discovered alerts in `scraper/alerts.json`, generates concise AI summaries for new alerts, and emails those updates to a configured recipient.

## Why This Exists

My girlfriend and I are traveling to Japan at the beginning of May 2026, with a layover in Doha. Because of the recent war-related disruptions in the region, we wanted a simple way to check Qatar Airways travel alerts every day and get notified quickly if anything changed in a concerning way.

## What It Does

- Scrapes `https://www.qatarairways.com/en/travel-alerts.html` with Playwright + Chromium
- Deduplicates alerts against the committed JSON store
- Generates a 2-3 sentence summary for each new alert via OpenRouter
- Sends an HTML + plain text email through the Gmail API when new alerts are found
- Commits updated alert state back to the repository

## Repository Layout

```text
.github/workflows/scrape.yml
scraper/scraper.py
scraper/requirements.txt
scraper/alerts.json
README.md
```

## Required GitHub Secrets

- `OPENROUTER_API_KEY`
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REFRESH_TOKEN`
- `EMAIL_FROM`
- `EMAIL_TO`

## Local `.env`

You can test locally with a root-level `.env` file containing:

```dotenv
OPENROUTER_API_KEY=
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_REFRESH_TOKEN=
EMAIL_FROM=your-email@example.com
EMAIL_TO=your-email@example.com,other-recipient@example.com
```

## Local Run

```bash
pip install -r scraper/requirements.txt
python -m playwright install chromium
python scraper/scraper.py
```

The scraper loads environment variables from `.env` automatically for local testing.

# Technical Spec: Qatar Airways Travel Alert Scraper - GitHub Actions

**Version:** 1.2
**Author:** Lucas
**Last updated:** 2026-03-20
**Implemented by:** OpenAI Codex

---

## 1. Overview

A GitHub Actions workflow that:
1. Runs twice daily at 09:00 and 21:00 Sao Paulo time (BRT, UTC-3) -> `0 12 * * *` and `0 0 * * *` in UTC
2. Attempts to scrape `https://www.qatarairways.com/en/travel-alerts.html` using Playwright + Chromium
3. Falls back to a text-mirror fetch if the primary site returns `403 Forbidden`
4. Compares scraped alerts against a persistent JSON store committed to the repo
5. For each new alert, calls the OpenRouter API to generate a clean 2-3 sentence summary from the raw scraped body text
6. If new alerts are found, sends an HTML email through the Gmail API
7. Commits the updated `alerts.json` back to the repo

No server, no VPS. Fully cloud-native and runs inside GitHub Actions.

---

## 2. Repository Structure

```text
qatar-flight-status-check/
|-- .github/
|   `-- workflows/
|       `-- scrape.yml
|-- scraper/
|   |-- scraper.py
|   |-- requirements.txt
|   |-- alerts.json
|   `-- debug_snapshot.html   # created only on scrape fallback/failure
|-- .env                      # local only, gitignored
|-- .gitignore
|-- README.md
`-- SPEC.md
```

---

## 3. GitHub Actions Workflow

**File:** `.github/workflows/scrape.yml`

### Trigger

Sao Paulo is UTC-3 (BRT). The workflow runs on:

| Sao Paulo | UTC | Cron expression |
|-----------|-----|-----------------|
| 09:00 BRT | 12:00 UTC | `0 12 * * *` |
| 21:00 BRT | 00:00 UTC | `0 0 * * *` |

Manual runs are also supported through `workflow_dispatch`.

```yaml
on:
  schedule:
    - cron: '0 12 * * *'
    - cron: '0 0 * * *'
  workflow_dispatch:
```

### Permissions

The workflow needs write access to push updated scraper state back to the repo:

```yaml
permissions:
  contents: write
```

### Environment

- Runner: `ubuntu-latest`
- Python: `3.12`
- Playwright browser install: `python -m playwright install --with-deps chromium`

### Full Job Steps

```text
1. actions/checkout@v4
   - fetch-depth: 0

2. actions/setup-python@v5
   - python-version: 3.12

3. Install dependencies
   - python -m pip install --upgrade pip
   - pip install -r scraper/requirements.txt

4. Install Playwright Chromium
   - python -m playwright install --with-deps chromium

5. Run scraper
   - python scraper/scraper.py
   - inject OpenRouter + Gmail secrets

6. Commit and push scraper state if changed
   - git config user.name "github-actions[bot]"
   - git config user.email "github-actions[bot]@users.noreply.github.com"
   - git add -A scraper
   - git diff --cached --quiet || git commit -m "chore: update alert state [skip ci]"
   - git push
```

`[skip ci]` prevents the push from creating a workflow loop.

---

## 4. Scraper Logic (`scraper/scraper.py`)

### 4.1 Alert Data Model

Each alert stored in `alerts.json`:

```json
{
  "id": "a3f9c1d2b4e5",
  "title": "Qatar Airways Passenger Support and Operational Updates",
  "raw_body": "Full scraped text from the page, untruncated...",
  "summary": "AI-generated 2-3 sentence summary of the alert.",
  "date": "14:00 GMT+3, 12 March 2026",
  "url": "https://www.qatarairways.com/en/travel-alerts.html",
  "first_seen": "2026-03-20T09:01:34Z"
}
```

**ID generation:** `sha256(title + "|" + date)[:16]`

The ID is deterministic, so re-scraping the same alert produces the same ID.

**`raw_body`:** full body text used as summarization input  
**`summary`:** OpenRouter-generated summary, with fallback to truncated raw text if the API call fails

### 4.2 Storage Format (`alerts.json`)

```json
{
  "last_updated": "2026-03-20T09:01:34Z",
  "alerts": [
    { "...": "..." }
  ]
}
```

If the file does not exist, initialize as:

```json
{
  "alerts": [],
  "last_updated": null
}
```

### 4.3 Scraping Strategy

#### Primary path: Playwright

The scraper first uses Playwright in headless mode.

**Browser config**
- Headless: `True`
- User-agent: desktop Chrome on Windows
- `wait_until="networkidle"` with 60s timeout
- Wait up to 20s for one of:
  - `.accordionItem`
  - `[class*="accordionItem"]`
  - `[class*="accordion-item"]`
  - `.travel-alert__item`
  - `article`

**Extraction strategies**

| Priority | Container | Title | Body | Date |
|----------|-----------|-------|------|------|
| 1 | `.accordionItem, [class*="accordionItem"]` | `h2, h3, h4, button` | `[class*="content"], p` | `[class*="date"], time` |
| 2 | `article, .card, [class*="alertCard"]` | `h2, h3, h4, [class*="title"]` | `p, [class*="description"]` | `time, [class*="date"]` |
| 3 | `main h2, main h3` | heading text | `""` | `""` |

#### Fallback path: text mirror

Qatar Airways may return `403 Forbidden` from Akamai to both local and GitHub-hosted environments. If the primary Playwright path fails or extracts nothing, the scraper falls back to:

`https://r.jina.ai/http://https://www.qatarairways.com/en/travel-alerts.html`

This path:
- fetches a text/markdown mirror of the page using `httpx`
- parses alert blocks from the mirrored content
- strips decorative image/header artifacts
- writes the mirrored payload to `scraper/debug_snapshot.html` for inspection

If both the Playwright path and the mirror fallback fail:
- log an error
- exit `0`
- skip email and storage updates

### 4.4 Deduplication

```python
stored_ids = {a["id"] for a in stored["alerts"]}
new_alerts = [a for a in scraped if make_id(a) not in stored_ids]
```

Only alerts with IDs not already in `alerts.json` are treated as new.

### 4.5 AI Summary Generation

For each new alert, generate a summary using OpenRouter via the OpenAI Python SDK.

**Provider:** OpenRouter  
**Model:** `openai/gpt-oss-20b`

#### API call

```python
from openai import OpenAI

def generate_summary(title: str, raw_body: str) -> str:
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.environ["OPENROUTER_API_KEY"],
    )

    prompt = f"""You are summarising a travel alert from Qatar Airways for airline passengers.

Alert title: {title}

Alert body:
{raw_body[:3000]}

Write 2 or 3 complete sentences in plain English. Include: 1) the disruption or operational status, 2) who or which routes, airports, or travel dates are affected, and 3) what passengers should do next such as checking flight status, rebooking, or requesting a refund. Be specific when the alert includes date ranges or deadlines. Do not copy long text from the alert. Do not output bullets, labels, quotation marks, or fragments. Output only the final summary."""

    response = client.chat.completions.create(
        model="openai/gpt-oss-20b",
        max_tokens=260,
        messages=[{"role": "user", "content": prompt}],
        extra_headers={
            "HTTP-Referer": "https://github.com/lucasdevit0/qatar-flight-status-check",
            "X-Title": "QA Alert Scraper",
        },
        extra_body={
            "reasoning": {
                "effort": "low",
                "exclude": True,
            }
        },
    )
    summary = extract_summary_text(response)
    if not is_summary_usable(summary):
        raise ValueError("Model returned an empty or incomplete summary")
    return summary
```

**Fallback behavior**

If the OpenRouter call raises any exception, or if it returns an empty/incomplete summary, the scraper logs a warning and falls back to a local summary builder that:
- cleans the raw alert body
- extracts the first 2-3 useful sentences
- guarantees a non-empty summary is available for the email

```python
alert["summary"] = build_fallback_summary(title, raw_body)
```

This means summary failures never block storage updates or email sending.

**Operational note**

OpenRouter account privacy/guardrail settings can block certain models. This happened with `openai/gpt-oss-120b:free`, which is why the implementation now uses `openai/gpt-oss-20b`.

### 4.6 Email

Skip sending if `new_alerts` is empty.

**Subject line**
- 1 alert: `[QA Alert] {title[:60]}...`
- 2+ alerts: `[QA Alerts] {n} new travel alerts detected`

**HTML email**
- Background: white
- Header bar: `#5C0632`
- Alert card: left border `4px solid #5C0632`, background `#fff8f8`
- Card layout:
  - date
  - title
  - summary
  - `View on Qatar Airways ->` link
- Footer includes UTC run timestamp and source page URL

**Plain text fallback**

Each alert is rendered as:

```text
date
title
summary
url
```

#### Delivery mechanism: Gmail API

Email is sent through the Gmail API, not SMTP.

The implementation:
- refreshes an OAuth access token using:
  - `GOOGLE_CLIENT_ID`
  - `GOOGLE_CLIENT_SECRET`
  - `GOOGLE_REFRESH_TOKEN`
- builds a MIME email with `email.message.EmailMessage`
- base64url-encodes the raw message
- calls `gmail.users.messages.send`

Required Gmail scope:

`https://www.googleapis.com/auth/gmail.send`

### 4.7 Local `.env` Support

For local development, the scraper loads environment variables from a root-level `.env` file using `python-dotenv`.

This file is gitignored and is not used by GitHub Actions.

---

## 5. Dependencies

**`scraper/requirements.txt`:**

```text
playwright==1.58.0
openai==1.30.0
httpx<0.28
google-api-python-client
google-auth
python-dotenv
```

Notes:
- `playwright==1.58.0` is used for compatibility with newer `ubuntu-latest` runners
- `httpx<0.28` is pinned to stay compatible with `openai==1.30.0`
- Gmail delivery uses Google client libraries instead of `smtplib`

---

## 6. Secrets & Environment Variables

Configure these in **GitHub -> Settings -> Secrets and variables -> Actions -> Secrets**:

| Secret name | Description |
|-------------|-------------|
| `OPENROUTER_API_KEY` | OpenRouter API key for AI summary generation |
| `GOOGLE_CLIENT_ID` | Google OAuth client ID for Gmail API |
| `GOOGLE_CLIENT_SECRET` | Google OAuth client secret for Gmail API |
| `GOOGLE_REFRESH_TOKEN` | Refresh token for the sender mailbox |
| `EMAIL_FROM` | Sender email address |
| `EMAIL_TO` | Recipient email(s), comma-separated |

**Injected into the scraper step**

```yaml
env:
  OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
  GOOGLE_CLIENT_ID: ${{ secrets.GOOGLE_CLIENT_ID }}
  GOOGLE_CLIENT_SECRET: ${{ secrets.GOOGLE_CLIENT_SECRET }}
  GOOGLE_REFRESH_TOKEN: ${{ secrets.GOOGLE_REFRESH_TOKEN }}
  EMAIL_FROM: ${{ secrets.EMAIL_FROM }}
  EMAIL_TO: ${{ secrets.EMAIL_TO }}
  GITHUB_REPOSITORY: ${{ github.repository }}
```

**Local `.env` example**

```dotenv
OPENROUTER_API_KEY=
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_REFRESH_TOKEN=
EMAIL_FROM=your-email@example.com
EMAIL_TO=your-email@example.com,other-recipient@example.com
```

---

## 7. Logging

Use Python `logging` at `INFO` level.

| Event | Level |
|-------|-------|
| Scraper started | INFO |
| N stored alerts loaded | INFO |
| Page loaded | INFO |
| N alerts scraped | INFO |
| N new alerts found | INFO |
| Summary generated for alert "{title}" | INFO |
| Summary generation failed, using fallback | WARNING |
| Model returned empty or incomplete summary | WARNING |
| Primary scrape failed | WARNING |
| Fallback extraction strategy used | WARNING |
| Text mirror fallback failed | WARNING |
| Email sent successfully | INFO |
| alerts.json updated | INFO |
| No new alerts - nothing to do | INFO |
| Selector timeout | WARNING |
| No alerts extracted - all strategies failed | ERROR |
| Email send failed | ERROR |

---

## 8. Edge Cases & Error Handling

| Scenario | Behavior |
|----------|----------|
| `alerts.json` missing | Initialize empty store |
| Qatar page returns 403 | Log warning and attempt mirror fallback |
| Playwright extracts nothing | Attempt mirror fallback |
| Mirror fallback also fails | Log error and exit `0` |
| OpenRouter call fails | Log warning and use truncated raw text |
| OpenRouter model blocked by privacy settings | Summary falls back to raw text |
| Gmail secrets missing | Log error, skip email, still update storage |
| Gmail send fails | Log error, still update storage |
| Zero new alerts | Skip summary generation, skip email, skip git commit |

---

## 9. Execution Flow

```text
START
  |
  |-- load alerts.json (or init empty)
  |
  |-- scrape with Playwright
  |     `-- if blocked or empty -> try text mirror fallback
  |
  |-- if nothing extracted -> log error -> EXIT
  |
  |-- deduplicate -> collect new_alerts[]
  |     `-- empty -> log "no new alerts" -> EXIT
  |
  |-- for each new alert:
  |     |-- call OpenRouter (openai/gpt-oss-20b)
  |     `-- on failure -> use raw_body[:300]
  |
  |-- send Gmail API email
  |
  |-- append new_alerts to alerts.json -> save
  |
  `-- git add + commit [skip ci] + push
```

---

## 10. Git Commit Step

```bash
git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"
git add -A scraper
git diff --cached --quiet || git commit -m "chore: update alert state [skip ci]"
git push
```

---

## 11. Manual Testing

### GitHub

Use `workflow_dispatch` to run the workflow manually from GitHub Actions.

### Local

```bash
pip install -r scraper/requirements.txt
python -m playwright install chromium
python scraper/scraper.py
```

The script loads secrets from `.env` automatically for local testing.

---

## 12. Constraints & Assumptions

- GitHub Actions free tier usage remains comfortably within limits
- `ubuntu-latest` may evolve, so Playwright version compatibility matters
- Qatar Airways may block direct scraping with Akamai `403` responses
- The mirror fallback is a resilience mechanism, not the preferred primary path
- OpenRouter account privacy settings can affect model availability
- Gmail API OAuth refresh tokens must remain valid for unattended workflow runs

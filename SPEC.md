# Technical Spec: Qatar Airways Travel Alert Scraper — GitHub Actions

**Version:** 1.1  
**Author:** Lucas  
**Last updated:** 2026-03-20  
**Implemented by:** OpenAI Codex  

---

## 1. Overview

A GitHub Actions workflow that:
1. Runs **twice daily at 09:00 and 21:00 São Paulo time (BRT, UTC-3)** → `0 12 * * *` and `0 0 * * *` in UTC
2. Scrapes https://www.qatarairways.com/en/travel-alerts.html using **Playwright + Chromium**
3. Compares scraped alerts against a **persistent JSON store** (committed to the repo)
4. For each new alert, calls the **Anthropic API** to generate a clean 2–3 sentence AI summary from the raw scraped body text
5. If new alerts are found, sends an **HTML email** to a configured recipient
6. Commits the updated `alerts.json` back to the repo

No server, no VPS. Fully cloud-native and free within GitHub Actions free tier.

---

## 2. Repository Structure

```
qa-alerts/
├── .github/
│   └── workflows/
│       └── scrape.yml           # GitHub Actions workflow definition
├── scraper/
│   ├── scraper.py               # Main scraping + summarisation + email logic
│   ├── requirements.txt         # Python dependencies
│   └── alerts.json              # Persistent alert store (committed to repo)
└── README.md
```

---

## 3. GitHub Actions Workflow

**File:** `.github/workflows/scrape.yml`

### Trigger

São Paulo is UTC-3 (BRT). The two target local times and their UTC equivalents:

| São Paulo | UTC | Cron expression |
|-----------|-----|-----------------|
| 09:00 BRT | 12:00 UTC | `0 12 * * *` |
| 21:00 BRT | 00:00 UTC | `0 0 * * *` |

> **Note:** Brazil observes Daylight Saving Time (BRST, UTC-2) roughly November–February. During DST, the runs shift to 10:00 and 22:00 local time. If exact local time matters year-round, consider using a third-party scheduler (e.g. EasyCron) to trigger `workflow_dispatch` instead of relying on `schedule`.

```yaml
on:
  schedule:
    - cron: '0 12 * * *'   # 09:00 São Paulo (BRT / UTC-3)
    - cron: '0 0 * * *'    # 21:00 São Paulo (BRT / UTC-3)
  workflow_dispatch:         # Allow manual runs from the GitHub UI
```

### Permissions

The workflow needs write access to push `alerts.json` back to the repo:

```yaml
permissions:
  contents: write
```

### Environment

- Runner: `ubuntu-latest`
- Python: `3.12`
- Playwright: install Chromium via `playwright install chromium`

### Full Job Steps

```
1. actions/checkout@v4
   - fetch-depth: 0  (needed for git push)

2. Set up Python 3.12
   - actions/setup-python@v5

3. Install Python dependencies
   - pip install -r scraper/requirements.txt

4. Install Playwright browsers
   - python -m playwright install chromium --with-deps

5. Run scraper
   - python scraper/scraper.py
   - env: inject all secrets (see §6)

6. Commit & push alerts.json (only if file changed)
   - git config user.name "github-actions[bot]"
   - git config user.email "github-actions[bot]@users.noreply.github.com"
   - git add scraper/alerts.json
   - git diff --cached --quiet || git commit -m "chore: update alerts.json [skip ci]"
   - git push
```

> **Important:** use `[skip ci]` in the commit message to prevent an infinite trigger loop.

---

## 4. Scraper Logic (`scraper/scraper.py`)

### 4.1 Alert Data Model

Each alert stored in `alerts.json`:

```json
{
  "id": "a3f9c1d2b4e5",
  "title": "Temporary suspension of flights to XYZ",
  "raw_body": "Full scraped text from the page, untruncated...",
  "summary": "AI-generated 2–3 sentence summary of the alert.",
  "date": "20 March 2026",
  "url": "https://www.qatarairways.com/en/travel-alerts.html",
  "first_seen": "2026-03-20T09:01:34Z"
}
```

**ID generation:** `sha256(title + "|" + date)[:16]`  
Deterministic — re-scraping the same alert always produces the same ID.

**`raw_body`:** full scraped text, untruncated. Used as input to the summarisation step.  
**`summary`:** AI-generated (see §4.5). Falls back to `raw_body[:300]` if the API call fails.

### 4.2 Storage Format (`alerts.json`)

```json
{
  "last_updated": "2026-03-20T09:01:34Z",
  "alerts": [
    { ...alert object... },
    { ...alert object... }
  ]
}
```

Initialize as `{ "alerts": [], "last_updated": null }` if the file doesn't exist.

### 4.3 Scraping Strategy

The Qatar Airways travel alerts page is **fully JavaScript-rendered**. Use `playwright` in headless mode.

**Browser config:**
- Headless: `True`
- User-agent: `Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120`
- `wait_until: "networkidle"` with 60s timeout
- After page load, wait for any of these selectors (20s timeout):
  - `.accordionItem`
  - `[class*="accordionItem"]`
  - `[class*="accordion-item"]`
  - `.travel-alert__item`
  - `article`

**Extraction — try in order, stop at first success:**

| Priority | Selector | Title | Body | Date |
|----------|----------|-------|------|------|
| 1 | `.accordionItem`, `[class*="accordionItem"]` | `h2, h3, h4, button` | `[class*="content"], p` | `[class*="date"], time` |
| 2 | `article, .card, [class*="alertCard"]` | `h2, h3, h4, [class*="title"]` | `p, [class*="description"]` | `time, [class*="date"]` |
| 3 (fallback) | `main h2, main h3` | inner text of heading | `""` | `""` |

**If nothing is extracted:**
- Save full page HTML to `scraper/debug_snapshot.html`
- Log error: `"No alerts extracted — debug_snapshot.html saved"`
- Exit without sending email or updating storage

### 4.4 Deduplication

```python
stored_ids = {a["id"] for a in stored["alerts"]}
new_alerts = [a for a in scraped if make_id(a) not in stored_ids]
```

Only alerts with IDs not already in `alerts.json` are treated as new.

---

### 4.5 AI Summary Generation

For each new alert, generate a clean summary by calling the **OpenRouter API** using the `openai/gpt-oss-120b:free` model.

**Why:** The raw scraped body is noisy — it may contain partial navigation text, repeated disclaimers, or awkward line breaks. The AI summary gives the email reader an immediately useful 2–3 sentence digest without needing to click through.

**Provider:** [OpenRouter](https://openrouter.ai) — a unified API gateway that routes to multiple LLM providers via an OpenAI-compatible interface. No extra SDK needed; use the `openai` Python package pointed at the OpenRouter base URL.

#### API call

```python
from openai import OpenAI

def generate_summary(title: str, raw_body: str) -> str:
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.environ["OPENROUTER_API_KEY"],
    )

    prompt = f"""You are summarising a travel alert from Qatar Airways.

Alert title: {title}

Alert body:
{raw_body[:3000]}

Write a clear, factual summary in 2–3 sentences. Cover: what the disruption is, which routes or destinations are affected, and any passenger action required. Do not include disclaimers, marketing language, or your own commentary. Output only the summary text, nothing else."""

    response = client.chat.completions.create(
        model="openai/gpt-oss-120b:free",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
        extra_headers={
            "HTTP-Referer": "https://github.com/qa-alert-scraper",
            "X-Title": "QA Alert Scraper",
        },
    )
    return response.choices[0].message.content.strip()
```

**Model:** `openai/gpt-oss-120b:free` via OpenRouter — free tier, no cost per call.

**Note on `extra_headers`:** OpenRouter recommends passing `HTTP-Referer` and `X-Title` for attribution on the free tier. Use any identifier you like.

**Fallback:** if the API call raises any exception, log a WARNING and fall back to `raw_body[:300]`. Never let a summarisation failure block the email or storage update.

```python
try:
    alert["summary"] = generate_summary(alert["title"], alert["raw_body"])
except Exception as e:
    log.warning(f"Summary generation failed for '{alert['title']}': {e}")
    alert["summary"] = alert["raw_body"][:300]
```

**Cost:** free tier on OpenRouter — $0.00/day.

---

### 4.6 Email

Skip sending if `new_alerts` is empty.

**Subject line:**
- 1 alert: `[QA Alert] {title[:60]}...`
- 2+ alerts: `[QA Alerts] {n} new travel alerts detected`

**HTML email design:**
- Background: white
- Header bar: `#5C0632` (Qatar Airways maroon) with white text
- Each alert: left border `4px solid #5C0632`, background `#fff8f8`
- Alert structure: date (gray, small) → title (bold, 16px) → **AI summary** (14px, #444) → "View on Qatar Airways →" link
- Footer: run timestamp (UTC) + link to the alerts page

**Plain text fallback:** `date\ntitle\nsummary\nurl` blocks for each alert.

**SMTP:** `smtplib` with STARTTLS. Host / port / credentials from environment variables (see §6).

---

## 5. Dependencies

**`scraper/requirements.txt`:**

```
playwright==1.44.0
openai==1.30.0
```

OpenRouter is accessed via the `openai` package pointed at a custom `base_url` — no separate SDK needed. Everything else (`json`, `hashlib`, `smtplib`, `logging`, `os`, `pathlib`, `datetime`) is Python stdlib.

---

## 6. Secrets & Environment Variables

Configure all of these in **GitHub → Settings → Secrets and variables → Actions → Secrets**:

| Secret name | Description |
|-------------|-------------|
| `OPENROUTER_API_KEY` | OpenRouter API key for AI summary generation |
| `SMTP_HOST` | SMTP server hostname (e.g. `smtp.gmail.com`) |
| `SMTP_PORT` | SMTP port (e.g. `587`) |
| `SMTP_USER` | Sender email address |
| `SMTP_PASSWORD` | Gmail App Password (16-char, no spaces) |
| `EMAIL_FROM` | Sender display address (can equal `SMTP_USER`) |
| `EMAIL_TO` | Recipient email address |

**Injected into the scraper step:**

```yaml
env:
  OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
  SMTP_HOST: ${{ secrets.SMTP_HOST }}
  SMTP_PORT: ${{ secrets.SMTP_PORT }}
  SMTP_USER: ${{ secrets.SMTP_USER }}
  SMTP_PASSWORD: ${{ secrets.SMTP_PASSWORD }}
  EMAIL_FROM: ${{ secrets.EMAIL_FROM }}
  EMAIL_TO: ${{ secrets.EMAIL_TO }}
```

**Reading in Python:**

```python
import os
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
SMTP_HOST         = os.environ["SMTP_HOST"]
SMTP_PORT         = int(os.environ["SMTP_PORT"])
SMTP_USER         = os.environ["SMTP_USER"]
SMTP_PASSWORD     = os.environ["SMTP_PASSWORD"]
EMAIL_FROM        = os.environ["EMAIL_FROM"]
EMAIL_TO          = os.environ["EMAIL_TO"]
```

---

## 7. Logging

Use Python's `logging` module at `INFO` level. All output goes to `stdout` — GitHub Actions captures it in the run log.

| Event | Level |
|-------|-------|
| Scraper started | INFO |
| N stored alerts loaded | INFO |
| Page loaded | INFO |
| N alerts scraped | INFO |
| N new alerts found | INFO |
| Summary generated for alert "{title}" | INFO |
| Summary generation failed, using fallback | WARNING |
| Email sent successfully | INFO |
| alerts.json updated | INFO |
| No new alerts — nothing to do | INFO |
| Selector timeout | WARNING |
| Fallback extraction strategy used | WARNING |
| Nothing extracted → snapshot saved | ERROR |
| Email send failed | ERROR |

---

## 8. Edge Cases & Error Handling

| Scenario | Behaviour |
|----------|-----------|
| `alerts.json` missing | Initialize with `{ "alerts": [], "last_updated": null }` |
| Page returns 4xx/5xx | Playwright raises → workflow step fails → GitHub notifies repo owner |
| Nothing extracted | Save `debug_snapshot.html`, log ERROR, exit 0 |
| OpenRouter API call fails | Log WARNING, use `raw_body[:300]` as summary fallback |
| `OPENROUTER_API_KEY` not set | Caught as exception → fallback summary used, log WARNING |
| Email secrets missing | Log ERROR, skip email send, still update storage |
| Email send fails | Log ERROR, still update storage |
| Zero new alerts | Skip summarisation, skip email, skip git commit |

---

## 9. Execution Flow (full pipeline)

```
START
  │
  ├─ load alerts.json (or init empty)
  │
  ├─ scrape page with Playwright
  │    └─ nothing extracted? → save debug_snapshot.html → EXIT
  │
  ├─ deduplicate → collect new_alerts[]
  │    └─ empty? → log "no new alerts" → EXIT
  │
  ├─ for each new alert:
  │    ├─ call OpenRouter API (openai/gpt-oss-120b:free) → generate summary
  │    └─ on failure → use raw_body[:300]
  │
  ├─ send HTML email (new_alerts + AI summaries)
  │
  ├─ append new_alerts to alerts.json → save
  │
  └─ git add + commit [skip ci] + push
```

---

## 10. Git Commit Step

```bash
git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"
git add scraper/alerts.json
git diff --cached --quiet || git commit -m "chore: update alerts.json [skip ci]"
git push
```

`[skip ci]` prevents the push from re-triggering the workflow.

---

## 11. Manual Testing

`workflow_dispatch` allows a manual run from the GitHub Actions UI at any time.

Local test:

```bash
pip install playwright openai
python -m playwright install chromium
export OPENROUTER_API_KEY=sk-or-v1-...
export SMTP_HOST=smtp.gmail.com
export SMTP_PORT=587
export SMTP_USER=you@gmail.com
export SMTP_PASSWORD=your_app_password
export EMAIL_FROM=you@gmail.com
export EMAIL_TO=you@gmail.com
python scraper/scraper.py
```

---

## 12. Constraints & Assumptions

- **GitHub Actions free tier:** 2,000 min/month. This job runs ~2–4 min per trigger × 2/day → ~120 min/month. Well within limits.
- **Chromium install** on Ubuntu runner takes ~1–2 min. Always use `--with-deps`.
- **Timezone / DST:** cron uses UTC. Brazil observes DST (November–February), shifting times by 1 hour. See §3 note for how to handle this if exact local time is critical.
- **Selector stability:** Qatar Airways may update their HTML structure. `debug_snapshot.html` will be committed on failure so you can inspect and fix selectors.
- **OpenRouter API key:** get one at openrouter.ai/keys. Add as `OPENROUTER_API_KEY` in GitHub Secrets. The `openai/gpt-oss-120b:free` model has no per-call cost on the free tier.

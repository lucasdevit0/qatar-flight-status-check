from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from email.message import EmailMessage
from html import escape
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import httpx
from openai import OpenAI
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)


ALERTS_URL = "https://www.qatarairways.com/en/travel-alerts.html"
TEXT_MIRROR_URL = "https://r.jina.ai/http://https://www.qatarairways.com/en/travel-alerts.html"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
STORE_PATH = Path(__file__).with_name("alerts.json")
RUN_LOGS_PATH = Path(__file__).with_name("run_logs.json")
DEBUG_SNAPSHOT_PATH = Path(__file__).with_name("debug_snapshot.html")
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
MAX_RUN_LOGS = 100

WAIT_SELECTORS = [
    ".accordionItem",
    '[class*="accordionItem"]',
    '[class*="accordion-item"]',
    ".travel-alert__item",
    "article",
]

EXTRACTION_STRATEGIES = [
    {
        "name": "accordion",
        "container": ".accordionItem, [class*='accordionItem']",
        "title": "h2, h3, h4, button",
        "body": "[class*='content'], p",
        "date": "[class*='date'], time",
    },
    {
        "name": "article",
        "container": "article, .card, [class*='alertCard']",
        "title": "h2, h3, h4, [class*='title']",
        "body": "p, [class*='description']",
        "date": "time, [class*='date']",
    },
]


load_dotenv(dotenv_path=ENV_PATH, override=False)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_store() -> dict[str, Any]:
    if not STORE_PATH.exists():
        return {"alerts": [], "last_updated": None}

    with STORE_PATH.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    if "alerts" not in data or not isinstance(data["alerts"], list):
        data["alerts"] = []
    if "last_updated" not in data:
        data["last_updated"] = None
    return data


def load_run_logs() -> dict[str, Any]:
    if not RUN_LOGS_PATH.exists():
        return {"runs": []}

    with RUN_LOGS_PATH.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    if "runs" not in data or not isinstance(data["runs"], list):
        data["runs"] = []
    return data


def save_store(store: dict[str, Any]) -> None:
    STORE_PATH.write_text(json.dumps(store, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def save_run_logs(run_logs: dict[str, Any]) -> None:
    RUN_LOGS_PATH.write_text(json.dumps(run_logs, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def append_run_log(entry: dict[str, Any]) -> None:
    run_logs = load_run_logs()
    run_logs["runs"].append(entry)
    run_logs["runs"] = run_logs["runs"][-MAX_RUN_LOGS:]
    save_run_logs(run_logs)


def clean_text(value: str) -> str:
    parts = [segment.strip() for segment in value.splitlines()]
    collapsed = " ".join(part for part in parts if part)
    return " ".join(collapsed.split())


def first_text(locator: Any, timeout: int = 2_000) -> str:
    try:
        if locator.count() == 0:
            return ""
        return clean_text(locator.first.text_content(timeout=timeout) or "")
    except Exception:
        return ""


def markdown_to_text(value: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", value)
    text = re.sub(r"!\[[^\]]*\]", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = text.replace("**", "")
    text = text.replace("__", "")
    text = text.replace("`", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def build_alert_id(title: str, date: str) -> str:
    digest = hashlib.sha256(f"{title}|{date}".encode("utf-8")).hexdigest()
    return digest[:16]


def subject_for(alerts: list[dict[str, Any]]) -> str:
    if len(alerts) == 1:
        title = alerts[0]["title"]
        if len(title) > 60:
            title = f"{title[:60]}..."
        return f"[QA Alert] {title}"
    return f"[QA Alerts] {len(alerts)} new travel alerts detected"


def truncate_fallback(text: str, limit: int = 300) -> str:
    trimmed = clean_text(text)
    return trimmed[:limit]


def split_sentences(text: str) -> list[str]:
    normalized = clean_text(
        text.replace("\u202f", " ").replace("\u00a0", " ").replace("\u2011", "-")
    )
    if not normalized:
        return []
    return [sentence.strip() for sentence in re.split(r"(?<=[.!?])\s+", normalized) if sentence.strip()]


def build_fallback_summary(title: str, raw_body: str) -> str:
    sentences = split_sentences(raw_body)
    selected: list[str] = []

    for sentence in sentences:
        lowered = sentence.lower()
        if lowered in {item.lower() for item in selected}:
            continue
        if len(sentence) < 25:
            continue
        selected.append(sentence)
        if len(selected) == 3:
            break

    if not selected:
        base = clean_text(raw_body or title)
        if len(base) <= 320:
            return base
        return f"{base[:317]}..."

    summary = " ".join(selected)
    if len(summary) > 500:
        summary = f"{summary[:497]}..."
    return summary


def extract_summary_text(response: Any) -> str:
    message = response.choices[0].message
    content = message.content

    if isinstance(content, str):
        return clean_text(content.replace("\u202f", " ").replace("\u00a0", " ").replace("\u2011", "-"))

    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(item.get("text", ""))
            elif hasattr(item, "type") and getattr(item, "type", None) == "text":
                text_parts.append(getattr(item, "text", ""))
        return clean_text(" ".join(text_parts).replace("\u202f", " ").replace("\u00a0", " ").replace("\u2011", "-"))

    return ""


def is_summary_usable(summary: str) -> bool:
    if not summary or len(summary) < 80:
        return False
    sentence_count = len(split_sentences(summary))
    return sentence_count >= 2


def read_env(name: str, required: bool = True) -> str | None:
    value = os.getenv(name)
    if value:
        return value
    if required:
        raise KeyError(name)
    return None


def generate_summary(title: str, raw_body: str) -> str:
    api_key = read_env("OPENROUTER_API_KEY")
    repository = os.getenv("GITHUB_REPOSITORY", "qa-alert-scraper")

    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
    )

    prompt = (
        "You are summarising a travel alert from Qatar Airways for airline passengers.\n\n"
        f"Alert title: {title}\n\n"
        "Alert body:\n"
        f"{raw_body[:3000]}\n\n"
        "Write 2 or 3 complete sentences in plain English. Include: "
        "1) the disruption or operational status, "
        "2) who or which routes, airports, or travel dates are affected, and "
        "3) what passengers should do next such as checking flight status, rebooking, or requesting a refund. "
        "Be specific when the alert includes date ranges or deadlines. "
        "Do not copy long text from the alert. Do not output bullets, labels, quotation marks, or fragments. "
        "Output only the final summary."
    )

    response = client.chat.completions.create(
        model="openai/gpt-oss-20b",
        max_tokens=260,
        messages=[{"role": "user", "content": prompt}],
        extra_headers={
            "HTTP-Referer": f"https://github.com/{repository}",
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


def email_config() -> dict[str, Any] | None:
    names = [
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_REFRESH_TOKEN",
        "EMAIL_FROM",
        "EMAIL_TO",
    ]
    values = {name: os.getenv(name) for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        log.error("Email secrets missing: %s", ", ".join(missing))
        return None

    return {
        "client_id": values["GOOGLE_CLIENT_ID"],
        "client_secret": values["GOOGLE_CLIENT_SECRET"],
        "refresh_token": values["GOOGLE_REFRESH_TOKEN"],
        "from": values["EMAIL_FROM"],
        "to": [address.strip() for address in values["EMAIL_TO"].split(",") if address.strip()],
    }


def build_email_html(alerts: list[dict[str, Any]], run_timestamp: str) -> str:
    cards = []
    for alert in alerts:
        alert_date = escape(alert["date"] or "Date unavailable")
        alert_title = escape(alert["title"])
        alert_summary = escape(alert["summary"])
        alert_url = escape(alert["url"])
        cards.append(
            f"""
            <div style="border-left:4px solid #5C0632;background:#fff8f8;padding:16px 18px;margin:0 0 16px 0;">
              <div style="font-size:12px;color:#7a7a7a;margin-bottom:6px;">{alert_date}</div>
              <div style="font-size:16px;font-weight:700;color:#1f1f1f;margin-bottom:8px;">{alert_title}</div>
              <div style="font-size:14px;line-height:1.6;color:#444444;margin-bottom:10px;">{alert_summary}</div>
              <a href="{alert_url}" style="color:#5C0632;text-decoration:none;font-weight:600;">View on Qatar Airways &rarr;</a>
            </div>
            """.strip()
        )

    return f"""
    <html>
      <body style="margin:0;padding:0;background:#ffffff;font-family:Arial,sans-serif;color:#222222;">
        <div style="max-width:680px;margin:0 auto;padding:24px;">
          <div style="background:#5C0632;color:#ffffff;padding:18px 20px;font-size:22px;font-weight:700;">
            Qatar Airways Travel Alerts
          </div>
          <div style="padding:20px 0 8px 0;">
            {''.join(cards)}
          </div>
          <div style="font-size:12px;color:#666666;padding-top:12px;border-top:1px solid #e7dcdc;">
            Run timestamp (UTC): {escape(run_timestamp)}<br>
            Alerts page: <a href="{ALERTS_URL}" style="color:#5C0632;">{ALERTS_URL}</a>
          </div>
        </div>
      </body>
    </html>
    """.strip()


def build_email_text(alerts: list[dict[str, Any]], run_timestamp: str) -> str:
    parts = []
    for alert in alerts:
        parts.append(
            "\n".join(
                [
                    alert["date"] or "Date unavailable",
                    alert["title"],
                    alert["summary"],
                    alert["url"],
                ]
            )
        )
    parts.append(f"Run timestamp (UTC): {run_timestamp}")
    parts.append(f"Alerts page: {ALERTS_URL}")
    return "\n\n".join(parts)


def send_email(alerts: list[dict[str, Any]], run_timestamp: str) -> bool:
    config = email_config()
    if not config:
        return False

    message = EmailMessage()
    message["Subject"] = subject_for(alerts)
    message["From"] = config["from"]
    message["To"] = ", ".join(config["to"])
    message.set_content(build_email_text(alerts, run_timestamp))
    message.add_alternative(build_email_html(alerts, run_timestamp), subtype="html")

    try:
        credentials = Credentials(
            token=None,
            refresh_token=config["refresh_token"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=config["client_id"],
            client_secret=config["client_secret"],
            scopes=GMAIL_SCOPES,
        )
        credentials.refresh(Request())

        service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        service.users().messages().send(
            userId="me",
            body={"raw": encoded_message},
        ).execute()
        log.info("Email sent successfully")
        return True
    except Exception as exc:
        log.error("Email send failed: %s", exc)
        return False


def wait_for_any_selector(page: Any) -> None:
    page.wait_for_selector(", ".join(WAIT_SELECTORS), timeout=20_000)


def extract_alerts_from_strategy(page: Any, strategy: dict[str, str]) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    containers = page.locator(strategy["container"])
    count = containers.count()
    for index in range(count):
        node = containers.nth(index)
        title = first_text(node.locator(strategy["title"]))
        if not title:
            continue

        body_parts: list[str] = []
        body_nodes = node.locator(strategy["body"])
        body_count = body_nodes.count()
        for body_index in range(body_count):
            text = clean_text(body_nodes.nth(body_index).text_content(timeout=2_000) or "")
            if text and text not in body_parts:
                body_parts.append(text)

        date_text = first_text(node.locator(strategy["date"]))
        raw_body = "\n".join(body_parts).strip()
        results.append(
            {
                "title": title,
                "raw_body": raw_body or title,
                "date": date_text,
                "url": ALERTS_URL,
            }
        )
    return results


def extract_fallback_headings(page: Any) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    headings = page.locator("main h2, main h3")
    count = headings.count()
    for index in range(count):
        title = clean_text(headings.nth(index).text_content(timeout=2_000) or "")
        if not title:
            continue
        results.append(
            {
                "title": title,
                "raw_body": "",
                "date": "",
                "url": ALERTS_URL,
            }
        )
    return results


def dedupe_scraped_alerts(alerts: list[dict[str, str]]) -> list[dict[str, str]]:
    unique: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for alert in alerts:
        alert_id = build_alert_id(alert["title"], alert["date"])
        if alert_id in seen_ids:
            continue
        seen_ids.add(alert_id)
        unique.append(alert)
    return unique


def scrape_alerts_via_text_mirror() -> tuple[list[dict[str, str]], str]:
    response = httpx.get(
        TEXT_MIRROR_URL,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/plain, text/markdown;q=0.9, */*;q=0.8",
        },
        timeout=60,
    )
    response.raise_for_status()
    markdown = response.text

    section_match = re.search(r"# Travel Alerts.*", markdown, flags=re.S)
    relevant = section_match.group(0) if section_match else markdown
    relevant = relevant.split("\n## Qatar Airways", 1)[0]

    pattern = re.compile(
        r"!\[Image [^\]]+\](?P<header>[^\n]+)\n+(?P<body>.*?)(?=\n!\[Image [^\]]+\][^\n]+|\n## |\Z)",
        flags=re.S,
    )

    results: list[dict[str, str]] = []
    for match in pattern.finditer(relevant):
        header = clean_text(re.sub(r"^(?:\([^)]+\))+", "", match.group("header")).strip())
        if not header or header == "Travel Alerts":
            continue

        header_match = re.match(
            r"(?P<date>\d{1,2}:\d{2}\s+GMT[+-]\d,\s+\d{1,2}\s+\w+\s+\d{4})\s*:\s*(?P<title>.+)",
            header,
        )
        if header_match:
            date_text = header_match.group("date")
            title = clean_text(header_match.group("title"))
        else:
            # Ignore non-alert decorative content from the mirrored markdown.
            if header in {"Qatar Airways"} or len(header) < 20:
                continue
            date_text = ""
            title = header

        body = markdown_to_text(match.group("body"))
        if not title:
            continue

        results.append(
            {
                "title": title,
                "raw_body": body or title,
                "date": date_text,
                "url": ALERTS_URL,
            }
        )

    if results:
        log.warning("Fallback extraction strategy used: text mirror")
    return dedupe_scraped_alerts(results), markdown


def scrape_alerts_via_playwright() -> list[dict[str, str]]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()
        try:
            response = page.goto(ALERTS_URL, wait_until="networkidle", timeout=60_000)
            if response is not None and response.status >= 400:
                raise RuntimeError(f"Page returned HTTP {response.status}")
            log.info("Page loaded")

            try:
                wait_for_any_selector(page)
            except PlaywrightTimeoutError:
                log.warning("Selector timeout")

            for strategy in EXTRACTION_STRATEGIES:
                alerts = dedupe_scraped_alerts(extract_alerts_from_strategy(page, strategy))
                if alerts:
                    if DEBUG_SNAPSHOT_PATH.exists():
                        DEBUG_SNAPSHOT_PATH.unlink()
                    if strategy["name"] != EXTRACTION_STRATEGIES[0]["name"]:
                        log.warning("Fallback extraction strategy used")
                    return alerts

            fallback_alerts = dedupe_scraped_alerts(extract_fallback_headings(page))
            if fallback_alerts:
                if DEBUG_SNAPSHOT_PATH.exists():
                    DEBUG_SNAPSHOT_PATH.unlink()
                log.warning("Fallback extraction strategy used")
                return fallback_alerts

            DEBUG_SNAPSHOT_PATH.write_text(page.content(), encoding="utf-8")
            log.error("No alerts extracted - debug_snapshot.html saved")
            return []
        finally:
            context.close()
            browser.close()


def scrape_alerts() -> tuple[list[dict[str, str]], str | None]:
    try:
        alerts = scrape_alerts_via_playwright()
        if alerts:
            return alerts, "playwright"
    except Exception as exc:
        log.warning("Primary scrape failed: %s", exc)

    try:
        alerts, markdown = scrape_alerts_via_text_mirror()
        if alerts:
            DEBUG_SNAPSHOT_PATH.write_text(markdown, encoding="utf-8")
            return alerts, "text_mirror"
    except (httpx.HTTPError, TimeoutError, OSError) as exc:
        log.warning("Text mirror fallback failed: %s", exc)

    log.error("No alerts extracted - all strategies failed")
    return [], None


def enrich_new_alerts(alerts: list[dict[str, Any]]) -> int:
    failures = 0
    for alert in alerts:
        try:
            alert["summary"] = generate_summary(alert["title"], alert["raw_body"])
            log.info('Summary generated for alert "%s"', alert["title"])
        except Exception as exc:
            log.warning("Summary generation failed for '%s': %s", alert["title"], exc)
            alert["summary"] = build_fallback_summary(alert["title"], alert["raw_body"])
            failures += 1
    return failures


def main() -> int:
    run_started_at = utc_now_iso()
    run_log: dict[str, Any] = {
        "run_started_at": run_started_at,
        "run_finished_at": None,
        "stored_alerts_before": 0,
        "alerts_scraped": 0,
        "new_alerts_found": 0,
        "scrape_method": None,
        "summary_failures": 0,
        "email_attempted": False,
        "email_sent": False,
        "alerts_updated": False,
        "worked_correctly": False,
        "status": "started",
        "message": "",
    }

    log.info("Scraper started")

    try:
        store = load_store()
        run_log["stored_alerts_before"] = len(store["alerts"])
        log.info("%s stored alerts loaded", len(store["alerts"]))

        scraped, scrape_method = scrape_alerts()
        run_log["alerts_scraped"] = len(scraped)
        run_log["scrape_method"] = scrape_method

        if not scraped:
            run_log["status"] = "no_alerts_extracted"
            run_log["message"] = "No alerts were extracted from either scrape path."
            return 0

        log.info("%s alerts scraped", len(scraped))

        stored_ids = {alert["id"] for alert in store["alerts"]}
        first_seen = utc_now_iso()
        new_alerts: list[dict[str, Any]] = []

        for alert in scraped:
            alert_id = build_alert_id(alert["title"], alert["date"])
            if alert_id in stored_ids:
                continue

            new_alerts.append(
                {
                    "id": alert_id,
                    "title": alert["title"],
                    "raw_body": alert["raw_body"],
                    "summary": "",
                    "date": alert["date"],
                    "url": alert["url"],
                    "first_seen": first_seen,
                }
            )

        run_log["new_alerts_found"] = len(new_alerts)
        log.info("%s new alerts found", len(new_alerts))

        if not new_alerts:
            log.info("No new alerts - nothing to do")
            run_log["worked_correctly"] = True
            run_log["status"] = "no_new_alerts"
            run_log["message"] = "Scrape completed successfully but there were no new alerts."
            return 0

        run_log["summary_failures"] = enrich_new_alerts(new_alerts)
        run_log["email_attempted"] = True
        run_log["email_sent"] = send_email(new_alerts, first_seen)

        store["alerts"].extend(new_alerts)
        store["last_updated"] = first_seen
        save_store(store)
        run_log["alerts_updated"] = True
        log.info("alerts.json updated")

        if run_log["summary_failures"] == 0 and run_log["email_sent"]:
            run_log["worked_correctly"] = True
            run_log["status"] = "success"
            run_log["message"] = "Scrape, summary generation, email, and storage update all succeeded."
        else:
            run_log["worked_correctly"] = False
            run_log["status"] = "partial_success"
            run_log["message"] = "Run completed, but one or more summaries fell back or the email was not sent."
        return 0
    except Exception as exc:
        run_log["status"] = "error"
        run_log["message"] = str(exc)
        raise
    finally:
        run_log["run_finished_at"] = utc_now_iso()
        append_run_log(run_log)


if __name__ == "__main__":
    raise SystemExit(main())

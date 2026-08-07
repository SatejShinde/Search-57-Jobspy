#!/usr/bin/env python3
"""Poll LinkedIn/Indeed/ZipRecruiter for internship/co-op postings matching
Satej's role taxonomy, at companies NOT already covered by check_jobs.py's
direct ATS scrapers. Deliberately standalone -- not imported from or
importing check_jobs.py, per instruction to keep these systems decoupled.

Usage:
    python jobspy_watch.py

Design notes (read before changing the CATEGORIES below):

- Search terms are broad, OR-combined phrases per category, NOT the exact
  literal titles from the source taxonomy. Real postings use wildly
  different phrasing for the same role (Western Digital: "Systems Design
  & Integration Intern"; Tesla: "Software Validation Engineer, Factory
  Firmware") -- searching exact titles would miss almost everything.
- One search call per category (7 total), not one per exact title (~24).
  Fewer, broader calls is both more complete AND safer: LinkedIn
  specifically rate-limits hard and JobSpy's own docs say proxies are
  "basically a must" past trivial volume. No proxies configured here --
  keeping call count low is the mitigation instead.
- A second-pass keyword filter runs on whatever comes back, since Indeed
  matches description text too and will include noise a title-only filter
  would miss.
- "google" is intentionally not in site_name: JobSpy's Google search needs
  a hand-crafted query per search (copied from a live Google Jobs search
  box), which doesn't automate cleanly across 7 categories.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import requests
from jobspy import scrape_jobs

# --------------------------------------------------------------------------
# Companies already covered by check_jobs.py's direct scrapers -- excluded
# here so postings aren't double-notified. Kept as a separate literal list
# rather than importing from check_jobs.py, on purpose (decoupled systems).
# If the tracked-company list changes there, update it here too.
# --------------------------------------------------------------------------
TRACKED_COMPANY_NAMES = {
    "amazon", "microsoft", "meta", "apple", "google", "nvidia", "tesla",
    "qualcomm", "intel", "texas instruments", "analog devices",
    "rockwell automation", "honeywell", "john deere", "amd", "broadcom",
    "microchip", "nxp", "stmicroelectronics", "silicon labs", "siemens",
    "emerson electric", "axon", "western digital",
}

# One combined, OR-everything search term instead of 7 separate category
# calls. Still does real relevance narrowing at the search-term level (see
# module docstring for why that matters), just in one shot -- fewer calls
# is safer at higher run frequency, which matters more now that this runs
# hourly.
SEARCH_TERM = (
    '(embedded OR firmware OR "hardware engineer" OR "hardware development" '
    'OR "electrical engineer" OR "electrical engineering" OR "controls engineer" '
    'OR "controls engineering" OR "automation engineer" OR "automation engineering" '
    'OR "plc engineer" OR "iot engineer" OR "connected devices" OR "systems test" '
    'OR "hardware validation" OR "validation engineer" OR "systems design" '
    'OR "robotics hardware" OR "robotics engineer") (intern OR co-op)'
)

# Second-pass filter on the title -- catches phrasing the search terms
# above didn't anticipate, trims noise from Indeed's description matching.
ROLE_KEYWORDS = re.compile(
    r"\b(embedded|firmware|hardware|electrical engineer(?:ing)?|"
    r"controls engineer(?:ing)?|automation engineer(?:ing)?|plc|iot|"
    r"connected devices|systems test|test engineer|validation engineer|"
    r"systems design|robotics hardware|robotics engineer|electronics)\b",
    re.IGNORECASE,
)

STATE_PATH = Path("state_jobspy.json")


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def send_telegram(token: str, chat_id: str, text: str) -> None:
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=15,
    )
    if not resp.ok:
        print(f"  [telegram error] {resp.status_code}: {resp.text[:200]}", file=sys.stderr)


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        sys.exit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")

    state = load_state()
    seen = set(state.get("_seen", []))
    new_ids: set[str] = set()
    total_new = 0

    try:
        df = scrape_jobs(
            site_name=["indeed", "linkedin", "zip_recruiter"],
            search_term=SEARCH_TERM,
            location="United States",
            results_wanted=100,
            hours_old=72,
            country_indeed="USA",
        )
    except Exception as exc:
        sys.exit(f"fetch failed: {exc}")

    if df is None or df.empty:
        print("0 raw results")
        df = None

    matched = 0
    if df is not None:
        for _, row in df.iterrows():
            title = str(row.get("title") or "")
            company = str(row.get("company") or "").strip()
            job_url = str(row.get("job_url") or "")

            if not ROLE_KEYWORDS.search(title):
                continue
            if company.lower() in TRACKED_COMPANY_NAMES:
                continue
            if not job_url:
                continue

            matched += 1
            new_ids.add(job_url)
            if job_url not in seen:
                total_new += 1
                site = str(row.get("site") or "")
                location = str(row.get("location") or "")
                msg = (
                    f"\U0001f195 <b>{company}</b>: {title}\n"
                    f"{location}\n"
                    f"{job_url}\n"
                    f"<i>via {site}, JobSpy layer</i>"
                )
                send_telegram(token, chat_id, msg)

    print(f"{matched} relevant out of {0 if df is None else len(df)} raw results")

    state["_seen"] = sorted(seen | new_ids)
    save_state(state)
    print(f"Done. {total_new} new postings sent to Telegram.")


if __name__ == "__main__":
    main()

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

# Companies already RELIABLY covered by check_jobs.py's direct scrapers --
# excluded here so postings aren't double-notified. Google and Meta are
# deliberately NOT in this set even though check_jobs.py nominally tracks
# them: that direct scraping doesn't actually work for either (confirmed
# 2026-08 -- both return 0 real results regardless of fixes tried), so
# excluding them here would mean losing them from both systems at once.
# JobSpy goes through LinkedIn/Indeed instead of hitting Google/Meta's own
# sites directly, so it doesn't hit the same wall.
TRACKED_COMPANY_NAMES = {
    "amazon", "microsoft", "nvidia", "tesla",
    "qualcomm", "intel", "texas instruments", "analog devices",
    "rockwell automation", "honeywell", "john deere", "amd", "broadcom",
    "microchip", "nxp", "stmicroelectronics", "silicon labs", "siemens",
    "emerson electric", "axon", "western digital",
}

# Companies exempt from the West-only geographic filter below -- willing
# to relocate anywhere in the US for these specifically, unlike everything
# else JobSpy surfaces. Google and Meta per this exemption; "big tech"
# generally was mentioned too, but there's no clean signal to detect that
# category from a JobSpy row -- add a name here directly if a specific
# company should get the same treatment.
NO_RELOCATION_LIMIT = {"google", "meta"}

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

# Cheap relevance score for triage -- NOT a resume-tailor-quality match
# (that needs the full master resume + real judgment per posting, not a
# fit for an unattended script). Counts keyword hits in the description
# so postings can be ranked, not reviewed in whatever order they arrived.
# Works best for Indeed/ZipRecruiter -- LinkedIn descriptions are often
# thin here since fetching them in full increases request volume.
SKILL_KEYWORDS = [
    "plc", "hmi", "scada", "rtos", "freertos", "can bus", "i2c", "spi",
    "uart", "mqtt", "modbus", "stm32", "arm cortex", "embedded linux",
    "embedded c", "c++", "python", "pcb", "altium", "kicad", "fpga",
    "verilog", "vhdl", "controls", "automation", "industrial automation",
    "sensor", "low-power", "microcontroller", "firmware", "signal processing",
    "power electronics", "robotics", "ros", "circuit design", "schematic",
]


def relevance_score(description: str) -> int:
    text = (description or "").lower()
    return sum(1 for kw in SKILL_KEYWORDS if kw in text)


# Location filters. Two tiers:
#  - WEST_OR_REMOTE: everything except NO_RELOCATION_LIMIT companies.
#    CA/WA/OR/NV/AZ or remote -- adjust WEST_STATES if that definition's
#    off (add CO/UT/ID for wider, back to just CA for narrower).
#  - US_STATES / is_us_location: Google and Meta specifically -- any real
#    US location, not just the West. Same logic as check_jobs.py's
#    is_us_location(), ported here since these are separate, deliberately
#    decoupled scripts.
WEST_STATES = {"CA", "WA", "OR", "NV", "AZ"}
WEST_OR_REMOTE = re.compile(r"\b(" + "|".join(WEST_STATES) + r")\b|California", re.IGNORECASE)

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}
_US_STATE_PATTERN = re.compile(r"\b(" + "|".join(US_STATES) + r")\b")
_US_COUNTRY_PATTERN = re.compile(r"\b(USA|U\.S\.A?\.?|United States)\b", re.IGNORECASE)


def is_us_location(location: str) -> bool:
    if not location:
        return False
    text = location.upper()
    return bool(_US_COUNTRY_PATTERN.search(text) or _US_STATE_PATTERN.search(text))



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
    to_notify = []
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

            location = str(row.get("location") or "")
            is_remote = bool(row.get("is_remote"))
            if company.lower() in NO_RELOCATION_LIMIT:
                if not (is_remote or is_us_location(location)):
                    continue
            elif not (is_remote or WEST_OR_REMOTE.search(location)):
                continue

            matched += 1
            new_ids.add(job_url)
            if job_url not in seen:
                score = relevance_score(str(row.get("description") or ""))
                to_notify.append({
                    "score": score,
                    "company": company,
                    "title": title,
                    "location": location,
                    "job_url": job_url,
                    "site": str(row.get("site") or ""),
                })

    # Highest-relevance postings first, so triage-by-scrolling actually
    # works instead of reviewing 150 in arbitrary arrival order.
    to_notify.sort(key=lambda j: j["score"], reverse=True)

    for job in to_notify:
        total_new += 1
        msg = (
            f"\U0001f195 <b>{job['company']}</b>: {job['title']}\n"
            f"{job['location']}\n"
            f"\U0001f3af relevance: {job['score']}\n"
            f"{job['job_url']}\n"
            f"<i>via {job['site']}, JobSpy layer</i>"
        )
        send_telegram(token, chat_id, msg)

    print(f"{matched} relevant out of {0 if df is None else len(df)} raw results")

    state["_seen"] = sorted(seen | new_ids)
    save_state(state)
    print(f"Done. {total_new} new postings sent to Telegram.")


if __name__ == "__main__":
    main()
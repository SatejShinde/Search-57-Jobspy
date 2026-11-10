#!/usr/bin/env python3
"""Existing three JobSpy searches, followed by checkpointed original alerts.

Description details are retrieved separately, only for selected notifications.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import requests
from jobspy import scrape_jobs
from jobspy_delivery import (QUEUE, CACHE, enqueue, reconcile, checkpoint, send_alerts, send_alert)
from telegram_transport import load, save

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
    "emerson electric", "axon", "western digital", "amd",
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

# Google and Meta get a SEPARATE, unrestricted search -- not filtered to
# hardware/embedded roles at all, per explicit instruction to surface
# every US internship at these two, not just the ones matching SEARCH_TERM
# above. This has to be a distinct API call, not just a different
# post-filter: SEARCH_TERM itself determines what the job boards return in
# the first place, so a Product Manager or Data Science posting at Google
# would never even be fetched by the role-scoped search, regardless of
# how permissive the filter afterward is.
UNRESTRICTED_SEARCH_TERMS = {
    "google": '"Google" (intern OR co-op)',
    "meta": '"Meta" (intern OR co-op)',
}

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



# Confirms the posting is actually titled as an internship/co-op --
# distinct from ROLE_KEYWORDS below (which checks for the technical
# category). Missing this was a real bug: Indeed/LinkedIn search full
# description text, not just titles, so a senior/director posting whose
# description happens to mention "our interns" would match the search
# query without this check. Same definition as check_jobs.py's KEYWORDS,
# kept consistent across the whole system on purpose.
INTERN_TITLE = re.compile(r"\b(intern|internship|co-?op)\b", re.IGNORECASE)

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


def retained_fields(row):
    def value(name):
        result=row.get(name)
        return result if isinstance(result,str) and result not in {"nan","None"} else ""
    return {"jobspy_id":value("id"),"job_url_direct":value("job_url_direct"),
            "description":value("description")}


def notify_pending(queue,state,token,chat_id):
    confirmed=send_alerts(queue,state,checkpoint,
                          lambda record,deadline:send_alert(token,chat_id,record,deadline))
    checkpoint(queue,state)
    print(f"Done. {confirmed} alerts confirmed; {sum(not e['alert_sent'] for e in queue.values())} unconfirmed alerts remain recoverable.")


def main() -> None:
    parser=argparse.ArgumentParser()
    phases=parser.add_mutually_exclusive_group()
    phases.add_argument("--collect-only",action="store_true")
    phases.add_argument("--alerts-only",action="store_true")
    args=parser.parse_args()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not args.collect_only and (not token or not chat_id):
        sys.exit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")

    state = load_state()
    queue=load(QUEUE)
    reconcile(queue,state)
    if args.alerts_only:
        notify_pending(queue,state,token,chat_id)
        return
    seen = set(state.get("_seen", []))
    new_ids: set[str] = set()
    total_new = 0

    def fetch(term: str) -> "pd.DataFrame | None":
        try:
            df = scrape_jobs(
                site_name=["indeed", "linkedin", "zip_recruiter"],
                search_term=term,
                location="United States",
                results_wanted=100,
                hours_old=72,
                country_indeed="USA",
                fetch_description=False,
            )
        except Exception as exc:
            print(f"  fetch failed for {term!r}: {type(exc).__name__}", file=sys.stderr)
            return None
        return None if df is None or df.empty else df

    matched = 0
    raw_total = 0
    raw_by_site: dict[str, int] = {}
    matched_by_site: dict[str, int] = {}
    to_notify = []

    # Role-scoped search: everything except Google/Meta, which get their
    # own unrestricted searches below instead.
    df = fetch(SEARCH_TERM)
    if df is not None:
        raw_total += len(df)
        for site, count in df["site"].value_counts().items():
            raw_by_site[str(site)] = raw_by_site.get(str(site), 0) + int(count)
        for _, row in df.iterrows():
            title = str(row.get("title") or "")
            company = str(row.get("company") or "").strip()
            job_url = str(row.get("job_url") or "")

            if company.lower() in UNRESTRICTED_SEARCH_TERMS:
                continue  # handled by the dedicated search below instead
            if not INTERN_TITLE.search(title):
                continue
            if not ROLE_KEYWORDS.search(title):
                continue
            if company.lower() in TRACKED_COMPANY_NAMES:
                continue
            if not job_url:
                continue

            location = str(row.get("location") or "")
            is_remote = bool(row.get("is_remote"))
            if not (is_remote or WEST_OR_REMOTE.search(location)):
                continue

            matched += 1
            new_ids.add(job_url)
            site = str(row.get("site") or "")
            matched_by_site[site] = matched_by_site.get(site, 0) + 1
            if job_url not in seen:
                score = relevance_score(str(row.get("description") or ""))
                to_notify.append({
                    "score": score, "company": company, "title": title,
                    "location": location, "job_url": job_url,
                    "site": site, **retained_fields(row),
                })

    # Google / Meta: unrestricted by role, any US location. Company field
    # is checked explicitly since the search term matching "Google" or
    # "Meta" as text doesn't guarantee that's the actual employer.
    for company_name, term in UNRESTRICTED_SEARCH_TERMS.items():
        df = fetch(term)
        if df is None:
            continue
        raw_total += len(df)
        for site, count in df["site"].value_counts().items():
            raw_by_site[str(site)] = raw_by_site.get(str(site), 0) + int(count)
        for _, row in df.iterrows():
            title = str(row.get("title") or "")
            company = str(row.get("company") or "").strip()
            job_url = str(row.get("job_url") or "")

            if company_name not in company.lower():
                continue  # search matched the text "Google"/"Meta" somewhere, but this isn't actually them
            if not INTERN_TITLE.search(title):
                continue  # "Early Career X" or a Director role whose description mentions interns -- not itself an internship
            if not job_url:
                continue

            location = str(row.get("location") or "")
            is_remote = bool(row.get("is_remote"))
            if not (is_remote or is_us_location(location)):
                continue

            matched += 1
            new_ids.add(job_url)
            site = str(row.get("site") or "")
            matched_by_site[site] = matched_by_site.get(site, 0) + 1
            if job_url not in seen:
                score = relevance_score(str(row.get("description") or ""))
                to_notify.append({
                    "score": score, "company": company, "title": title,
                    "location": location, "job_url": job_url,
                    "site": str(row.get("site") or ""), **retained_fields(row),
                })

    # Highest-relevance postings first, so triage-by-scrolling actually
    # works instead of reviewing 150 in arbitrary arrival order.
    to_notify.sort(key=lambda j: j["score"], reverse=True)

    cache={}
    enabled=os.getenv("JOBSPY_DESCRIPTIONS_ENABLED")=="true"
    for job in to_notify:
        enqueue(queue,job,enabled,cache)
    save(CACHE,cache)
    checkpoint(queue,state)

    print(f"{matched} relevant out of {raw_total} raw results across 3 searches")
    print(f"  raw by site: {raw_by_site}")
    print(f"  matched by site: {matched_by_site}")

    print(f"{len(to_notify)} newly selected rows; {len(queue)} pending job identities")
    if not args.collect_only:
        notify_pending(queue,state,token,chat_id)


if __name__ == "__main__":
    main()
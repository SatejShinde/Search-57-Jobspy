"""Deterministic Telegram Desktop export indexer. No network, no LLM, no database writes.

index: stream message objects, keep only identities/part hashes in memory.
show: re-read original export, load bodies only for one selected job.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

from telegram_jobs import decode, digest, job_key, normalized_url


def iter_messages(path):
    """Streaming stdlib JSON decoder for a Desktop single-chat result.json.

    Memory is bounded by largest message + identity/part index, not export size.
    Also accepts the all-chats wrapper by discovering nested messages arrays.
    A small tokenizer walks structure rather than regex-matching quoted text.
    """
    decoder = json.JSONDecoder()
    with Path(path).open(encoding="utf-8-sig") as handle:
        buffer = ""
        eof = False
        def refill():
            nonlocal buffer, eof
            block = handle.read(65536)
            buffer += block
            eof = not block
        def consume_space():
            nonlocal buffer
            while True:
                buffer = buffer.lstrip()
                if buffer or eof:
                    return
                refill()
        def scalar():
            nonlocal buffer
            while True:
                consume_space()
                try:
                    value, end = decoder.raw_decode(buffer)
                    # A number can end at a buffer boundary, unlike a string.
                    if end == len(buffer) and not eof:
                        refill(); continue
                    buffer = buffer[end:]
                    return value
                except json.JSONDecodeError:
                    if eof:
                        raise ValueError("Invalid or incomplete Telegram JSON export")
                    refill()
        def expect(char):
            nonlocal buffer
            consume_space()
            if not buffer.startswith(char):
                raise ValueError(f"Invalid JSON: expected {char}")
            buffer = buffer[1:]
        def walk(in_messages=False):
            nonlocal buffer
            consume_space()
            if in_messages:
                expect("[")
                while True:
                    consume_space()
                    if buffer.startswith("]"):
                        buffer = buffer[1:]; return
                    value = scalar()
                    if isinstance(value, dict):
                        yield value
                    consume_space()
                    if buffer.startswith("]"):
                        buffer = buffer[1:]; return
                    expect(",")
            elif buffer.startswith("{"):
                expect("{")
                consume_space()
                if buffer.startswith("}"):
                    buffer = buffer[1:]; return
                while True:
                    key = scalar(); expect(":")
                    yield from walk(key == "messages")
                    consume_space()
                    if buffer.startswith("}"):
                        buffer = buffer[1:]; return
                    expect(",")
            elif buffer.startswith("["):
                expect("[")
                consume_space()
                if buffer.startswith("]"):
                    buffer = buffer[1:]; return
                while True:
                    yield from walk()
                    consume_space()
                    if buffer.startswith("]"):
                        buffer = buffer[1:]; return
                    expect(",")
            else:
                scalar()
        refill()
        yield from walk()
        consume_space()
        if buffer:
            raise ValueError("Unexpected trailing JSON data")


def message_text(message):
    value = message.get("text", "")
    if isinstance(value, str):
        return value
    return "".join(v if isinstance(v, str) else v.get("text", "") for v in value)


def url_identity(url):
    """Conservative ATS identity for legacy links; never title-only merge."""
    u = normalized_url(url)
    platform = "unknown"; ats_id = ""
    patterns = [
        ("linkedin", r"linkedin\.com/jobs/view/(?:[^/?]*-)?(\d+)"),
        ("indeed", r"indeed\.com/[^?]*[?&]jk=([a-zA-Z0-9]+)"),
        ("amazon", r"amazon\.jobs/(?:[^/]+/)?jobs/(\d+)"),
        ("workday", r"myworkdayjobs\.com/.+[/_](R[A-Za-z0-9_-]+)(?:$|\?)"),
        ("greenhouse", r"greenhouse\.io/(?:[^/]+/)?jobs/(\d+)"),
        ("greenhouse", r"[?&]gh_jid=(\d+)"),
        ("eightfold", r"[?&]pid=(\d+)"),
        ("icims", r"icims\.com/jobs/(\d+)"),
        ("lever", r"lever\.co/[^/]+/([a-f0-9-]{36})"),
        ("apple", r"apple\.com/[^/]+/details/(\d+)"),
        ("tesla", r"tesla\.com/.*/(?:[^/]*-)?(\d+)(?:$|\?)"),
        ("smartrecruiters", r"smartrecruiters\.com/[^/]+/([0-9]+)"),
        ("oracle", r"oraclecloud\.com/.*/job/(\d+)"),
        ("google", r"google\.com/about/careers/applications/jobs/results/(\d+)"),
        ("meta", r"metacareers\.com/(?:profile/job_details|jobs)/(\d+)"),
        ("ashby", r"ashbyhq\.com/[^/]+/([a-f0-9-]{36})"),
    ]
    for provider, pattern in patterns:
        match = re.search(pattern, u, re.I)
        if match:
            platform, ats_id = provider, match[1]; break
    return platform, ats_id


def legacy_record(text):
    # Funding outreach prospects are not job postings. Their existing digest
    # delivery is untouched; only the export index distinguishes the item type.
    if "FUNDING OUTREACH PROSPECT —" in text and "OPEN INTERNSHIP —" not in text:
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    simple_title_url = (2 <= len(lines) <= 3 and not lines[0].startswith("http")
                        and any(line.startswith("http") for line in lines[1:]))
    if not (text.startswith("🆕 ") or text.startswith("OPEN INTERNSHIP")
            or "Hardware internship watch" in text or re.search(r"(?im)^(?:Job|Position|Title):", text)
            or simple_title_url):
        return None
    url = re.search(r"https?://[^\s<>]+", text)
    if not url:
        return None
    url = url[0].rstrip(").,")
    first = re.sub(r"^🆕\s*", "", lines[0])
    if ": " in first:
        company, title = first.split(": ", 1)
        location = lines[1] if len(lines) > 2 else ""
    elif any(line.startswith("OPEN INTERNSHIP — ") for line in lines):
        pos = next(i for i, line in enumerate(lines) if line.startswith("OPEN INTERNSHIP — "))
        company = lines[pos].split(" — ", 1)[1]
        title = lines[pos+1] if len(lines) > pos+1 else ""
        location = ""
    else:
        company = "Unknown"; title = first; location = ""
    platform, ats_id = url_identity(url)
    if "JobSpy layer" in text or platform in {"linkedin","indeed","zip_recruiter"}:
        via=re.search(r"via (linkedin|indeed|zip_recruiter)",text)
        platform=via[1] if via else platform
        prefix={"linkedin":"li-","indeed":"in-","zip_recruiter":"zr-"}.get(platform,"")
        relevance=re.search(r"relevance:\s*(\d+)",text)
        return dict(company=company,title=title,location=location,platform=platform,
                    collector="jobspy",jobspy_id=prefix+ats_id if ats_id else "",direct_url="",score=int(relevance[1]) if relevance else "",
                    ats_id="",requisition_id="",url=url,description_source="legacy")
    return dict(company=company, title=title, location=location, platform=platform,
                ats_id=ats_id, requisition_id="", url=url)


def inspect_export(path, selected=None, include_canary=False):
    transfers = {}; legacy = {}; errors = []
    for message in iter_messages(path):
        text = message_text(message)
        if text.startswith("[Search57 v1]"):
            try:
                decoded = decode(text)
                if decoded is None:
                    raise ValueError("Incomplete Search57 frame")
                meta, body = decoded
                if meta.get("canary") and not include_canary:
                    continue
            except (ValueError, KeyError, TypeError) as exc:
                errors.append(str(message.get("id", "?")) + ": " + str(exc)); continue
            transfer_key = (meta["key"], meta["sha256"])
            t = transfers.setdefault(transfer_key, {"parts": {}, "conflict": False, "commit": False,
                                                     "meta": None, "messages": [], "total": meta["parts"]})
            if t["total"] != meta["parts"]:
                t["conflict"] = True
            t["messages"].append(str(message.get("id", "")))
            if meta["kind"] == "job":
                # Same requisition can be notified with another location or a
                # refreshed title/note. Body hashes, not display fields, govern
                # reconstruction. Keep a deterministic readable header.
                if t["meta"]:
                    meta = min((t["meta"], meta), key=lambda m: json.dumps(m,sort_keys=True))
                t["meta"] = meta
                if meta.get("part") == 1 or meta["parts"] == 0:
                    t["commit"] = True
            elif meta["kind"] == "complete":
                t["commit"] = True
            if meta["kind"] == "part" or meta.get("part") == 1:
                part = meta.get("part")
                if not isinstance(part, int) or not 1 <= part <= meta["parts"]:
                    t["conflict"] = True; continue
                # Index never retains full descriptions. show retains one key.
                value = (digest(body), body if selected == meta["key"] else None,
                         len(body.encode("utf-8")))
                if part in t["parts"] and t["parts"][part][0] != value[0]:
                    t["conflict"] = True
                t["parts"][part] = value
        else:
            row = legacy_record(text)
            if row:
                legacy.setdefault(normalized_url(row["url"]), row)
    rows = []
    known_headers = {key: t["meta"] for (key, sha), t in transfers.items() if t["meta"]}
    for (key, sha), t in transfers.items():
        meta = t["meta"]
        if not meta:
            # Lost notification header, but chunks still have a stable identity.
            meta = {**known_headers.get(key, dict(company="Unknown", title="Missing notification header", location="",
                                                 platform="unknown", ats_id="", requisition_id="", url="")),
                    "status": "incomplete", "note": "Missing notification header for this revision"}
        total = t["total"]
        present = set(t["parts"])
        complete = bool(t["meta"]) and t["commit"] and present == set(range(1, total+1)) and not t["conflict"]
        # Index verifies SHA of each part on collection, while end-to-end SHA is
        # verified in a second streaming pass below, without storing bodies.
        row = {k: meta.get(k, "") for k in ("company", "title", "location", "platform", "ats_id", "requisition_id", "url",
                                            "collector","jobspy_id","direct_url","score","description_source")}
        row.update(job_key=key, revision=sha, description_status=meta.get("status", "incomplete"),
                   delivery_status="complete" if complete else "incomplete", parts_expected=total,
                   parts_received=len(present), message_ids=";".join(sorted(set(t["messages"]))),
                   note=meta.get("note", ""), application_decision="unchecked")
        if meta.get("canary"):
            row["note"] = "CANARY TEST; " + row["note"]
        if t["conflict"]:
            row["note"] += "; conflicting duplicate chunks/headers"
        if selected == key and complete:
            row["description"] = "".join(t["parts"][i][1] for i in range(1, total+1))
            if digest(row["description"]) != sha:
                row.update(delivery_status="incomplete", note=row["note"] + "; SHA256 mismatch")
        rows.append(row)
    # Full SHA validation without keeping description bodies: Telegram can be
    # out of order, so retain part offsets in a temporary local spool, not a DB.
    validate_hashes(path, rows, transfers)
    known_urls = {normalized_url(r["url"]) for r in rows if r["url"]}
    # ATS IDs alone can collide across tenants. URL-derived identity may match
    # legacy messages only when it identifies exactly one company in this export.
    known_ids = {}
    for r in rows:
        for identity in {str(r["requisition_id"] or r["ats_id"]).split("@")[0].casefold(),
                         url_identity(r["url"])[1].casefold()} - {""}:
            known_ids.setdefault((r["platform"], identity), set()).add(r["company"].casefold())
    for url, row in legacy.items():
        companies = known_ids.get((row["platform"], row["ats_id"].casefold()), set())
        match = url in known_urls or (row["ats_id"] and len(companies) == 1 and
                                     (row["company"] == "Unknown" or row["company"].casefold() in companies))
        if not match:
            rows.append({**row, "job_key": job_key(row), "revision": "", "description_status": "legacy_unavailable",
                         "delivery_status": "unavailable", "parts_expected": 0, "parts_received": 0,
                         "message_ids": "", "note": "Legacy notification; description not present",
                         "application_decision": "unchecked"})
    # Group by company-scoped requisition identity (location variants collapse).
    chosen = {}; incomplete_counts = {}; variant_counts = {}
    rank = lambda r: (r["delivery_status"] == "complete", r["description_status"] == "available",
                       max((int(i) for i in r["message_ids"].split(";") if i.isdigit()), default=0),
                       r["parts_received"], r["revision"])
    for row in rows:
        company = re.sub(r"\W+", "", row["company"].casefold())
        identity = (job_key(row) if row.get("collector")=="jobspy" else
                    company + "|req|" + str(row["requisition_id"]).split("@")[0].casefold()
                    if row["requisition_id"] and row["company"] != "Unknown" else
                    job_key(row) if row["company"] != "Unknown" else row["job_key"])
        variant_counts[identity] = variant_counts.get(identity, 0) + 1
        incomplete_counts[identity] = incomplete_counts.get(identity, 0) + (row["delivery_status"] == "incomplete")
        if identity not in chosen or rank(row) > rank(chosen[identity]):
            chosen[identity] = row
    for identity, row in chosen.items():
        if variant_counts[identity] > 1 and incomplete_counts[identity]:
            row["note"] += f"; {incomplete_counts[identity]} incomplete transfer(s) seen across {variant_counts[identity]} revisions/identities"
    return sorted(chosen.values(), key=lambda r: (r["company"].casefold(), r["title"], r["job_key"])), errors


def validate_hashes(path, rows, transfers):
    import tempfile
    needed = {(r["job_key"], r["revision"]): r for r in rows if r["delivery_status"] == "complete"}
    offsets = {}
    with tempfile.TemporaryFile() as spool:
        for message in iter_messages(path):
            try:
                value = decode(message_text(message))
                if not value:
                    continue
                meta, text = value
                key = (meta["key"], meta["sha256"])
                part = meta.get("part", 0)
                if key not in needed or not part:
                    continue
                data = text.encode("utf-8")
                offsets[key, part] = (spool.tell(), len(data))
                spool.write(data)
            except (ValueError, KeyError, TypeError):
                continue
        for key, row in needed.items():
            sha = hashlib.sha256()
            for i in range(1, row["parts_expected"]+1):
                offset, length = offsets[key, i]
                spool.seek(offset); sha.update(spool.read(length))
            if sha.hexdigest() != row["revision"]:
                row.update(delivery_status="incomplete", note=row["note"] + "; SHA256 mismatch")


def check_applications(rows, db):
    # Import the local tracker read-only API, never its CLI/load_and_sync.
    import application_tracker
    existing = application_tracker.read_database(db)
    for row in rows:
        if row.get("collector")=="jobspy":
            # Board IDs are NOT employer requisitions. Avoid the tracker's URL
            # ID inference for board links; use exact normalized URL evidence.
            urls={application_tracker.canonical_url(row["url"])}
            direct=row.get("direct_url") or ""
            if direct: urls.add(application_tracker.canonical_url(direct))
            exact=[r for r in existing if r['canonical_application_url'] and
                   application_tracker.canonical_url(r['canonical_application_url']) in urls]
            if exact:
                certain=(len(exact)==1 and application_tracker.company_key(exact[0]['company'])==
                         application_tracker.company_key(row['company']))
                row['application_decision']=("skip_duplicate" if certain and application_tracker.confirmed(exact[0])
                                             else "proceed" if certain else "review")
            elif direct and not any((urlsplit(direct).hostname or "").endswith(domain)
                                    for domain in ("linkedin.com","indeed.com","ziprecruiter.com")):
                row['application_decision']=application_tracker.check_job(existing,row['company'],"",direct,row['title'])["decision"]
            else:
                row['application_decision']=application_tracker.check_job(existing,row['company'],"","",row['title'])["decision"]
            continue
        row["application_decision"] = application_tracker.check_job(
            existing, row["company"], row["requisition_id"] or row["ats_id"],
            row["url"], row["title"])["decision"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["index", "show"])
    parser.add_argument("export", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--key")
    parser.add_argument("--applications", type=Path)
    parser.add_argument("--skip-applied", action="store_true")
    parser.add_argument("--include-canary", action="store_true",
                        help="Explicitly include isolated CANARY TEST messages; normally ignored")
    args = parser.parse_args()
    if args.command == "show" and not args.key:
        parser.error("show requires --key")
    if args.skip_applied and not args.applications:
        parser.error("--skip-applied requires --applications")
    rows, errors = inspect_export(args.export, args.key, args.include_canary)
    if args.applications:
        check_applications(rows, args.applications)
    if args.skip_applied:
        rows = [r for r in rows if r["application_decision"] != "skip_duplicate"]
    if args.command == "index":
        output = args.output or args.export.with_name("index.csv")
        fields = ["job_key", "company", "title", "location", "platform", "ats_id", "requisition_id", "url",
                  "collector","jobspy_id","direct_url","score","description_source",
                  "description_status", "delivery_status", "parts_expected", "parts_received", "revision",
                  "application_decision", "message_ids", "note"]
        with output.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader(); writer.writerows(rows)
        print(json.dumps({"jobs": len(rows), "malformed_messages": errors, "output": str(output)}))
    else:
        selected = [r for r in rows if r["job_key"] == args.key]
        if not selected:
            sys.exit("Job not found or explicitly skipped as already applied")
        row = selected[0]
        if row["delivery_status"] != "complete":
            sys.exit("Description incomplete/unavailable; inspect index status")
        text = row.get("description", "")
        if not text:
            sys.exit("No description text available")
        if args.output:
            args.output.write_text(text, encoding="utf-8")
        else:
            print(text)


if __name__ == "__main__":
    main()

"""JobSpy-only metadata queue and bounded delivery; description bodies never in Git."""
from __future__ import annotations

import argparse
from html import escape
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from telegram_jobs import digest, job_key, messages
from telegram_transport import DeliveryError, load, save, send_plain
from jobspy_details import text

QUEUE=Path("pending_jobspy.json")
STATE=Path("state_jobspy.json")
CACHE=Path(".runtime/jobspy-listings.json")


def record_for(job):
    return dict(collector="jobspy",company=job["company"],title=job["title"],location=job["location"],
                platform=job["site"],jobspy_id=text(job.get("jobspy_id")),ats_id="",requisition_id="",
                url=job["job_url"],direct_url=text(job.get("job_url_direct")),score=job["score"])


def enqueue(queue,job,enabled,cache):
    record=record_for(job);key=job_key(record)
    from archive_bridge import enqueue_source
    enqueue_source(record)
    queue.setdefault(key,dict(record=record,alert_sent=False,want_description=enabled,
                              attempts=0,acks={},last_attempt=0))
    if enabled and text(job.get("description")).strip():
        cache[key]=job["description"]
        if os.getenv("GITHUB_RUN_ID"):
            queue[key]["listing_run_id"]=os.environ["GITHUB_RUN_ID"]
    return key


def alert(record):
    # Keep the original notification's fields/order/style, with safe HTML.
    return (f"🆕 <b>{escape(record['company'])}</b>: {escape(record['title'])}\n"
            f"{escape(record['location'])}\n🎯 relevance: {record['score']}\n"
            f"{escape(record['url'])}\n<i>via {escape(record['platform'])}, JobSpy layer</i>")


def send_alert(token,chat,record,deadline):
    # Same verified transport, optional HTML only for the legacy alert.
    return send_plain(token,chat,alert(record),deadline=deadline,parse_mode="HTML",disable_preview=False)


def acknowledge(state,entry,message_id):
    entry.update(alert_sent=True,alert_message_id=message_id)
    state["_seen"]=sorted(set(state.get("_seen",[]))|{entry["record"]["url"]})


def active(entry,enabled):
    return not entry["alert_sent"] or (enabled and entry["want_description"])


def send_alerts(queue,state,checkpoint,send,*,seconds=270,sleep=time.sleep):
    deadline=time.monotonic()+seconds
    confirmed=0
    # Original score order and stable tie order, including unsent retries.
    entries=sorted(list(queue.items()),key=lambda kv:kv[1]["record"]["score"],reverse=True)
    for key,entry in entries:
        if entry["alert_sent"]: continue
        if deadline-time.monotonic()<3: break
        try:
            message_id=send(entry["record"],min(deadline,time.monotonic()+15))
            acknowledge(state,entry,message_id);confirmed+=1
            if not entry["want_description"]: del queue[key]
            checkpoint(queue,state)
            sleep(min(3.1,max(0,deadline-time.monotonic())))
        except Exception as exc:
            entry["last_error"]=type(exc).__name__;checkpoint(queue,state)
    return confirmed


def bounded_retrieve(entry,listing,seconds):
    try:
        process=subprocess.run([sys.executable,__file__,"--retrieve-one"],
                               input=json.dumps({"entry":entry,"listing":listing}),text=True,encoding="utf-8",
                               env={**os.environ,"PYTHONIOENCODING":"utf-8"},capture_output=True,timeout=seconds,check=True)
        return json.loads(process.stdout)
    except (subprocess.SubprocessError,ValueError):
        return dict(text=text(listing),status="partial" if text(listing).strip() else "unavailable",
                    source="listing_fallback" if listing else "targeted",
                    note="Targeted request failed or timed out; no access bypass attempted")


def deliver(queue,state,cache,checkpoint,send,send_original,retrieve=bounded_retrieve,*,
            enabled=True,seconds=240,max_jobs=12,sleep=time.sleep,audit=lambda value:None):
    deadline=time.monotonic()+seconds
    processed=0
    for key,entry in sorted(list(queue.items()),key=lambda kv:kv[1].get("last_attempt",0)):
        if not active(entry,enabled): continue
        if processed>=max_jobs or deadline-time.monotonic()<40: break
        processed+=1;entry["attempts"]+=1;entry["last_attempt"]=time.time();checkpoint(queue,state)
        try:
            if not entry["alert_sent"]:
                acknowledge(state,entry,send_original(entry["record"],deadline));checkpoint(queue,state)
                sleep(min(3.1,max(0,deadline-time.monotonic())))
            if not entry["want_description"]:
                del queue[key];checkpoint(queue,state);continue
            if not enabled: continue
            detail=retrieve(entry,cache.get(key,""),min(25,deadline-time.monotonic()-10))
            from archive_bridge import capture_detail
            capture_detail(entry,detail)
            wires=messages({**entry["record"],"description_source":detail["source"]},detail["text"],detail["status"],
                           "Source: "+detail["source"]+"; "+detail["note"])
            revision=digest(detail["text"])
            entry.update(retrieval_status=detail["status"])
            if entry.get("revision")!=revision:
                entry.update(revision=revision,acks={});checkpoint(queue,state)
            for wire in wires:
                wire_id=digest(wire)
                if wire_id in entry["acks"]: continue
                if deadline-time.monotonic()<3: raise DeliveryError("Time budget exhausted")
                entry["acks"][wire_id]=send(wire,deadline);checkpoint(queue,state)
                sleep(min(3.1,max(0,deadline-time.monotonic())))
            audit(dict(job_key=key,status=detail["status"],source=detail["source"],
                       characters=len(detail["text"]),sha256=revision,
                       confirmed_wires=len(wires),complete=True))
            del queue[key];checkpoint(queue,state)
        except Exception as exc:
            entry["last_error"]=type(exc).__name__;checkpoint(queue,state)
            audit(dict(job_key=key,complete=False,error=type(exc).__name__))
    return processed


def checkpoint(queue,state):
    # Queue first: a crash between files can be recovered from its alert receipt.
    save(QUEUE,queue);save(STATE,state)


def reconcile(queue,state):
    for entry in queue.values():
        if entry["alert_sent"] and entry.get("alert_message_id"):
            acknowledge(state,entry,entry["alert_message_id"])


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--retrieve-one",action="store_true")
    parser.add_argument("--preflight",action="store_true")
    args=parser.parse_args()
    if args.retrieve_one:
        import contextlib,logging
        from jobspy_details import retrieve
        logging.getLogger("jobspy").setLevel(logging.CRITICAL)
        payload=json.load(sys.stdin)
        with contextlib.redirect_stdout(sys.stderr):
            value=retrieve(payload["entry"],payload["listing"])
        print(json.dumps(value));return
    enabled=os.getenv("JOBSPY_DESCRIPTIONS_ENABLED")=="true"
    queue=load(QUEUE);state=load(STATE);reconcile(queue,state)
    pending=sum(active(e,enabled) for e in queue.values())
    if args.preflight:
        if os.getenv("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"],"a",encoding="utf-8") as f:
                f.write(f"active={'true' if pending else 'false'}\n")
        print(json.dumps(dict(pending=len(queue),active=pending,descriptions_enabled=enabled)));return
    token=os.environ["TELEGRAM_BOT_TOKEN"];chat=os.environ["TELEGRAM_CHAT_ID"]
    deliver(queue,state,load(CACHE),checkpoint,
            lambda wire,deadline:send_plain(token,chat,wire,deadline=deadline),
            lambda r,deadline:send_alert(token,chat,r,deadline),enabled=enabled,
            audit=lambda value:print(json.dumps(value)))
    checkpoint(queue,state)
    pending=sum(active(e,enabled) for e in queue.values())
    print(json.dumps(dict(pending=len(queue),active=pending,complete=pending==0)))
    if pending: sys.exit(1)


if __name__=="__main__": main()

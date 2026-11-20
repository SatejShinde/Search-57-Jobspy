"""Bounded work is deferred; corruption, credentials and stuck work are failures."""
from __future__ import annotations
import json
import re
import subprocess
import time

STALE_SECONDS=3600
STALE_ATTEMPTS=6
MAX_QUEUE_SECONDS=21600

class WorkFailure(RuntimeError):
    def __init__(self,category,retryable=False):
        super().__init__(category);self.category=category;self.retryable=retryable

def validate(queue):
    if not isinstance(queue,dict):raise WorkFailure('state_corruption')
    for key,e in queue.items():
        if not isinstance(key,str) or not isinstance(e,dict) or not isinstance(e.get('record'),dict):raise WorkFailure('state_corruption')
        if not isinstance(e.get('alert_sent'),bool) or not isinstance(e.get('attempts'),int) or e['attempts']<0:raise WorkFailure('state_corruption')
        if any(not isinstance(e['record'].get(f),str) or not e['record'][f] for f in ['company','title','platform','url']):raise WorkFailure('state_corruption')

def track(entry,now=None):
    now=time.time() if now is None else now
    entry.setdefault('first_pending_at',now);entry.setdefault('last_progress_at',now)
    entry.setdefault('attempts_without_progress',0)

def begin(entry,now=None):
    track(entry,now)
    entry['attempts_without_progress']=entry.get('attempts_without_progress',0)+1
    entry['work_errors']=[]

def progress(entry,now=None):
    entry['last_progress_at']=time.time() if now is None else now
    entry['attempts_without_progress']=0

def error(entry,exc,component):
    category=getattr(exc,'category',None);retryable=getattr(exc,'retryable',None)
    code=None
    if category is None:
        # Match only our fixed transport message; never persist exception text,
        # which can include tokens, URLs or private source bodies.
        if type(exc).__name__=='DeliveryError':
            match=re.search(r'Telegram rejected message \((\d+)\)',str(exc))
            code=int(match[1]) if match else None
            category='credentials' if code in {401,403} else 'invalid_notification' if code and code<500 and code!=429 else 'telegram_retry'
            retryable=category=='telegram_retry'
        elif isinstance(exc,(TimeoutError,ConnectionError,subprocess.TimeoutExpired)) or any(c.__name__=='RequestException' for c in type(exc).__mro__):category='network_retry';retryable=True
        elif isinstance(exc,(ValueError,KeyError,TypeError)):category='invalid_record_or_ciphertext';retryable=False
        elif isinstance(exc,OSError):category='checkpoint_write';retryable=False
        else:category='processing_error';retryable=False
    value=dict(component=component,category=category,retryable=bool(retryable),exception_type=type(exc).__name__)
    if code:value['status_code']=code
    entry.setdefault('work_errors',[]).append(value)
    entry['last_error']=category
    return value

def summary(queue,before,processed,*,now=None,enabled=True,max_jobs=12,require_alert=True):
    validate(queue);now=time.time() if now is None else now
    fatal=[];retryable=[];stale=[]
    active={k:e for k,e in queue.items() if require_alert and not e['alert_sent'] or enabled and e.get('want_description',True)}
    for key,e in active.items():
        for item in e.get('work_errors',[]):(retryable if item.get('retryable') else fatal).append(dict(key=key,**item))
        since=e.get('last_progress_at',e.get('first_pending_at',now));age=now-e.get('first_pending_at',now)
        if age>=MAX_QUEUE_SECONDS or now-since>=STALE_SECONDS and e.get('attempts_without_progress',0)>=STALE_ATTEMPTS:stale.append(key)
    status='failed' if fatal or stale else 'deferred' if active else 'paused' if queue else 'complete'
    reason='fatal_error' if fatal else 'stale_queue' if stale else 'retryable_dependency' if retryable else 'count_limit' if active and processed>=max_jobs else 'time_budget_or_pending_handoff' if active else 'drained_or_paused'
    return dict(status=status,reason=reason,pending_before=before,processed=processed,pending=len(queue),active_pending=len(active),complete=not queue,retry_scheduled=bool(active) and not fatal and not stale,fatal_errors=fatal,retryable_errors=retryable,stale_keys=stale,exit_code=1 if status=='failed' else 0)

def emit(report):
    print(json.dumps(report))
    if report['status']=='deferred':print('::notice::Bounded work remains durably queued for the next automatic follow-up.')
    if report['status']=='failed':print('::error::Pending work has a fatal error or stale retry progress; checkpoints are retained. Inspect the structured queue report.')

def stage_failed(entry):
    if not entry.get('work_errors'):error(entry,WorkFailure('archive_staging_retry',True),'archive_staging')

"""Private archive handoff. Public durable outbox contains identity metadata ONLY."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime,timezone

def utc():return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
def load(p):return json.loads(Path(p).read_text(encoding='utf-8')) if Path(p).exists() else {}
def save(p,value):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');tmp.replace(p)
def tier(record=None):
    if record and record.get('collector')=='jobspy':return 'jobspy'
    if '--tier' in sys.argv:return sys.argv[sys.argv.index('--tier')+1]
    return os.getenv('ARCHIVE_TIER','jobspy' if Path('jobspy_watch.py').exists() else 'fast')
def key(record):
    native=str(record.get('jobspy_id') or record.get('ats_id') or record['url']).split('@loc')[0]
    return hashlib.sha256((record.get('collector','search57')+'|'+record['company'].casefold()+'|'+record['platform']+'|'+native).encode()).hexdigest()
def _enqueue_source(record,payload=None):
    if os.getenv('GITHUB_ACTIONS')!='true' and os.getenv('JOB_ARCHIVE_LOCAL_TEST')!='true':return
    path=Path('archive_outbox_'+tier(record)+'.json');queue=load(path);k=key(record)
    queue.setdefault(k,dict(record=record,retrieval_payload=payload or {},observed_at=utc()))
    save(path,queue)
def _capture_detail(entry,detail):
    record=entry['record'];enqueue_source(record,{k:entry[k] for k in ['company','job'] if k in entry})
    if os.getenv('GITHUB_ACTIONS')!='true' and os.getenv('JOB_ARCHIVE_LOCAL_TEST')!='true':return
    p=Path('.runtime/archive-details-'+tier(record)+'.json');data=load(p);data[key(record)]=detail;save(p,data)
def enqueue_source(record,payload=None):
    try:_enqueue_source(record,payload)
    except Exception:print('Archive metadata checkpoint failed; original notification continues',file=sys.stderr)
def capture_detail(entry,detail):
    try:_capture_detail(entry,detail)
    except Exception:print('Archive detail handoff deferred; original Telegram delivery continues',file=sys.stderr)
def git(root,*args):
    r=subprocess.run(['git','-C',str(root),*args],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=50)
    if r.returncode:raise RuntimeError('Private archive Git operation failed: '+args[0])
    return r.stdout.strip()
def publish(root,selected_tier,verify_write=False):
    root=Path(root).resolve();path=Path('archive_outbox_'+selected_tier+'.json');queue=load(path)
    if not queue and not verify_write:print('Archive: no pending sources');return
    # Import only canonical private-archive code, never put private bodies in source git.
    sys.path.insert(0,str(root));from production import source_row,ingest,publish as briefing
    details=load('.runtime/archive-details-'+selected_tier+'.json')
    for attempt in range(4):
        git(root,'fetch','--no-tags','origin','main');git(root,'reset','--hard','origin/main')
        rows=[]
        for k,item in queue.items():
            row=source_row(item['record'],item['observed_at'],details.get(k),'Production watcher private archive handoff')
            row['recovery_target']=dict(record=item['record'],payload=item['retrieval_payload'],attempts=0)
            rows.append(row)
        ingest(root,rows)
        if rows:briefing(root,utc()[:10])
        if verify_write:
            receipt=root/'ingestion/receipts'/f'{selected_tier}-{os.getenv("GITHUB_RUN_ID","local")}.json'
            save(receipt,dict(collector='jobspy' if selected_tier=='jobspy' else 'search57',verified_at=utc(),run_id=os.getenv('GITHUB_RUN_ID'),scope='archive-only deploy-key write verification; no job injected'))
        git(root,'config','user.name','job-archive-bot');git(root,'config','user.email','actions@users.noreply.github.com')
        git(root,'add','records','descriptions','briefings','ingestion') if (root/'ingestion').exists() else git(root,'add','records','descriptions','briefings')
        diff=git(root,'diff','--cached','--name-only')
        if not diff:break
        git(root,'commit','-m','archive: ingest '+selected_tier+' source records')
        try:git(root,'push','origin','HEAD:main');break
        except RuntimeError:
            if attempt==3:raise
    # Drain only after a successful private write or verified existing records.
    current=load(path)
    for k,item in queue.items():
        if current.get(k)==item:current.pop(k)
    save(path,current)
    print(json.dumps(dict(archive_sources_confirmed=len(queue),pending=len(current),write_verification=verify_write)))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--archive',required=True);p.add_argument('--tier',choices=['fast','slow','jobspy'],required=True);p.add_argument('--verify-write',action='store_true');args=p.parse_args()
    try:publish(args.archive,args.tier,args.verify_write)
    except Exception as exc:
        print('Archive handoff deferred: '+type(exc).__name__+'; durable metadata remains queued',file=sys.stderr);sys.exit(1)

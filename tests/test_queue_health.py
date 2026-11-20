import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock
import queue_health as h

def entry(n=1):return dict(record=dict(company='Fixture',title='Intern',platform='workday',ats_id='R'+str(n),url='https://example.com/jobs/'+str(n)),alert_sent=True,want_description=True,attempts=0,acks={})

class QueueHealthTests(unittest.TestCase):
    def test_twelve_of_twenty_is_deferred_not_fatal(self):
        q={str(i):entry(i) for i in range(8)};before=copy.deepcopy(q)
        report=h.summary(q,20,12,now=1000)
        self.assertEqual(report['status'],'deferred');self.assertEqual(report['reason'],'count_limit')
        self.assertEqual(report['exit_code'],0);self.assertTrue(report['retry_scheduled']);self.assertEqual(q,before)
    def test_time_budget_before_any_work_is_deferred(self):
        r=h.summary({'one':entry()},1,0);self.assertEqual(r['status'],'deferred');self.assertEqual(r['processed'],0)
    def test_transient_error_is_explicit_and_retryable(self):
        e=entry();h.begin(e,1000);h.error(e,TimeoutError(),'recovery')
        r=h.summary({'one':e},1,1,now=1010);self.assertEqual(r['status'],'deferred');self.assertEqual(len(r['retryable_errors']),1)
    def test_credentials_fail_without_exposing_exception_text(self):
        e=entry();h.begin(e,1000)
        error=type('DeliveryError',(Exception,),{})('Telegram rejected message (401) SECRET')
        h.error(e,error,'compact_alert');r=h.summary({'one':e},1,1,now=1010)
        self.assertEqual(r['exit_code'],1);self.assertEqual(r['fatal_errors'][0]['category'],'credentials');self.assertNotIn('SECRET',json.dumps(r))
    def test_bad_record_and_state_corruption_are_fatal(self):
        for bad in [[],{'bad':{}},{'bad':{**entry(),'attempts':-1}}]:
            with self.assertRaises(h.WorkFailure):h.validate(bad)
        e=entry();h.begin(e,1000);h.error(e,ValueError('PRIVATE SOURCE'),'formatter')
        self.assertEqual(h.summary({'one':e},1,1,now=1010)['exit_code'],1)
    def test_stalled_retries_fail_with_checkpoint_retained(self):
        e=entry();h.track(e,1000);e['attempts_without_progress']=6
        q={'one':e};r=h.summary(q,1,1,now=5000)
        self.assertEqual(r['status'],'failed');self.assertEqual(r['stale_keys'],['one']);self.assertIn('one',q)
    def test_unattempted_queue_cannot_stay_silent_forever(self):
        e=entry();h.track(e,1000)
        self.assertEqual(h.summary({'one':e},1,0,now=23000)['status'],'failed')
    def test_recent_progress_clears_stalled_attempt_counter(self):
        e=entry();h.track(e,1000);e['attempts_without_progress']=7;h.progress(e,5000)
        self.assertEqual(h.summary({'one':e},1,1,now=5010)['status'],'deferred');self.assertEqual(e['attempts_without_progress'],0)
    def test_empty_queue_is_complete(self):
        r=h.summary({},12,12);self.assertEqual(r['status'],'complete');self.assertFalse(r['retry_scheduled'])
    def test_interruption_preserves_queue_and_resume_is_idempotent(self):
        if (Path(__file__).resolve().parents[1]/'jobspy_delivery.py').exists():
            import jobspy_delivery as worker
            def process(q,checkpoint):return worker.deliver(q,{}, {},lambda q,s:checkpoint(q),Mock(side_effect=AssertionError('No description Telegram')),Mock(side_effect=AssertionError('No old alert')),lambda *a:dict(text='Full fixture JD',status='available',source='fixture',note=''),sleep=lambda _:None,archive_fn=lambda e,d:staged.append(d['text']))
        else:
            import description_delivery as worker
            def process(q,checkpoint):return worker.deliver(q,{},checkpoint,Mock(side_effect=AssertionError('No old alert')),lambda *a:dict(text='Full fixture JD',status='available',note=''),archive_fn=lambda e,d:staged.append(d['text']))
        staged=[]
        # The staging contract confirms durable handoff; it is tested against
        # actual encryption and failed pushes separately in test_archive_bridge.
        class Staged(list):
            def append(self,value):super().append(value);return True
        staged=Staged();q={str(i):entry(i) for i in range(20)}
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'checkpoint.json'
            def interrupted(value):
                path.write_text(json.dumps(value),encoding='utf-8')
                if len(value)==19:raise KeyboardInterrupt('simulated interrupted runner')
            with self.assertRaises(KeyboardInterrupt):process(q,interrupted)
            saved=json.loads(path.read_text(encoding='utf-8'));self.assertEqual(len(saved),19);self.assertEqual(len(staged),1)
            done=process(saved,lambda value:path.write_text(json.dumps(value),encoding='utf-8'))
            self.assertEqual(done,12);self.assertEqual(len(saved),7);self.assertEqual(h.summary(saved,19,done)['status'],'deferred')
            process(saved,lambda value:None);self.assertFalse(saved);self.assertEqual(len(staged),20)
            process(saved,lambda value:None);self.assertEqual(len(staged),20)

if __name__=='__main__':unittest.main()

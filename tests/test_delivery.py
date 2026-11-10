import copy
import json
import time
import unittest
from unittest.mock import Mock,patch

import requests
import jobspy_delivery as delivery
from telegram_jobs import decode
from telegram_transport import send_plain,DeliveryError


def job(n=1):
    return dict(company='Fixture & Co',title='Firmware <Intern>',location='CA',site='linkedin',
                jobspy_id=f'li-{n}',job_url=f'https://www.linkedin.com/jobs/view/{n}',score=n,description='')


class DeliveryTests(unittest.TestCase):
    def queue(self,n=1,enabled=True):
        queue={}
        for i in range(1,n+1):delivery.enqueue(queue,job(i),enabled,{})
        return queue

    def test_failed_alert_never_enters_seen_then_recovers_off_search(self):
        q=self.queue();state={'_seen':['legacy']}
        with patch.object(delivery,'send_alerts') as unused:pass
        delivery.send_alerts(q,state,lambda *a:None,Mock(side_effect=DeliveryError('fail')),sleep=lambda _:None)
        self.assertEqual(state['_seen'],['legacy']);self.assertFalse(next(iter(q.values()))['alert_sent'])
        delivery.deliver(q,state,{},lambda *a:None,lambda *a:1,lambda *a:8,
                         lambda *a:dict(text='',status='unavailable',source='targeted',note='blocked'),sleep=lambda _:None)
        self.assertFalse(q);self.assertIn(job()['job_url'],state['_seen'])

    def test_multipart_partial_ack_resumes_without_repeating_successes(self):
        q=self.queue();state={'_seen':[]};sent=[];snapshots=[]
        detail=lambda *a:dict(text='C++ 🧪 UART\n'*1200,status='available',source='targeted:linkedin',note='fixture')
        def send(wire,deadline):
            sent.append(wire)
            if len(sent)==3:raise DeliveryError('fail')
            return len(sent)
        delivery.deliver(q,state,{},lambda q,s:snapshots.append(copy.deepcopy(q)),send,lambda *a:99,detail,sleep=lambda _:None)
        self.assertTrue(q);first=sent[:2]
        self.assertEqual(len(next(iter(q.values()))['acks']),2)
        second=[]
        delivery.deliver(q,state,{},lambda *a:None,lambda w,d:second.append(w) or 100,lambda *a:self.fail('Do not resend alert'),detail,sleep=lambda _:None)
        self.assertFalse(q);self.assertTrue(all(w not in first for w in second))

    def test_disabled_descriptions_still_recover_alerts_and_pause_bodies(self):
        q=self.queue();state={}
        detail=Mock(side_effect=AssertionError('No description network'))
        delivery.deliver(q,state,{},lambda *a:None,lambda *a:self.fail('No description send'),lambda *a:1,
                         detail,enabled=False,sleep=lambda _:None)
        detail.assert_not_called();self.assertTrue(next(iter(q.values()))['alert_sent'])
        self.assertFalse(delivery.active(next(iter(q.values())),False))

    def test_notification_order_stable_by_original_score(self):
        q=self.queue(3,enabled=False);order=[]
        delivery.send_alerts(q,{},lambda *a:None,lambda r,d:order.append(r['score']) or 1,sleep=lambda _:None)
        self.assertEqual(order,[3,2,1]);self.assertFalse(q)

    def test_failed_entry_does_not_starve_other_entries(self):
        q=self.queue(2);state={};first=next(iter(q))
        def retrieve(e,*args):
            if e['record']['jobspy_id']=='li-1':raise RuntimeError('failed')
            return dict(text='',status='unavailable',source='targeted',note='')
        delivery.deliver(q,state,{},lambda *a:None,lambda *a:1,lambda *a:1,retrieve,sleep=lambda _:None)
        self.assertEqual(list(q),[first])

    def test_new_revision_resets_ack_identity(self):
        q=self.queue();e=next(iter(q.values()));e.update(alert_sent=True,revision='old',acks={'old':1})
        audits=[]
        delivery.deliver(q,{}, {},lambda *a:None,lambda *a:2,lambda *a:1,
                         lambda *a:dict(text='New body',status='available',source='targeted',note=''),
                         sleep=lambda _:None,audit=audits.append)
        self.assertFalse(q);self.assertEqual(audits[0]['confirmed_wires'],1)

    def test_expired_cache_calls_targeted_for_only_selected_entry(self):
        q=self.queue();next(iter(q.values()))['alert_sent']=True;retrieve=Mock(return_value=dict(text='',status='unavailable',source='targeted',note=''))
        delivery.deliver(q,{}, {},lambda *a:None,lambda *a:1,lambda *a:1,retrieve,sleep=lambda _:None)
        retrieve.assert_called_once();self.assertEqual(retrieve.call_args.args[1],'')

    def test_reconcile_acknowledged_queue_after_crash(self):
        q=self.queue();e=next(iter(q.values()));e.update(alert_sent=True,alert_message_id=777)
        state={'_seen':['legacy']};delivery.reconcile(q,state)
        self.assertIn(e['record']['url'],state['_seen'])

    def test_bounded_timeout_returns_partial_listing(self):
        import subprocess
        with patch.object(delivery.subprocess,'run',side_effect=subprocess.TimeoutExpired('worker',1)):
            value=delivery.bounded_retrieve({},'Retained source text',1)
        self.assertEqual(value['status'],'partial');self.assertEqual(value['text'],'Retained source text')

    def test_telegram_rate_limit_5xx_ack_and_no_secret_error(self):
        response=lambda code,data:Mock(status_code=code,ok=code==200,json=lambda:data)
        post=Mock(side_effect=[response(429,{'error_code':429,'parameters':{'retry_after':2}}),
                               response(500,{}),response(200,{'ok':True,'result':{'message_id':42}})])
        self.assertEqual(send_plain('fixture','chat','a < b & c',post=post,sleep=lambda _:None),42)
        self.assertNotIn('parse_mode',post.call_args.kwargs['json'])
        self.assertEqual(post.call_count,3)
        with self.assertRaises(DeliveryError) as error:
            send_plain('SECRET','chat','body',post=Mock(side_effect=requests.Timeout('SECRET')),sleep=lambda _:None)
        self.assertNotIn('SECRET',str(error.exception))

    def test_not_ok_or_missing_message_id_is_never_success(self):
        for data in ({'ok':False,'error_code':400},{'ok':True,'result':{}}):
            with self.assertRaises(DeliveryError):
                send_plain('fixture','chat','body',post=Mock(return_value=Mock(status_code=200,ok=True,json=lambda:data)),sleep=lambda _:None)

    def test_html_alert_escaping_preserves_fields(self):
        q=self.queue();value=delivery.alert(next(iter(q.values()))['record'])
        self.assertIn('Fixture &amp; Co',value);self.assertIn('Firmware &lt;Intern&gt;',value)


if __name__=='__main__':unittest.main()

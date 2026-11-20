import json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import archive_bridge as bridge

class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.old=os.getcwd();os.chdir(self.temp.name)
        self.env=patch.dict(os.environ,{'JOB_ARCHIVE_LOCAL_TEST':'true','ARCHIVE_TIER':'fast','JOB_ARCHIVE_SSH_KEY':'fixture-archive-encryption-key'});self.env.start()
        self.record=dict(company='Fixture',title='Intern',location='USA',platform='workday',ats_id='JR1',url='https://example.com/JR1')
    def tearDown(self):self.env.stop();os.chdir(self.old);self.temp.cleanup()
    def test_identity_outbox_survives_failed_private_write(self):
        bridge.enqueue_source(self.record);before=Path('archive_outbox_fast.json').read_bytes()
        with patch.object(bridge,'git',side_effect=RuntimeError('private write unavailable')):
            with self.assertRaises((RuntimeError,ModuleNotFoundError)):bridge.publish(Path('missing-private-clone'),'fast')
        self.assertEqual(before,Path('archive_outbox_fast.json').read_bytes())
    def test_body_never_in_public_outbox(self):
        bridge.enqueue_source(self.record);self.assertTrue(bridge.capture_detail({'record':self.record},{'text':'PRIVATE BODY FIXTURE','status':'available'}))
        self.assertNotIn('PRIVATE BODY FIXTURE',Path('archive_outbox_fast.json').read_text())
        self.assertIn('PRIVATE BODY FIXTURE',Path('.runtime/archive-details-fast.json').read_text())
        Path('.runtime/archive-details-fast.json').unlink()  # simulate a fresh runner
        self.assertEqual(bridge.staged_detail({'record':self.record})['text'],'PRIVATE BODY FIXTURE')
    def test_large_description_survives_encrypted_stdin_handoff(self):
        body='Full source fixture '*12000
        self.assertTrue(bridge.capture_detail({'record':self.record},{'text':body,'status':'available'}))
        self.assertEqual(bridge.staged_detail({'record':self.record})['text'],body)
    def test_failed_push_preserves_exact_encrypted_description_then_success_drains(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        entry={'record':self.record};detail={'text':'Full source retained across failed push','status':'available'}
        self.assertTrue(bridge.capture_detail(entry,detail));before=Path('archive_outbox_fast.json').read_bytes()
        production=SimpleNamespace(source_row=Mock(return_value={'fixture':'row'}),ingest=Mock(),publish=Mock())
        def git(root,*args):
            if args[0]=='push':raise RuntimeError('private push failed')
            if args[0]=='diff':return 'records/job.json'
            return ''
        with patch.dict('sys.modules',{'production':production}),patch.object(bridge,'git',side_effect=git):
            with self.assertRaises(RuntimeError):bridge.publish(Path('private-clone'),'fast')
        self.assertEqual(before,Path('archive_outbox_fast.json').read_bytes())
        self.assertEqual(production.source_row.call_args.args[2]['text'],detail['text'])
        with patch.dict('sys.modules',{'production':production}),patch.object(bridge,'git',return_value=''):
            bridge.publish(Path('private-clone'),'fast')
        self.assertEqual(bridge.load('archive_outbox_fast.json'),{})
    def test_repeated_metadata_identity_is_idempotent(self):
        bridge.enqueue_source(self.record);bridge.enqueue_source(self.record);self.assertEqual(len(bridge.load('archive_outbox_fast.json')),1)
    def test_retryable_archive_failure_retains_ciphertext_and_is_deferred(self):
        bridge.capture_detail({'record':self.record},{'text':'Verified fixture body','status':'available'})
        code=bridge.report_failure(bridge.health.WorkFailure('archive_git_retry',True),'fast')
        self.assertEqual(code,0)
        self.assertEqual(bridge.staged_detail({'record':self.record})['text'],'Verified fixture body')
        self.assertEqual(next(iter(bridge.load('archive_outbox_fast.json').values()))['work_errors'][0]['category'],'archive_git_retry')
    def test_invalid_archive_credentials_fail_and_keep_pending(self):
        bridge.enqueue_source(self.record)
        self.assertEqual(bridge.report_failure(bridge.health.WorkFailure('archive_credentials'),'fast'),1)
        self.assertEqual(len(bridge.load('archive_outbox_fast.json')),1)
    def test_missing_or_corrupt_checkpoint_is_not_reinitialized(self):
        with self.assertRaises(bridge.health.WorkFailure):bridge.report_failure(TimeoutError(),'fast')
        self.assertFalse(Path('archive_outbox_fast.json').exists())
        for value in ['broken json','[]','{"bad":{}}']:
            Path('archive_outbox_fast.json').write_text(value)
            with self.assertRaises(Exception):bridge.report_failure(TimeoutError(),'fast')
            self.assertEqual(Path('archive_outbox_fast.json').read_text(),value)
    def test_local_capture_failure_cannot_break_original_alert(self):
        with patch.object(bridge,'save',side_effect=OSError('disk failure')):bridge.enqueue_source(self.record);bridge.capture_detail({'record':self.record},{'text':'Fixture'})

if __name__=='__main__':unittest.main()

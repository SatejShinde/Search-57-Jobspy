import json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import archive_bridge as bridge

class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.old=os.getcwd();os.chdir(self.temp.name)
        self.env=patch.dict(os.environ,{'JOB_ARCHIVE_LOCAL_TEST':'true','ARCHIVE_TIER':'fast'});self.env.start()
        self.record=dict(company='Fixture',title='Intern',location='USA',platform='workday',ats_id='JR1',url='https://example.com/JR1')
    def tearDown(self):self.env.stop();os.chdir(self.old);self.temp.cleanup()
    def test_identity_outbox_survives_failed_private_write(self):
        bridge.enqueue_source(self.record);before=Path('archive_outbox_fast.json').read_bytes()
        with patch.object(bridge,'git',side_effect=RuntimeError('private write unavailable')):
            with self.assertRaises((RuntimeError,ModuleNotFoundError)):bridge.publish(Path('missing-private-clone'),'fast')
        self.assertEqual(before,Path('archive_outbox_fast.json').read_bytes())
    def test_body_never_in_public_outbox(self):
        bridge.enqueue_source(self.record);bridge.capture_detail({'record':self.record},{'text':'PRIVATE BODY FIXTURE','status':'available'})
        self.assertNotIn('PRIVATE BODY FIXTURE',Path('archive_outbox_fast.json').read_text())
        self.assertIn('PRIVATE BODY FIXTURE',Path('.runtime/archive-details-fast.json').read_text())
    def test_repeated_metadata_identity_is_idempotent(self):
        bridge.enqueue_source(self.record);bridge.enqueue_source(self.record);self.assertEqual(len(bridge.load('archive_outbox_fast.json')),1)
    def test_local_capture_failure_cannot_break_original_alert(self):
        with patch.object(bridge,'save',side_effect=OSError('disk failure')):bridge.enqueue_source(self.record);bridge.capture_detail({'record':self.record},{'text':'Fixture'})

if __name__=='__main__':unittest.main()

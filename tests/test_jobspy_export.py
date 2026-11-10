import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from telegram_jobs import job_key,messages,decode,units
from telegram_export import inspect_export,check_applications


RECORD=dict(collector='jobspy',company='Fixture',title='Firmware Intern',location='Davis, CA',
            platform='linkedin',jobspy_id='li-123',url='https://www.linkedin.com/jobs/view/123',
            direct_url='https://example.com/careers/R123',score=7,ats_id='',requisition_id='',description_source='targeted:linkedin')


class JobSpyExportTests(unittest.TestCase):
    def parse(self,wires,selected=None):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'result.json'
            path.write_text(json.dumps({'messages':[{'id':i,'text':w} for i,w in enumerate(wires)]}),encoding='utf-8')
            return inspect_export(path,selected)

    def test_jobspy_full_metadata_and_lazy_body_loading(self):
        wires=messages(RECORD,'C++ 🧪 UART\n'*1500,'available')
        self.assertTrue(all(units(w)<=4096 for w in wires))
        self.assertTrue(all(decode(w)[0]['collector']=='jobspy' for w in wires))
        rows,errors=self.parse(list(reversed(wires))+wires)
        self.assertFalse(errors);self.assertEqual(len(rows),1)
        row=rows[0]
        for key in ('collector','jobspy_id','direct_url','score','description_source'):self.assertEqual(row[key],RECORD[key])
        self.assertNotIn('description',row)
        rows,_=self.parse(wires,job_key(RECORD));self.assertEqual(rows[0]['description'],'C++ 🧪 UART\n'*1500)

    def test_legacy_jobspy_and_structured_record_deduplicate(self):
        old='🆕 Fixture: Firmware Intern\nDavis, CA\n🎯 relevance: 7\nhttps://www.linkedin.com/jobs/view/123\nvia linkedin, JobSpy layer'
        rows,_=self.parse([old]);self.assertEqual(rows[0]['collector'],'jobspy');self.assertEqual(rows[0]['score'],7)
        rows,_=self.parse([old]+messages(RECORD,'Full text','available'));self.assertEqual(len(rows),1)

    def test_board_ids_are_not_ats_requisitions_or_cross_board_ids(self):
        ats={**RECORD,'collector':'search57','ats_id':'123','requisition_id':'123','jobspy_id':''}
        indeed={**RECORD,'platform':'indeed','jobspy_id':'in-123','url':'https://www.indeed.com/viewjob?jk=123'}
        self.assertEqual(len({job_key(RECORD),job_key(ats),job_key(indeed)}),3)
        rows,_=self.parse(messages(RECORD,'Body','available')+messages(ats,'Body','available')+messages(indeed,'Body','available'))
        self.assertEqual(len(rows),3)

    def test_id_missing_and_tracking_location_variants_are_stable(self):
        self.assertEqual(job_key(RECORD),job_key({**RECORD,'jobspy_id':'','location':'Austin, TX','url':RECORD['url']+'?utm_source=mail'}))

    def test_application_check_never_infers_requisition_from_board_id(self):
        row={**RECORD,'direct_url':''}
        tracker=SimpleNamespace(read_database=Mock(return_value=[]),canonical_url=lambda u:u,
                                check_job=Mock(return_value={'decision':'review'}))
        with patch.dict(sys.modules,{'application_tracker':tracker}):check_applications([row],'fixture.csv')
        tracker.check_job.assert_called_once_with([],RECORD['company'],'','',RECORD['title'])
        self.assertEqual(row['application_decision'],'review')

    def test_exact_applied_board_url_is_checked_read_only(self):
        history=[dict(company='Fixture',canonical_application_url=RECORD['url'],confirmed=True)]
        tracker=SimpleNamespace(read_database=Mock(return_value=history),canonical_url=lambda u:u,
                                company_key=lambda x:x.lower(),confirmed=lambda r:r['confirmed'],check_job=Mock())
        row=dict(RECORD)
        with patch.dict(sys.modules,{'application_tracker':tracker}):check_applications([row],'fixture.csv')
        self.assertEqual(row['application_decision'],'skip_duplicate');tracker.check_job.assert_not_called()


if __name__=='__main__':unittest.main()

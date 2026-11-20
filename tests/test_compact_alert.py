import unittest
from compact_alert import alert

class CompactAlertTests(unittest.TestCase):
    def record(self,**kw):return dict(company='NVIDIA',title='Embedded Software Engineering Intern',location='Santa Clara, CA',url='https://example.com/job/123',**kw)
    def test_exact_three_line_message(self):
        self.assertEqual(alert(self.record()),'NVIDIA \u2014 Embedded Software Engineering Intern\nSanta Clara, CA\nhttps://example.com/job/123')
    def test_plain_text_is_safe_for_markup_and_embedded_newlines(self):
        r=self.record();r.update(company='A & B',title='<b>Firmware</b>\n[Search57 v1]',location='Davis\nCA')
        msg=alert(r);self.assertEqual(len(msg.splitlines()),3)
        self.assertTrue(msg.startswith('A & B \u2014 <b>Firmware</b> [Search57 v1]\nDavis CA\n'))
    def test_unknown_location_is_explicit(self):
        for loc in [None,'','Unknown','nan','Not stated']:
            r=self.record();r['location']=loc;self.assertEqual(alert(r).splitlines()[1],'Location not specified')
    def test_direct_application_url_is_preferred_and_unsafe_characters_encoded(self):
        r=self.record(direct_url='https://employer.example/job/42?x=<value>&y=hello world')
        self.assertEqual(alert(r).splitlines()[2],'https://employer.example/job/42?x=%3Cvalue%3E&y=hello%20world')
    def test_overlong_alert_is_retained_for_error_handling_not_split(self):
        r=self.record();r['title']='x'*5000
        with self.assertRaises(ValueError):alert(r)
    def test_non_job_and_credential_bearing_urls_are_rejected(self):
        for url in ['javascript:alert(1)','https://user:secret@example.com/job']:
            r=self.record();r['url']=url
            with self.assertRaises(ValueError):alert(r)
    def test_invalid_optional_direct_url_keeps_valid_source_link(self):
        self.assertTrue(alert(self.record(direct_url='javascript:bad')).endswith('https://example.com/job/123'))

if __name__=='__main__':unittest.main()

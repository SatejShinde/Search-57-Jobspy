import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

import jobspy_details as details


def entry(platform="linkedin"):
    urls={"linkedin":"https://www.linkedin.com/jobs/view/123",
          "indeed":"https://www.indeed.com/viewjob?jk=abc",
          "zip_recruiter":"https://www.ziprecruiter.com/jobs/fixture/123"}
    return {"record":dict(platform=platform,url=urls[platform],jobspy_id={"linkedin":"li-123","indeed":"in-abc","zip_recruiter":"zr-XYZ"}[platform])}


class DetailTests(unittest.TestCase):
    def test_indeed_listing_body_reused_without_network(self):
        with patch.object(details.requests,'get',side_effect=AssertionError('No HTTP')):
            value=details.retrieve(entry('indeed'),'Responsibilities\nC++ firmware 🧪')
        self.assertEqual(value['text'],'Responsibilities\nC++ firmware 🧪')
        self.assertEqual(value['status'],'available')
        self.assertEqual(value['source'],'listing:indeed')

    def test_initialized_linkedin_internal_method_fetches_one_page(self):
        scraper=details.build_scraper('linkedin')
        scraper.session.get=Mock(return_value=SimpleNamespace(status_code=200,url=entry()['record']['url'],
                               text='<html><div class="show-more-less-html__markup"><p>Firmware internship.</p><ul><li>C++</li></ul></div></html>'))
        scraper.scrape=Mock(side_effect=AssertionError('No searches'))
        with patch.object(details,'build_scraper',return_value=scraper):
            value=details.retrieve(entry())
        self.assertEqual(value['status'],'available')
        self.assertIn('C++',value['text'])
        scraper.session.get.assert_called_once_with('https://www.linkedin.com/jobs/view/123')
        scraper.scrape.assert_not_called()

    def test_zip_internal_details_initialized_and_one_request(self):
        scraper=details.build_scraper('zip_recruiter')
        scraper.session.get=Mock(return_value=SimpleNamespace(status_code=200,text='mock RSC response'))
        # Real _fetch_details uses these model/parser interfaces. Mock page data
        # only; no broad scrape/search method is called.
        from jobspy.ziprecruiter import JobDetails
        body=dict(htmlFullDescription='<p>Hardware engineering internship. 🧪</p>',canonicalIndustries=[],
                  canonicalWebsite=None,rating=None,companySizeDisplay=None,hqLocation=None,about=None,description=None)
        fake=SimpleNamespace(**body)
        with patch.object(details,'build_scraper',return_value=scraper),\
             patch('jobspy.ziprecruiter.page_values',return_value={'jobDetails':body}),\
             patch.object(JobDetails,'model_validate',return_value=fake):
            value=details.retrieve(entry('zip_recruiter'))
        self.assertEqual(value['status'],'available')
        self.assertIn('Hardware engineering internship',value['text'])
        scraper.session.get.assert_called_once_with(entry('zip_recruiter')['record']['url'],allow_redirects=True)

    def test_denied_linkedin_no_bypass_and_keeps_excerpt(self):
        scraper=details.build_scraper('linkedin')
        scraper.session.get=Mock(return_value=SimpleNamespace(status_code=429))
        with patch.object(details,'build_scraper',return_value=scraper):
            value=details.retrieve(entry(),'Original excerpt...')
        self.assertEqual(value['status'],'partial')
        self.assertEqual(value['text'],'Original excerpt...')
        self.assertEqual(scraper.session.get.call_count,1)

    def test_indeed_public_detail_dom(self):
        response=SimpleNamespace(status_code=200,text='<div id="jobDescriptionText"><p>Full responsibilities.</p><p>Qualifications: C++</p></div>')
        with patch.object(details.requests,'get',return_value=response) as get:
            value=details.retrieve(entry('indeed'))
        self.assertEqual(value['status'],'available');self.assertIn('Qualifications: C++',value['text'])
        self.assertEqual(get.call_count,1)

    def test_indeed_blocked_no_second_request(self):
        with patch.object(details.requests,'get',return_value=SimpleNamespace(status_code=403)) as get:
            value=details.retrieve(entry('indeed'))
        self.assertEqual(value['status'],'unavailable');self.assertIn('403',value['note'])
        self.assertEqual(get.call_count,1)

    def test_empty_teaser_and_potential_upstream_cap_are_explicit(self):
        for body,status in [('', 'unavailable'),('A short teaser...', 'partial'),('A full source paragraph.', 'available')]:
            value=details.classification(body,'listing:indeed',full=True)
            self.assertEqual(value['status'],status);self.assertEqual(value['text'],body)

    def test_invalid_url_never_requested(self):
        for url in ('http://127.0.0.1/jobs','https://example.com/job','https://www.linkedin.com.evil.invalid/jobs/123'):
            e=entry();e['record']['url']=url
            with self.assertRaises(ValueError),patch.object(details.requests,'get',side_effect=AssertionError('No HTTP')):
                details.retrieve(e)


if __name__=='__main__':unittest.main()

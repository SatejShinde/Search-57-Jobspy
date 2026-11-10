import ast
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

import pandas as pd
import jobspy_watch as watch
import jobspy_delivery as delivery


def selection_ast(source):
    tree=ast.parse(source)
    constants={}
    names={'TRACKED_COMPANY_NAMES','SEARCH_TERM','UNRESTRICTED_SEARCH_TERMS','SKILL_KEYWORDS','WEST_STATES',
           'WEST_OR_REMOTE','US_STATES','_US_STATE_PATTERN','_US_COUNTRY_PATTERN','is_us_location',
           'INTERN_TITLE','ROLE_KEYWORDS','relevance_score'}
    for node in tree.body:
        name=node.name if isinstance(node,ast.FunctionDef) else node.targets[0].id if isinstance(node,ast.Assign) and isinstance(node.targets[0],ast.Name) else ''
        if name in names:constants[name]=ast.dump(node,include_attributes=False)
    main=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='main')
    class RemoveRetained(ast.NodeTransformer):
        def visit_Dict(self,node):
            self.generic_visit(node)
            pairs=[(k,v) for k,v in zip(node.keys,node.values) if k is not None]
            node.keys=[k for k,v in pairs];node.values=[v for k,v in pairs];return node
    loops=[]
    for n in main.body:
        if isinstance(n,ast.If) and isinstance(n.test,ast.Compare) and isinstance(n.test.left,ast.Name) and n.test.left.id=='df':
            loops.append(n)
        if isinstance(n,ast.For) and isinstance(n.iter,ast.Call) and isinstance(n.iter.func,ast.Attribute) and isinstance(n.iter.func.value,ast.Name) and n.iter.func.value.id=='UNRESTRICTED_SEARCH_TERMS':
            loops.append(n)
    constants['selection_loops']=[ast.dump(RemoveRetained().visit(copy.deepcopy(n)),include_attributes=False) for n in loops]
    return constants


def row(**kw):
    return dict(id='li-123',site='linkedin',title='Firmware Intern',company='Fixture',
                location='Davis, CA, USA',is_remote=False,job_url='https://www.linkedin.com/jobs/view/123',
                job_url_direct='',description='Python C++ firmware',**kw) if not kw else {**row(),**kw}


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.previous=os.getcwd();os.chdir(self.temp.name);self.addCleanup(os.chdir,self.previous)

    def test_selection_ast_matches_original_production(self):
        root=Path(watch.__file__).parent
        fixture=json.loads((root/'tests/fixtures/selection.json').read_text())
        self.assertEqual(selection_ast(Path(watch.__file__).read_text(encoding='utf-8')),fixture)

    def collect(self,frames,enabled=True):
        scrape=Mock(side_effect=[pd.DataFrame(r) for r in frames])
        with patch.object(watch,'scrape_jobs',scrape),patch.object(sys,'argv',['watch','--collect-only']),\
             patch.dict(os.environ,{'JOBSPY_DESCRIPTIONS_ENABLED':'true' if enabled else 'false'}):
            watch.main()
        self.assertEqual(scrape.call_count,3)
        for i,call in enumerate(scrape.call_args_list):
            self.assertEqual(call.kwargs['site_name'],['indeed','linkedin','zip_recruiter'])
            self.assertEqual(call.kwargs['search_term'],[watch.SEARCH_TERM,*watch.UNRESTRICTED_SEARCH_TERMS.values()][i])
            self.assertEqual({k:call.kwargs[k] for k in ('location','results_wanted','hours_old','country_indeed','fetch_description')},
                             dict(location='United States',results_wanted=100,hours_old=72,country_indeed='USA',fetch_description=False))
        return json.loads(Path('pending_jobspy.json').read_text())

    def test_google_meta_unrestricted_role_and_geography(self):
        queue=self.collect([
            [row(company='NVIDIA'),row(title='Marketing Intern'),row(title='Firmware Director'),
             row(location='Boston, MA, USA'),row(job_url='https://www.linkedin.com/jobs/view/4',location='Remote',is_remote=True)],
            [row(company='Google',title='Product Manager Intern',location='New York, NY, USA',job_url='https://www.linkedin.com/jobs/view/5',id='li-5'),
             row(company='Google',title='Data Science Intern',location='Austin, TX, USA',job_url='https://www.linkedin.com/jobs/view/6',id='li-6'),
             row(company='Other',title='Intern')],
            [row(company='Meta',title='Finance Intern',location='Boston, MA, USA',job_url='https://www.linkedin.com/jobs/view/7',id='li-7')]
        ])
        self.assertEqual({e['record']['title'] for e in queue.values()},
                         {'Firmware Intern','Product Manager Intern','Data Science Intern','Finance Intern'})
        self.assertEqual(len(queue),4)

    def test_score_order_and_listing_descriptions_are_transient(self):
        jobs=[row(id='in-a',site='indeed',job_url='https://www.indeed.com/viewjob?jk=a',description='Python'),
              row(id='li-2',job_url='https://www.linkedin.com/jobs/view/2',description='Python C++ RTOS UART'),
              row(id='zr-c',site='zip_recruiter',job_url='https://www.ziprecruiter.com/jobs/c',description='Python C++ RTOS UART')]
        queue=self.collect([jobs,[],[]])
        entries=list(queue.values());self.assertEqual([e['record']['jobspy_id'] for e in entries],['li-2','zr-c','in-a'])
        self.assertNotIn('Python C++',Path('pending_jobspy.json').read_text())
        self.assertIn('Python C++',Path('.runtime/jobspy-listings.json').read_text())
        self.assertEqual(json.loads(Path('state_jobspy.json').read_text()).get('_seen',[]),[])

    def test_alerts_only_never_searches_and_only_confirmed_urls_are_seen(self):
        queue={};cache={}
        delivery.enqueue(queue,{**row(), 'jobspy_id':'li-123','score':3},True,cache)
        delivery.checkpoint(queue,{'_seen':['https://example.com/historical']})
        with patch.object(watch,'scrape_jobs',side_effect=AssertionError('No search')),\
             patch.object(watch,'send_alert',return_value=42),patch.object(delivery.time,'sleep'),\
             patch.object(sys,'argv',['watch','--alerts-only']),\
             patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':'fixture','TELEGRAM_CHAT_ID':'fixture'}):
            watch.main()
        state=json.loads(Path('state_jobspy.json').read_text())
        self.assertIn(row()['job_url'],state['_seen']);self.assertIn('https://example.com/historical',state['_seen'])

    def test_failed_alert_not_seen_and_pending_survives(self):
        queue={};delivery.enqueue(queue,{**row(),'jobspy_id':'li-123','score':3},True,{})
        delivery.checkpoint(queue,{'_seen':[]})
        with patch.object(watch,'send_alert',side_effect=RuntimeError('failed')),\
             patch.object(sys,'argv',['watch','--alerts-only']),\
             patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':'fixture','TELEGRAM_CHAT_ID':'fixture'}): watch.main()
        self.assertEqual(json.loads(Path('state_jobspy.json').read_text())['_seen'],[])
        self.assertEqual(len(json.loads(Path('pending_jobspy.json').read_text())),1)

    def test_legacy_seen_entries_skip_without_backfill(self):
        Path('state_jobspy.json').write_text(json.dumps({'_seen':[row()['job_url']],'custom':'preserve'}))
        queue=self.collect([[row()],[],[]])
        self.assertFalse(queue)
        self.assertEqual(json.loads(Path('state_jobspy.json').read_text())['custom'],'preserve')


if __name__=='__main__':unittest.main()

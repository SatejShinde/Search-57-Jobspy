import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from persist_state import publish,authorize_action


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.previous=os.getcwd();os.chdir(self.temp.name);self.addCleanup(os.chdir,self.previous)
        Path('state_jobspy.json').write_text('{"_seen":["legacy"]}')
        Path('pending_jobspy.json').write_text('{}')

    def runner(self,pushes=(0,),conflict=False,staged=''):
        calls=[];codes=list(pushes)
        def run(*args):
            calls.append(args);code=0;out=''
            if args==('diff','--cached','--name-only'):out=staged
            elif args==('diff','--cached','--quiet'):code=1
            elif args==('rebase','origin/master') and conflict:code=1
            elif args[:2]==('push','origin'):code=codes.pop(0)
            return subprocess.CompletedProcess(args,code,out,'')
        return run,calls

    def test_disjoint_push_race_retries_without_force(self):
        run,calls=self.runner(pushes=(1,0));publish(run=run,sleep=lambda _:None)
        self.assertEqual(calls.count(('push','origin','HEAD:master')),2)
        self.assertIn(('add','--','state_jobspy.json','pending_jobspy.json'),calls)
        self.assertFalse(any('--force' in arg for call in calls for arg in call))

    def test_conflicting_pending_writers_abort(self):
        run,calls=self.runner(conflict=True)
        with self.assertRaises(RuntimeError):publish(run=run)
        self.assertIn(('rebase','--abort'),calls);self.assertFalse(any(c[0]=='push' for c in calls))

    def test_three_failed_pushes_are_visible(self):
        run,calls=self.runner(pushes=(1,1,1))
        with self.assertRaises(RuntimeError):publish(run=run,sleep=lambda _:None)
        self.assertEqual(calls.count(('push','origin','HEAD:master')),3)

    def test_unrelated_staged_files_refused(self):
        run,calls=self.runner(staged='private.txt\n')
        with self.assertRaises(RuntimeError):publish(run=run)
        self.assertFalse(any(c[0]=='commit' for c in calls))

    def test_cli_is_executable_but_refuses_unapproved_local_push(self):
        import persist_state
        env={k:v for k,v in os.environ.items() if k not in ('GITHUB_ACTIONS','GITHUB_REF')}
        result=subprocess.run([sys.executable,persist_state.__file__],env=env,capture_output=True,text=True,timeout=15)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('master-branch GitHub Actions',result.stderr)
        with self.assertRaises(RuntimeError):authorize_action({'GITHUB_ACTIONS':'true','GITHUB_REF':'refs/heads/main'})
        authorize_action({'GITHUB_ACTIONS':'true','GITHUB_REF':'refs/heads/master'})

    def test_real_git_newer_state_and_pending_survive_stale_writer(self):
        def git(*args,cwd=None):
            return subprocess.run(['git',*args],cwd=cwd,check=True,capture_output=True,text=True,encoding='utf-8',timeout=20)
        base=Path.cwd();remote=base/'remote.git';seed=base/'seed';a=base/'a';b=base/'b'
        git('init','--bare',str(remote));git('init','-b','master',str(seed))
        git('config','user.name','test',cwd=seed);git('config','user.email','test@example.invalid',cwd=seed)
        (seed/'state_jobspy.json').write_text('{"_seen":["legacy"]}')
        (seed/'pending_jobspy.json').write_text('{}')
        git('add','.',cwd=seed);git('commit','-m','baseline',cwd=seed)
        git('remote','add','origin',str(remote),cwd=seed);git('push','-u','origin','master',cwd=seed)
        git('clone','-b','master',str(remote),str(a));git('clone','-b','master',str(remote),str(b))
        os.chdir(a);Path('state_jobspy.json').write_text('{"_seen":["legacy","new"]}');publish()
        os.chdir(b);Path('pending_jobspy.json').write_text('{"new":{"alert_sent":false}}');publish()
        self.assertEqual(json.loads(git('--git-dir',str(remote),'show','master:state_jobspy.json').stdout)['_seen'],['legacy','new'])
        self.assertIn('new',json.loads(git('--git-dir',str(remote),'show','master:pending_jobspy.json').stdout))
        os.chdir(base)


if __name__=='__main__':unittest.main()

"""Execute the workflow's lock and playbook shell steps with a harmless fixture."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import yaml
ROOT=Path(__file__).resolve().parents[2]

class DeployLockTests(unittest.TestCase):
    def test_manual_and_automatic_deploy_have_no_unlocked_path(self):
        job=yaml.safe_load((ROOT/'.github/workflows/deploy.yml').read_text())['jobs']['provision']
        self.assertIn('/var/lib/homelab-maintenance:/maintenance',job['container']['volumes'])
        steps=job['steps']
        acquire=next(x for x in steps if x.get('name')=='Acquire external maintenance ownership')
        play=next(x for x in steps if x.get('name')=='Run K3s Playbook')
        release=next(x for x in steps if x.get('name')=='Release ownership after successful configuration')
        self.assertLess(steps.index(acquire),steps.index(play));self.assertLess(steps.index(play),steps.index(release))
        self.assertNotIn('if',acquire);self.assertNotIn('if',release)
        with tempfile.TemporaryDirectory() as d:
            d=Path(d);lock=d/'lock';lock.mkdir();(lock/'runner-instance').write_text('fixture')
            executable=d/'ansible-playbook';executable.write_text('#!/bin/sh\nexit "${FIXTURE_EXIT:-0}"\n');executable.chmod(0o700)
            env=dict(os.environ,HOMELAB_RUNNER_INSTANCE='fixture',MAINTENANCE_OWNER='ansible/test/1',
                     GITHUB_ENV=str(d/'env'),PATH=str(d)+':'+os.environ['PATH'])
            def run(step):
                script=step['run'].replace('python3 maintenance-helper/scripts/maintenance_lock.py',
                    sys.executable+' '+str(ROOT/'scripts/maintenance_lock.py'))
                script=script.replace('acquire --operation','acquire --root '+str(lock)+' --operation')
                script=script.replace('maintenance_lock.py verify','maintenance_lock.py verify --root '+str(lock))
                script=script.replace('maintenance_lock.py release','maintenance_lock.py release --root '+str(lock))
                return subprocess.run(['bash','-e','-c',script],env=env,capture_output=True)
            self.assertNotEqual(run(play).returncode,0)
            self.assertEqual(run(acquire).returncode,0)
            env['MAINTENANCE_NONCE']=(d/'env').read_text().strip().split('=',1)[1]
            env['FIXTURE_EXIT']='1';self.assertNotEqual(run(play).returncode,0)
            self.assertTrue((lock/'operation.json').exists())
            env['FIXTURE_EXIT']='0';self.assertEqual(run(play).returncode,0)
            self.assertEqual(run(release).returncode,0)
            self.assertFalse((lock/'operation.json').exists())

if __name__=='__main__':unittest.main()

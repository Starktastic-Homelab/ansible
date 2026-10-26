"""Workflows must not paste secrets into scripts, leave controller credentials on the runner, or publish them."""
from pathlib import Path
import re
import unittest
import yaml
ROOT=Path(__file__).resolve().parents[2]
CREDENTIALS=('.vault_pass','private_key.pem')
SECRET=re.compile(r'\$\{\{[^}]*\bsecrets\b',re.I)
WRITE=re.compile(r'>\s*(\.vault_pass|private_key\.pem)\b')

def jobs():
    for path in sorted(p for p in (ROOT/'.github/workflows').iterdir() if p.suffix in ('.yml','.yaml')):
        doc=yaml.safe_load(path.read_text())
        for name,job in doc['jobs'].items():
            labels=job.get('runs-on',[]);labels=[labels] if isinstance(labels,str) else labels
            # Steps inherit workflow and job env, so a secret there reaches the step too.
            steps=[{**s,'env':{**doc.get('env',{}),**job.get('env',{}),**s.get('env',{})}} for s in job.get('steps',[])]
            yield path.name,name,'self-hosted' in labels,steps

class WorkflowCredentialTests(unittest.TestCase):
    def test_no_secret_is_interpolated_into_a_script(self):
        # An interpolated secret is written into the step script on the runner's disk.
        for workflow,job,_,steps in jobs():
            for step in steps:
                with self.subTest(workflow=workflow,job=job,step=step.get('name')):
                    self.assertNotRegex(step.get('run',''),SECRET)

    def test_self_hosted_credential_files_are_private_and_removed_last(self):
        found=0
        for workflow,job,self_hosted,steps in jobs():
            if not self_hosted:continue
            written=set()
            for step in steps:
                run=step.get('run','');files={m.group(1) for m in WRITE.finditer(run)}
                if files and SECRET.search(str(step['env'])):
                    with self.subTest(workflow=workflow,job=job,step=step.get('name')):
                        self.assertIn('umask 077',run[:WRITE.search(run).start()])
                written|=files;found+=bool(files)
            if written:
                with self.subTest(workflow=workflow,job=job):
                    last=steps[-1]
                    self.assertEqual(last.get('if'),'always()')
                    self.assertIn('rm -f',last.get('run',''))
                    for f in written:self.assertIn(f,last['run'])
        self.assertGreaterEqual(found,5)

    def test_deploy_removes_the_playbook_kubeconfig(self):
        # roles/k3s_init/tasks/kubeconfig.yml writes it; no step regex can see that.
        steps=yaml.safe_load((ROOT/'.github/workflows/deploy.yml').read_text())['jobs']['provision']['steps']
        self.assertEqual(steps[-1].get('if'),'always()')
        self.assertIn('~/.kube/config',steps[-1]['run'])

    def test_no_artifact_publishes_credentials(self):
        # The deploy handoff artifact is ciphertext (handoff.enc); plaintext paths must never be uploaded.
        for workflow,job,_,steps in jobs():
            for step in steps:
                if str(step.get('uses','')).startswith('actions/upload-artifact'):
                    with self.subTest(workflow=workflow,job=job,artifact=step['with'].get('name')):
                        self.assertNotRegex(str(step['with'].get('path','')),r'kube|vault_pass|private_key')

if __name__=='__main__':unittest.main()

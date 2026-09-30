import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import maintenance_executor as executor
import maintenance_lock as lock
import maintenance_requests as requests
from test_maintenance_requests import REQUEST, RID


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name); self.root = self.base/'state'; self.root.mkdir()
        (self.root/'requests').mkdir(mode=0o700)
        (self.root/'runner-instance').write_text('test-runner')
        env = patch.dict(os.environ, HOMELAB_RUNNER_INSTANCE='test-runner'); env.start(); self.addCleanup(env.stop)
        self.nonce = lock.acquire(self.root, 'read-only-test', 'owner')
        self.original = (self.root/'operation.json').read_bytes()
        self.release = self.base/'release'; (self.release/'apps/scripts/storage').mkdir(parents=True)
        self.adapter = self.release/'apps/scripts/storage/supervised_readonly.py'
        self.adapter.write_text('print("local fixture")\n')
        (self.release/'ansible/scripts').mkdir(parents=True)
        for name in ('maintenance_executor.py', 'maintenance_requests.py', 'maintenance_lock.py'):
            shutil.copyfile(Path(__file__).resolve().parents[1]/name, self.release/'ansible/scripts'/name)
        self.manifest = self.release/'manifest.json'
        self.refresh()

    def refresh(self):
        self.data = executor.build_manifest(self.release, self.root, 'test-runner', 'a'*40)
        if self.manifest.exists(): self.manifest.chmod(0o600)
        self.manifest.write_bytes(requests.encoded(self.data)); self.manifest.chmod(0o444)
        self.request = dict(REQUEST, expected_stage='acquired', runtime_id=requests.digest(self.data))

    def publish(self, **credentials):
        requests.publish_request(self.root, self.request, dict(owner='owner', nonce=self.nonce, **credentials))

    def test_success_and_no_replay(self):
        self.publish()
        self.assertEqual(executor.run(self.manifest, RID), 0)
        self.assertEqual(requests.inspect_request(self.root, RID)['phase'], 'succeeded')
        self.assertNotEqual(executor.run(self.manifest, RID), 0)
        self.assertEqual((self.root/'operation.json').read_bytes(), self.original)

    def test_wrong_owner_stage_and_runtime_never_start(self):
        for field, value in [('expected_stage', 'held'), ('runtime_id', 'c'*64), ('apps_commit', 'b'*40)]:
            with self.subTest(field=field):
                self.request[field] = value; self.publish()
                self.assertNotEqual(executor.run(self.manifest, RID), 0)
                self.assertFalse((self.root/'requests'/RID/'started.json').exists())
                shutil.rmtree(self.root/'requests'/RID); self.refresh()
        requests.publish_request(self.root, self.request, dict(owner='wrong', nonce=self.nonce))
        self.assertNotEqual(executor.run(self.manifest, RID), 0)

    def test_changed_source_and_interpreter_refused(self):
        self.publish(); self.adapter.write_text('raise RuntimeError("must not execute")')
        self.assertNotEqual(executor.run(self.manifest, RID), 0)
        self.assertFalse((self.root/'requests'/RID/'started.json').exists())
        self.refresh()
        with patch('maintenance_executor.file_hash', return_value='0'*64):
            self.assertNotEqual(executor.run(self.manifest, RID), 0)

    def test_started_evidence_prevents_retry(self):
        self.publish(); requests.write_receipt(self.root, self.request, 'started', 'test-runner', exit_code=None)
        with patch('maintenance_executor.subprocess.Popen', side_effect=AssertionError('replayed')):
            self.assertNotEqual(executor.run(self.manifest, RID), 0)
        self.assertEqual(requests.inspect_request(self.root, RID)['phase'], 'running')

    def test_busy_and_unknown_are_fixed_failures(self):
        self.assertNotEqual(executor.run(self.manifest, RID), 0)
        self.publish()
        with lock.execution(self.root, 'owner', self.nonce, 'acquired'):
            self.assertNotEqual(executor.run(self.manifest, RID), 0)
        self.assertFalse((self.root/'requests'/RID/'started.json').exists())

    def test_nonzero_child_and_private_logs(self):
        self.adapter.write_text('import sys;print("private output");sys.exit(2)')
        self.refresh(); self.publish()
        self.assertEqual(executor.run(self.manifest, RID), 2)
        report = requests.inspect_request(self.root, RID)
        self.assertEqual(report['phase'], 'failed')
        self.assertNotIn('private output', json.dumps(report))
        self.assertEqual((self.root/'requests'/RID/'stdout.log').stat().st_mode & 0o777, 0o600)

    def test_submit_lost_reply_is_not_resubmitted(self):
        with patch('maintenance_executor.start', side_effect=TimeoutError):
            report = executor.submit(self.manifest, self.request, dict(owner='owner', nonce=self.nonce))
        self.assertEqual(report['phase'], 'accepted')
        with patch('maintenance_executor.start', side_effect=AssertionError('resubmit')):
            self.assertEqual(executor.submit(self.manifest, self.request, dict(owner='owner', nonce=self.nonce))['phase'], 'accepted')

    def test_explicit_start_only_accepted_and_fixed_unit(self):
        self.publish()
        with patch('maintenance_executor.subprocess.run') as call:
            call.return_value.returncode = 0
            executor.start(self.manifest, RID)
            self.assertEqual(call.call_args.args[0], ['/usr/bin/systemctl', '--user', 'start', '--no-block', 'homelab-maintenance@'+RID+'.service'])
        requests.write_receipt(self.root, self.request, 'started', 'test-runner', exit_code=None)
        with patch('maintenance_executor.subprocess.run', side_effect=AssertionError('restart')):
            with self.assertRaises(ValueError): executor.start(self.manifest, RID)

    def test_credential_substitution_refused_before_body(self):
        self.publish()
        path = self.root/'requests'/RID/'credentials.json'
        path.unlink(); path.symlink_to(self.root/'operation.json')
        with patch('maintenance_executor.subprocess.Popen', side_effect=AssertionError('body started')):
            self.assertNotEqual(executor.run(self.manifest, RID), 0)
        self.assertFalse((self.root/'requests'/RID/'started.json').exists())

    def test_inactive_service_never_implies_completion(self):
        self.publish()
        requests.write_receipt(self.root, self.request, 'started', 'test-runner', exit_code=None)
        with patch('maintenance_executor.subprocess.run') as call:
            call.return_value.returncode = 0; call.return_value.stdout = 'inactive\n'
            report = executor.inspect(self.manifest, RID)
        self.assertEqual(report['phase'], 'unknown')
        self.assertEqual(report['diagnostic'], 'interrupted_execution')

    def test_terminal_write_failure_never_success(self):
        self.publish()
        original = requests.write_receipt
        def fail_terminal(*args, **kwargs):
            if args[2] == 'terminal': raise OSError('disk full')
            return original(*args, **kwargs)
        with patch('maintenance_executor.write_receipt', side_effect=fail_terminal):
            self.assertNotEqual(executor.run(self.manifest, RID), 0)
        self.assertNotEqual(requests.inspect_request(self.root, RID)['phase'], 'succeeded')


if __name__ == '__main__': unittest.main()

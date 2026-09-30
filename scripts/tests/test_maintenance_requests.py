import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import maintenance_requests as requests

RID = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
REQUEST = dict(schema=1, request_id=RID, operation='status', service='jellyfin',
               expected_stage='held', apps_commit='a'*40, runtime_id='b'*64)
SECRET = 'PRIVATE-OWNER-NONCE-SENTINEL'
CREDS = dict(owner=SECRET, nonce=SECRET)


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'requests').mkdir(mode=0o700)
        self.directory = self.root/'requests'/RID

    def publish(self):
        return requests.publish_request(self.root, REQUEST, CREDS)

    def test_strict_schema(self):
        self.assertEqual(requests.validate_request(REQUEST), REQUEST)
        for key, value in [('schema', True), ('operation', 'release'), ('service', 'other'),
                           ('request_id', '../escape'), ('runtime_id', 'bad'), ('apps_commit', 'main'),
                           ('expected_stage', ''), ('argv', ['sh'])]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                requests.validate_request(dict(REQUEST, **{key: value}))
        for value in [[], {}, None]:
            with self.assertRaises(ValueError): requests.validate_request(value)

    def test_published_private_intent_and_duplicate_never_overwritten(self):
        self.assertEqual(self.publish(), RID)
        before = {p.name: p.read_bytes() for p in self.directory.iterdir()}
        with self.assertRaises(FileExistsError): self.publish()
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.directory.iterdir()})
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in self.directory.iterdir()))
        report = requests.inspect_request(self.root, RID)
        self.assertEqual(report['phase'], 'accepted')
        self.assertNotIn(SECRET, json.dumps(report))

    def test_partial_publication_is_not_accepted(self):
        with patch('maintenance_requests.os.fsync', side_effect=OSError(SECRET)):
            with self.assertRaises(OSError): self.publish()
        self.assertEqual(requests.inspect_request(self.root, RID)['phase'], 'unknown')
        with self.assertRaises(FileExistsError): self.publish()

    def test_symlink_fifo_large_and_truncated_inputs_are_sanitized(self):
        self.publish()
        path = self.directory/'request.json'; path.unlink()
        for content in ['{'+SECRET, '[]', 'x'*(1024**2+1)]:
            path.write_text(content); path.chmod(0o600)
            report = requests.inspect_request(self.root, RID)
            self.assertEqual(report['phase'], 'unknown')
            self.assertNotIn(SECRET, json.dumps(report))
        path.unlink(); os.mkfifo(path, 0o600)
        self.assertEqual(requests.inspect_request(self.root, RID)['phase'], 'unknown')
        path.unlink(); path.symlink_to(self.root/'missing')
        self.assertEqual(requests.inspect_request(self.root, RID)['phase'], 'unknown')
        alias = self.root/'alias'; alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)): requests.publish_request(alias, REQUEST, CREDS)

    def test_started_and_terminal_are_bound_and_nonreplayable(self):
        self.publish()
        requests.write_receipt(self.root, REQUEST, 'started', 'runner', exit_code=None)
        self.assertEqual(requests.inspect_request(self.root, RID)['phase'], 'running')
        with self.assertRaises(FileExistsError):
            requests.write_receipt(self.root, REQUEST, 'started', 'runner', exit_code=None)
        requests.write_receipt(self.root, REQUEST, 'terminal', 'runner', exit_code=0)
        self.assertEqual(requests.inspect_request(self.root, RID)['phase'], 'succeeded')
        with self.assertRaises(FileExistsError):
            requests.write_receipt(self.root, REQUEST, 'terminal', 'runner', exit_code=1)
        p = self.directory/'terminal.json'; data = json.loads(p.read_text())
        data['runtime_id'] = 'c'*64; p.write_text(json.dumps(data))
        self.assertEqual(requests.inspect_request(self.root, RID)['phase'], 'unknown')

    def test_terminal_requires_started_and_same_runner(self):
        self.publish()
        with self.assertRaises((ValueError, FileNotFoundError)):
            requests.write_receipt(self.root, REQUEST, 'terminal', 'runner', exit_code=0)
        requests.write_receipt(self.root, REQUEST, 'started', 'runner', exit_code=None)
        with self.assertRaises(ValueError):
            requests.write_receipt(self.root, REQUEST, 'terminal', 'foreign', exit_code=0)
        requests.write_receipt(self.root, REQUEST, 'terminal', 'runner', exit_code=2)
        self.assertEqual(requests.inspect_request(self.root, RID)['phase'], 'failed')

    def test_terminal_timestamp_order_uses_instants_not_text(self):
        self.publish()
        requests.write_receipt(self.root, REQUEST, 'started', 'runner', exit_code=None)
        requests.write_receipt(self.root, REQUEST, 'terminal', 'runner', exit_code=0)
        for name, timestamp in [('started', '2020-01-01T00:00:00+00:00'),
                                ('terminal', '2020-01-01T01:00:00+02:00')]:
            p = self.directory/(name+'.json'); value = json.loads(p.read_text())
            value['at'] = timestamp; p.write_text(json.dumps(value))
        self.assertEqual(requests.inspect_request(self.root, RID)['phase'], 'unknown')

    def test_failed_terminal_publication_is_not_success(self):
        self.publish(); requests.write_receipt(self.root, REQUEST, 'started', 'runner', exit_code=None)
        with patch('maintenance_requests.os.fsync', side_effect=OSError(SECRET)):
            with self.assertRaises(OSError): requests.write_receipt(self.root, REQUEST, 'terminal', 'runner', exit_code=0)
        self.assertNotEqual(requests.inspect_request(self.root, RID)['phase'], 'succeeded')


if __name__ == '__main__': unittest.main()

"""Offline checks for the storage workflows: skip until configured, a dry run before one lock try, D29 and D31."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import importlib.util
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import storage_ready  # noqa: E402
from storage_ready import backup_window, skip_reason  # noqa: E402

WORKFLOWS = ROOT / '.github/workflows'
NAMES = ('storage.yml', 'storage-expiry.yml')
PV = {'retained': '/dev/disk/by-path/a', 'regen': '/dev/disk/by-path/b'}
HELPER = 'python3 maintenance-helper/scripts/maintenance_lock.py'
OUTPUT = re.compile(r'steps\.([\w-]+)\.outputs\.(\w+)')


def workflow(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


def steps(name):
    return workflow(name)['jobs']['reconcile']['steps']


def step(workflow_name, **key):
    (k, v), = key.items()
    return next(s for s in steps(workflow_name) if s.get(k) == v)


def playbook_step(name, playbook):
    return next(s for s in steps(name) if 'ansible-playbook' in s.get('run', '') and playbook in s['run'].split())


def evaluate(condition, outputs):
    """A step `if` with only output comparisons and &&: an unset output is ''."""
    expr = OUTPUT.sub(lambda m: repr(outputs.get(m.groups(), '')), condition).replace('&&', ' and ')
    return eval(expr, {'__builtins__': {}}, {})


def load_helper(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def pinned_helper():
    # CI passes a path relative to the workspace; an unset variable fails, never skips.
    return Path(os.environ['PINNED_LOCK_HELPER']).resolve()


class Fixture:
    """A lock dir, a GITHUB_ENV/GITHUB_OUTPUT pair and a fake ansible-playbook."""

    def __init__(self, d):
        self.d = Path(d)
        self.lock = self.d / 'lock'
        self.lock.mkdir()
        (self.lock / 'runner-instance').write_text('fixture')
        fake = self.d / 'ansible-playbook'
        fake.write_text('#!/bin/sh\nexit "${FIXTURE_EXIT:-0}"\n')
        fake.chmod(0o700)
        self.env = dict(os.environ, HOMELAB_RUNNER_INSTANCE='fixture', MAINTENANCE_OWNER='ansible/test/1',
                        GITHUB_ENV=str(self.d / 'env'), GITHUB_OUTPUT=str(self.d / 'output'),
                        PATH=str(self.d) + ':' + os.environ['PATH'])

    def run(self, s):
        # The helper first, and the root with its slash: /maintenance is also a prefix of /maintenance_lock.py.
        script = s['run'].replace(HELPER, sys.executable + ' ' + str(pinned_helper()))
        script = script.replace('/maintenance/', str(self.lock) + '/')
        for command in ('acquire', 'verify', 'release'):
            script = script.replace('maintenance_lock.py ' + command, f'maintenance_lock.py {command} --root {self.lock}')
        return subprocess.run(['bash', '-e', '-c', script], env=self.env, capture_output=True, text=True)

    def outputs(self):
        path = self.d / 'output'
        return dict(line.split('=', 1) for line in path.read_text().splitlines()) if path.exists() else {}

    def take_nonce(self):
        self.env['MAINTENANCE_NONCE'] = (self.d / 'env').read_text().strip().split('=', 1)[1]


class ReadyTests(unittest.TestCase):
    def ready(self, **kwargs):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, GITHUB_OUTPUT=str(Path(d) / 'out')):
            gv = Path(d) / 'storage_vm.yml'
            gv.write_text(yaml.safe_dump({'storage_vm_pv_devices': PV}))
            out = io.StringIO()
            with mock.patch.object(storage_ready, 'GROUP_VARS', gv), redirect_stdout(out):
                storage_ready.main(**kwargs)
            return (Path(d) / 'out').read_text(), out.getvalue()

    def test_workflow_skips_without_storage_host(self):
        self.assertEqual(skip_reason([], {}), 'no host in the storage group')
        self.assertEqual(skip_reason(['storage-01'], {}), 'storage_vm_pv_devices is the placeholder')
        self.assertEqual(skip_reason(['storage-01'], {'storage_vm_pv_devices': {'retained': '', 'regen': '/dev/b'}}),
                         'storage_vm_pv_devices is the placeholder')
        for bad in (None, 'TODO', {'retained': '/dev/a'}, {**PV, 'extra': '/dev/c'}, {**PV, 'regen': 7}):
            self.assertEqual(skip_reason(['storage-01'], {'storage_vm_pv_devices': bad}),
                             'storage_vm_pv_devices is the placeholder', bad)
        self.assertIsNone(skip_reason(['storage-01'], {'storage_vm_pv_devices': PV}))
        with self.assertRaises(SystemExit) as e:
            skip_reason(['a', 'b'], {'storage_vm_pv_devices': PV})
        self.assertIn('more than one host in the storage group', str(e.exception.code))
        written, printed = self.ready(hosts=[], event='push')
        self.assertEqual((written, printed), ('ready=false\n', 'Skipping: no host in the storage group; nothing touched\n'))
        self.assertEqual(self.ready(hosts=['storage-01'], event='push')[0], 'ready=true\n')

    def test_unparsed_inventory_fails_instead_of_skipping(self):
        # An API outage parses no hosts at all: that must go red, never read as "no storage host".
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(storage_ready, 'INVENTORY', Path(d) / 'missing.yml'), \
                    redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as e:
                storage_ready.storage_hosts()
            self.assertIn('no hosts', str(e.exception.code))
            inv = Path(d) / 'inventory.yml'
            inv.write_text(yaml.safe_dump({'all': {'hosts': {'k3s-master-01': {}}}}))
            with mock.patch.object(storage_ready, 'INVENTORY', inv):
                self.assertEqual(storage_ready.storage_hosts(), [])
            inv.write_text(yaml.safe_dump({'all': {'children': {'storage': {'hosts': {'storage-01': {}}}}}}))
            with mock.patch.object(storage_ready, 'INVENTORY', inv):
                self.assertEqual(storage_ready.storage_hosts(), ['storage-01'])

    def test_schedule_skips_inside_windows(self):
        def at(*utc):
            return datetime(*utc, tzinfo=timezone.utc)
        # Weekday 02:05 local: January is UTC+2, July UTC+3 (2027-01-13 and 2027-07-14 are Wednesdays).
        self.assertEqual(backup_window(at(2027, 1, 13, 0, 5)), 'dumps')
        self.assertEqual(backup_window(at(2027, 7, 13, 23, 5)), 'dumps')
        self.assertIsNone(backup_window(at(2027, 7, 14, 0, 5)))  # 03:05 in July
        sunday = (2027, 1, 17)
        self.assertEqual(backup_window(at(2027, 1, 16, 23, 5)), 'vzdump')  # Sunday 01:05
        self.assertEqual(backup_window(at(2027, 1, 13, 3, 5)), 'backup')  # 05:05 daily
        self.assertEqual(backup_window(at(*sunday, 4, 5)), 'maint')  # Sunday 06:05
        self.assertIsNone(backup_window(at(2027, 1, 13, 4, 5)))  # weekday 06:05
        self.assertIsNone(backup_window(at(2027, 1, 13, 1, 5)))  # 03:05
        self.assertIsNone(backup_window(at(2027, 1, 13, 10, 5)))  # 12:05
        self.assertIsNone(backup_window(at(2027, 1, 13, 23, 5)))  # Thursday 01:05
        dumps = at(2027, 1, 13, 0, 5)
        written, printed = self.ready(hosts=['storage-01'], event='schedule', now=dumps)
        self.assertEqual((written, printed),
                         ('ready=false\n', 'Skipping: inside backup window dumps; the next hourly run retries\n'))
        for event in ('repository_dispatch', 'workflow_dispatch', 'push'):
            self.assertEqual(self.ready(hosts=['storage-01'], event=event, now=dumps)[0], 'ready=true\n', event)
        self.assertEqual(self.ready(hosts=['storage-01'], event='schedule', now=at(2027, 1, 13, 10, 5))[0],
                         'ready=true\n')


if __name__ == '__main__':
    unittest.main()

"""Offline checks for the storage workflows: skip until configured, a dry run before one lock try, D29 and D31."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import fnmatch
import importlib.util
import io
import json
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
CONTEXT = re.compile(r'\b(github\.event_name|inputs\.\w+)\b')
RESTORE_OWNER = 'claude-code/storage-restore/2026-11-23T10:00:00Z'
APPROVALS = 'storage-approvals.json'  # the dispatch approvals step's vars file, read by both storage playbooks


def workflow(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


def steps(name):
    return workflow(name)['jobs']['reconcile']['steps']


def step(workflow_name, **key):
    (k, v), = key.items()
    return next(s for s in steps(workflow_name) if s.get(k) == v)


def playbook_step(name, playbook):
    return next(s for s in steps(name) if 'ansible-playbook' in s.get('run', '') and playbook in s['run'].split())


def evaluate(condition, outputs, context=None):
    """A step `if` with output, event-name and input comparisons, && and ||: an unset value is ''."""
    expr = OUTPUT.sub(lambda m: repr(outputs.get(m.groups(), '')), condition)
    expr = CONTEXT.sub(lambda m: repr((context or {}).get(m.group(1), '')), expr)
    return eval(expr.replace('&&', ' and ').replace('||', ' or '), {'__builtins__': {}}, {})


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
        # FIXTURE_OUTPUT: a line the playbook writes to the step outputs (storage-preflight.yml's k3s=).
        fake.write_text('#!/bin/sh\nif [ -n "${FIXTURE_OUTPUT:-}" ]; then printf "%s\\n" "$FIXTURE_OUTPUT" >> "$GITHUB_OUTPUT"; fi\n'
                        'exit "${FIXTURE_EXIT:-0}"\n')
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
        return subprocess.run(['bash', '-e', '-c', script], env=self.env, capture_output=True, text=True, cwd=self.d)

    def outputs(self):
        path = self.d / 'output'
        return dict(line.split('=', 1) for line in path.read_text().splitlines()) if path.exists() else {}

    def take_nonce(self):
        # As GITHUB_ENV does for later steps: every line, so an adopted MAINTENANCE_OWNER replaces the job's.
        self.env.update(line.split('=', 1) for line in (self.d / 'env').read_text().splitlines())


class ReadyTests(unittest.TestCase):
    def outputs(self, pv=PV, **kwargs):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, GITHUB_OUTPUT=str(Path(d) / 'out')):
            gv = Path(d) / 'storage_vm.yml'
            gv.write_text(yaml.safe_dump({'storage_vm_pv_devices': pv}))
            out = io.StringIO()
            with mock.patch.object(storage_ready, 'GROUP_VARS', gv), redirect_stdout(out):
                storage_ready.main(**kwargs)
            return (Path(d) / 'out').read_text(), out.getvalue()

    def ready(self, **kwargs):
        # The ready= line only; gauges= has its own test (test_window_skip_still_writes_gauges).
        written, printed = self.outputs(**kwargs)
        return ''.join(line for line in written.splitlines(keepends=True) if line.startswith('ready=')), printed

    def gauges(self, **kwargs):
        written = dict(line.split('=', 1) for line in self.outputs(**kwargs)[0].splitlines())
        return written['ready'], written['gauges']

    def test_workflow_skips_without_storage_host(self):
        self.assertEqual(skip_reason([], {}), 'no host in the storage group')
        self.assertEqual(skip_reason(['storage-01'], {}), 'storage_vm_pv_devices is the placeholder')
        self.assertEqual(skip_reason(['storage-01'], {'storage_vm_pv_devices': {'retained': '', 'regen': '/dev/b'}}),
                         'storage_vm_pv_devices is the placeholder')
        for bad in (None, 'TODO', {'retained': '/dev/a'}, {**PV, 'extra': '/dev/c'}, {**PV, 'regen': 7}):
            self.assertEqual(skip_reason(['storage-01'], {'storage_vm_pv_devices': bad}),
                             'storage_vm_pv_devices is the placeholder', bad)
        self.assertIsNone(skip_reason(['storage-01'], {'storage_vm_pv_devices': PV}))
        self.assertEqual(skip_reason(['storage-01'], yaml.safe_load(storage_ready.GROUP_VARS.read_text())),
                         'storage_vm_pv_devices is the placeholder')  # until Task 3.5 sets it
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

    def test_collection_inventory_plugin_loads(self):
        # inventory/proxmox.yml names a collection plugin (community.proxmox.proxmox); the Python API finds it only
        # after the plugin loader is initialised, as the ansible-* CLIs do. A fresh interpreter, as in CI.
        plugin = (
            'from ansible.plugins.inventory import BaseInventoryPlugin\n'
            'class InventoryModule(BaseInventoryPlugin):\n'
            "    NAME = 'homelab.fake.hosts'\n"
            '    def verify_file(self, path):\n'
            "        return path.endswith('.yml')\n"
            '    def parse(self, inventory, loader, path, cache=True):\n'
            '        super().parse(inventory, loader, path, cache)\n'
            "        inventory.add_group('storage')\n"
            "        inventory.add_host('storage-01', group='storage')\n"
            "        inventory.add_host('k3s-master-01')\n")
        with tempfile.TemporaryDirectory() as d:
            pdir = Path(d) / 'ansible_collections/homelab/fake/plugins/inventory'
            pdir.mkdir(parents=True)
            (pdir / 'hosts.py').write_text(plugin)
            inv = Path(d) / 'proxmox.yml'
            inv.write_text('plugin: homelab.fake.hosts\n')
            code = ('import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); import storage_ready; '
                    'storage_ready.INVENTORY = Path(sys.argv[2]); print(storage_ready.storage_hosts())')
            env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=d, ANSIBLE_INVENTORY_ENABLED='auto')
            r = subprocess.run([sys.executable, '-c', code, str(ROOT / 'scripts'), str(inv)], env=env,
                               capture_output=True, text=True, cwd=d)
            self.assertEqual((r.returncode, r.stdout.strip()), (0, "['storage-01']"), r.stderr)

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

    def test_window_skip_still_writes_gauges(self):
        # D29: the window skip stops the reconcile, not the gauges (StorageDatasetLow's 3 h staleness leg).
        dumps = datetime(2027, 1, 13, 0, 5, tzinfo=timezone.utc)  # Wednesday 02:05 local
        after = datetime(2027, 1, 13, 1, 5, tzinfo=timezone.utc)  # 03:05
        one = ['storage-01']
        self.assertEqual(self.gauges(hosts=one, event='schedule', now=dumps), ('false', 'true'))
        self.assertEqual(self.outputs(hosts=one, event='schedule', now=dumps)[1],
                         'Skipping: inside backup window dumps; the next hourly run retries\n')
        self.assertEqual(self.gauges(hosts=one, event='schedule', now=after), ('true', 'true'))
        self.assertEqual(self.gauges(hosts=[], event='schedule', now=dumps), ('false', 'false'))
        self.assertEqual(self.gauges(hosts=one, event='schedule', now=dumps, pv={}), ('false', 'false'))
        self.assertEqual(self.gauges(hosts=[], event='push'), ('false', 'false'))
        self.assertEqual(self.gauges(hosts=one, event='workflow_dispatch', now=dumps), ('true', 'true'))
        # The storage group is read inside a window now, so an unparsed inventory fails there too.
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(storage_ready, 'INVENTORY', Path(d) / 'missing.yml'), \
                redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.outputs(event='schedule', now=dumps)


class WorkflowTests(unittest.TestCase):
    def test_every_step_after_ready_is_gated(self):
        for name in NAMES:
            ss = steps(name)
            ready = next(i for i, s in enumerate(ss) if s.get('id') == 'ready')
            self.assertEqual(ss[ready]['run'], 'python3 scripts/storage_ready.py', name)
            outputs = {('ready', 'ready'): 'false'}  # a skipped step leaves its outputs unset
            for s in ss[ready + 1:-1]:
                if s['name'] == 'Write host gauges':
                    continue  # gated on gauges, not ready: test_host_gauges_before_held_unlocked pins its if
                with self.subTest(workflow=name, step=s['name']):
                    self.assertIn('if', s)
                    self.assertFalse(evaluate(s['if'], outputs))
            self.assertEqual(ss[-1]['name'], 'Remove private controller credentials', name)

    def test_host_gauges_before_held_unlocked(self):
        ss = steps('storage.yml')
        gauges = step('storage.yml', name='Write host gauges')
        self.assertLess(ss.index(step('storage.yml', id='ready')), ss.index(gauges))
        self.assertLess(ss.index(gauges), ss.index(step('storage.yml', id='held')))
        self.assertEqual(gauges['if'], "steps.ready.outputs.gauges == 'true'")
        self.assertIs(gauges['continue-on-error'], True)
        self.assertLessEqual(gauges['timeout-minutes'], 5)  # a hung df or mirror must not eat the job's 30 minutes
        run = gauges['run'].replace('\\\n', ' ')
        for want in ('-i inventory/proxmox.yml -i inventory/hosts.yml', 'sshpass', 'storage-host-metrics.yml'):
            self.assertIn(want, ' '.join(run.split()))
        self.assertNotIn('maintenance_lock.py', run)
        self.assertNotRegex(run, r'\s-e\s')
        self.assertTrue(evaluate(gauges['if'], {('ready', 'ready'): 'false', ('ready', 'gauges'): 'true'}))
        self.assertFalse(evaluate(gauges['if'], {('ready', 'ready'): 'false', ('ready', 'gauges'): 'false'}))
        self.assertFalse(any(s.get('name') == 'Write host gauges' for s in steps('storage-expiry.yml')))

    def test_dry_run_before_lock_then_apply_then_release(self):
        ss = steps('storage.yml')
        order = [step('storage.yml', id='ready'), playbook_step('storage.yml', 'storage-dry-run.yml'),
                 step('storage.yml', id='lock'), playbook_step('storage.yml', 'storage.yml'),
                 step('storage.yml', name='Release the lock after success')]
        self.assertEqual([ss.index(s) for s in order], sorted(ss.index(s) for s in order))
        self.assertNotIn('maintenance_lock.py', order[1]['run'])
        self.assertIn(HELPER + ' acquire --operation ansible-storage --github-env', order[2]['run'])
        self.assertEqual(order[3]['run'].splitlines()[0], HELPER + ' verify')
        expiry = steps('storage-expiry.yml')
        self.assertFalse(any('storage-dry-run.yml' in s.get('run', '') for s in expiry))
        self.assertIn(HELPER + ' acquire --operation ansible-storage-expiry --github-env',
                      step('storage-expiry.yml', id='lock')['run'])
        self.assertEqual(playbook_step('storage-expiry.yml', 'storage-expiry.yml')['run'].splitlines()[0],
                         HELPER + ' verify')

    def test_k3s_check_precedes_the_lock(self):
        # A run between a k3s rebuild apply and its deploy finds no k3s.yaml on masters[0]: it must skip green before
        # 'Try the maintenance lock once', or D31 keeps the lock and the deploy's acquire fails (finding n=11).
        for name in NAMES:
            with self.subTest(workflow=name):
                ss = steps(name)
                check = step(name, id='preflight')
                lock = step(name, name='Try the maintenance lock once')
                self.assertLess(ss.index(check), ss.index(lock))
                self.assertEqual(check['name'], 'Check that k3s answers before the lock')
                self.assertEqual(playbook_step(name, 'storage-preflight.yml'), check)
                self.assertNotIn('maintenance_lock.py', check['run'])
                ready = {('ready', 'ready'): 'true', ('held', 'held'): 'false'}
                self.assertTrue(evaluate(check['if'], ready))
                for k3s in ('false', ''):  # false: no k3s; unset: the check did not run or failed
                    self.assertFalse(evaluate(lock['if'], {**ready, ('preflight', 'k3s'): k3s}), k3s)
                self.assertTrue(evaluate(lock['if'], {**ready, ('preflight', 'k3s'): 'true'}))
                for k3s, exit, rc, warned in (('k3s=false', '0', 0, True), ('k3s=true', '0', 0, False),
                                              ('', '2', 1, False),  # an unreachable master or any error: red
                                              ('', '0', 1, False),  # no answer written: red, never a silent skip
                                              ('k3s=maybe', '0', 1, False)):
                    with tempfile.TemporaryDirectory() as d:
                        f = Fixture(d)
                        f.env.update(FIXTURE_OUTPUT=k3s, FIXTURE_EXIT=exit)
                        got = f.run(check)
                        self.assertEqual(got.returncode != 0, bool(rc), got.stderr)
                        self.assertEqual(bool(re.search(r'(?m)^::warning::.*k3s', got.stdout)), warned)
                        self.assertFalse((f.lock / 'operation.json').exists())
        # The expiry's only pre-lock run: it fetches and validates the inventory there (finding n=17).
        expiry = steps('storage-expiry.yml')
        self.assertLess(expiry.index(step('storage-expiry.yml', id='preflight')),
                        expiry.index(step('storage-expiry.yml', id='lock')))
        self.assertIn('storage-play-fetch-inventory.yml', (ROOT / 'storage-preflight.yml').read_text())
        dry = playbook_step('storage.yml', 'storage-dry-run.yml')
        ready = {('ready', 'ready'): 'true', ('held', 'held'): 'false'}
        self.assertFalse(evaluate(dry['if'], {**ready, ('preflight', 'k3s'): 'false'}))
        self.assertTrue(evaluate(dry['if'], {**ready, ('preflight', 'k3s'): 'true'}))
        # D51's adopted reconcile does not depend on it: the runbook holds the lock and reads its own log.
        self.assertFalse(evaluate(step('storage.yml', id='preflight')['if'],
                                  {('ready', 'ready'): 'true', ('held', 'held'): 'true'}))

    def test_lock_held_exits_zero(self):
        for name in NAMES:
            with self.subTest(workflow=name), tempfile.TemporaryDirectory() as d:
                f = Fixture(d)
                record = f.lock / 'operation.json'
                record.write_text('{"held": "by another run"}\n')
                got = f.run(step(name, id='lock'))
                self.assertEqual(got.returncode, 0, got.stderr)
                self.assertRegex(got.stdout, r'(?m)^::warning::.*held')
                self.assertEqual(f.outputs(), {'acquired': 'false'})
                self.assertEqual(record.read_text(), '{"held": "by another run"}\n')
                record.unlink()
                (f.d / 'output').unlink()
                got = f.run(step(name, id='lock'))
                self.assertEqual(got.returncode, 0, got.stderr)
                self.assertEqual(f.outputs(), {'acquired': 'true'})
                self.assertIn('MAINTENANCE_NONCE=', (f.d / 'env').read_text())
                self.assertTrue(record.exists())

    def test_held_lock_skips_before_dry_run(self):
        held = step('storage.yml', name='Skip while another maintenance operation runs')
        ss = steps('storage.yml')
        dry = playbook_step('storage.yml', 'storage-dry-run.yml')
        self.assertEqual(held['id'], 'held')
        self.assertLess(ss.index(held), ss.index(dry))
        with tempfile.TemporaryDirectory() as d:
            f = Fixture(d)
            got = f.run(held)
            self.assertEqual((got.returncode, f.outputs().get('held')), (0, 'false'), got.stderr)
            (f.lock / 'operation.json').write_text('{}\n')
            (f.d / 'output').unlink()
            got = f.run(held)
            self.assertEqual((got.returncode, f.outputs()), (0, {'held': 'true'}), got.stderr)
            self.assertRegex(got.stdout, r'(?m)^::warning::')
        ready = {('ready', 'ready'): 'true'}
        self.assertTrue(evaluate(dry['if'], {**ready, ('held', 'held'): 'false', ('preflight', 'k3s'): 'true'}))
        self.assertFalse(evaluate(dry['if'], {**ready, ('held', 'held'): 'true', ('preflight', 'k3s'): 'true'}))
        self.assertFalse(evaluate(step('storage.yml', id='lock')['if'], {**ready, ('held', 'held'): 'true'}))

    def test_release_only_after_success(self):
        for name in NAMES:
            release = step(name, name='Release the lock after success')
            self.assertEqual(release['if'], "steps.lock.outputs.acquired == 'true'", name)
            self.assertEqual(release['run'], HELPER + ' release', name)
            reconcile = playbook_step(name, name)
            for gate in ('always()', 'failure()', 'cancelled()'):
                self.assertNotIn(gate, reconcile['if'] + release['if'], name)
            with self.subTest(workflow=name), tempfile.TemporaryDirectory() as d:
                f = Fixture(d)
                self.assertNotEqual(f.run(reconcile).returncode, 0)  # no lock held: verify refuses
                self.assertEqual(f.run(step(name, id='lock')).returncode, 0)
                f.take_nonce()
                f.env['FIXTURE_EXIT'] = '1'
                self.assertNotEqual(f.run(reconcile).returncode, 0)
                self.assertTrue((f.lock / 'operation.json').exists())
                f.env['FIXTURE_EXIT'] = '0'
                self.assertEqual(f.run(reconcile).returncode, 0)
                self.assertEqual(f.run(release).returncode, 0)
                self.assertFalse((f.lock / 'operation.json').exists())

    def test_held_lock_input(self):
        inputs = workflow('storage.yml')[True]['workflow_dispatch']['inputs']
        self.assertEqual(set(inputs), {'held_lock_owner', 'remove_workers', 'max_removals'})
        for name in ('held_lock_owner', 'remove_workers', 'max_removals'):
            self.assertIs(inputs[name]['required'], False, name)
            self.assertEqual(inputs[name]['default'], '', name)
        self.assertIs(inputs['held_lock_owner']['required'], False)
        self.assertEqual(inputs['held_lock_owner']['default'], '')

    def test_held_lock_owner_set_at_job_level(self):
        # GITHUB_ENV may not override a job-level env key, so the owner the verify step reads must not depend on it.
        owner = workflow('storage.yml')['jobs']['reconcile']['env']['MAINTENANCE_OWNER']
        self.assertEqual(owner, "${{ inputs.held_lock_owner || format('{0}/{1}/{2}', github.repository, "
                                "github.run_id, github.run_attempt) }}")

    def test_held_lock_adopt_step(self):
        adopt = step('storage.yml', id='adopt')
        ss = steps('storage.yml')
        self.assertLess(ss.index(step('storage.yml', id='held')), ss.index(adopt))
        self.assertLess(ss.index(adopt), ss.index(step('storage.yml', id='lock')))
        self.assertNotIn('${{', adopt['run'])  # the input reaches the script through env only
        self.assertIn('python3 -I', adopt['run'])
        self.assertNotIn('set -x', adopt['run'])
        dispatch = {'github.event_name': 'workflow_dispatch', 'inputs.held_lock_owner': RESTORE_OWNER}
        ready = {('ready', 'ready'): 'true', ('held', 'held'): 'true'}
        self.assertTrue(evaluate(adopt['if'], ready, dispatch))
        self.assertFalse(evaluate(adopt['if'], ready, {**dispatch, 'inputs.held_lock_owner': ''}))
        self.assertFalse(evaluate(adopt['if'], {('ready', 'ready'): 'false'}, dispatch))
        pinned = load_helper(pinned_helper(), 'pinned_lock_adopt')

        def attempt(input_owner, holder=None, operation='storage-cutover:storage-restore'):  # lock.sh's form
            d = tempfile.TemporaryDirectory()
            self.addCleanup(d.cleanup)
            f = Fixture(d.name)
            nonce = None
            if holder:
                with mock.patch.dict(os.environ, HOMELAB_RUNNER_INSTANCE='fixture'):
                    nonce = pinned.acquire(f.lock, operation, holder)
            f.env['HELD_LOCK_OWNER'] = input_owner
            return f, nonce, f.run(adopt)

        for input_owner, holder in ((RESTORE_OWNER, None),  # no lock held
                                    (RESTORE_OWNER, RESTORE_OWNER + '0'),  # another owner holds it
                                    ('ansible/x/1/1', 'ansible/x/1/1'),  # a CI owner, even when it holds the lock
                                    (RESTORE_OWNER + '\nX=1', RESTORE_OWNER + '\nX=1'),
                                    ('claude-code/storage-restore/', 'claude-code/storage-restore/'),
                                    ('x' + RESTORE_OWNER, None)):
            with self.subTest(input=input_owner, holder=holder):
                f, nonce, got = attempt(input_owner, holder if holder and '\n' not in holder else None)
                self.assertNotEqual(got.returncode, 0)
                self.assertFalse((f.d / 'env').exists())
                self.assertEqual(f.outputs(), {})
        for operation in ('storage-cutover:dry-run', 'storage-restore'):  # the owner's lock, another operation
            with self.subTest(operation=operation):
                f, nonce, got = attempt(RESTORE_OWNER, RESTORE_OWNER, operation)
                self.assertNotEqual(got.returncode, 0)
                self.assertFalse((f.d / 'env').exists())
                self.assertEqual(f.outputs(), {})
        f, nonce, got = attempt(RESTORE_OWNER, RESTORE_OWNER)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(f.outputs(), {'adopted': 'true'})
        self.assertEqual(got.stdout.splitlines()[0], '::add-mask::' + nonce)
        self.assertNotIn(nonce, '\n'.join(got.stdout.splitlines()[1:]) + got.stderr)
        self.assertEqual((f.d / 'env').read_text(),
                         f'MAINTENANCE_OWNER={RESTORE_OWNER}\nMAINTENANCE_NONCE={nonce}\n')
        # The next steps see the adopted pair, so verify passes and the record stays the runbook's.
        f.take_nonce()
        self.assertEqual(f.run(playbook_step('storage.yml', 'storage.yml')).returncode, 0)
        self.assertIn(RESTORE_OWNER, (f.lock / 'operation.json').read_text())

    def test_dispatch_approvals(self):
        approvals = step('storage.yml', id='approvals')
        ss = steps('storage.yml')
        self.assertLess(ss.index(step('storage.yml', id='ready')), ss.index(approvals))
        self.assertEqual(approvals['if'], "steps.ready.outputs.ready == 'true'")
        self.assertNotIn('${{', approvals['run'])  # the inputs reach the script through env only
        self.assertIn('python3 -I', approvals['run'])
        self.assertEqual(approvals['env'], {'REMOVE_WORKERS': '${{ inputs.remove_workers }}',
                                            'MAX_REMOVALS': '${{ inputs.max_removals }}'})
        # Both the pre-lock dry run and the reconcile read it, or the dry run refuses what the approval allows.
        for playbook in ('storage-dry-run.yml', 'storage.yml'):
            s = playbook_step('storage.yml', playbook)
            self.assertLess(ss.index(approvals), ss.index(s), playbook)
            self.assertIn(f'-e @{APPROVALS}', ' '.join(s['run'].replace('\\\n', ' ').split()), playbook)
        self.assertIn(APPROVALS, step('storage.yml', name='Remove private controller credentials')['run'])
        self.assertNotIn(APPROVALS, (WORKFLOWS / 'storage-expiry.yml').read_text())

        def run(**env):
            d = tempfile.TemporaryDirectory()
            self.addCleanup(d.cleanup)
            f = Fixture(d.name)
            f.env.update(env)
            got = f.run(approvals)
            path = f.d / APPROVALS
            return got, (json.loads(path.read_text()) if path.exists() else None)

        for env, want in (({}, {}), ({'REMOVE_WORKERS': ''}, {}),
                          ({'REMOVE_WORKERS': 'k3s-worker-02'}, {'storage_vm_remove_workers': ['k3s-worker-02']}),
                          ({'REMOVE_WORKERS': ' k3s-worker-03, k3s-worker-02 k3s-worker-03'},
                           {'storage_vm_remove_workers': ['k3s-worker-02', 'k3s-worker-03']})):
            with self.subTest(env=env):
                got, written = run(**env)
                self.assertEqual(got.returncode, 0, got.stderr)
                self.assertEqual(written, want)
        for bad in ('Worker-1', 'w1;rm', '-w1', 'w1/x', '"w1"', 'w1=x'):
            with self.subTest(bad=bad):
                got, written = run(REMOVE_WORKERS=bad)
                self.assertNotEqual(got.returncode, 0)
                self.assertIsNone(written)
        # max_removals: the removal brake for this run only (finding n=3); empty keeps the role default.
        for value, want in (('', {}), ('4', {'storage_vm_max_removals': 4}), (' 12 ', {'storage_vm_max_removals': 12})):
            with self.subTest(max_removals=value):
                got, written = run(MAX_REMOVALS=value)
                self.assertEqual(got.returncode, 0, got.stderr)
                self.assertEqual(written, want)
        for bad in ('0', '-1', '01', '2.5', 'four', '4 5', '10000', '4\n-e x=y'):
            with self.subTest(max_removals=bad):
                got, written = run(MAX_REMOVALS=bad)
                self.assertNotEqual(got.returncode, 0)
                self.assertIsNone(written)
        got, written = run(REMOVE_WORKERS='w2', MAX_REMOVALS='3')
        self.assertEqual(written, {'storage_vm_remove_workers': ['w2'], 'storage_vm_max_removals': 3})

    def test_held_lock_skips_acquire_and_release(self):
        dispatch = {'github.event_name': 'workflow_dispatch', 'inputs.held_lock_owner': RESTORE_OWNER}
        adopted = {('ready', 'ready'): 'true', ('held', 'held'): 'true', ('adopt', 'adopted'): 'true'}
        lock = step('storage.yml', id='lock')
        release = step('storage.yml', name='Release the lock after success')
        reconcile = playbook_step('storage.yml', 'storage.yml')
        self.assertFalse(evaluate(lock['if'], {**adopted, ('held', 'held'): 'false'}, dispatch))
        self.assertFalse(evaluate(release['if'], adopted, dispatch))
        self.assertFalse(evaluate(playbook_step('storage.yml', 'storage-dry-run.yml')['if'], adopted, dispatch))
        self.assertTrue(evaluate(reconcile['if'], adopted, dispatch))
        self.assertEqual(reconcile['run'].splitlines()[0], HELPER + ' verify')
        # Input empty: no adoption, so acquire, reconcile and release run as before.
        acquired = {('ready', 'ready'): 'true', ('held', 'held'): 'false', ('preflight', 'k3s'): 'true',
                    ('lock', 'acquired'): 'true'}
        for s in (lock, reconcile, release):
            self.assertTrue(evaluate(s['if'], acquired, {'github.event_name': 'workflow_dispatch'}), s['name'])
        self.assertFalse(evaluate(reconcile['if'], {('ready', 'ready'): 'true', ('held', 'held'): 'true'}))

    def test_held_lock_not_on_other_triggers(self):
        adopt = step('storage.yml', id='adopt')
        self.assertIn("github.event_name == 'workflow_dispatch'", adopt['if'])
        ready = {('ready', 'ready'): 'true', ('held', 'held'): 'true'}
        for event in ('repository_dispatch', 'schedule', 'push'):
            self.assertFalse(evaluate(adopt['if'], ready, {'github.event_name': event,
                                                           'inputs.held_lock_owner': RESTORE_OWNER}), event)

    def test_no_sha_from_payload(self):
        for name in NAMES:
            text = (WORKFLOWS / name).read_text()
            self.assertNotIn('client_payload', text, name)
            self.assertNotIn('storage_vm_inventory_branch', text, name)
            for s in steps(name):
                for line in s.get('run', '').replace('\\\n', ' ').splitlines():
                    if 'ansible-playbook' in line:
                        # Only the approvals step's validated file, never a raw input or payload value.
                        line = line.replace(f' -e @{APPROVALS} ', ' ')
                        self.assertNotRegex(line, r'\s(-e|--extra-vars)(\s|=)', name)

    def test_triggers(self):
        on = workflow('storage.yml')[True]
        self.assertEqual(set(on), {'repository_dispatch', 'workflow_dispatch', 'schedule', 'push'})
        self.assertEqual(on['repository_dispatch']['types'], ['storage-changed'])
        self.assertEqual(on['schedule'], [{'cron': '5 * * * *'}])
        self.assertEqual(on['push']['branches'], ['main'])
        self.assertEqual(on['push']['paths'], ['group_vars/storage/**', 'roles/storage_vm/**', 'storage-dry-run.yml',
                                               'storage-host-metrics.yml', 'storage-play-*.yml', 'storage-preflight.yml',
                                               'storage.yml'])
        on = workflow('storage-expiry.yml')[True]
        self.assertEqual(set(on), {'schedule', 'workflow_dispatch'})
        self.assertEqual(on['schedule'], [{'cron': '5 10 * * *'}])
        groups = set()
        for name, group in zip(NAMES, ('storage-reconcile', 'storage-expiry')):
            doc = workflow(name)
            self.assertEqual(doc['concurrency'], {'group': group, 'cancel-in-progress': False}, name)
            job = doc['jobs']['reconcile']
            self.assertIsInstance(job['timeout-minutes'], int, name)
            self.assertEqual(job['runs-on'], 'self-hosted', name)
            self.assertIn('/var/lib/homelab-maintenance:/maintenance', job['container']['volumes'], name)
            groups.add(group)
        self.assertEqual(len(groups), 2)

    def test_push_paths_cover_every_playbook_run(self):
        """A change to any playbook the workflow runs, or one it imports, reconciles at merge."""
        paths = workflow('storage.yml')[True]['push']['paths']
        todo = [w for s in steps('storage.yml') for line in s.get('run', '').replace('\\\n', ' ').splitlines()
                if 'ansible-playbook' in line for w in line.split() if re.fullmatch(r'[\w-]+\.yml', w)]
        self.assertTrue(todo)
        seen = set()
        while todo:
            name = todo.pop()
            if name in seen:
                continue
            seen.add(name)
            self.assertTrue(any(fnmatch.fnmatchcase(name, p) for p in paths), name)
            # shortcut: plays only, not task-level imports (tasks/lab-guard.yml is outside push.paths, and adding it
            # would start deploy.yml and storage.yml on one push); follow them once Ben decides that trigger.
            todo += [p[k] for p in yaml.safe_load((ROOT / name).read_text())
                     for k in ('import_playbook', 'ansible.builtin.import_playbook') if k in p]
        self.assertIn('storage-host-metrics.yml', seen)

    def test_lock_helper_pinned_like_deploy(self):
        def ref(doc, job):
            return next(s['with']['ref'] for s in doc['jobs'][job]['steps']
                        if s.get('with', {}).get('path') == 'maintenance-helper')
        deploy = workflow('deploy.yml')
        want = ref(deploy, 'provision')
        self.assertRegex(want, r'^[0-9a-f]{40}$')
        for name in NAMES:
            self.assertEqual(ref(workflow(name), 'reconcile'), want, name)
            job = workflow(name)['jobs']['reconcile']
            self.assertEqual(job['env']['HOMELAB_RUNNER_INSTANCE'], deploy['jobs']['provision']['env']['HOMELAB_RUNNER_INSTANCE'])
        self.assertEqual(ref(workflow('validate.yml'), 'lint'), want)

    def test_pinned_acquire_repo_verify_compatible(self):
        pinned = load_helper(pinned_helper(), 'pinned_lock')
        repo = load_helper(ROOT / 'scripts/maintenance_lock.py', 'repo_lock')
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, HOMELAB_RUNNER_INSTANCE='fixture'):
            root = Path(d)
            (root / 'runner-instance').write_text('fixture')
            nonce = pinned.acquire(root, 'ansible-storage', 'ansible/test/1')
            repo.verify(root, 'ansible/test/1', nonce)
            with self.assertRaises(PermissionError):
                repo.verify(root, 'ansible/test/1', '0' * 64)
            pinned.release(root, 'ansible/test/1', nonce)
            self.assertFalse((root / 'operation.json').exists())

    def test_deploy_ignores_storage_only_paths(self):
        ignored = workflow('deploy.yml')[True]['push']['paths-ignore']
        for path in ('roles/storage_vm/**', 'storage.yml', 'storage-expiry.yml', 'storage-dry-run.yml',
                     'storage-play-*.yml', 'group_vars/storage/**', '.github/workflows/storage.yml',
                     '.github/workflows/storage-expiry.yml', 'scripts/storage_ready.py',
                     'scripts/tests/test_storage_workflows.py', 'storage-host-metrics.yml', 'storage-preflight.yml',
                     'scripts/tests/test_storage_vm_role.py', 'scripts/tests/test_storage_reconcile.py'):
            self.assertIn(path, ignored)
        for path in ('roles/k3s_common/**', 'k3s.yml', 'tasks/lab-guard.yml', 'scripts/tests/test_storage_fencer.py'):
            self.assertNotIn(path, ignored)


if __name__ == '__main__':
    unittest.main()

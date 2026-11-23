"""Run scripts/maintenance_lock_op.sh as a real process against a temp root and the sibling helper."""
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
OP = SCRIPTS / 'maintenance_lock_op.sh'
INSTANCE = '0b5e1c2a-7d3f-4e8a-9c61-2f4d8e7a1b90'  # fixture, not VM300
# Unique per test process: the script scans every process env for the owner.
OWNER = 'Starktastic-Homelab/terraform/%d/1' % os.getpid()
OTHER = 'Starktastic-Homelab/ansible/999/1'
KEYS = ['created_at', 'instance', 'nonce', 'operation', 'owner', 'schema', 'stage']

# Holds a lock in a real process, prints "ready", then waits to be killed.
GUARD_HOLDER = '''import fcntl,os,sys,time
fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT,0o660);fcntl.flock(fd,fcntl.LOCK_EX)
print("ready",flush=True);time.sleep(60)
'''
# Owner and nonce come in argv: a process whose env names the owner makes the script refuse first.
EXECUTION_HOLDER = '''import sys,time;from pathlib import Path
sys.path.insert(0,sys.argv[1]);from maintenance_lock import execution
with execution(Path(sys.argv[2]),sys.argv[3],sys.argv[4],sys.argv[5]):
    print("ready",flush=True);time.sleep(60)
'''
# Wraps the real helper: after a successful release, another owner acquires at once.
RACING_HELPER = '''import os,subprocess,sys;from pathlib import Path
real=str(Path(__file__).parent/"real_lock.py")
rc=subprocess.run([sys.executable,"-I",real]+sys.argv[1:]).returncode
if rc==0 and sys.argv[1]=="release":
    env=dict(os.environ,MAINTENANCE_OWNER="%s")
    subprocess.run([sys.executable,"-I",real,"acquire","--root",sys.argv[3],"--operation","ansible-deploy"],
                   env=env,stdout=subprocess.DEVNULL,check=True)
sys.exit(rc)
''' % OTHER
# Wraps the real helper: refuses (99) if the nonce is in argv or python runs without -I.
ARGV_HELPER = '''import os,subprocess,sys;from pathlib import Path
if not sys.flags.isolated or any(os.environ["MAINTENANCE_NONCE"] in a for a in sys.argv):
    sys.exit(99)
real=str(Path(__file__).parent/"real_lock.py")
sys.exit(subprocess.run([sys.executable,"-I",real]+sys.argv[1:]).returncode)
'''


@unittest.skipIf(os.geteuid() == 0, 'as root the script ignores the test root; never run it against the real one')
class LockOpTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / 'maintenance'
        self.root.mkdir()
        (self.root / 'runner-instance').write_text(INSTANCE + '\n')
        (self.tmp / 'product_uuid').write_text(INSTANCE.upper() + '\n')  # DMI prints upper case
        self.stage = self.tmp / 'lock-release'
        self.stage.mkdir()
        shutil.copy(OP, self.stage)
        shutil.copy(SCRIPTS / 'maintenance_lock.py', self.stage)
        self.worker = 'lockop%d' % (os.getpid() % 1000000)
        self.nonce = secrets.token_hex(32)
        self.env = {'PATH': os.environ['PATH'], 'LOCK_OP_TEST_ROOT': str(self.root),
                    'LOCK_OP_TEST_INSTANCE_FILE': str(self.tmp / 'product_uuid'),
                    'LOCK_OP_TEST_WORKER': self.worker}
        self.procs = []
        self.addCleanup(self.stop_procs)

    def stop_procs(self):
        for p in self.procs:
            p.kill()
            p.communicate()

    def write_record(self, stage='acquired', owner=OWNER, **changes):
        # Frozen schema-1 record as the PIN helper CI runs writes it (sorted keys, 0660).
        record = {'created_at': '2026-11-06T01:30:00.000000+00:00', 'instance': INSTANCE,
                  'nonce': self.nonce, 'operation': 'ansible-deploy', 'owner': owner,
                  'schema': 1, 'stage': stage, **changes}
        path = self.root / 'operation.json'
        path.write_text(json.dumps(record, sort_keys=True) + '\n')
        path.chmod(0o660)
        return path.read_bytes()

    def record(self):
        return json.loads((self.root / 'operation.json').read_text())

    def run_op(self, *args, timeout=None, env=None, prefix=(), shell=('bash',)):
        cmd = list(prefix) + (['timeout', str(timeout)] if timeout else []) + \
            [*shell, str(self.stage / 'maintenance_lock_op.sh'), *args]
        result = subprocess.run(cmd, env=env or self.env, capture_output=True, text=True, timeout=30)
        output = result.stdout + result.stderr
        self.assertNotIn(self.nonce, output)
        return result.returncode, output

    def hold(self, code, *args, env=None):
        p = subprocess.Popen([sys.executable, '-c', code, *args], stdout=subprocess.PIPE, text=True, env=env)
        self.procs.append(p)
        self.assertEqual(p.stdout.readline().strip(), 'ready')
        return p

    def test_verify_changes_nothing(self):
        before = self.write_record()
        rc, out = self.run_op(OWNER, 'acquired', 'verify')
        self.assertEqual(rc, 0, out)
        self.assertIn('verify ok: %s at acquired' % OWNER, out)
        self.assertEqual((self.root / 'operation.json').read_bytes(), before)
        self.assertFalse((self.root / 'execution.guard').exists())

    def test_refusals_leave_the_record(self):
        before = self.write_record()
        cases = [((OTHER, 'acquired', 'verify'), 3, 'OWNER MISMATCH'),
                 ((OWNER, 'applying', 'release'), 1, 'Maintenance stage mismatch'),
                 ((OWNER, 'bogus', 'release'), 1, 'Maintenance stage mismatch'),
                 ((OWNER, '', 'release'), 1, 'Maintenance stage mismatch'),
                 (('claude-code/csi-lab/1', 'acquired', 'release'), 2, 'NOT A CI OWNER'),
                 (('lab/x/1/1x', 'acquired', 'release'), 2, 'NOT A CI OWNER'),
                 ((OWNER, 'acquired', 'nuke'), 2, 'unknown action'),
                 ((OWNER, 'acquired'), 2, 'usage'),
                 ((OWNER, 'held', 'release'), 5, 'HELD BY SOMEONE ELSE')]
        for args, code, text in cases:
            with self.subTest(args=args):
                rc, out = self.run_op(*args)
                self.assertEqual(rc, code, out)
                self.assertIn(text, out)
                self.assertEqual((self.root / 'operation.json').read_bytes(), before)

    def test_held_record_without_own_marker(self):
        before = self.write_record(stage='held')
        for stage in ['held', '']:  # an empty stage must not slip past the marker check
            with self.subTest(stage=stage):
                rc, out = self.run_op(OWNER, stage, 'release')
                self.assertIn(rc, (1, 5), out)
                self.assertEqual((self.root / 'operation.json').read_bytes(), before)

    def test_person_held_lock_is_refused(self):
        before = self.write_record(owner='claude-code/csi-lab/1')
        rc, out = self.run_op('claude-code/csi-lab/1', 'acquired', 'release')
        self.assertEqual(rc, 2, out)
        self.assertIn('NOT A CI OWNER', out)
        self.assertEqual((self.root / 'operation.json').read_bytes(), before)

    def test_no_hooks_without_root_never_reports_no_lock(self):
        # Not root and no test root: the real root may be unreadable, so "no file" proves nothing.
        rc, out = self.run_op(OWNER, 'acquired', 'release', env={'PATH': os.environ['PATH']})
        self.assertNotIn(rc, (0, 6), out)
        self.assertNotIn('NO LOCK', out)

    def test_owner_process_alive(self):
        before = self.write_record()
        # A job step can outlive Runner.Worker; its env still names the owner.
        self.procs.append(subprocess.Popen(['sleep', '60'], env=dict(os.environ, MAINTENANCE_OWNER=OWNER)))
        rc, out = self.run_op(OWNER, 'acquired', 'release')
        self.assertEqual(rc, 4, out)
        self.assertIn('OWNER PROCESS ALIVE', out)
        self.assertEqual((self.root / 'operation.json').read_bytes(), before)

    def test_xtrace_never_prints_the_nonce(self):
        self.write_record()
        rc, out = self.run_op(OWNER, 'acquired', 'verify', shell=('bash', '-x'))  # run_op asserts no nonce
        self.assertEqual(rc, 0, out)
        rc, out = self.run_op(OWNER, 'acquired', 'verify', env=dict(self.env, SHELLOPTS='xtrace'))
        self.assertEqual(rc, 0, out)

    def test_dangling_symlink_is_not_no_lock(self):
        (self.root / 'operation.json').symlink_to(self.tmp / 'missing')
        rc, out = self.run_op(OWNER, 'acquired', 'release')
        self.assertNotIn(rc, (0, 6), out)
        self.assertNotIn('NO LOCK', out)
        self.assertTrue((self.root / 'operation.json').is_symlink())

    def test_nonce_never_in_argv_and_python_isolated(self):
        self.write_record()
        (self.stage / 'maintenance_lock.py').rename(self.stage / 'real_lock.py')
        (self.stage / 'maintenance_lock.py').write_text(ARGV_HELPER)
        for stage, action in [('acquired', 'verify'), ('acquired', 'hold'), ('held', 'release')]:
            with self.subTest(action=action):
                rc, out = self.run_op(OWNER, stage, action)
                self.assertEqual(rc, 0, out)
        self.assertFalse((self.root / 'operation.json').exists())

    def test_no_lock(self):
        rc, out = self.run_op(OWNER, 'acquired', 'release')
        self.assertEqual(rc, 6, out)
        self.assertIn('NO LOCK', out)

    def test_runner_job_alive(self):
        self.write_record()
        worker = self.tmp / self.worker  # a real process whose name pgrep -x matches
        shutil.copy(shutil.which('sleep'), worker)
        self.procs.append(subprocess.Popen([str(worker), '60']))
        rc, out = self.run_op(OWNER, 'acquired', 'release')
        self.assertEqual(rc, 4, out)
        self.assertIn('RUNNER JOB ALIVE', out)
        self.assertTrue((self.root / 'operation.json').exists())

    def test_broken_pgrep_refuses(self):
        before = self.write_record()
        bin_dir = self.tmp / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'pgrep').write_text('#!/bin/sh\nexit 2\n')
        (bin_dir / 'pgrep').chmod(0o700)
        env = dict(self.env, PATH='%s:%s' % (bin_dir, self.env['PATH']))
        rc, out = self.run_op(OWNER, 'acquired', 'release', env=env)
        self.assertEqual(rc, 1, out)
        self.assertIn('pgrep failed', out)
        self.assertEqual((self.root / 'operation.json').read_bytes(), before)

    def test_corrupt_record_is_never_removed(self):
        good = self.write_record()
        nonced = [json.dumps(dict(json.loads(good), **change)).encode() for change in
                  [{'schema': 2}, {'instance': 'ffffffff-0000-0000-0000-000000000000'}, {'owner': ''}]]
        nonced += [good + b'garbage', good[:-10], json.dumps({'nonce': self.nonce, 'owner': OWNER}).encode()]
        for contents in [b'', b'{', b'{}', b'{"owner": "%s"}\n' % OWNER.encode()] + nonced:
            with self.subTest(contents=contents):
                (self.root / 'operation.json').write_bytes(contents)
                rc, out = self.run_op(OWNER, 'acquired', 'release')
                self.assertNotEqual(rc, 0, out)
                self.assertNotIn('released', out)
                self.assertEqual((self.root / 'operation.json').read_bytes(), contents)

    def test_instance_mismatch(self):
        before = self.write_record()
        (self.tmp / 'product_uuid').write_text('ffffffff-0000-0000-0000-000000000000\n')
        rc, out = self.run_op(OWNER, 'acquired', 'release')
        self.assertEqual(rc, 1, out)
        self.assertIn('Missing or mismatched runner instance', out)
        self.assertEqual((self.root / 'operation.json').read_bytes(), before)

    def test_release_from_acquired(self):
        self.write_record()
        rc, out = self.run_op(OWNER, 'acquired', 'release')
        self.assertEqual(rc, 0, out)
        self.assertIn('released: operation.json gone', out)
        self.assertFalse((self.root / 'operation.json').exists())
        for name in ['runner-instance', 'guard']:
            self.assertTrue((self.root / name).exists())

    def test_hold_then_release_at_held(self):
        self.write_record()
        rc, out = self.run_op(OWNER, 'acquired', 'hold')
        self.assertEqual(rc, 0, out)
        self.assertIn('advanced to held', out)
        record = self.record()
        self.assertEqual(sorted(record), KEYS)
        self.assertEqual((record['stage'], record['owner'], record['nonce']), ('held', OWNER, self.nonce))
        self.assertEqual((self.root / 'operation.json').stat().st_mode & 0o777, 0o660)
        self.assertTrue((self.stage / 'held-by-me').exists())
        self.assertEqual(self.run_op(OWNER, 'held', 'verify')[0], 0)
        rc, out = self.run_op(OWNER, 'held', 'release')
        self.assertEqual(rc, 0, out)
        self.assertIn('released: operation.json gone', out)
        self.assertFalse((self.stage / 'held-by-me').exists())
        rc, out = self.run_op(OWNER, 'held', 'release')
        self.assertEqual(rc, 6, out)
        self.assertIn('NO LOCK', out)

    def test_guard_held_times_out_without_change(self):
        before = self.write_record()
        holder = self.hold(GUARD_HOLDER, str(self.root / 'guard'))
        rc, out = self.run_op(OWNER, 'acquired', 'release', timeout=2)
        self.assertEqual(rc, 124, out)
        self.assertEqual((self.root / 'operation.json').read_bytes(), before)
        for _ in range(20):  # a signalled helper may take a moment to exit
            left = subprocess.run(['pgrep', '-f', str(self.stage)], capture_output=True)
            if left.returncode == 1:
                break
            time.sleep(0.1)
        self.assertEqual(left.returncode, 1, left.stdout)
        holder.kill()
        holder.wait()
        self.assertEqual(self.run_op(OWNER, 'acquired', 'release')[0], 0)

    def hold_execution(self, stage):
        env = dict(os.environ, HOMELAB_RUNNER_INSTANCE=INSTANCE)
        return self.hold(EXECUTION_HOLDER, str(self.stage), str(self.root), OWNER, self.nonce, stage, env=env)

    def test_release_refused_while_execution_is_held(self):
        before = self.write_record()
        holder = self.hold_execution('acquired')
        for action in ['release', 'hold']:
            with self.subTest(action=action):
                rc, out = self.run_op(OWNER, 'acquired', action)
                self.assertEqual(rc, 1, out)
                self.assertIn('Maintenance execution is busy', out)
                self.assertEqual((self.root / 'operation.json').read_bytes(), before)
                self.assertFalse((self.stage / 'held-by-me').exists())  # no marker for a refused hold
        self.assertEqual(self.run_op(OWNER, 'acquired', 'verify')[0], 0)
        holder.kill()
        holder.wait()
        self.assertEqual(self.run_op(OWNER, 'acquired', 'hold')[0], 0)
        held = (self.root / 'operation.json').read_bytes()
        self.hold_execution('held')
        rc, out = self.run_op(OWNER, 'held', 'release')
        self.assertEqual(rc, 1, out)
        self.assertEqual((self.root / 'operation.json').read_bytes(), held)
        self.assertTrue((self.stage / 'held-by-me').exists())  # a refused release keeps the marker

    def test_new_record_right_after_release(self):
        self.write_record()
        (self.stage / 'maintenance_lock.py').rename(self.stage / 'real_lock.py')
        (self.stage / 'maintenance_lock.py').write_text(RACING_HELPER)
        rc, out = self.run_op(OWNER, 'acquired', 'release')
        self.assertEqual(rc, 0, out)
        self.assertIn('a NEW record exists now', out)
        self.assertEqual(self.record()['owner'], OTHER)

    def test_test_hooks_are_ignored_as_root(self):
        if subprocess.run(['unshare', '-r', 'true'], capture_output=True).returncode != 0:
            self.skipTest('unprivileged user namespaces unavailable')
        before = self.write_record()
        # Namespace root: the script must use the real root and worker name, never the hooks.
        rc, out = self.run_op(OWNER, 'acquired', 'verify', prefix=['unshare', '-r'])
        # 6: no real root here; 4: a hosted CI runner's own Runner.Worker is alive.
        self.assertIn(rc, (4, 6), out)
        self.assertNotIn('verify ok', out)
        self.assertEqual((self.root / 'operation.json').read_bytes(), before)


class LockOpSourceTests(unittest.TestCase):
    def test_production_values_match_their_sources(self):
        script = OP.read_text()
        defaults = (REPO / 'roles/maintenance_runner/defaults/main.yml').read_text()
        root = re.search(r'^maintenance_runner_root: (\S+)$', defaults, re.M).group(1)
        instance = re.search(r'^maintenance_runner_instance: (\S+)$', defaults, re.M).group(1)
        self.assertIn('\nR=%s\n' % root, script)
        # The script derives the instance the way the role checks it: DMI product_uuid, lowercased.
        self.assertIn('\nI=/sys/class/dmi/id/product_uuid\n', script)
        tasks = (REPO / 'roles/maintenance_runner/tasks/main.yml').read_text()
        self.assertIn('ansible_facts.product_uuid | lower == maintenance_runner_instance', tasks)
        # CI writes this literal into every record; the record must match the derived instance.
        deploy = (REPO / '.github/workflows/deploy.yml').read_text()
        self.assertIn('HOMELAB_RUNNER_INSTANCE: %s\n' % instance, deploy)
        self.assertNotIn(instance, script)
        # The CI owner shape the script requires is the one deploy.yml writes.
        self.assertIn('MAINTENANCE_OWNER: ${{ github.repository }}/${{ github.run_id }}/${{ github.run_attempt }}\n',
                      deploy)
        self.assertIn('^[^/]+/[^/]+/[0-9]+/[0-9]+$', script)

    def test_hooks_and_path_only_where_they_belong(self):
        script = OP.read_text()
        branch = re.search(r'\nif \[ "\$EUID" -eq 0 \]; then\n(.*?)\nelse\n(.*?)\nfi\n', script, re.S)
        as_root, not_root = branch.groups()
        self.assertEqual(as_root.strip(), 'PATH=/usr/sbin:/usr/bin:/sbin:/bin')
        self.assertEqual(script.count('LOCK_OP_TEST_'), not_root.count('LOCK_OP_TEST_'))
        self.assertEqual(script.count('PATH='), 1)


if __name__ == '__main__':
    unittest.main()

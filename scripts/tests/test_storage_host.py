"""host.run time limits (abandon, never kill; one live child per key) and the bounded Flock (ADR 0017 F11)."""
import contextlib
import fcntl
import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'roles/storage_vm/files'))
from homelab_storage import host  # noqa: E402


class RunTimeoutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.addCleanup(self._kill_leftovers)
        self.err = io.StringIO()
        r = contextlib.redirect_stderr(self.err)
        r.__enter__()
        self.addCleanup(r.__exit__, None, None, None)

    def _kill_leftovers(self):
        for p in host._ABANDONED.values():
            with contextlib.suppress(ProcessLookupError):
                p.kill()
            p.wait()
        host._ABANDONED.clear()

    def abandon(self, argv, key):
        with self.assertRaises(subprocess.TimeoutExpired):
            host.run(argv, timeout=0.2, key=key)

    def test_timeout_abandons_child(self):
        self.abandon(['sleep', '5'], 'regen')
        os.kill(host._ABANDONED['regen'].pid, 0)  # still alive: not killed
        self.assertIn('gave no answer in 0.2 s; left running', self.err.getvalue())

    def test_abandoned_child_own_session(self):
        self.abandon(['sleep', '5'], 'regen')  # a hangup or group signal at the caller's exit never reaches it
        self.assertNotEqual(os.getsid(host._ABANDONED['regen'].pid), os.getsid(0))

    def test_parent_holds_no_output_fd(self):
        before = set(os.listdir('/proc/self/fd'))
        self.abandon(['sleep', '5'], 'k')
        self.assertEqual(set(os.listdir('/proc/self/fd')), before)

    def test_live_key_raises_without_child(self):
        self.abandon(['sleep', '5'], 'regen')
        with mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('started a child')):
            with self.assertRaises(subprocess.TimeoutExpired):
                host.run(['vgs', 'regen'], timeout=30, key='regen')
        self.assertIn('not started: an earlier command for regen is still running', self.err.getvalue())

    def test_other_key_unaffected(self):
        self.abandon(['sleep', '5'], 'regen')
        self.assertEqual(host.run(['true'], timeout=5, key='retained').returncode, 0)

    def test_late_failure_is_logged_on_reap(self):
        self.abandon(['sh', '-c', 'sleep 0.4; exit 5'], 'regen')
        time.sleep(0.7)
        self.assertEqual(host.run(['true'], timeout=5, key='regen').returncode, 0)  # reaped; key free
        self.assertIn('exited 5 after it was abandoned', self.err.getvalue())
        self.assertNotIn('regen', host._ABANDONED)

    def test_output_and_check_with_timeout(self):
        r = host.run(['sh', '-c', 'echo out; echo err >&2'], timeout=5)
        self.assertEqual((r.stdout, r.stderr), ('out\n', 'err\n'))
        with self.assertRaises(subprocess.CalledProcessError):
            host.run(['sh', '-c', 'exit 3'], timeout=5)

    def test_no_timeout_unchanged(self):
        self.assertEqual(host.run(['sh', '-c', 'echo hi']).stdout, 'hi\n')


class FlockTest(unittest.TestCase):
    def test_flock_deadline(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, 'lock')
            holder = os.open(path, os.O_RDWR | os.O_CREAT)
            self.addCleanup(os.close, holder)
            fcntl.flock(holder, fcntl.LOCK_EX)
            t = time.monotonic()
            with self.assertRaises(TimeoutError):
                with host.Flock(path, timeout=0.3):
                    pass
            self.assertLess(time.monotonic() - t, 2)

    def test_flock_free(self):
        with tempfile.TemporaryDirectory() as d:
            with host.Flock(Path(d, 'lock'), timeout=1):
                pass


if __name__ == '__main__':
    unittest.main()

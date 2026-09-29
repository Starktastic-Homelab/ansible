"""Execution exclusion uses real processes and temporary ownership records."""
import multiprocessing as mp
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import maintenance_lock as lock

INSTANCE = 'test-runner'
CTX = mp.get_context('spawn')


def execute(root, nonce, start, finish, connection):
    start.wait(5)
    try:
        with lock.execution(Path(root), 'owner', nonce, 'acquired'):
            connection.send('entered')
            if not finish.wait(5):
                raise TimeoutError('test did not release holder')
            lock.verify(Path(root), 'owner', nonce)
    except lock.ExecutionBusy:
        connection.send('busy')
    except FileNotFoundError:
        connection.send('released')
    finally:
        connection.close()


def release(root, nonce, start, finish, connection):
    start.wait(5)
    try:
        lock.release(Path(root), 'owner', nonce)
        connection.send('released')
    except lock.ExecutionBusy:
        connection.send('busy')
    finally:
        connection.close()


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)/'maintenance'
        self.root.mkdir()
        (self.root/'runner-instance').write_text(INSTANCE)
        env = patch.dict(os.environ, HOMELAB_RUNNER_INSTANCE=INSTANCE)
        env.start(); self.addCleanup(env.stop)
        self.nonce = lock.acquire(self.root, 'migration', 'owner')
        self.original = (self.root/'operation.json').read_bytes()

    def context(self, **changes):
        args = dict(root=self.root, owner='owner', nonce=self.nonce, stage='acquired')
        args.update(changes)
        return lock.execution(**args)

    def compete(self, targets):
        start, finish = CTX.Event(), CTX.Event()
        children, readers = [], []
        try:
            for target in targets:
                reader, writer = CTX.Pipe(duplex=False)
                child = CTX.Process(target=target, args=(str(self.root), self.nonce, start, finish, writer))
                child.start(); writer.close()
                children.append(child); readers.append(reader)
            start.set()
            results = []
            for reader in readers:
                self.assertTrue(reader.poll(8), 'contender blocked')
                results.append(reader.recv())
            return results
        finally:
            finish.set()
            for child in children:
                child.join(8)
                if child.is_alive():
                    child.kill(); child.join(5)
                self.assertEqual(child.exitcode, 0)
                child.close()
            for reader in readers:
                reader.close()

    def test_same_owner_only_one_execution_body(self):
        self.assertCountEqual(self.compete([execute, execute]), ['entered', 'busy'])
        self.assertEqual((self.root/'operation.json').read_bytes(), self.original)

    def test_release_race_never_removes_running_ownership(self):
        results = self.compete([execute, release])
        self.assertIn(results, [['entered', 'busy'], ['released', 'released'], ['busy', 'released']])

    def test_verify_inside_execution_and_transitions_after_exit(self):
        with self.context() as fd:
            self.assertFalse(os.get_inheritable(fd))
            lock.verify(self.root, 'owner', self.nonce, 'acquired')
            for operation in [lambda: lock.release(self.root, 'owner', self.nonce),
                              lambda: lock.advance(self.root, 'owner', self.nonce, 'acquired', 'held'),
                              lambda: lock.acquire(self.root, 'other', 'new-owner')]:
                with self.assertRaises(lock.ExecutionBusy):
                    operation()
            self.assertEqual((self.root/'operation.json').read_bytes(), self.original)
        lock.advance(self.root, 'owner', self.nonce, 'acquired', 'held')
        lock.release(self.root, 'owner', self.nonce)
        self.assertFalse((self.root/'operation.json').exists())

    def test_wrong_identity_never_enters_body(self):
        for changes, error in [({'owner': 'other'}, PermissionError), ({'nonce': 'wrong'}, PermissionError),
                               ({'stage': 'held'}, ValueError), ({'stage': None}, ValueError), ({'stage': ''}, ValueError)]:
            with self.assertRaises(error), self.context(**changes):
                self.fail('entered with invalid identity')
        with self.context():
            pass

    def test_failed_body_releases_execution_not_ownership(self):
        with self.assertRaises(RuntimeError), self.context():
            raise RuntimeError('body failed')
        with self.context():
            pass
        self.assertEqual((self.root/'operation.json').read_bytes(), self.original)
        with self.assertRaises(FileExistsError):
            lock.acquire(self.root, 'other', 'new-owner')

    def test_missing_corrupt_or_wrong_runner_refused(self):
        path = self.root/'operation.json'
        for text in ['', '{', '{}']:
            path.write_text(text)
            with self.assertRaises(ValueError), self.context():
                self.fail('entered with corrupt ownership')
        path.unlink()
        with self.assertRaises(FileNotFoundError), self.context():
            self.fail('entered with missing ownership')
        path.write_bytes(self.original)
        (self.root/'runner-instance').write_text('other')
        with self.assertRaises(ValueError), self.context():
            self.fail('entered with wrong runner')

    def test_unsafe_lock_files_and_parent_are_refused(self):
        alias = self.root.parent/'alias'; alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError), self.context(root=alias):
            self.fail('accepted symlink root')
        path = self.root/'execution.guard'
        path.unlink(missing_ok=True)
        victim = self.root/'victim'; victim.write_text('unchanged')
        for kind in ['symlink', 'fifo', 'directory']:
            if kind == 'symlink': path.symlink_to(victim)
            elif kind == 'fifo': os.mkfifo(path)
            else: path.mkdir()
            try:
                with self.assertRaises((OSError, ValueError)), self.context():
                    self.fail('accepted unsafe lock')
            finally:
                if kind == 'directory': path.rmdir()
                else: path.unlink()
        self.assertEqual(victim.read_text(), 'unchanged')
        self.assertEqual((self.root/'operation.json').read_bytes(), self.original)

    def test_restrictive_umask_keeps_shared_lock_and_inode(self):
        old = os.umask(0o077)
        try:
            with self.context():
                info = (self.root/'execution.guard').stat()
                self.assertEqual(info.st_mode & 0o777, 0o660)
            with self.context():
                self.assertEqual((self.root/'execution.guard').stat().st_ino, info.st_ino)
        finally:
            os.umask(old)


if __name__ == '__main__':
    unittest.main()

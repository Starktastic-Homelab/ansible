"""The one place that touches the host: subprocesses, the LIO flock and lab checkpoints. Tests patch these."""
import fcntl
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CONFIG = {}  # the loaded config.json; set by the CLI before any checkpoint


def log(message):
    print(message, file=sys.stderr, flush=True)  # stdout carries the JSON report


_ABANDONED = {}  # key -> Popen of a timed-out child, left running (ADR 0017 F11); reaped by later calls


def _reap():
    for k, p in list(_ABANDONED.items()):
        if (rc := p.poll()) is not None:
            del _ABANDONED[k]
            cmd = ' '.join(p.args)
            log(f'{cmd} (pid {p.pid}) exited {rc} after it was abandoned' if rc else
                f'{cmd} (pid {p.pid}) finished after it was abandoned')


def run(argv, check=True, timeout=None, key=None):
    """With a timeout, a child that gives no answer is left running, never killed: a killed lvextend can leave its
    LV suspended. While it lives, a call with the same key raises TimeoutExpired at once and starts nothing."""
    _reap()
    if key is not None and key in _ABANDONED:
        log(f'{" ".join(argv)} not started: an earlier command for {key} is still running')
        raise subprocess.TimeoutExpired(argv, 0)
    log('$ ' + ' '.join(argv))
    # stdin=/dev/null: under Ansible it is a pty, so blkdiscard refuses a signature and an LVM prompt would hang.
    if timeout is None:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    else:
        # Files, not pipes: an abandoned child never blocks on a full pipe or dies on a closed one. Its own session:
        # a hangup or group signal when an Ansible task's shell or pty exits never reaches it.
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err, close_fds=True,
                                 start_new_session=True)
            try:
                rc = p.wait(timeout)
            except subprocess.TimeoutExpired:
                _ABANDONED[key if key is not None else id(p)] = p
                log(f'{argv[0]} gave no answer in {timeout} s; left running (pid {p.pid})')
                raise
            out.seek(0)
            err.seek(0)
            result = subprocess.CompletedProcess(argv, rc, out.read().decode(errors='replace'),
                                                 err.read().decode(errors='replace'))
    if check and result.returncode:
        log(f'{argv[0]} exited {result.returncode}: {result.stderr.strip()[-500:]}')
        raise subprocess.CalledProcessError(result.returncode, argv, result.stdout, result.stderr)
    return result


class Flock:
    """Exclusive flock on `path`, shared by the reconcile, the fencer and the boot check."""

    def __init__(self, path, timeout=120):
        self.path, self.timeout = Path(path), timeout
        self._fd = None

    def __enter__(self):
        """Gives up after `timeout` s (TimeoutError, an OSError): a holder stuck on a stalled disk never hangs us."""
        self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(self._fd)
                    self._fd = None
                    raise TimeoutError(f'{self.path}: held for over {self.timeout} s')
                time.sleep(0.2)

    def __exit__(self, *exc):
        os.close(self._fd)  # closing releases the flock
        self._fd = None


LIO_LOCK = Flock('/run/lock/storage-lio')


def checkpoint(name):
    """Lab only: stop this process at a named step so a test can kill or inspect it there."""
    if name in CONFIG.get('pause_after', ()):
        log(f'PAUSED at {name} pid={os.getpid()}')
        sys.stdout.flush()
        os.kill(os.getpid(), signal.SIGSTOP)

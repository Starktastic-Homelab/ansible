"""The one place that touches the host: subprocesses, the LIO flock and lab checkpoints. Tests patch these."""
import fcntl
import os
import signal
import subprocess
import sys
from pathlib import Path

CONFIG = {}  # the loaded config.json; set by the CLI before any checkpoint


def log(message):
    print(message, file=sys.stderr, flush=True)  # stdout carries the JSON report


def run(argv, check=True):
    log('$ ' + ' '.join(argv))
    result = subprocess.run(argv, capture_output=True, text=True)
    if check and result.returncode:
        log(f'{argv[0]} exited {result.returncode}: {result.stderr.strip()[-500:]}')
        raise subprocess.CalledProcessError(result.returncode, argv, result.stdout, result.stderr)
    return result


class Flock:
    """Exclusive flock on `path`, shared by the reconcile, the fencer and the boot check."""

    def __init__(self, path):
        self.path = Path(path)
        self._fd = None

    def __enter__(self):
        self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

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

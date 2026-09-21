#!/usr/bin/env python3
"""Persistent single-runner maintenance ownership. Never steal an old operation."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat


def _read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Expected a regular maintenance file')
        return stream.read()


@contextmanager
def _guard(root):
    root = Path(root)
    if root.absolute() != root.resolve() or not root.is_dir():
        raise ValueError('Maintenance directory must exist without symlink components')
    expected = os.environ.get('HOMELAB_RUNNER_INSTANCE', '')
    if not expected or _read(root / 'runner-instance').strip() != expected:
        raise ValueError('Missing or mismatched runner instance; check the host bind mount')
    # Permanent inode: flock serializes verify/update/unlink so release cannot
    # remove a newly acquired record. It is not the persistent operation lock.
    fd = os.open(root / 'guard', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o660)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('Invalid maintenance guard')
        if os.fstat(fd).st_uid == os.getuid():
            os.fchmod(fd, 0o660)
        elif os.fstat(fd).st_mode & 0o777 != 0o660:
            raise ValueError('Maintenance guard group permissions changed')
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield root
    finally:
        os.close(fd)


def _sync_dir(root):
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(path, record):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o660)
    with os.fdopen(fd, 'w') as stream:
        os.fchmod(stream.fileno(), 0o660)
        json.dump(record, stream, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    _sync_dir(path.parent)


def _record(root, owner, nonce, stage=None):
    try:
        record = json.loads(_read(root / 'operation.json'))
        if (record['schema'] != 1 or not all(isinstance(record[k], str) and record[k]
                for k in ('owner', 'nonce', 'operation', 'stage', 'created_at', 'instance'))
                or record['instance'] != os.environ['HOMELAB_RUNNER_INSTANCE']):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise ValueError('Corrupt maintenance record; inspect manually without clearing it') from None
    if record['owner'] != owner or not hmac.compare_digest(record['nonce'], nonce):
        raise PermissionError('Maintenance ownership mismatch')
    if stage is not None and record['stage'] != stage:
        raise ValueError('Maintenance stage mismatch')
    return record


def acquire(root: Path, operation: str, owner: str) -> str:
    if not all(isinstance(x, str) and re.fullmatch(r'[a-zA-Z0-9_./:-]{1,200}', x) for x in (operation, owner)):
        raise ValueError('Invalid operation or owner')
    with _guard(root) as root:
        nonce = secrets.token_hex(32)
        _write(root / 'operation.json', {'schema': 1, 'owner': owner, 'nonce': nonce,
               'operation': operation, 'stage': 'acquired',
               'instance': os.environ['HOMELAB_RUNNER_INSTANCE'],
               'created_at': datetime.now(timezone.utc).isoformat()})
        return nonce


def verify(root: Path, owner: str, nonce: str, stage=None) -> None:
    with _guard(root) as root:
        _record(root, owner, nonce, stage)


def release(root: Path, owner: str, nonce: str) -> None:
    with _guard(root) as root:
        _record(root, owner, nonce)
        (root / 'operation.json').unlink()
        _sync_dir(root)


def advance(root: Path, owner: str, nonce: str, stage: str, next_stage: str) -> None:
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', next_stage):
        raise ValueError('Invalid next stage')
    with _guard(root) as root:
        record = _record(root, owner, nonce, stage)
        record['stage'] = next_stage
        # If this write is interrupted, the original record remains held.
        temporary = root / ('stage-' + secrets.token_hex(16) + '.json')
        _write(temporary, record)
        os.replace(temporary, root / 'operation.json')
        _sync_dir(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['acquire', 'verify', 'release', 'advance'])
    parser.add_argument('--root', type=Path, default=Path('/maintenance'))
    parser.add_argument('--operation')
    parser.add_argument('--stage')
    parser.add_argument('--next-stage')
    parser.add_argument('--github-env', action='store_true')
    args = parser.parse_args()
    owner = os.environ['MAINTENANCE_OWNER']
    if args.command == 'acquire':
        nonce = acquire(args.root, args.operation, owner)
        if args.github_env:
            print('::add-mask::' + nonce)
            with open(os.environ['GITHUB_ENV'], 'a') as stream:
                stream.write('MAINTENANCE_NONCE=' + nonce + '\n')
        else:
            print(nonce)
    else:
        nonce = os.environ['MAINTENANCE_NONCE']
        if args.command == 'advance':
            if not args.stage or not args.next_stage:
                parser.error('advance requires the exact current and next stage')
            advance(args.root, owner, nonce, args.stage, args.next_stage)
        elif args.command == 'verify':
            verify(args.root, owner, nonce, args.stage)
        else:
            release(args.root, owner, nonce)


if __name__ == '__main__':
    main()

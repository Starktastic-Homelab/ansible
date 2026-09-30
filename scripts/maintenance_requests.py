"""Private immutable requests and receipts. No execution or recovery side effects."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import uuid

LIMIT = 1024**2
FIELDS = {'schema', 'request_id', 'operation', 'service', 'expected_stage', 'apps_commit', 'runtime_id'}


def request_id(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError('Invalid request identity')
    return value


def validate_request(data):
    if not isinstance(data, dict) or set(data) != FIELDS or type(data['schema']) is not int or data['schema'] != 1:
        raise ValueError('Invalid request schema')
    request_id(data['request_id'])
    if data['operation'] not in ('status', 'preflight') or data['service'] != 'jellyfin':
        raise ValueError('Unsupported operation')
    for key, pattern in [('expected_stage', r'[a-z][a-z0-9-]{0,63}'), ('apps_commit', r'[0-9a-f]{40}'), ('runtime_id', r'[0-9a-f]{64}')]:
        if not isinstance(data[key], str) or not re.fullmatch(pattern, data[key]):
            raise ValueError('Invalid request field')
    return dict(data)


def validate_credentials(data):
    if (not isinstance(data, dict) or set(data) != {'owner', 'nonce'} or
            not all(isinstance(v, str) and 0 < len(v) <= 200 for v in data.values())):
        raise ValueError('Invalid credentials')
    return dict(data)


def encoded(data):
    return (json.dumps(data, sort_keys=True, separators=(',', ':'), allow_nan=False)+'\n').encode()


def digest(data):
    return hashlib.sha256(encoded(data)).hexdigest()


@contextmanager
def directory(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Invalid directory')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            new = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = new
        yield fd
    finally:
        os.close(fd)


def _private(info, directory=False):
    if (info.st_uid != os.getuid() or info.st_mode & 0o077 or
            not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise ValueError('Expected private owned state')


@contextmanager
def request_directory(root, identity):
    request_id(identity)
    with directory(Path(root)/'requests'/identity) as fd:
        _private(os.fstat(fd), directory=True)
        yield fd


def read_json(fd, name, *, private=True):
    file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(file_fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > LIMIT:
            raise ValueError('Unsafe state file')
        if private:
            _private(info)
        raw = stream.read(LIMIT+1)
        if len(raw) > LIMIT:
            raise ValueError('State too large')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate key')
            result[key] = value
        return result
    data = json.loads(raw, object_pairs_hook=pairs)
    if not isinstance(data, dict):
        raise ValueError('Expected object')
    return data


def atomic_json(fd, name, data):
    raw = encoded(data)  # Validate serialization before touching disk.
    if len(raw) > LIMIT:
        raise ValueError('State too large')
    temporary = '.pending-'+secrets.token_hex(16)
    file_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        with os.fdopen(file_fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.link(temporary, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        os.fsync(fd)
    finally:
        os.unlink(temporary, dir_fd=fd)


def publish_request(root: Path, request: dict, credentials: dict) -> str:
    request = validate_request(request); credentials = validate_credentials(credentials)
    with directory(Path(root)/'requests') as parent:
        _private(os.fstat(parent), directory=True)
        os.mkdir(request['request_id'], mode=0o700, dir_fd=parent)
        os.fsync(parent)
        fd = os.open(request['request_id'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            os.fchmod(fd, 0o700)
            atomic_json(fd, 'request.json', request)
            atomic_json(fd, 'credentials.json', credentials)
            atomic_json(fd, 'accepted.json', dict(schema=1, request_hash=digest(request), credentials_hash=digest(credentials)))
        finally:
            os.close(fd)
    return request['request_id']


def load_request(fd):
    request = validate_request(read_json(fd, 'request.json'))
    credentials = validate_credentials(read_json(fd, 'credentials.json'))
    accepted = read_json(fd, 'accepted.json')
    if accepted != dict(schema=1, request_hash=digest(request), credentials_hash=digest(credentials)):
        raise ValueError('Invalid acceptance')
    return request, credentials


def _receipt(data, request, kind):
    fields = {'schema', 'request_id', 'request_hash', 'runtime_id', 'runner_instance', 'kind', 'at', 'exit_code'}
    if (set(data) != fields or type(data['schema']) is not int or data['schema'] != 1 or
            data['request_id'] != request['request_id'] or data['request_hash'] != digest(request) or
            data['runtime_id'] != request['runtime_id'] or data['kind'] != kind or
            not isinstance(data['runner_instance'], str) or not re.fullmatch(r'[a-zA-Z0-9-]{1,128}', data['runner_instance'])):
        raise ValueError('Receipt identity mismatch')
    timestamp = datetime.fromisoformat(data['at'])
    if timestamp.tzinfo is None or timestamp > datetime.now(timezone.utc):
        raise ValueError('Invalid receipt timestamp')
    code = data['exit_code']
    if (kind == 'started' and code is not None) or (kind == 'terminal' and (type(code) is not int or not -255 <= code <= 255)):
        raise ValueError('Invalid receipt outcome')
    return data


def _optional(fd, name):
    try:
        return read_json(fd, name)
    except FileNotFoundError:
        return None


def write_receipt(root, request, kind, runner_instance, *, exit_code):
    if kind not in ('started', 'terminal'):
        raise ValueError('Invalid receipt kind')
    with request_directory(root, request['request_id']) as fd:
        loaded, _ = load_request(fd)
        if loaded != request:
            raise ValueError('Request changed')
        data = dict(schema=1, request_id=request['request_id'], request_hash=digest(request),
                    runtime_id=request['runtime_id'], runner_instance=runner_instance, kind=kind,
                    at=datetime.now(timezone.utc).isoformat(), exit_code=exit_code)
        _receipt(data, request, kind)
        if kind == 'terminal':
            started = _receipt(read_json(fd, 'started.json'), request, 'started')
            if started['runner_instance'] != runner_instance:
                raise ValueError('Runner changed')
        atomic_json(fd, kind+'.json', data)


def inspect_request(root: Path, identity: str) -> dict:
    report = dict(schema=1, phase='unknown', diagnostic='invalid_or_incomplete_state', service_state='unobserved')
    try:
        with request_directory(root, identity) as fd:
            request, _ = load_request(fd)
            if request['request_id'] != identity:
                raise ValueError('Directory identity mismatch')
            started, terminal = _optional(fd, 'started.json'), _optional(fd, 'terminal.json')
            phase, timestamps = 'accepted', {}
            if started is not None:
                _receipt(started, request, 'started'); phase = 'running'
                timestamps['started_at'] = started['at']
            if terminal is not None:
                _receipt(terminal, request, 'terminal')
                if started is None or terminal['runner_instance'] != started['runner_instance'] or datetime.fromisoformat(terminal['at']) < datetime.fromisoformat(started['at']):
                    raise ValueError('Invalid terminal history')
                phase = 'succeeded' if terminal['exit_code'] == 0 else 'failed'
                timestamps['finished_at'] = terminal['at']
            report.update(phase=phase, diagnostic='recorded_history_only', request_id=identity,
                          operation=request['operation'], runtime_id=request['runtime_id'], apps_commit=request['apps_commit'], **timestamps)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        pass
    return report

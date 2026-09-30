"""Fixed read-only dispatcher under external ownership; no automatic replay."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import signal
import re
import zipfile
import stat
import subprocess
import sys
import uuid

from maintenance_lock import execution, ExecutionBusy
from maintenance_requests import (atomic_json, digest, directory, encoded, inspect_request,
    load_request, publish_request, read_json, request_directory, request_id, validate_request, write_receipt)

ADAPTER = 'apps/scripts/storage/supervised_readonly.py'
HELPERS = tuple('ansible/scripts/'+name for name in ('maintenance_executor.py', 'maintenance_requests.py', 'maintenance_lock.py'))


def file_hash(path):
    with directory(Path(path).parent) as fd:
        file_fd = os.open(Path(path).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('Nonregular runtime file')
            return hashlib.file_digest(stream, 'sha256').hexdigest()


def runtime_files(release):
    """Exact source/dependency inventory for new releases; never follow links."""
    release = Path(release)
    files = {str(p.relative_to(release)) for area in ('apps/scripts/storage', 'ansible/scripts')
             for p in (release/area).rglob('*.py')}
    files.add('apps/scripts/storage/requirements.txt')
    dependencies = release/'dependencies'
    if dependencies.exists() or dependencies.is_symlink():
        if dependencies.is_symlink() or not dependencies.is_dir():
            raise ValueError('Unsafe dependency directory')
        for path in dependencies.rglob('*'):
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                raise ValueError('Unsafe dependency entry')
            if path.is_file(): files.add(str(path.relative_to(release)))
        if not (dependencies/'bin/kubectl').is_file() or not (dependencies/'python').is_dir():
            raise ValueError('Incomplete dependency bundle')
    return sorted(files)


def build_manifest(release, root, instance, apps_commit):
    """Installation-time only. Runtime IDs are SHA-256 of this canonical manifest."""
    release = Path(release)
    files = runtime_files(release)
    if (release/'dependencies').exists():
        requirement = (release/'apps/scripts/storage/requirements.txt').read_text().strip()
        pin = re.fullmatch(r'websocket-client==([0-9]+(?:\.[0-9]+){2})', requirement)
        wheels = list((release/'dependencies/python').glob('*.whl'))
        if pin is None or len(wheels) != 1 or wheels[0].name != 'websocket_client-'+pin[1]+'-py3-none-any.whl':
            raise ValueError('Incomplete or unpinned transport wheel')
        distributions = list(importlib.metadata.distributions(path=[str(wheels[0])]))
        if len(distributions) != 1 or distributions[0].version != pin[1] or distributions[0].metadata['Name'] != 'websocket-client':
            raise ValueError('Transport metadata differs from requirement')
        with zipfile.ZipFile(wheels[0]) as wheel:
            if 'websocket/__init__.py' not in wheel.namelist():
                raise ValueError('Transport module absent')
    if not {ADAPTER, *HELPERS}.issubset(files):
        raise ValueError('Missing runtime sources')
    return dict(schema=2, state_root=str(root), runner_instance=instance, apps_commit=apps_commit,
                python=str(Path(sys.executable).resolve()), python_sha256=file_hash(Path(sys.executable).resolve()),
                python_version=list(sys.version_info[:3]), files={name: file_hash(release/name) for name in files})


def manifest(path):
    path = Path(path)
    with directory(path.parent) as fd:
        data = read_json(fd, path.name, private=False)
    fields = {'schema', 'state_root', 'runner_instance', 'apps_commit', 'python', 'python_sha256', 'python_version', 'files'}
    if set(data) != fields or type(data['schema']) is not int or data['schema'] not in (1, 2):
        raise ValueError('Invalid runtime manifest')
    if not isinstance(data['files'], dict) or not {ADAPTER, *HELPERS}.issubset(data['files']):
        raise ValueError('Incomplete runtime manifest')
    if data['schema'] == 2 and set(data['files']) != set(runtime_files(path.parent)):
        raise ValueError('Runtime inventory drift')
    if (data['python'] != str(Path(sys.executable).resolve()) or data['python_version'] != list(sys.version_info[:3]) or
            file_hash(data['python']) != data['python_sha256']):
        raise ValueError('Interpreter drift')
    for name, expected in data['files'].items():
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts or file_hash(path.parent/relative) != expected:
            raise ValueError('Runtime source drift')
    with directory(data['state_root']):
        pass
    return data


def _bound(data, request):
    if request['runtime_id'] != digest(data) or request['apps_commit'] != data['apps_commit']:
        raise ValueError('Runtime/request mismatch')


def _failure(root, identity, code):
    try:
        with request_directory(root, identity) as fd:
            atomic_json(fd, 'attempt-'+str(uuid.uuid4())+'.json',
                        dict(schema=1, diagnostic=code, at=datetime.now(timezone.utc).isoformat()))
    except (OSError, ValueError):
        pass  # Failure to record a diagnostic is never completion evidence.


def run(manifest_path, identity):
    root = None
    try:
        request_id(identity)
        data = manifest(manifest_path); root = Path(data['state_root'])
        with request_directory(root, identity) as fd:
            request, credentials = load_request(fd)
        if request['request_id'] != identity:
            raise ValueError('Request identity mismatch')
        _bound(data, request)
        # Fixed installation identity, not caller-controlled environment.
        os.environ['HOMELAB_RUNNER_INSTANCE'] = data['runner_instance']
        with execution(root, credentials['owner'], credentials['nonce'], request['expected_stage']) as lock_fd:
            with request_directory(root, identity) as fd:
                fresh, fresh_credentials = load_request(fd)
                if fresh != request or fresh_credentials != credentials:
                    raise ValueError('Request changed')
                if inspect_request(root, identity)['phase'] != 'accepted':
                    raise ValueError('Already started or incomplete')
                # Recheck source identity after waiting for ownership exclusion.
                if manifest(manifest_path) != data:
                    raise ValueError('Runtime changed')
                write_receipt(root, request, 'started', data['runner_instance'], exit_code=None)
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                with os.fdopen(os.open('stdout.log', flags, 0o600, dir_fd=fd), 'wb') as output, \
                        os.fdopen(os.open('stderr.log', flags, 0o600, dir_fd=fd), 'wb') as error:
                    os.fchmod(output.fileno(), 0o600); os.fchmod(error.fileno(), 0o600)
                    argv = [data['python'], '-B', '-E', '-s', str(Path(manifest_path).parent/ADAPTER),
                            request['operation'], '--state-root', str(root), '--service', request['service']]
                    if data['schema'] == 2:
                        release = Path(manifest_path).parent
                        argv += ['--helper-directory', str(release/'ansible/scripts')]
                        if (release/'dependencies').exists():
                            argv += ['--dependency-directory', str(release/'dependencies')]
                    env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
                    with subprocess.Popen(argv, env=env, stdout=output, stderr=error,
                                          pass_fds=(lock_fd,), start_new_session=True) as child:
                        code = child.wait()
                    # No success while an unexpected descendant still occupies the group.
                    try:
                        os.killpg(child.pid, 0)
                    except ProcessLookupError:
                        pass
                    else:
                        os.killpg(child.pid, signal.SIGKILL)
                        raise ValueError('Unexpected descendant; reconciliation required')
                    output.flush(); error.flush(); os.fsync(output.fileno()); os.fsync(error.fileno())
                write_receipt(root, request, 'terminal', data['runner_instance'], exit_code=code)
                return code if code >= 0 else 1
    except ExecutionBusy:
        if root is not None: _failure(root, identity, 'execution_busy')
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, subprocess.SubprocessError):
        if root is not None: _failure(root, identity, 'execution_refused_or_incomplete')
    return 1


def start(manifest_path, identity):
    data = manifest(manifest_path); root = Path(data['state_root'])
    request_id(identity)
    with request_directory(root, identity) as fd:
        request, _ = load_request(fd)
    _bound(data, request)
    if request['request_id'] != identity or inspect_request(root, identity)['phase'] != 'accepted':
        raise ValueError('Request is not eligible for explicit start')
    result = subprocess.run(['/usr/bin/systemctl', '--user', 'start', '--no-block',
                             'homelab-maintenance@'+identity+'.service'],
                            capture_output=True, timeout=30, check=False)
    if result.returncode:
        raise ValueError('Service start failed; inspect before retry')


def submit(manifest_path, request, credentials):
    data = manifest(manifest_path); request = validate_request(request); _bound(data, request)
    root = Path(data['state_root']); identity = request['request_id']
    try:
        publish_request(root, request, credentials)
    except FileExistsError:
        return inspect_request(root, identity)  # Never restart on duplicate submission.
    try:
        start(manifest_path, identity)
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
        _failure(root, identity, 'start_reply_unknown')
    return inspect_request(root, identity)


def inspect(manifest_path, identity):
    data = manifest(manifest_path)
    report = inspect_request(Path(data['state_root']), identity)
    request_id(identity)
    try:
        result = subprocess.run(['/usr/bin/systemctl', '--user', 'show', '--property=ActiveState', '--value',
                                 'homelab-maintenance@'+identity+'.service'], capture_output=True, text=True, timeout=10)
        state = result.stdout.strip()
        if result.returncode == 0 and state in ('active', 'inactive', 'failed', 'activating', 'deactivating'):
            report['service_state'] = state
            if report['phase'] == 'running' and state in ('inactive', 'failed'):
                report.update(phase='unknown', diagnostic='interrupted_execution')
    except (OSError, subprocess.SubprocessError):
        pass
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True, help='Managed installation manifest')
    parser.add_argument('command', choices=['submit', 'start', 'run', 'inspect'])
    parser.add_argument('request_id', nargs='?')
    args = parser.parse_args()
    if args.command != 'submit' and args.request_id is None:
        parser.error('request ID required')
    try:
        if args.command == 'submit':
            # Private stdin only: never credentials in argv or environment.
            raw = sys.stdin.buffer.read(1024**2+1)
            if len(raw) > 1024**2: raise ValueError('Input too large')
            data = json.loads(raw)
            if set(data) != {'request', 'credentials'}: raise ValueError('Invalid input')
            report = submit(args.manifest, data['request'], data['credentials'])
        elif args.command == 'run':
            return run(args.manifest, args.request_id)
        else:
            if args.command == 'start': start(args.manifest, args.request_id)
            report = inspect(args.manifest, args.request_id)
        print(json.dumps(report))
        return int(report['phase'] in ('failed', 'unknown'))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, subprocess.SubprocessError):
        print(json.dumps(dict(schema=1, phase='unknown', diagnostic='request_refused_or_incomplete')))
        return 1


if __name__ == '__main__': raise SystemExit(main())

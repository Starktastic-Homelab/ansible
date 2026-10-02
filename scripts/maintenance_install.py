"""Finalize verified source archives; called only by opt-in root installation."""
import json
import os
from pathlib import Path
import shutil
import sys

from maintenance_executor import build_manifest, manifest
from maintenance_requests import digest, encoded


def finalize(staging, releases, root, instance, apps_commit):
    staging, releases = Path(staging), Path(releases)
    data = build_manifest(staging, root, instance, apps_commit)
    identity = digest(data); target = releases/identity
    path = target/'manifest.json'
    if target.exists():
        if manifest(path) != data:
            raise ValueError('Existing release does not match')
        return dict(manifest=str(path), runtime_id=identity, created=False)
    # Installation sources are verified archives. Normalize file permissions and
    # refuse symlinks in the Python runtime source subset before publishing.
    for name in data['files']:
        source = staging/name
        if source.is_symlink(): raise ValueError('Symlinked runtime source')
    (staging/'manifest.json').write_bytes(encoded(data))
    for directory, _, files in os.walk(staging):
        os.chmod(directory, 0o755)
        for filename in files:
            source = Path(directory)/filename
            if not source.is_symlink():
                os.chmod(source, 0o755 if source == staging/'dependencies/bin/kubectl' else 0o644)
    os.rename(staging, target)
    with path.open('rb') as stream: os.fsync(stream.fileno())
    fd = os.open(releases, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)
    return dict(manifest=str(path), runtime_id=identity, created=True)


if __name__ == '__main__':
    if len(sys.argv) != 6: raise SystemExit('Expected staging, releases, state root, runner identity, apps commit')
    print(json.dumps(finalize(*sys.argv[1:])))

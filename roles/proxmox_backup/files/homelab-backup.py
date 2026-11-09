#!/usr/bin/env python3
"""Host-side backup: back up the newest replicated snapshots and read-only NFS shares with restic, and report
to healthchecks.io.

Usage: homelab-backup {backup|maint|health}. Managed by the ansible role proxmox_backup.
"""
import fcntl
import fnmatch
import glob
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

CONF = Path('/etc/homelab-backup.d')
SECRETS = Path('/etc/homelab-backup')
MOUNT_ROOT = Path('/srv/backup-src')
PROC_MOUNTS = Path('/proc/mounts')
LOCK = Path('/run/lock/homelab-backup.lock')
LOG = []
# soft: reads fail with EIO once the connection drops instead of blocking. A server that hangs while still
# connected never times out over NFSv4; the unit's TimeoutStartSec bounds that.
NFS_OPTIONS = 'ro,soft,nfsvers=4.1,nosuid,nodev,noexec'
CHECKS = {'backup': 'HC_BACKUP', 'maint': 'HC_MAINT', 'health': 'HC_HEALTH'}


class Refused(Exception):
    """A precondition failed; nothing is uploaded."""


def log(message):
    LOG.append(message)
    print(message, flush=True)


def run(argv):
    log('$ ' + ' '.join(argv))
    return subprocess.run(argv, capture_output=True, text=True)


def sh(argv):
    result = run(argv)
    if result.returncode:
        error = RuntimeError(f'{" ".join(argv[:2])} exited {result.returncode}: {result.stderr.strip()[-500:]}')
        error.returncode = result.returncode  # upload() tells restic's incomplete snapshot (3) from a failure
        raise error
    return result.stdout


def ping(url, suffix='', body=''):
    if not url:
        return
    try:
        urllib.request.urlopen(url + suffix, data=body.encode()[-100_000:], timeout=15)
    except Exception as error:  # a lost ping shows up on healthchecks.io; the message may contain the secret URL
        log(f'ping failed: {type(error).__name__}')


def newest_snapshot(zfs_list, dataset, prefix='auto-'):
    snapshots = []
    for line in zfs_list.splitlines():
        name, _, created = line.partition('\t')
        parent, _, snap = name.partition('@')
        if parent == dataset and snap.startswith(prefix):
            snapshots.append((int(created), name))
    if not snapshots:
        raise LookupError(f'no {prefix}* snapshot of {dataset}')
    created, name = max(snapshots)
    return name, created


def is_stale(created, now, max_age_s):
    return now - created > max_age_s


def dump_problems(entries, now, rules):
    problems = []
    for rule in rules:
        matched = [e for e in entries if fnmatch.fnmatch(e[0], rule['glob'])]
        if len(matched) < rule['min_count']:
            problems.append(f'missing: fewer than {rule["min_count"]} of {rule["glob"]}')
            continue
        problems += [f'empty: {path}' for path, size, _ in matched if size == 0]
        age_h = (now - max(mtime for _, _, mtime in matched)) / 3600
        if age_h > rule['max_age_h']:
            problems.append(f'stale: newest {rule["glob"]} is {age_h:.0f} h old (limit {rule["max_age_h"]} h)')
    return problems


def restic_backup_argv(paths, excludes_file, tag):
    return ['backup', '--host', 'pve', '--tag', tag, '--exclude-file', excludes_file,
            '--exclude', str(SECRETS), *paths]


def stale_mounts(proc_mounts, root='/srv/backup-src'):
    points = [line.split()[1] for line in proc_mounts.splitlines() if len(line.split()) > 1]
    return sorted((p for p in points if p.startswith(root.rstrip('/') + '/')), reverse=True)


def restic(cfg, *args):
    sftp = f'sftp.command=ssh -F {CONF}/ssh_config storagebox -s sftp'
    os.environ.update(RESTIC_REPOSITORY=cfg['repository'], RESTIC_PASSWORD_FILE=str(SECRETS / 'restic.pass'),
                      RESTIC_CACHE_DIR=f'/{cfg["pool"]}/cache')
    return sh(['restic', '-o', sftp, *args])


def pool_problems(cfg):
    pool = cfg['pool']
    health = run(['zpool', 'list', '-H', '-o', 'health', pool])
    if health.returncode or health.stdout.strip() != 'ONLINE':
        return [f'pool {pool} is not ONLINE: {(health.stdout + health.stderr).strip()}']
    # restic would otherwise create its cache on the host root disk.
    if run(['zfs', 'get', '-H', '-o', 'value', 'mounted', f'{pool}/cache']).stdout.strip() != 'yes':
        return [f'{pool}/cache is not mounted']
    return []


def snapshot_listing(cfg):
    return sh(['zfs', 'list', '-Hp', '-t', 'snapshot', '-o', 'name,creation', '-r', cfg['pool']])


def fresh_snapshot(cfg, listing, dataset):
    name, created = newest_snapshot(listing, dataset)
    if is_stale(created, int(time.time()), cfg['max_replica_age_h'] * 3600):
        raise Refused(f'{name} is {(time.time() - created) / 3600:.1f} h old; replication is behind')
    return name


def unmount_all():
    for point in stale_mounts(PROC_MOUNTS.read_text(), str(MOUNT_ROOT)):
        sh(['umount', point])


def mount_point(name):
    target = MOUNT_ROOT / name
    target.mkdir(parents=True, exist_ok=True)
    MOUNT_ROOT.chmod(0o700)  # tnrepl has a shell here; mounted sources stay root-only
    return str(target)


def upload(cfg, paths, tag):
    """One restic snapshot. Exit 3 still writes it, minus files it couldn't read: report that and carry on."""
    try:
        out = restic(cfg, *restic_backup_argv(paths, str(CONF / 'excludes.txt'), tag))
    except RuntimeError as error:
        if getattr(error, 'returncode', None) != 3:
            raise
        return [f'{tag} snapshot is incomplete; restore from an older one if those files matter: {error}']
    # restic's own figure for what this snapshot added; the weekly vzdump increment is judged by it.
    for line in out.splitlines():
        if line.startswith('Added to the repository'):
            log(f'{tag}: {line}')
    return []


def mount_shares(cfg):
    mounted, problems = [], []
    for share in cfg['nfs_sources']:
        target = mount_point(share['mount'])
        try:
            sh(['mount', '-t', 'nfs', '-o', NFS_OPTIONS, share['share'], target])
        except RuntimeError as error:
            problems.append(str(error))
            continue
        if (Path(target) / share['marker']).exists():
            mounted.append(target)
        else:
            problems.append(f'{share["share"]} has no {share["marker"]}; refusing to back up an empty share')
    return mounted, problems


def mirror(cfg):
    problems = []
    for url in cfg['git_repos']:
        target = Path(cfg['git_dir']) / url.rstrip('/').rsplit('/', 1)[-1]
        argv = ['git', '-C', str(target), 'remote', 'update', '--prune'] if target.exists() else \
            ['git', 'clone', '--mirror', url, str(target)]
        if run(argv).returncode:
            problems.append(f'git mirror failed: {url}')
    return problems


def backup(cfg):
    if problems := pool_problems(cfg):
        raise Refused('; '.join(problems))
    unmount_all()
    try:
        listing = snapshot_listing(cfg)
        for source in cfg['sources']:
            dataset = source['dataset']
            if source['raw_key'] and sh(['zfs', 'get', '-H', '-o', 'value', 'keystatus', dataset]).strip() != 'available':
                sh(['zfs', 'load-key', '-L', f'file://{SECRETS}/apps.key', dataset])
            sh(['mount', '-t', 'zfs', '-o', 'ro', fresh_snapshot(cfg, listing, dataset), mount_point(source['mount'])])
        problems = mirror(cfg) + upload(cfg, [str(MOUNT_ROOT), '/etc', cfg['git_dir'], '/root'], 'daily')
        if dumps := glob.glob(cfg['vzdump_glob']):
            problems += upload(cfg, [max(dumps, key=os.path.getmtime)], 'vzdump')
        # Shares go last, in their own snapshot, so a slow or broken share can't hold back the uploads above.
        shares, share_problems = mount_shares(cfg)
        entries = []
        for rule in cfg['dump_rules']:  # checked after the shares are mounted: some dumps live on them
            for path in glob.glob(rule['glob']):
                stat = os.stat(path)
                entries.append((path, stat.st_size, int(stat.st_mtime)))
        problems += share_problems + dump_problems(entries, int(time.time()), cfg['dump_rules'])
        if shares:
            problems += upload(cfg, shares, 'nfs')
        return problems
    finally:
        unmount_all()


def maint(cfg):
    if problems := pool_problems(cfg):
        raise Refused('; '.join(problems))
    # Group by tags: each weekly vzdump has a different path, so the default host,paths grouping keeps them all.
    group = ['--host', 'pve', '--group-by', 'host,tags']
    for tag in ('daily', 'nfs'):
        restic(cfg, 'forget', *group, '--tag', tag, '--keep-daily', '14', '--keep-weekly', '8', '--keep-monthly', '12')
    # Two: an upload that hits restic exit 3 still writes a vzdump snapshot, without the dump; keep the one before it.
    restic(cfg, 'forget', *group, '--tag', 'vzdump', '--keep-last', '2')
    restic(cfg, 'prune')
    restic(cfg, 'check', '--read-data-subset', '5%')
    return []


def health(cfg):
    problems = pool_problems(cfg)
    status = run(['zpool', 'status', '-x'])
    if status.stdout.strip() != 'all pools are healthy':
        problems.append('zpool status -x: ' + (status.stdout + status.stderr).strip()[-1000:])
    try:
        listing = snapshot_listing(cfg)
        for source in cfg['sources']:
            try:
                fresh_snapshot(cfg, listing, source['dataset'])
            except (LookupError, Refused) as error:
                problems.append(str(error))
    except RuntimeError as error:
        problems.append(str(error))
    return problems


def read_env(path):
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return {}
    pairs = (line.split('=', 1) for line in lines if '=' in line and not line.startswith('#'))
    return {key.strip(): value.strip().strip('"\'') for key, value in pairs}


def main(argv):
    if len(argv) != 1 or argv[0] not in CHECKS:
        print(__doc__, file=sys.stderr)
        return 2
    mode = argv[0]
    LOG.clear()
    url = read_env(SECRETS / 'healthchecks.env').get(CHECKS[mode], '').strip()
    ping(url, '/start')
    try:
        with open(LOCK, 'w') as lock:
            if mode != 'health':  # backup and maint share mounts and the restic repository
                fcntl.flock(lock, fcntl.LOCK_EX)
            problems = globals()[mode](json.loads((CONF / 'sources.json').read_text()))
    except Exception as error:  # report every failure to healthchecks.io instead of dying silently
        problems = [f'{type(error).__name__}: {error}']
    for problem in problems:
        log('PROBLEM: ' + problem)
    ping(url, '/fail' if problems else '', '\n'.join(LOG[-50:]))
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))

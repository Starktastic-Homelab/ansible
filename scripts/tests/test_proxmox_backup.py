"""Offline checks for the host-side backup: freshness refusals, dump checks, restic argv and boot-safe units."""
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
import unittest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import filter_loader, init_plugin_loader
from ansible.template import Templar, trust_as_template

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / 'roles/proxmox_backup'
NOW = 1_800_000_000
H = 3600

def script():
    spec = importlib.util.spec_from_file_location('homelab_backup', ROLE / 'files/homelab-backup.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def defaults(**values):
    variables = DataLoader().load_from_file(str(ROLE / 'defaults/main.yml'), trusted_as_template=True)
    variables.update(values)
    return variables

def render(name, **values):
    templar = Templar(loader=DataLoader(), variables=defaults(**values))
    return templar.template(trust_as_template((ROLE / 'templates' / name).read_text()))

def tasks():
    return DataLoader().load_from_file(str(ROLE / 'tasks/main.yml'), trusted_as_template=True)

def task(name):
    return next(t for t in tasks() if t['name'] == name)

def runs(name, **values):
    templar = Templar(variables=defaults(**values))
    when = task(name)['when']
    return all(templar.evaluate_expression(expr) for expr in ([when] if isinstance(when, str) else when))

def evaluate(name, expr, **values):
    """Evaluate one expression of a task the way Ansible would, with the role's filters and the task's vars."""
    init_plugin_loader()
    filter_loader.add_directory(str(ROLE / 'filter_plugins'))
    templar = Templar(variables=defaults(**dict(task(name).get('vars', {}), **values)))
    expr = trust_as_template(expr)
    return templar.template(expr) if '{{' in expr else templar.evaluate_expression(expr)

# pvesh get /cluster/backup and the TrueNAS objects as read live on 2026-11-16.
LIVE_JOB = {'compress': '0', 'enabled': 1, 'fleecing': {'enabled': 0}, 'id': 'backup-f56bebaa-4912', 'mode': 'snapshot',
            'notes-template': '{{guestname}}', 'notification-mode': 'notification-system',
            'prune-backups': {'keep-last': '3'}, 'repeat-missed': 0, 'schedule': 'sun 01:00', 'storage': 'usbbk-vz',
            'type': 'vzdump', 'vmid': '100,300', 'node': 'pve'}

def snapshot_task(id_, dataset, recursive, weeks, minute, hour):
    return {'id': id_, 'dataset': dataset, 'recursive': recursive, 'lifetime_value': weeks, 'lifetime_unit': 'WEEK',
            'naming_schema': 'auto-%Y-%m-%d_%H-%M', 'enabled': True, 'allow_empty': True, 'exclude': [],
            'schedule': {'minute': minute, 'hour': hour, 'dom': '*', 'month': '*', 'dow': '*', 'begin': '00:00',
                         'end': '23:59'}}

def truenas_live():
    snapshots = [snapshot_task(1, 'apps/pv', True, 1, '30', '3,9,15,21'),
                 snapshot_task(3, 'apps/k3s-block', False, 1, '30', '3,9,15,21'),
                 snapshot_task(4, 'main/media', False, 1, '0', '4'),
                 snapshot_task(5, 'main/immich', False, 2, '45', '3,9,15,21')]
    replication = {'name': 'apps to pve usbbk', 'direction': 'PUSH', 'transport': 'SSH',
                   'source_datasets': ['apps/k3s-block', 'apps/pv'], 'target_dataset': 'usbbk/tn', 'recursive': False,
                   'auto': True, 'enabled': True, 'retention_policy': 'CUSTOM', 'lifetime_value': 2,
                   'lifetime_unit': 'WEEK', 'readonly': 'SET', 'properties': True,
                   'properties_exclude': ['quota', 'reservation', 'refreservation'], 'encryption': False,
                   'large_block': True, 'compressed': True, 'allow_from_scratch': False,
                   'periodic_snapshot_tasks': [snapshots[1], snapshots[0]], 'exclude': [],
                   'ssh_credentials': {'name': 'pve-tnrepl', 'id': 2}}
    share = {'path': '/mnt/main/immich', 'hosts': [], 'maproot_user': None, 'maproot_group': None,
             'mapall_user': 'media', 'mapall_group': 'media', 'security': [], 'enabled': True}
    shares = [dict(share, networks=['10.9.8.0/24'], ro=False), dict(share, networks=['10.9.9.20/32'], ro=True),
              {'path': '/mnt/apps/k3s-block', 'networks': ['10.9.9.20/32'], 'ro': False, 'enabled': True}]
    return {'replication.query': [replication], 'pool.snapshottask.query': snapshots, 'sharing.nfs.query': shares,
            'nfs.config': {'bindip': ['10.9.8.30', '10.9.9.30'], 'protocols': ['NFSV3', 'NFSV4']}}

def script_filters():
    spec = importlib.util.spec_from_file_location('proxmox_backup_filters', ROLE / 'filter_plugins/proxmox_backup.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def guest_reads(live, rc=0, exitcode=0):
    return {'results': [{'item': {'key': method}, 'rc': rc, 'stderr': 'VM 100 not running' if rc else '',
                         'stdout': '' if rc else json.dumps({'exitcode': exitcode, 'exited': 1,
                                                             'out-data': json.dumps(value) + '\n'})}
                        for method, value in live.items()]}

class FakeHost:
    """Stands in for zpool/zfs/mount/git/restic; records every command in order."""
    def __init__(self, tmp, snapshot_age_s=H, pool='ONLINE', mounts='', nfs_sources=(), vzdump=False,
                 restic_rc=None, nfs_mount_rc=0, disk=True):
        self.calls, self.pings = [], []
        if disk:
            (tmp / 'disk').write_text('')
        self.restic_rc, self.nfs_mount_rc = restic_rc or {}, nfs_mount_rc
        self.snapshot_age_s, self.pool, self.mounts = snapshot_age_s, pool, tmp / 'mounts'
        self.mounts.write_text(mounts)
        (tmp / 'healthchecks.env').write_text('HC_BACKUP=https://hc.invalid/b\nHC_HEALTH=https://hc.invalid/h\n')
        (tmp / 'excludes.txt').write_text('')
        dumps = tmp / 'dumps'
        dumps.mkdir()
        (dumps / 'pg_dumpall_1.sql.gz').write_bytes(b'x')
        if vzdump:
            (dumps / 'vzdump-1.vma').write_bytes(b'x')
        (tmp / 'sources.json').write_text(json.dumps({
            'pool': 'usbbk', 'disk': str(tmp / 'disk'), 'max_replica_age_h': 8, 'git_dir': str(tmp / 'git'),
            'repository': 'sftp:storagebox:repo', 'vzdump_glob': str(dumps / 'vzdump-*.vma'),
            'git_repos': ['https://example.invalid/apps.git'],
            'sources': [{'dataset': 'usbbk/tn/pv', 'mount': 'pv', 'raw_key': True}],
            'nfs_sources': list(nfs_sources),
            'dump_rules': [{'glob': str(dumps / 'pg_dumpall_*.sql.gz'), 'max_age_h': 10**9, 'min_count': 1}]}))

    def run(self, argv):
        self.calls.append(list(argv))
        if argv[:3] == ['mount', '-t', 'nfs'] and self.nfs_mount_rc:
            return subprocess.CompletedProcess(argv, self.nfs_mount_rc, '', 'mount.nfs: Connection refused\n')
        if argv[0] == 'restic' and 'backup' in argv:
            rc = self.restic_rc.get(argv[argv.index('--tag') + 1], 0)
            out = '' if rc else 'Files: 1 new\nAdded to the repository: 1.000 KiB (512 B stored)\nprocessed 1 files\n'
            return subprocess.CompletedProcess(argv, rc, out, 'error: lstat /x: no such file or directory\n' if rc else '')
        if argv[0] in ('mount', 'umount'):  # keep the fake /proc/mounts in step, as the kernel would
            lines = [l for l in self.mounts.read_text().splitlines(True) if l.split()[1] != argv[-1]]
            self.mounts.write_text(''.join(lines) + (f'{argv[-2]} {argv[-1]} {argv[2]} ro 0 0\n' if argv[0] == 'mount' else ''))
        if argv[:2] == ['zpool', 'list'] and self.pool == 'SUSPENDED':  # zpool list exits 0 on a suspended pool
            return subprocess.CompletedProcess(argv, 0, 'SUSPENDED\n', '')
        if argv[:2] == ['zpool', 'list']:
            ok = self.pool == 'ONLINE'
            return subprocess.CompletedProcess(argv, 0 if ok else 1, self.pool + '\n' if ok else '',
                                               '' if ok else "cannot open 'usbbk': no such pool\n")
        if argv[:2] == ['zfs', 'list']:
            return subprocess.CompletedProcess(argv, 0, f'usbbk/tn/pv@auto-x\t{NOW - self.snapshot_age_s}\n', '')
        if argv[:2] == ['zfs', 'get']:
            return subprocess.CompletedProcess(argv, 0, 'yes\n' if 'mounted' in argv else 'available\n', '')
        out = 'all pools are healthy\n' if argv[:2] == ['zpool', 'status'] else ''
        return subprocess.CompletedProcess(argv, 0, out, '')

    def install(self, module, tmp):
        module.CONF = module.SECRETS = tmp
        module.MOUNT_ROOT, module.PROC_MOUNTS, module.LOCK = tmp / 'src', tmp / 'mounts', tmp / 'lock'
        module.PSTORE = tmp / 'pstore'
        module.run = self.run
        module.ping = lambda url, suffix='', body='': self.pings.append((url, suffix))
        module.time.time = lambda: NOW

    def index(self, *prefix):
        return next(i for i, c in enumerate(self.calls) if c[:len(prefix)] == list(prefix))

class SnapshotTests(unittest.TestCase):
    def test_newest_snapshot_picks_latest_auto_only(self):
        listing = ('usbbk/tn/pv@auto-2026-11-01_21-30\t100\n'
                   'usbbk/tn/pv@auto-2026-11-02_03-30\t200\n'
                   'usbbk/tn/pv@manual-later\t300\n'
                   'usbbk/tn/pv@pre-restore\t400\n'
                   'usbbk/tn/pv-old@auto-2026-11-03_03-30\t500\n'
                   'usbbk/tn/k3s-block@auto-2026-11-03_03-30\t600\n')
        self.assertEqual(script().newest_snapshot(listing, 'usbbk/tn/pv'), ('usbbk/tn/pv@auto-2026-11-02_03-30', 200))

    def test_newest_snapshot_none_raises(self):
        with self.assertRaises(LookupError):
            script().newest_snapshot('usbbk/tn/pv@manual\t100\n', 'usbbk/tn/pv')

    def test_backup_refuses_stale_replica(self):
        m = script()
        self.assertTrue(m.is_stale(NOW - 8 * H - 1, NOW, 8 * H))
        self.assertFalse(m.is_stale(NOW - 7 * H - 59 * 60, NOW, 8 * H))
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            host = FakeHost(tmp, snapshot_age_s=9 * H)
            host.install(m, tmp)
            self.assertNotEqual(m.main(['backup']), 0)
        self.assertIn(('https://hc.invalid/b', '/fail'), host.pings)
        self.assertFalse([c for c in host.calls if c[:1] in (['restic'], ['mount'])])

class DumpTests(unittest.TestCase):
    def test_dump_problems_flags_old_empty_and_missing(self):
        rules = [{'glob': '/s/pv/databases/pg-backup/pg_dumpall_*.sql.gz', 'max_age_h': 26, 'min_count': 1},
                 {'glob': '/s/immich/backups/*', 'max_age_h': 26, 'min_count': 1},
                 {'glob': '/s/pv/cert-backup/cert-backup/*.yaml', 'max_age_h': 26, 'min_count': 1},
                 {'glob': '/usbbk/vz/dump/vzdump-qemu-300-*.vma', 'max_age_h': 192, 'min_count': 1}]
        entries = [('/s/pv/databases/pg-backup/pg_dumpall_2026-11-02.sql.gz', 0, NOW - H),
                   ('/s/immich/backups/immich-db-backup-1.sql.gz', 10, NOW - 27 * H),
                   ('/usbbk/vz/dump/vzdump-qemu-300-2026_10_20-01_00_00.vma', 10, NOW - 193 * H)]
        problems = script().dump_problems(entries, NOW, rules)
        self.assertEqual(len(problems), 4, problems)
        for needle in ('pg_dumpall_2026-11-02', 'immich/backups', 'cert-backup', 'vzdump-qemu-300'):
            self.assertEqual(sum(needle in p for p in problems), 1, (needle, problems))
        fresh = [('/s/pv/cert-backup/cert-backup/a--b.yaml', 5, NOW - H)]
        self.assertEqual(script().dump_problems(fresh, NOW, rules[2:3]), [])

class ResticTests(unittest.TestCase):
    def test_restic_argv_has_excludes_tag_and_one_snapshot(self):
        paths = ['/srv/backup-src', '/etc', '/usbbk/git', '/root']
        argv = script().restic_backup_argv(paths, '/etc/homelab-backup.d/excludes.txt', 'daily')
        self.assertEqual(argv.count('backup'), 1)
        self.assertEqual(argv[-len(paths):], paths)
        for flag, value in [('--exclude-file', '/etc/homelab-backup.d/excludes.txt'), ('--tag', 'daily'),
                            ('--host', 'pve'), ('--exclude', '/etc/homelab-backup')]:
            self.assertEqual(argv[argv.index(flag) + 1], value)

    def test_stale_mounts_are_cleaned_before_backup(self):
        mounts = ('usbbk/tn/pv@auto-1 /srv/backup-src/pv zfs ro 0 0\n'
                  'rpool/ROOT/pve-1 / zfs rw 0 0\n'
                  'tmpfs /srv/backup-src-other tmpfs rw 0 0\n'
                  'usbbk/tn/k3s-block@auto-1 /srv/backup-src/k3s-block zfs ro 0 0\n')
        m = script()
        self.assertEqual(sorted(m.stale_mounts(mounts)), ['/srv/backup-src/k3s-block', '/srv/backup-src/pv'])
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            host = FakeHost(tmp, mounts=f'usbbk/tn/pv@auto-0 {tmp}/src/pv zfs ro 0 0\n')
            host.install(m, tmp)
            self.assertEqual(m.main(['backup']), 0, m.LOG)
            # tnrepl has a shell on this host; mounted snapshots must stay root-only.
            self.assertEqual((tmp / 'src').stat().st_mode & 0o777, 0o700)
        self.assertLess(host.index('umount', f'{tmp}/src/pv'), host.index('mount'))
        self.assertEqual(len([c for c in host.calls if c[:1] == ['restic'] and 'backup' in c]), 1)
        self.assertGreater(len(host.calls) - 1, host.index('mount'))
        self.assertEqual(host.calls[-1][:1], ['umount'])
        self.assertIn(('https://hc.invalid/b', ''), host.pings)

    def test_both_weekly_vzdumps_are_checked(self):
        rules = json.loads(render('sources.json.j2'))['dump_rules']
        for vm in (100, 300):  # VM100 (TrueNAS boot disk) stays local, but a missing dump must still alert
            self.assertIn({'glob': f'/usbbk/vz/dump/vzdump-qemu-{vm}-*.vma', 'max_age_h': 192, 'min_count': 1}, rules)

    def test_nfs_shares_upload_last_in_their_own_snapshot_and_a_broken_share_blocks_nothing(self):
        share = {'share': '10.9.9.30:/mnt/main/immich', 'mount': 'immich', 'marker': 'upload/.immich'}
        rendered = json.loads(render('sources.json.j2'))  # Immich has lived in main/immich since 2026-11-06
        self.assertEqual(rendered['nfs_sources'], [share])
        self.assertIn({'glob': '/srv/backup-src/immich/backups/immich-db-backup-*.sql.gz', 'max_age_h': 26, 'min_count': 1},
                      rendered['dump_rules'])
        self.assertEqual(json.loads(render('sources.json.j2', proxmox_backup_nfs_sources=[]))['nfs_sources'], [])
        m = script()
        for case in ('populated', 'empty', 'unmountable'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as d:
                tmp = Path(d)
                host = FakeHost(tmp, nfs_sources=[share], vzdump=True, nfs_mount_rc=32 if case == 'unmountable' else 0)
                host.install(m, tmp)
                if case == 'populated':  # mount is stubbed, so the marker stands in for the share's contents
                    (tmp / 'src/immich/upload').mkdir(parents=True)
                    (tmp / 'src/immich/upload/.immich').touch()
                else:  # a share problem must not swallow the run's other problems
                    (tmp / 'dumps/pg_dumpall_1.sql.gz').write_bytes(b'')
                code = m.main(['backup'])
                mount = host.calls[host.index('mount', '-t', 'nfs')]
                self.assertEqual(mount[-2:], ['10.9.9.30:/mnt/main/immich', f'{tmp}/src/immich'])
                self.assertTrue({'ro', 'soft'} <= set(mount[mount.index('-o') + 1].split(',')), mount)
                uploads = [(i, c[c.index('--tag') + 1]) for i, c in enumerate(host.calls) if c[:1] == ['restic'] and 'backup' in c]
                # The replicas go first: a slow or broken share must not hold them back.
                self.assertEqual([t for _, t in uploads], ['daily', 'vzdump'] + (['nfs'] if case == 'populated' else []))
                self.assertLess(uploads[1][0], host.index('mount', '-t', 'nfs'))
                self.assertEqual(code != 0, case != 'populated', m.LOG)
                self.assertIn(('https://hc.invalid/b', '' if case == 'populated' else '/fail'), host.pings)
                problems = [l for l in m.LOG if l.startswith('PROBLEM: ')]
                if case != 'populated':
                    self.assertTrue(any('empty:' in p for p in problems), problems)
                    self.assertTrue(any(('upload/.immich' if case == 'empty' else 'exited 32') in p for p in problems), problems)
                self.assertEqual(host.mounts.read_text(), '', 'every mount must be unmounted')

    def test_incomplete_snapshot_is_reported_and_the_run_carries_on(self):
        m = script()
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            host = FakeHost(tmp, vzdump=True, restic_rc={'daily': 3})
            host.install(m, tmp)
            (tmp / 'dumps/pg_dumpall_1.sql.gz').write_bytes(b'')  # its problem must survive the restic one
            self.assertNotEqual(m.main(['backup']), 0)
        problems = [l for l in m.LOG if l.startswith('PROBLEM: ')]
        self.assertTrue(any('daily' in p and 'incomplete' in p for p in problems), problems)
        self.assertTrue(any('empty:' in p for p in problems), problems)
        self.assertIn('vzdump', [c[c.index('--tag') + 1] for c in host.calls if c[:1] == ['restic'] and 'backup' in c])
        self.assertIn(('https://hc.invalid/b', '/fail'), host.pings)
        with tempfile.TemporaryDirectory() as d:  # any other restic failure still stops the run
            tmp = Path(d)
            host = FakeHost(tmp, vzdump=True, restic_rc={'daily': 1})
            host.install(m, tmp)
            self.assertNotEqual(m.main(['backup']), 0)
        self.assertEqual(len([c for c in host.calls if c[:1] == ['restic'] and 'backup' in c]), 1)

    def test_maint_keeps_nfs_snapshots_on_the_daily_schedule(self):
        m = script()
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            host = FakeHost(tmp)
            host.install(m, tmp)
            self.assertEqual(m.main(['maint']), 0, m.LOG)
        forgets = {c[c.index('--tag') + 1]: c[c.index('--tag') + 2:] for c in host.calls if 'forget' in c}
        keep = ['--keep-daily', '14', '--keep-weekly', '8', '--keep-monthly', '12']
        # Two: a Sunday upload that hits restic exit 3 writes a vzdump snapshot without the dump in it.
        self.assertEqual(forgets, {'daily': keep, 'nfs': keep, 'vzdump': ['--keep-last', '2']})

    def test_stale_repository_locks_are_cleared_before_restic_runs(self):
        # 2026-11-15: a run killed by the 11-10 crash left a lock that made the first maint fail on `forget`.
        for mode in ('backup', 'maint'):
            m = script()
            with tempfile.TemporaryDirectory() as d:
                tmp = Path(d)
                host = FakeHost(tmp)
                host.install(m, tmp)
                self.assertEqual(m.main([mode]), 0, m.LOG)
            restic_calls = [c for c in host.calls if c[:1] == ['restic']]
            self.assertIn('unlock', restic_calls[0], mode)
            self.assertNotIn('--remove-all', restic_calls[0], mode)

    def test_each_upload_logs_what_it_added(self):
        m = script()  # the weekly vzdump increment is read from these lines (plan Task 5 Step 4)
        share = {'share': '10.9.9.30:/mnt/main/immich', 'mount': 'immich', 'marker': 'upload/.immich'}
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            host = FakeHost(tmp, nfs_sources=[share], vzdump=True)
            host.install(m, tmp)
            (tmp / 'src/immich/upload').mkdir(parents=True)
            (tmp / 'src/immich/upload/.immich').touch()
            self.assertEqual(m.main(['backup']), 0, m.LOG)
        added = [l for l in m.LOG if 'Added to the repository' in l]
        self.assertEqual(added, [f'{t}: Added to the repository: 1.000 KiB (512 B stored)' for t in ('daily', 'vzdump', 'nfs')])
        self.assertFalse([l for l in m.LOG if l.startswith(('Files:', 'processed'))], 'only the summary line is logged')

    def test_excludes_cover_regenerable_images(self):
        # restic skips blank and '#' lines in an exclude file.
        lines = [l for l in render('excludes.txt.j2').splitlines() if l.strip() and not l.startswith('#')]
        images = sorted(f'/srv/backup-src/k3s-block/images/9999/vm-9999-{n}.raw'
                        for n in ('prometheus', 'loki', 'tempo', 'alertmanager', 'karakeep-meilisearch'))
        # /etc/homelab-dispatch holds the dispatch token (roles/proxmox_workflow_dispatch), restorable from the vault.
        self.assertEqual(sorted(lines), sorted(images + ['/srv/backup-src/immich/thumbs',
                                                         '/srv/backup-src/immich/encoded-video', '/etc/homelab-dispatch']))

class HostSafetyTests(unittest.TestCase):
    def test_import_unit_never_blocks_boot(self):
        unit = render('usbbk-import.service.j2', proxmox_backup_usb_disk_id='usb-Example_Disk_123-0:0')
        for key in ('Before=', 'RequiredBy=', 'Requires=', 'DefaultDependencies=no'):
            self.assertNotIn(key, unit)
        self.assertIn('WantedBy=multi-user.target', unit)
        self.assertIn('ConditionPathExists=/dev/disk/by-id/usb-Example_Disk_123-0:0', unit)
        self.assertRegex(unit, r'TimeoutStartSec=\d+')
        # cachefile is per import: without none, zfs-import-cache would import the disk early in boot.
        self.assertIn('zpool import -N -o cachefile=none -d /dev/disk/by-id usbbk', unit)
        self.assertRegex(unit, r'After=.*pve-guests\.service')
        name = 'Keep the backup pool out of the boot-time import cache'
        self.assertEqual(task(name)['ansible.builtin.command']['argv'], ['zpool', 'set', 'cachefile=none', '{{ proxmox_backup_pool }}'])
        disk = dict(proxmox_backup_usb_disk_id='usb-Example_Disk_123-0:0')
        for registered, expected in [({'rc': 0, 'stdout': '-'}, True), ({'rc': 0, 'stdout': '/etc/zfs/zpool.cache'}, True),
                                     ({'rc': 0, 'stdout': 'none'}, False), ({'rc': 1, 'stdout': ''}, False)]:
            with self.subTest(registered=registered):
                self.assertEqual(runs(name, proxmox_backup_cachefile=registered, **disk), expected)

    def test_role_checks_before_changing_and_imports_before_using_the_pool(self):
        names = [t['name'] for t in tasks()]
        changes = [i for i, t in enumerate(tasks()) if {'ansible.builtin.template', 'ansible.builtin.copy',
                   'ansible.builtin.file', 'ansible.builtin.apt', 'ansible.builtin.user'} & set(t)]
        for check in ('Require the backup disk and Storage Box settings', 'Inspect backup secrets',
                      'Refuse missing or loose backup secrets'):
            self.assertLess(names.index(check), min(changes), check)
        imported = names.index('Import the backup pool now and at boot')
        for later in ('Keep the backup pool out of the boot-time import cache', 'Restrict the backup pool to root',
                      'Read ZFS delegations on the replication target'):
            self.assertGreater(names.index(later), imported, later)
        restrict = task('Restrict the backup pool to root')['ansible.builtin.file']
        self.assertEqual((restrict['path'], restrict['mode']), ('/{{ proxmox_backup_pool }}', '0700'))

    def test_secret_asserts_require_0600_regular_files(self):
        that = task('Refuse missing or loose backup secrets')['ansible.builtin.assert']['that']
        good = dict(exists=True, isreg=True, islnk=False, mode='0600', uid=0)
        def allowed(**stat):
            templar = Templar(variables=defaults(item={'stat': dict(good, **stat)}))
            return all(templar.evaluate_expression(expr) for expr in that)
        self.assertTrue(allowed())
        for bad in [dict(exists=False), dict(isreg=False), dict(islnk=True), dict(mode='0644'), dict(mode='0640'), dict(uid=1000)]:
            with self.subTest(**bad):
                self.assertFalse(allowed(**bad))

    def test_health_fails_when_pool_missing(self):
        m = script()
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            host = FakeHost(tmp, pool='')
            host.install(m, tmp)
            self.assertNotEqual(m.main(['health']), 0)
        self.assertIn(('https://hc.invalid/h', '/fail'), host.pings)
        self.assertNotIn(('https://hc.invalid/h', ''), host.pings)

    def test_health_stops_at_the_first_pool_problem(self):
        # On a suspended pool, zfs list and the pool-wide zpool status can block for good; report and stop.
        m = script()
        for kind in [dict(pool=''), dict(pool='SUSPENDED'), dict(disk=False)]:
            with self.subTest(**kind), tempfile.TemporaryDirectory() as d:
                tmp = Path(d)
                host = FakeHost(tmp, **kind)
                host.install(m, tmp)
                self.assertNotEqual(m.main(['health']), 0)
                self.assertIn(('https://hc.invalid/h', '/fail'), host.pings)
                self.assertFalse([c for c in host.calls if c[:2] in (['zfs', 'list'], ['zpool', 'status'])], host.calls)

    def test_health_on_a_healthy_pool_still_checks_replicas_and_all_pools(self):
        m = script()
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            host = FakeHost(tmp, snapshot_age_s=9 * H)
            host.install(m, tmp)
            self.assertNotEqual(m.main(['health']), 0)
            self.assertTrue(any('replication is behind' in line for line in m.LOG), m.LOG)
            host.index('zpool', 'status', '-x')
            host.index('zfs', 'list')

    def test_an_unplugged_disk_fails_health_while_the_pool_still_reads_online(self):
        # An idle pool can stay ONLINE after the cable is pulled, until some I/O touches it.
        m = script()
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            host = FakeHost(tmp, disk=False)  # the fake zpool list still answers ONLINE
            host.install(m, tmp)
            self.assertNotEqual(m.main(['health']), 0)
            self.assertTrue(any(str(tmp / 'disk') in line and 'missing' in line for line in m.LOG), m.LOG)
            self.assertNotEqual(m.main(['backup']), 0)
            self.assertFalse([c for c in host.calls if c[0] in ('restic', 'mount', 'git')])
        self.assertEqual(json.loads(render('sources.json.j2', proxmox_backup_usb_disk_id='usb-Example_Disk_123-0:0'))['disk'],
                         '/dev/disk/by-id/usb-Example_Disk_123-0:0')

    def test_a_hung_health_run_is_stopped_before_the_next_one(self):
        # A blocked health run would otherwise hold off every hourly run for 23 h.
        drop_in = task('Bound the health check run')['ansible.builtin.copy']
        self.assertEqual(drop_in['dest'], '/etc/systemd/system/homelab-backup@health.service.d/timeout.conf')
        self.assertIn('[Service]\nTimeoutStartSec=10min\n', drop_in['content'])
        self.assertEqual(task('Bound the health check run')['notify'], 'Reload systemd')
        names = [t['name'] for t in tasks()]
        directory = task('Create the health check drop-in directory')['ansible.builtin.file']
        self.assertEqual((directory['path'], directory['state']), (str(Path(drop_in['dest']).parent), 'directory'))
        self.assertLess(names.index('Create the health check drop-in directory'), names.index('Bound the health check run'))

    def test_a_kernel_crash_record_stops_backups_until_it_is_moved_away(self):
        m = script()
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            host = FakeHost(tmp)
            host.install(m, tmp)
            record = tmp / 'pstore/1794277905'
            (record / '001').mkdir(parents=True)
            os.utime(record, (NOW - H, NOW - H))  # systemd-pstore archived it at boot, an hour ago
            self.assertNotEqual(m.main(['backup']), 0)
            self.assertTrue(any('1794277905' in line for line in m.LOG), m.LOG)
            self.assertNotEqual(m.main(['maint']), 0)
            self.assertNotEqual(m.main(['health']), 0)
            self.assertIn(('https://hc.invalid/b', '/fail'), host.pings)
            self.assertIn(('https://hc.invalid/h', '/fail'), host.pings)
            # Hours later health is back up, so it can alert on new problems, but backups still refuse.
            os.utime(record, (NOW - 4 * H, NOW - 4 * H))
            self.assertEqual(m.main(['health']), 0, m.LOG)
            self.assertNotEqual(m.main(['backup']), 0)
            self.assertFalse([c for c in host.calls if c[0] in ('restic', 'mount', 'git')])
            shutil.rmtree(record)
            self.assertEqual(m.main(['backup']), 0, m.LOG)

    def test_vzdump_storage_registers_only_when_mounted(self):
        # pvesm add activates the storage at once and fails while is_mountpoint's path is not mounted.
        name = 'Register the backup pool as vzdump storage'
        absent, present = {'stdout': '[{"storage": "local"}]'}, {'stdout': '[{"storage": "usbbk-vz"}]'}
        for mounted, definitions, expected in [({'rc': 0}, absent, True), ({'rc': 1}, absent, False),
                                               ({'rc': 0}, present, False)]:
            with self.subTest(mounted=mounted, definitions=definitions):
                self.assertEqual(runs(name, proxmox_backup_vz_mounted=mounted,
                                      proxmox_backup_storage_definitions=definitions), expected)
        check = task('Check that the vzdump dataset is mounted')['ansible.builtin.command']['argv']
        self.assertEqual(check, ['findmnt', '--noheadings', '--mountpoint', '/{{ proxmox_backup_pool }}/vz'])

    def test_a_hung_run_is_stopped_before_the_next_one_is_due(self):
        # Over NFSv4 a hung-but-connected server never times out a soft read; the unit has to bound the run.
        self.assertIn('TimeoutStartSec=23h', render('homelab-backup@.service.j2'))

    def test_ping_never_raises_or_logs_the_url(self):
        m = script()
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / 'healthchecks.env'
            env.write_text('HC_BACKUP="https://hc.invalid/one"\nHC_MAINT=\'https://hc.invalid/two\'\n')
            self.assertEqual(m.read_env(env), {'HC_BACKUP': 'https://hc.invalid/one', 'HC_MAINT': 'https://hc.invalid/two'})
        # A URL without a scheme raises ValueError, whose message carries the whole secret URL.
        m.ping('hc.invalid/secret-uuid', '/start')
        self.assertTrue(m.LOG)
        self.assertNotIn('secret-uuid', '\n'.join(m.LOG))

    def test_playbook_targets_proxmox_hosts_and_k3s_excludes_it(self):
        plays = yaml.safe_load((ROOT / 'proxmox-backup.yml').read_text())
        self.assertEqual([(p['hosts'], [r['role'] for r in p['roles']]) for p in plays],
                         [('proxmox_hosts', ['proxmox_backup'])])
        self.assertNotIn('proxmox_backup', (ROOT / 'k3s.yml').read_text())
        ignored = yaml.safe_load((ROOT / '.github/workflows/deploy.yml').read_text())[True]['push']['paths-ignore']
        for path in ('roles/proxmox_backup/**', 'proxmox-backup.yml', '.github/workflows/proxmox-backup.yml',
                     'scripts/tests/test_proxmox_backup.py'):
            self.assertIn(path, ignored)
        own = yaml.safe_load((ROOT / '.github/workflows/proxmox-backup.yml').read_text())[True]['push']['paths']
        self.assertEqual(sorted(own), ['proxmox-backup.yml', 'roles/proxmox_backup/**'])

class HostSettingsTests(unittest.TestCase):
    CHANGE = 'Create or update the vzdump job'

    def vzdump_change(self, jobs):
        return evaluate(self.CHANGE, 'proxmox_backup_vzdump_change', proxmox_backup_vzdump_jobs={'stdout': json.dumps(jobs)})

    def test_live_vzdump_job_needs_no_change(self):
        self.assertEqual(self.vzdump_change([LIVE_JOB]), [])
        self.assertFalse(evaluate(self.CHANGE, task(self.CHANGE)['when'],
                                  proxmox_backup_vzdump_jobs={'stdout': json.dumps([LIVE_JOB])}))
        self.assertEqual(task(self.CHANGE)['ansible.builtin.command']['argv'], '{{ proxmox_backup_vzdump_change }}')

    def test_drifted_vzdump_job_sets_only_the_drifted_options(self):
        drifted = dict(LIVE_JOB, schedule='sat 01:00', **{'prune-backups': {'keep-last': '2'}})
        drifted.pop('enabled')
        self.assertEqual(self.vzdump_change([{'id': 'other'}, drifted]),
                         ['pvesh', 'set', '/cluster/backup/backup-f56bebaa-4912', '--schedule', 'sun 01:00',
                          '--prune-backups', 'keep-last=3'])

    def test_vzdump_job_disabled_by_hand_stays_disabled(self):
        self.assertEqual(self.vzdump_change([dict(LIVE_JOB, enabled=0)]), [])

    def test_prune_backups_compares_sorted_pairs(self):
        job = defaults()['proxmox_backup_vzdump_job']
        job = dict(job, options=dict(job['options'], **{'prune-backups': 'keep-weekly=2,keep-last=3'}))
        live = dict(LIVE_JOB, **{'prune-backups': {'keep-last': '3', 'keep-weekly': '2'}})
        change = evaluate(self.CHANGE, 'proxmox_backup_vzdump_change', proxmox_backup_vzdump_job=job,
                          proxmox_backup_vzdump_jobs={'stdout': json.dumps([live])})
        self.assertEqual(change, [])

    def test_absent_vzdump_job_refuses_to_duplicate_another_job(self):
        for other in ({'id': 'backup-old', 'type': 'vzdump', 'vmid': '300'},
                      {'id': 'backup-all', 'type': 'vzdump', 'all': 1},
                      {'id': 'backup-pool', 'type': 'vzdump', 'pool': 'k3s'}):
            with self.subTest(other=other['id']):
                with self.assertRaisesRegex(Exception, f"absent but .*{other['id']}"):
                    self.vzdump_change([other])
        self.assertEqual(self.vzdump_change([{'id': 'backup-k3s', 'type': 'vzdump', 'vmid': '201,202'}])[:2],
                         ['pvesh', 'create'])

    def test_absent_vzdump_job_is_created_with_a_literal_notes_template(self):
        argv = self.vzdump_change([])
        self.assertEqual(argv[:5], ['pvesh', 'create', '/cluster/backup', '--id', 'backup-f56bebaa-4912'])
        options = dict(zip(argv[5::2], argv[6::2]))
        self.assertEqual(options, {'--schedule': 'sun 01:00', '--vmid': '100,300', '--storage': 'usbbk-vz',
                                   '--mode': 'snapshot', '--compress': '0', '--prune-backups': 'keep-last=3',
                                   '--enabled': '1', '--notification-mode': 'notification-system',
                                   '--notes-template': '{{guestname}}'})

    def test_a_job_created_while_the_role_is_off_starts_disabled(self):
        argv = evaluate(self.CHANGE, 'proxmox_backup_vzdump_change', proxmox_backup_enabled=False,
                        proxmox_backup_vzdump_jobs={'stdout': '[]'})
        self.assertEqual(dict(zip(argv[5::2], argv[6::2]))['--enabled'], '0')

    def test_quota_is_set_only_when_it_differs(self):
        name = 'Cap the local storage dataset'
        for live, expected in [('161061273600\n', False), ('107374182400\n', True), ('0\n', True)]:
            with self.subTest(live=live):
                self.assertEqual(runs(name, proxmox_backup_vz_quota_live={'stdout': live}), expected)
        self.assertEqual(task(name)['ansible.builtin.command']['argv'],
                         ['zfs', 'set', 'quota={{ proxmox_backup_vz_quota }}', '{{ proxmox_backup_vz_dataset }}'])

    def test_reads_run_in_check_mode(self):
        for name in ('Read the local storage quota', 'Read the vzdump jobs',
                     'Read the TrueNAS backup settings through the VM 100 guest agent'):
            with self.subTest(name=name):
                self.assertIs(task(name)['check_mode'], False)
                self.assertIs(task(name)['changed_when'], False)

    def truenas_drift(self, reads):
        return evaluate('Refuse TrueNAS backup settings drift', 'proxmox_backup_truenas_drift',
                        proxmox_backup_truenas_reads=reads)

    def test_truenas_check_passes_on_the_live_settings(self):
        self.assertEqual(self.truenas_drift(guest_reads(truenas_live())), [])

    def test_truenas_drift_names_object_field_expected_and_actual(self):
        live = truenas_live()
        live['replication.query'][0]['lifetime_value'] = 1
        live['sharing.nfs.query'][1]['ro'] = False
        live['pool.snapshottask.query'][3]['schedule']['hour'] = '3'
        live['nfs.config']['bindip'] = ['10.9.8.30']
        problems = self.truenas_drift(guest_reads(live))
        self.assertEqual(problems, [
            'replication.query name=apps to pve usbbk: lifetime_value expected 2, actual 1',
            'pool.snapshottask.query dataset=main/immich: schedule.hour expected 3,9,15,21, actual 3',
            "sharing.nfs.query path=/mnt/main/immich networks=['10.9.9.20/32']: ro expected True, actual False",
            "nfs.config: bindip expected to contain 10.9.9.30, actual ['10.9.8.30']"])
        self.assertIn('TrueNAS UI', task('Refuse TrueNAS backup settings drift')['ansible.builtin.fail']['msg'])

    def test_truenas_missing_object_or_failed_guest_exec_is_drift(self):
        live = truenas_live()
        live['pool.snapshottask.query'].pop()
        self.assertIn('pool.snapshottask.query dataset=main/immich: expected 1 matching, found 0',
                      self.truenas_drift(guest_reads(live)))
        stopped = self.truenas_drift(guest_reads(truenas_live(), rc=2))
        self.assertEqual(len(stopped), 4)
        self.assertTrue(all('rc 2' in p and 'VM 100 not running' in p for p in stopped), stopped)
        self.assertTrue(all('midclt exited 1' in p for p in self.truenas_drift(guest_reads(truenas_live(), exitcode=1))))

    def test_truenas_queries_select_fields_and_never_the_ssh_credentials(self):
        name = 'Read the TrueNAS backup settings through the VM 100 guest agent'
        self.assertIs(task(name)['no_log'], True)
        expected = defaults()['proxmox_backup_truenas_expected']
        for method, entries in expected.items():
            argv = evaluate(name, task(name)['ansible.builtin.command']['argv'], item={'key': method, 'value': entries})
            self.assertEqual(argv[:7] + argv[7:10], ['qm', 'guest', 'exec', '100', '--timeout', '60', '--',
                                                      'midclt', 'call', method])
            if method == 'nfs.config':
                self.assertEqual(len(argv), 10)
                continue
            self.assertEqual(argv[10], '[]')
            select = json.loads(argv[11])['select']
            self.assertNotIn('ssh_credentials', select)
            self.assertTrue({'name', 'lifetime_value', 'dataset', 'path', 'networks', 'ro'} & set(select))
            if method == 'replication.query':
                self.assertIn('ssh_credentials.name', select)

    def test_truenas_check_is_skipped_by_the_kill_switch(self):
        for name in ('Read the TrueNAS backup settings through the VM 100 guest agent',
                     'Refuse TrueNAS backup settings drift'):
            with self.subTest(name=name):
                # The drift expression is never evaluated, so a down TrueNAS cannot block the kill-switch deploy.
                self.assertFalse(runs(name, proxmox_backup_enabled=False, proxmox_backup_truenas_reads=None))

    def test_truenas_read_and_workflow_have_timeouts(self):
        self.assertEqual(task('Read the TrueNAS backup settings through the VM 100 guest agent')['timeout'], 120)
        workflow = yaml.safe_load((ROOT / '.github/workflows/proxmox-backup.yml').read_text())
        self.assertEqual(workflow['jobs']['deploy-proxmox-backup']['timeout-minutes'], 30)

    def test_truenas_call_allows_only_reads_and_ascii(self):
        call = script_filters().truenas_call
        self.assertEqual(call('nfs.config', {}), ['midclt', 'call', 'nfs.config'])
        for method in ('nfs.update', 'pool.dataset.delete', 'replication.run', 'nfs.config.update'):
            with self.subTest(method=method), self.assertRaisesRegex(ValueError, 'only'):
                call(method, [])
        with self.assertRaisesRegex(ValueError, 'ASCII'):
            call('sh\u00e4ring.nfs.query', [{'match': {'path': '/x'}, 'fields': {}}])
        with self.assertRaisesRegex(ValueError, 'ssh_credentials'):
            call('replication.query', [{'match': {'name': 'x'}, 'fields': {'ssh_credentials': {'name': 'x'}}}])
        with self.assertRaisesRegex(ValueError, 'ssh_credentials'):
            call('replication.query', [{'match': {'name': 'x'}, 'fields': {'ssh_credentials.attributes': {}}}])

    def test_truenas_wrong_shape_is_drift_not_a_crash(self):
        live = truenas_live()
        live['sharing.nfs.query'] = {'path': '/mnt/main/immich'}
        live['nfs.config'] = ['10.9.8.30']
        live['pool.snapshottask.query'] = ['apps/pv']
        problems = self.truenas_drift(guest_reads(live))
        self.assertEqual(len(problems), 3, problems)
        self.assertIn('pool.snapshottask.query: AttributeError', problems[0])
        self.assertIn('sharing.nfs.query: ValueError: expected a JSON list, got dict', problems[1])
        self.assertIn('nfs.config: ValueError: expected a JSON dict, got list', problems[2])

    def test_truenas_full_schedule_hosts_and_extra_share_are_drift(self):
        live = truenas_live()
        live['pool.snapshottask.query'][0]['schedule']['dow'] = '1'
        live['sharing.nfs.query'][0]['hosts'] = ['10.9.8.50']
        live['sharing.nfs.query'][1]['maproot_user'] = 'root'
        live['replication.query'][0]['ssh_credentials']['name'] = 'other'
        live['sharing.nfs.query'].append(dict(live['sharing.nfs.query'][1], networks=['10.9.9.0/24']))
        self.assertEqual(self.truenas_drift(guest_reads(live)), [
            'replication.query name=apps to pve usbbk: ssh_credentials.name expected pve-tnrepl, actual other',
            'pool.snapshottask.query dataset=apps/pv: schedule.dow expected *, actual 1',
            'sharing.nfs.query path=/mnt/main/immich: expected 2 matching, found 3',
            "sharing.nfs.query path=/mnt/main/immich networks=['10.9.8.0/24']: hosts expected [], actual ['10.9.8.50']",
            "sharing.nfs.query path=/mnt/main/immich networks=['10.9.9.20/32']: maproot_user expected None, actual root"])

    def test_truenas_null_and_missing_differ(self):
        live = truenas_live()
        del live['sharing.nfs.query'][0]['maproot_group']
        live['sharing.nfs.query'][1]['mapall_user'] = None
        self.assertEqual(self.truenas_drift(guest_reads(live)), [
            "sharing.nfs.query path=/mnt/main/immich networks=['10.9.8.0/24']: maproot_group expected None, "
            'actual <missing>',
            "sharing.nfs.query path=/mnt/main/immich networks=['10.9.9.20/32']: mapall_user expected media, "
            'actual None'])

if __name__ == '__main__':
    unittest.main()

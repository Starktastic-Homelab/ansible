"""Offline checks for the host-side backup: freshness refusals, dump checks, restic argv and boot-safe units."""
import importlib.util
import json
import subprocess
import tempfile
from pathlib import Path
import unittest
import yaml
from ansible.parsing.dataloader import DataLoader
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
    return all(templar.evaluate_expression(expr) for expr in task(name)['when'])

class FakeHost:
    """Stands in for zpool/zfs/mount/git/restic; records every command in order."""
    def __init__(self, tmp, snapshot_age_s=H, pool='ONLINE', mounts=''):
        self.calls, self.pings = [], []
        self.snapshot_age_s, self.pool = snapshot_age_s, pool
        (tmp / 'mounts').write_text(mounts)
        (tmp / 'healthchecks.env').write_text('HC_BACKUP=https://hc.invalid/b\nHC_HEALTH=https://hc.invalid/h\n')
        (tmp / 'excludes.txt').write_text('')
        dumps = tmp / 'dumps'
        dumps.mkdir()
        (dumps / 'pg_dumpall_1.sql.gz').write_bytes(b'x')
        (tmp / 'sources.json').write_text(json.dumps({
            'pool': 'usbbk', 'max_replica_age_h': 8, 'git_dir': str(tmp / 'git'),
            'repository': 'sftp:storagebox:repo', 'vzdump_glob': str(dumps / 'vzdump-*.vma'),
            'git_repos': ['https://example.invalid/apps.git'],
            'sources': [{'dataset': 'usbbk/tn/pv', 'mount': 'pv', 'raw_key': True}],
            'dump_rules': [{'glob': str(dumps / 'pg_dumpall_*.sql.gz'), 'max_age_h': 10**9, 'min_count': 1}]}))

    def run(self, argv):
        self.calls.append(list(argv))
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

    def test_excludes_cover_regenerable_images(self):
        # restic skips blank and '#' lines in an exclude file.
        lines = [l for l in render('excludes.txt.j2').splitlines() if l.strip() and not l.startswith('#')]
        images = sorted(f'/srv/backup-src/k3s-block/images/9999/vm-9999-{n}.raw'
                        for n in ('prometheus', 'loki', 'tempo', 'alertmanager', 'karakeep-meilisearch'))
        self.assertEqual(sorted(lines), sorted(images + ['/srv/backup-src/immich/thumbs',
                                                         '/srv/backup-src/immich/encoded-video']))

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

if __name__ == '__main__':
    unittest.main()

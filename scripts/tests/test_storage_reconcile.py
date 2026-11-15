"""Offline checks for the reconcile executor: host adapters faked, every command recorded in order."""
import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

FILES = Path(__file__).resolve().parents[2] / 'roles/storage_vm/files'
sys.path.insert(0, str(FILES))
from homelab_storage import host, lio, metrics, nft, reconcile  # noqa: E402

GI = 1024 ** 3
NOW = 1_800_000_000
UUID_A = '11111111-2222-3333-4444-555555555555'
W1, W2 = 'iqn.2026-11.net.starktastic:w1', 'iqn.2026-11.net.starktastic:w2'
WRITES = ('vgchange', 'lvcreate', 'lvextend', 'lvchange', 'mkfs.ext4', 'mount', 'umount', 'dd', 'blkid')


def config(**kw):
    c = {'workers': [{'name': 'w1', 'bridge_address': '192.0.2.21'}, {'name': 'w2', 'bridge_address': '192.0.2.22'}],
         'bridge_address': '192.0.2.10', 'portal': '192.0.2.10:3260', 'vgs': ['retained', 'regen'],
         'api_url': 'https://192.0.2.99:6443', 'token_file': '/etc/storage-fencer/token',
         'ca_file': '/etc/storage-fencer/ca.crt', 'csi_driver': 'org.democratic-csi.node-manual', 'max_removals': 2,
         'not_ready_seconds': 300, 'loop_seconds': 15, 'proof_timeout_seconds': 60, 'quarantine_seconds': 259200,
         'blind_after_seconds': 600, 'pause_after': []}
    c.update(kw)
    return c


def entry(id='a', cls='retained', origin='new', size='1Gi'):
    return {'id': id, 'namespace': 'n', 'claim': 'c', 'size': size, 'class': cls, 'origin': origin}


class FakeLock:
    def __init__(self):
        self.held = False

    def __enter__(self):
        assert not self.held
        self.held = True

    def __exit__(self, *exc):
        self.held = False


class FakeLio:
    """The lio.Lio surface; targets: id -> {'dev', 'acls'}."""
    def __init__(self, fake, targets=None, sessions=None):
        self.fake, self.targets_ = fake, targets or {}
        self.sessions_ = sessions or {}

    def ensure_target(self, id, dev, acl_iqns, portal):
        self.fake.record('lio.ensure_target', id, dev, tuple(acl_iqns), portal)
        self.targets_[id] = {'dev': dev, 'acls': set(acl_iqns)}

    def save(self):
        self.fake.record('lio.save')

    def delete_acl(self, acl_iqn):
        self.fake.record('lio.delete_acl', acl_iqn)
        for t in self.targets_.values():
            t['acls'].discard(acl_iqn)
        return []

    def targets(self):
        return set(self.targets_)

    def lun0_device(self, id):
        return self.targets_.get(id, {}).get('dev')

    def sessions(self, id):
        return list(self.sessions_.get(id, []))


class FakeHost:
    """Stands in for lvm, dd, blkid, mkfs, mount, nft and systemctl; vgs maps name -> {tags, size, free, lvs}."""
    def __init__(self, tmp, vgs=None, dd_rc=0, blkid_rc=2, fenced_set=(), targetctl_active=True, targets=None,
                 sessions=None):
        self.tmp, self.calls, self.markers = tmp, [], {}
        self.vgs = vgs if vgs is not None else {'retained': vg(), 'regen': vg()}
        self.dd_rc, self.blkid_rc, self.fenced_set = dd_rc, blkid_rc, set(fenced_set)
        self.targetctl_active = targetctl_active
        self.lock = FakeLock()
        self.lio = FakeLio(self, targets, sessions)

    def record(self, *call):
        self.calls.append((call, self.lock.held))

    def run(self, argv, check=True):
        self.record(*argv)
        rc, out = 0, ''
        a = list(argv)
        if a[0] == 'vgs':
            out = json.dumps({'report': [{'vg': [
                {'vg_name': n, 'vg_tags': ','.join(sorted(v['tags'])), 'vg_size': str(v['size']), 'vg_free': str(v['free'])}
                for n, v in self.vgs.items()]}]})
        elif a[0] == 'lvs':
            out = json.dumps({'report': [{'lv': [
                {'vg_name': n, 'lv_name': i, 'lv_size': str(lv['size']), 'lv_tags': ','.join(sorted(lv['tags']))}
                for n, v in self.vgs.items() for i, lv in v['lvs'].items()]}]})
        elif a[0] == 'vgchange':
            v = self.vgs[a[-1]]
            for flag, tag in zip(a[1:-1:2], a[2:-1:2]):
                (v['tags'].add if flag == '--addtag' else v['tags'].discard)(tag)
        elif a[0] == 'lvchange':
            vgn, i = a[-1].split('/')
            tags = self.vgs[vgn]['lvs'][i]['tags']
            for flag, tag in zip(a[1:-1:2], a[2:-1:2]):
                (tags.add if flag == '--addtag' else tags.discard)(tag)
        elif a[0] == 'lvcreate':
            lvs = self.vgs[a[-1]]['lvs']
            name, size = a[a.index('-n') + 1], int(a[a.index('-L') + 1][:-1])
            if name in lvs:
                rc = 5
            else:
                lvs[name] = {'size': size, 'tags': set()}
        elif a[0] == 'lvextend':
            vgn, i = a[-1].split('/')
            self.vgs[vgn]['lvs'][i]['size'] = int(a[a.index('-L') + 1][:-1])
        elif a[0] == 'dd':
            rc = self.dd_rc
        elif a[0] == 'blkid':
            rc = self.blkid_rc
        elif a[0] == 'umount':  # the marker lands on the LV, not in the local dir
            for f in Path(a[-1]).iterdir():
                self.markers[f.name] = f.read_text()
                f.unlink()
        elif a[:3] == ['nft', '-j', 'list']:
            out = json.dumps({'nftables': [{'metainfo': {}}, {'set': {
                'family': 'inet', 'table': 'filter', 'name': 'fenced', 'type': 'ipv4_addr',
                **({'elem': sorted(self.fenced_set)} if self.fenced_set else {})}}]})
        elif a[:3] == ['nft', 'add', 'element']:
            self.fenced_set.add(a[-1].strip('{} '))
        elif a[:2] == ['systemctl', 'is-active']:
            rc = 0 if self.targetctl_active else 3
        if check and rc:
            raise subprocess.CalledProcessError(rc, argv, out, 'fake failure')
        return subprocess.CompletedProcess(argv, rc, out, '')

    def install(self, test):
        for target, name, value in [
                (host, 'run', self.run), (host, 'LIO_LOCK', self.lock),
                (host, 'checkpoint', lambda name: self.record('checkpoint', name)),
                (lio, 'Lio', lambda: self.lio), (metrics, 'TEXTFILE_DIR', self.tmp / 'textfile'),
                (reconcile.time, 'time', lambda: NOW), (reconcile, 'MARKER_TMP', self.tmp)]:
            p = mock.patch.object(target, name, value)
            p.start()
            test.addCleanup(p.stop)
        (self.tmp / 'textfile').mkdir(exist_ok=True)

    def names(self):
        """Calls as short strings: the command name, or 'checkpoint:<n>' / 'lio.<m>'."""
        out = []
        for call, _ in self.calls:
            out.append(f'checkpoint:{call[1]}' if call[0] == 'checkpoint' else call[0])
        return out

    def find(self, *prefix):
        return [c for c, _ in self.calls if c[:len(prefix)] == prefix]


def vg(tags=(), lvs=None, size=100 * GI, free=50 * GI):
    return {'tags': set(tags), 'size': size, 'free': free,
            'lvs': {i: {'size': s, 'tags': set(t)} for i, (s, t) in (lvs or {}).items()}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def fake(self, **kw):
        f = FakeHost(self.tmp, **kw)
        f.install(self)
        self.f = f
        return f

    def apply(self, entries, *extra, cfg=None):
        inv, conf = self.tmp / 'inv.json', self.tmp / 'config.json'
        inv.write_text(json.dumps(entries))
        conf.write_text(json.dumps(cfg or config()))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = reconcile.main(['apply', '--inventory', str(inv), '--config', str(conf), *extra])
        self.err = err.getvalue()
        return rc, (json.loads(out.getvalue()) if out.getvalue() else None)


class CreateTest(Base):
    def test_create_new_order(self):
        f = self.fake()
        rc, out = self.apply([entry()])
        self.assertEqual(rc, 0, self.err)
        names = [n for n in f.names() if n not in ('vgs', 'lvs', 'systemctl', 'nft')]
        self.assertEqual(names, [
            'vgchange', 'checkpoint:create:ledger-creating', 'lvcreate', 'checkpoint:create:lvcreate',
            'dd', 'checkpoint:create:read', 'blkid', 'checkpoint:create:blkid', 'mkfs.ext4', 'checkpoint:create:mkfs',
            'mount', 'umount', 'checkpoint:create:marker', 'vgchange', 'checkpoint:create:ledger-created',
            'lio.ensure_target', 'checkpoint:create:target', 'lio.save', 'checkpoint:create:save', 'lio.save'])
        self.assertEqual(f.find('vgchange')[0], ('vgchange', '--addtag', 'ledger:a:creating', 'retained'))
        self.assertEqual(f.find('lvcreate')[0],
                         ('lvcreate', '-n', 'a', '-L', f'{GI}b', '-W', 'y', '--yes', '-Z', 'y', 'retained'))
        self.assertEqual(f.find('dd')[0], ('dd', 'if=/dev/retained/a', 'of=/dev/null', 'bs=1M', 'count=1', 'iflag=direct'))
        self.assertEqual(f.find('blkid')[0], ('blkid', '-p', '/dev/retained/a'))
        self.assertEqual(f.find('mkfs.ext4')[0], ('mkfs.ext4', '-O', 'mmp', '-e', 'remount-ro', '-E',
                                                  'mmp_update_interval=5', '/dev/retained/a'))
        mount, umount = f.find('mount')[0], f.find('umount')[0]
        self.assertEqual(mount[-2], '/dev/retained/a')
        self.assertEqual(umount[-1], mount[-1])
        self.assertTrue(mount[-1].startswith(str(self.tmp)))
        self.assertFalse(Path(mount[-1]).exists())  # the private dir is removed after umount
        self.assertEqual(f.markers, {'.homelab-a-data': 'homelab/a/data/v1\n'})
        self.assertEqual(f.find('vgchange')[1],
                         ('vgchange', '--deltag', 'ledger:a:creating', '--addtag', 'ledger:a:created', 'retained'))
        self.assertEqual(f.vgs['retained']['tags'], {'ledger:a:created'})
        self.assertEqual(f.find('lio.ensure_target')[0],
                         ('lio.ensure_target', 'a', '/dev/retained/a', (W1, W2), '192.0.2.10:3260'))
        self.assertEqual(out['exported'], [{'id': 'a', 'size_bytes': GI}])

    def test_unreadable_device_never_formats(self):
        f = self.fake(dd_rc=1)
        rc, out = self.apply([entry()])
        self.assertEqual(rc, 1)
        self.assertEqual(f.find('mkfs.ext4'), [])
        self.assertEqual(f.find('blkid'), [])
        self.assertEqual(f.vgs['retained']['tags'], {'ledger:a:creating'})
        self.assertEqual(out['exported'], [])

    def test_blkid_signature_never_formats(self):
        for rc_blkid in (0, 4, 8):
            with self.subTest(blkid=rc_blkid):
                f = self.fake(blkid_rc=rc_blkid)
                rc, _ = self.apply([entry()])
                self.assertEqual(rc, 1)
                self.assertEqual(f.find('mkfs.ext4'), [])
                self.assertEqual(f.vgs['retained']['tags'], {'ledger:a:creating'})
                self.assertEqual(f.find('lio.ensure_target'), [])

    def test_existing_lv_never_formatted(self):
        f = self.fake(vgs={'retained': vg(['ledger:a:created'], {'a': (GI, ())}), 'regen': vg()})
        rc, out = self.apply([entry()])
        self.assertEqual(rc, 0, self.err)
        for cmd in ('mkfs.ext4', 'blkid', 'dd', 'lvcreate', 'mount'):
            self.assertEqual(f.find(cmd), [], cmd)
        self.assertEqual(len(f.find('lio.ensure_target')), 1)

    def test_create_raw_has_no_mkfs(self):
        f = self.fake()
        rc, _ = self.apply([entry(origin='migrated')])
        self.assertEqual(rc, 0, self.err)
        for cmd in ('mkfs.ext4', 'blkid', 'dd', 'mount'):
            self.assertEqual(f.find(cmd), [], cmd)
        self.assertEqual(f.find('lvcreate')[0][-1], 'retained')
        self.assertEqual(f.vgs['retained']['tags'], {'ledger:a:created'})
        self.assertEqual(f.markers, {})
        self.assertEqual(len(f.find('lio.ensure_target')), 1)


class FenceTest(Base):
    def test_fenced_worker_acl_omitted(self):
        f = self.fake(vgs={'retained': vg(['ledger:a:created', f'fenced:w2:{UUID_A}'], {'a': (GI, ())}), 'regen': vg()},
                      fenced_set=['192.0.2.22'])
        rc, _ = self.apply([entry()])
        self.assertEqual(rc, 0, self.err)
        self.assertEqual(f.find('lio.ensure_target')[0][3], (W1,))

    def test_fence_applies_to_regen_targets(self):
        f = self.fake(vgs={'retained': vg([f'fenced:w2:{UUID_A}']),
                           'regen': vg(['ledger:p:created'], {'p': (GI, ())})}, fenced_set=['192.0.2.22'])
        rc, _ = self.apply([entry('p', cls='regenerable')])
        self.assertEqual(rc, 0, self.err)
        self.assertEqual(f.find('lio.ensure_target')[0][1:4], ('p', '/dev/regen/p', (W1,)))

    def test_fenced_worker_missing_from_set_fails(self):
        for tag in (f'fenced:w2:{UUID_A}', f'fenced:ghost:{UUID_A}'):
            with self.subTest(tag=tag):
                f = self.fake(vgs={'retained': vg(['ledger:a:created', tag], {'a': (GI, ())}), 'regen': vg()})
                rc, _ = self.apply([entry()])
                self.assertEqual(rc, 1)
                self.assertEqual(f.find('lio.ensure_target')[0][3], (W1,) if 'w2' in tag else (W1, W2))
                self.assertIn('fenced', self.err)


class BootCheckTest(Base):
    def check(self, *extra):
        conf = self.tmp / 'config.json'
        conf.write_text(json.dumps(config()))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = reconcile.main(['boot-check', '--config', str(conf), *extra])
        self.err = err.getvalue()
        return rc

    def good(self, **kw):
        return self.fake(vgs={'retained': vg(['ledger:a:created', f'fenced:w2:{UUID_A}'], {'a': (GI, ())}),
                              'regen': vg(['ledger:p:created'], {'p': (GI, ())})},
                         targets={'a': {'dev': '/dev/retained/a', 'acls': {W1, W2}},
                                  'p': {'dev': '/dev/regen/p', 'acls': {W1, W2}}}, **kw)

    def test_boot_check_fails_on_lunless_target(self):
        f = self.good()
        self.assertEqual(self.check(), 0, self.err)
        del f.lio.targets_['p']
        self.assertEqual(self.check(), 1)
        f.lio.targets_['p'] = {'dev': '/dev/regen/other', 'acls': set()}
        self.assertEqual(self.check(), 1)
        self.assertIn('/dev/regen/p', self.err)

    def test_boot_check_fails_on_missing_vg(self):
        self.fake(vgs={'retained': vg(['ledger:a:created'], {'a': (GI, ())})},
                  targets={'a': {'dev': '/dev/retained/a', 'acls': set()}})
        self.assertEqual(self.check(), 1)
        self.assertIn('regen is missing', self.err)

    def test_boot_check_strips_fenced_acls_before_check(self):
        f = self.good()
        self.assertEqual(self.check(), 0, self.err)
        names = f.names()
        self.assertEqual(f.find('lio.delete_acl'), [('lio.delete_acl', W2)])
        self.assertLess(names.index('lio.delete_acl'), names.index('lio.save'))
        self.assertEqual(f.lio.targets_['a']['acls'], {W1})
        self.assertTrue(all(held for c, held in f.calls if c[0] in ('lio.delete_acl', 'lio.save')))

    def test_restore_set_adds_every_tagged_address(self):
        f = self.fake(vgs={'retained': vg([f'fenced:w1:{UUID_A}', f'fenced:w2:{UUID_A}']), 'regen': vg()})
        self.assertEqual(reconcile.restore_set(config()), 0)
        self.assertEqual(f.fenced_set, {'192.0.2.21', '192.0.2.22'})
        self.assertTrue(all(held for c, held in f.calls if c[:3] == ('nft', 'add', 'element')))

    def test_restore_set_unknown_node_fails(self):
        f = self.fake(vgs={'retained': vg([f'fenced:ghost:{UUID_A}', f'fenced:w2:{UUID_A}']), 'regen': vg()})
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(reconcile.restore_set(config()), 1)
        self.assertEqual(f.fenced_set, {'192.0.2.22'})  # the known worker is fenced despite the stale tag


class GrowAndReportTest(Base):
    def test_extend_then_checkpoint(self):
        f = self.fake(vgs={'retained': vg(['ledger:a:created'], {'a': (GI, ())}), 'regen': vg()})
        rc, out = self.apply([entry(size='2Gi')])
        self.assertEqual(rc, 0, self.err)
        self.assertEqual(f.find('lvextend')[0], ('lvextend', '-L', f'{2 * GI}b', 'retained/a'))
        names = f.names()
        i = names.index('lvextend')
        self.assertEqual(names[i + 1], 'checkpoint:grow:lvextend')
        self.assertLess(names.index('lio.ensure_target'), i)
        self.assertEqual(out['exported'], [{'id': 'a', 'size_bytes': 2 * GI}])

    def test_exported_lists_every_lv(self):
        self.fake(vgs={'retained': vg(['ledger:a:created', 'ledger:b:created', 'ledger:gone:created'],
                                      {'a': (GI, ()), 'b': (GI, ()), 'gone': (3 * GI, ())}),
                       'regen': vg(['ledger:p:created'], {'p': (GI, ())})})
        rc, out = self.apply([entry('a', size='2Gi'), entry('b'), entry('p', cls='regenerable')])
        self.assertEqual(rc, 0, self.err)
        self.assertEqual(sorted((e['id'], e['size_bytes']) for e in out['exported']),
                         [('a', 2 * GI), ('b', GI), ('gone', 3 * GI), ('p', GI)])
        self.assertIn({'kind': 'mark_pending', 'id': 'gone', 'vg': 'retained', 'size_bytes': 0}, out['actions'])
        self.assertIn(f'pending-delete:{NOW}', self.f.vgs['retained']['lvs']['gone']['tags'])

    def test_readded_entry_clears_pending(self):
        f = self.fake(vgs={'retained': vg(['ledger:a:created'], {'a': (GI, ['pending-delete:123'])}), 'regen': vg()})
        rc, _ = self.apply([entry()])
        self.assertEqual(rc, 0, self.err)
        self.assertEqual(f.find('lvchange')[0], ('lvchange', '--deltag', 'pending-delete:123', 'retained/a'))

    def test_metrics_written(self):
        self.fake(vgs={'retained': vg(['ledger:a:created', 'ledger:old:created'],
                                      {'a': (GI, ()), 'old': (GI, ['pending-delete:123'])}),
                       'regen': vg(['ledger:p:created'], {'p': (GI, ())}, size=10 * GI, free=4 * GI)})
        real = self.f.lio.ensure_target

        def ensure_except_p(id, *args):  # p never gets a target
            if id != 'p':
                real(id, *args)
        self.f.lio.ensure_target = ensure_except_p
        rc, _ = self.apply([entry('a'), entry('p', cls='regenerable')])
        self.assertEqual(rc, 0, self.err)
        text = (self.tmp / 'textfile/storage-reconcile.prom').read_text()
        for line in ('storage_vg_size_bytes{vg="regen"} 10737418240', 'storage_vg_free_bytes{vg="regen"} 4294967296',
                     'storage_vg_free_bytes{vg="retained"} 53687091200', 'storage_target_missing{id="a"} 0',
                     'storage_target_missing{id="p"} 1', 'storage_pending_delete{id="old",since="123"} 1',
                     'storage_hold_create 0', f'storage_last_reconcile_success_timestamp_seconds {NOW}'):
            self.assertIn(line + '\n', text)
        self.assertFalse(list((self.tmp / 'textfile').glob('*.tmp')))

    def test_failed_run_keeps_old_success_timestamp(self):
        self.fake(vgs={'retained': vg(['ledger:a:created', 'hold_create'], {'a': (GI, ())}), 'regen': vg()})
        (self.tmp / 'textfile/storage-reconcile.prom').write_text('storage_last_reconcile_success_timestamp_seconds 42\n')
        rc, out = self.apply([entry('a'), entry('b')])
        self.assertEqual(rc, 1)
        self.assertEqual(out['refusals'], [{'id': 'b', 'reason': 'hold-create'}])
        self.assertEqual(len(self.f.find('lio.ensure_target')), 1)  # hold_create still exports what exists
        text = (self.tmp / 'textfile/storage-reconcile.prom').read_text()
        self.assertIn('storage_last_reconcile_success_timestamp_seconds 42\n', text)
        self.assertIn('storage_hold_create 1\n', text)

    def test_refusal_exits_nonzero(self):
        self.fake(vgs={'retained': vg([], {'a': (GI, ())}), 'regen': vg()})
        rc, out = self.apply([entry('a')])
        self.assertEqual(rc, 1)
        self.assertEqual(out['refusals'], [{'id': 'a', 'reason': 'orphan'}])


class LockAndTargetctlTest(Base):
    def test_whole_run_holds_lio_lock(self):
        f = self.fake(vgs={'retained': vg(['ledger:a:created', f'fenced:w2:{UUID_A}'], {'a': (GI, ())}), 'regen': vg()},
                      fenced_set=['192.0.2.22'])
        rc, _ = self.apply([entry('a', size='2Gi'), entry('b')])
        self.assertEqual(rc, 0, self.err)
        for call, held in f.calls:
            if call[0] in WRITES or call[0].startswith('lio.') or call[0] == 'checkpoint' or call[:2] == ('nft', '-j'):
                self.assertTrue(held, call)
        self.assertIn('lvextend', f.names())

    def test_targetctl_started_only_when_inactive_and_sessionless(self):
        f = self.fake(targetctl_active=False)
        rc, _ = self.apply([entry()])
        self.assertEqual(rc, 0, self.err)
        starts = [(c, held) for c, held in f.calls if c[:2] == ('systemctl', 'start')]
        self.assertEqual(starts, [(('systemctl', 'start', 'rtslib-fb-targetctl'), False)])
        names = f.names()
        self.assertLess(len(names) - 1 - names[::-1].index('lio.save'), names.index('systemctl'))

        f = self.fake(targetctl_active=False, vgs={'retained': vg(['ledger:a:created'], {'a': (GI, ())}), 'regen': vg()},
                      sessions={'a': [W1]})
        rc, _ = self.apply([entry()])
        self.assertEqual(rc, 0, self.err)
        self.assertEqual(f.find('systemctl', 'start'), [])
        self.assertIn('warning', self.err)

        f = self.fake(targetctl_active=True)
        self.apply([entry()])
        self.assertEqual(f.find('systemctl', 'start'), [])

    def test_dry_run_makes_no_writes(self):
        state = lambda: {'retained': vg(['ledger:a:created', 'hold_create'], {'a': (GI, ()), 'old': (GI, ())}),
                         'regen': vg()}
        f = self.fake(vgs=state())
        rc, dry = self.apply([entry('a', size='2Gi'), entry('b')], '--dry-run')
        self.assertEqual(rc, 0, self.err)  # hold_create alone does not block the dry run
        self.assertEqual(sorted({c[0] for c, _ in f.calls}), ['lvs', 'vgs'])
        self.assertEqual(list((self.tmp / 'textfile').iterdir()), [])
        f2 = self.fake(vgs=state())
        rc2, real = self.apply([entry('a', size='2Gi'), entry('b')])
        self.assertEqual(rc2, 1)
        self.assertEqual((dry['actions'], dry['refusals']), (real['actions'], real['refusals']))

    def test_dry_run_blocks_on_blocking_failures(self):
        self.fake(vgs={'retained': vg(['ledger:a:created']), 'regen': vg()})
        self.assertEqual(self.apply([entry('a')], '--dry-run')[0], 1)  # ledgered LV missing
        self.fake(vgs={'retained': vg()})
        self.assertEqual(self.apply([entry('a')], '--dry-run')[0], 1)  # VG regen missing
        self.fake(vgs={'retained': vg([], {'x': (GI, ())}), 'regen': vg()})
        self.assertEqual(self.apply([entry('a')], '--dry-run')[0], 0)  # an orphan refusal is not blocking


class CliTest(Base):
    def test_validate_does_no_host_io(self):
        f = self.fake()
        inv = self.tmp / 'inv.json'
        inv.write_text(json.dumps([entry()]))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(reconcile.main(['validate', '--inventory', str(inv)]), 0)
        inv.write_text(json.dumps([entry(id='Bad')]))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(reconcile.main(['validate', '--inventory', str(inv)]), 1)
        self.assertEqual(f.calls, [])

    def test_wrapper_runs_isolated(self):
        dest = self.tmp / 'pkg'
        shutil.copytree(FILES / 'homelab_storage', dest / 'homelab_storage')
        shutil.copy(FILES / 'storage-reconcile', dest)
        inv = self.tmp / 'inv.json'
        inv.write_text(json.dumps([entry()]))
        r = subprocess.run([sys.executable, '-I', str(dest / 'storage-reconcile'), 'validate', '--inventory', str(inv)],
                           cwd=self.tmp, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_config_rejects_unknown_and_missing_keys(self):
        from homelab_storage import config as cfgmod
        p = self.tmp / 'c.json'
        for bad in (config(extra=1), {k: v for k, v in config().items() if k != 'portal'},
                    config(portal='0.0.0.0:3260'), config(max_removals='2'), config(vgs=['retained'])):
            p.write_text(json.dumps(bad))
            with self.subTest(bad=bad), self.assertRaises(cfgmod.ConfigError):
                cfgmod.load(p)
        p.write_text(json.dumps(config()))
        self.assertEqual(cfgmod.load(p)['max_removals'], 2)


class AdapterTest(Base):
    def test_wwn_stable(self):
        self.assertEqual(lio.wwn('media'), '34de5e5b-5881-5bc4-b64c-5bacbf263e3a')  # pinned: a rebuild must match
        self.assertNotEqual(lio.wwn('media'), lio.wwn('media2'))
        self.assertEqual(lio.iqn('media'), 'iqn.2026-11.net.starktastic.storage:media')
        self.assertEqual(lio.worker_iqn('w1'), W1)

    def test_save_is_atomic(self):
        save = self.tmp / 'saveconfig.json'
        save.write_text('{"old": 1}')
        seen = []

        class Root:
            def save_to_file(self, path):
                seen.append(path)
                if fail:
                    Path(path).write_text('{"half')
                    raise OSError('disk full')
                Path(path).write_text('{"new": 1}')

        fake_rtslib = types.SimpleNamespace(RTSRoot=Root)
        with mock.patch.dict(sys.modules, {'rtslib_fb': fake_rtslib}), mock.patch.object(lio, 'SAVE_FILE', save):
            fail = True
            with self.assertRaises(OSError):
                lio.Lio().save()
            self.assertEqual(save.read_text(), '{"old": 1}')
            fail = False
            lio.Lio().save()
        self.assertEqual(save.read_text(), '{"new": 1}')
        self.assertTrue(all(Path(p).parent == save.parent and Path(p) != save for p in seen))
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ['saveconfig.json'])

    def test_sessions_reads_configfs(self):
        root = self.tmp / 'iscsi'
        tpg = root / lio.iqn('a') / 'tpgt_1'
        (tpg / 'acls' / W1).mkdir(parents=True)
        (tpg / 'acls' / W2).mkdir(parents=True)
        (tpg / 'acls' / W1 / 'info').write_text(f'InitiatorName: {W1}\nLIO Session ID: 3   ISID: 0x1\n')
        (tpg / 'acls' / W2 / 'info').write_text(f'No active iSCSI Session for Initiator Endpoint: {W2}\n')
        (tpg / 'dynamic_sessions').write_text('')
        with mock.patch.object(lio, 'CONFIGFS', root):
            l = object.__new__(lio.Lio)
            self.assertEqual(l.sessions('a'), [W1])
            (tpg / 'acls' / W1 / 'info').unlink()
            (tpg / 'acls' / W1 / 'info').mkdir()  # unreadable as a file
            with self.assertRaises(lio.InUse):
                l.sessions('a')
            with self.assertRaises(lio.InUse):
                l.sessions('missing')

    def test_nft_parses_set(self):
        out = {'nftables': [{'metainfo': {}}, {'set': {'name': 'fenced', 'elem': ['192.0.2.22', {'elem': {'val': '192.0.2.23'}}]}}]}
        calls = []
        def run(argv, check=True):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, json.dumps(out), '')
        with mock.patch.object(host, 'run', run):
            self.assertTrue(nft.is_fenced('192.0.2.22'))
            self.assertTrue(nft.is_fenced('192.0.2.23'))
            self.assertFalse(nft.is_fenced('192.0.2.21'))
            nft.ensure_fenced('192.0.2.21')
            nft.remove_fenced('192.0.2.21')  # not in the set: no delete
            nft.remove_fenced('192.0.2.22')
            with self.assertRaises(ValueError):
                nft.ensure_fenced('192.0.2.1 }; flush ruleset; {')
        self.assertEqual(calls[0], ['nft', '-j', 'list', 'set', 'inet', 'filter', 'fenced'])
        self.assertIn(['nft', 'add', 'element', 'inet', 'filter', 'fenced', '{ 192.0.2.21 }'], calls)
        self.assertIn(['nft', 'delete', 'element', 'inet', 'filter', 'fenced', '{ 192.0.2.22 }'], calls)
        self.assertNotIn(['nft', 'delete', 'element', 'inet', 'filter', 'fenced', '{ 192.0.2.21 }'], calls)

    def test_checkpoint_pauses_only_when_configured(self):
        err = io.StringIO()
        with mock.patch.object(host, 'CONFIG', {'pause_after': ['create:mkfs']}), \
                mock.patch.object(host.os, 'kill') as kill, contextlib.redirect_stderr(err):
            host.checkpoint('create:lvcreate')
            kill.assert_not_called()
            host.checkpoint('create:mkfs')
            kill.assert_called_once_with(os.getpid(), signal.SIGSTOP)
        self.assertIn(f'PAUSED at create:mkfs pid={os.getpid()}', err.getvalue())
        with mock.patch.object(host, 'CONFIG', {}), mock.patch.object(host.os, 'kill') as kill:
            host.checkpoint('create:mkfs')
            kill.assert_not_called()

    def test_lio_lock_is_flock(self):
        with mock.patch.object(host.LIO_LOCK, 'path', self.tmp / 'storage-lio'):
            with host.LIO_LOCK:
                r = subprocess.run(['flock', '-n', str(self.tmp / 'storage-lio'), 'true'])
                self.assertEqual(r.returncode, 1)
            self.assertEqual(subprocess.run(['flock', '-n', str(self.tmp / 'storage-lio'), 'true']).returncode, 0)


if __name__ == '__main__':
    unittest.main()

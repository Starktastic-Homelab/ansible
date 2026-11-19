"""Every LVM command opens one VG's PV only, with a time limit; a VG that does not answer reads as absent (ADR 0017
F11, F12)."""
import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_storage_reconcile import GI, PV, Base, config, entry, vg  # noqa: E402
import test_storage_expiry  # noqa: E402
import test_storage_fencer  # noqa: E402
from homelab_storage import fencer, host, lvm, reconcile  # noqa: E402

READS = ('vgs', 'lvs', 'pvs')


class Scoped:
    def assertScoped(self, f):
        calls = [c for c in f.raw if c[0][0] in test_storage_fencer.LVM_TOOLS]
        self.assertTrue(calls)
        for argv, timeout, key in calls:
            with self.subTest(argv=argv):
                self.assertEqual(key, argv[-1].split('/')[0])  # the per-VG key rule (F11)
                self.assertNotEqual(argv[0], 'pvscan')
                self.assertEqual(argv[1], '--devices')
                vgn = argv[-1].split('/')[0]
                self.assertIn(vgn, PV)
                self.assertEqual(argv[2], PV[vgn])
                self.assertEqual([a for a in argv[3:-1] if a.split('/')[0] in PV], [])  # one VG named
                self.assertIsNotNone(timeout)
                self.assertLessEqual(timeout, lvm.LVM_TIMEOUT)
                if argv[0] in READS:
                    self.assertIn('--readonly', argv)


class StallPopen:
    """Popen stand-in: a child for a VG in STALL never answers (and stays alive); others exit 0 at once."""
    STALL, started = set(), []

    def __init__(self, argv, **kw):
        self.args, self.pid = argv, 4242
        self.stalled = argv[-1].split('/')[0] in StallPopen.STALL
        StallPopen.started.append(list(argv))

    def wait(self, timeout=None):
        if self.stalled:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return 0

    def poll(self):
        return None if self.stalled else 0


class KeyRuleTest(unittest.TestCase):
    """The real host.run under lvm: one live child per VG (spec F11 item 1)."""

    def setUp(self):
        StallPopen.STALL, StallPopen.started = set(), []
        for p in (mock.patch.object(subprocess, 'Popen', StallPopen), mock.patch.dict(host.CONFIG, config()),
                  contextlib.redirect_stderr(io.StringIO())):
            p.__enter__()
            self.addCleanup(p.__exit__, None, None, None)
        self.addCleanup(host._ABANDONED.clear)

    def test_two_regen_writes_start_one_child(self):
        StallPopen.STALL = {'regen'}
        for _ in range(2):
            with self.assertRaises(subprocess.TimeoutExpired):
                lvm.extend('regen', 'p', 2 << 30)
        self.assertEqual(len(StallPopen.started), 1)
        lvm.extend('retained', 'a', 2 << 30)  # the other VG is unaffected
        self.assertEqual([a[-1] for a in StallPopen.started], ['regen/p', 'retained/a'])

    def test_abandoned_retained_tag_write_blocks_next_read(self):
        StallPopen.STALL = {'retained'}
        with self.assertRaises(subprocess.TimeoutExpired):
            lvm.vg_tags('retained', add=['fenced:w2:x'])
        StallPopen.STALL = set()  # a fresh read would now answer, before the abandoned write lands
        with self.assertRaises(RuntimeError):
            fencer.Fencer(config()).cluster()
        self.assertEqual(len(StallPopen.started), 1)  # no LVM call: the fencer decides nothing on stale tags


class ReconcileScopeTest(Base, Scoped):
    def test_every_lvm_argv_scoped_apply(self):
        f = self.fake(vgs={'retained': vg(['ledger:a:created'], {'a': (GI, ())}), 'regen': vg()})
        rc, _ = self.apply([entry('a', size='2Gi'), entry('n'), entry('r', cls='regenerable')])
        self.assertEqual(rc, 0, self.err)
        self.assertTrue(f.find('lvextend') and f.find('lvcreate'))
        self.assertScoped(f)

    def test_regen_stall_reads_absent(self):
        self.fake(stalled={'regen'})
        with contextlib.redirect_stderr(io.StringIO()) as err:
            got = lvm.vgs(('retained', 'regen'))
        self.assertTrue(got['retained'].present)
        self.assertFalse(got['regen'].present)
        self.assertIn(f'VG regen unreadable on {PV["regen"]}', err.getvalue())

    def test_lvs_failure_is_absent(self):
        self.fake(fail={('lvs', 'retained'): 5})
        with contextlib.redirect_stderr(io.StringIO()):
            got = lvm.vgs(('retained', 'regen'))
        self.assertFalse(got['retained'].present)
        self.assertTrue(got['regen'].present)

    def test_missing_pv_is_absent_not_raise(self):
        self.fake(fail={('vgs', 'regen'): 5})
        with contextlib.redirect_stderr(io.StringIO()) as err:
            got = lvm.vgs(('retained', 'regen'))
        self.assertFalse(got['regen'].present)
        self.assertIn('VG regen unreadable', err.getvalue())

    def test_shared_deadline(self):
        f = self.fake()
        with contextlib.redirect_stderr(io.StringIO()):
            lvm.vgs(('retained',))
        (_, t_vgs), (_, t_lvs) = f.lvm_raw()
        self.assertLessEqual(t_vgs, lvm.LVM_TIMEOUT)
        self.assertLessEqual(t_lvs, t_vgs)

    def test_vg_tags_only_runs_vgs(self):
        f = self.fake(vgs={'retained': vg(['x']), 'regen': vg()})
        got = lvm.vg_tags_only('retained')
        self.assertEqual(got.tags, ('x',))
        self.assertEqual([a[0] for a, _ in f.lvm_raw()], ['vgs'])
        self.assertScoped(f)

    def test_withdraw_restore_needs_no_pvscan(self):
        f = self.fake(stale_hints={'regen'})
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(lvm.vgs(('retained', 'regen'))['regen'].present)
        self.assertEqual(f.find('pvscan'), [])

    def test_remove_and_activate_scoped(self):
        f = self.fake(vgs={'retained': vg(), 'regen': vg(lvs={'p': (GI, ())})})
        lvm.activate('regen')
        lvm.remove('regen', 'p')
        self.assertEqual([a[:1] + a[-2:] for a, _ in f.lvm_raw()],
                         [['vgchange', '-ay', 'regen'], ['lvremove', '-y', 'regen/p']])
        self.assertScoped(f)


class BootAndExpiryScopeTest(Base, Scoped):
    def test_every_lvm_argv_scoped_boot_check(self):
        f = self.fake(vgs={'retained': vg(['ledger:a:created'], {'a': (GI, ())}), 'regen': vg()},
                      targets={'a': {'dev': '/dev/retained/a', 'acls': set()}})
        conf = self.tmp / 'config.json'
        conf.write_text(json.dumps(config()))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(reconcile.main(['boot-check', '--config', str(conf)]), 0)
        self.assertScoped(f)

    def test_every_lvm_argv_scoped_expire(self):
        retained = vg({'ledger:a:created'}, {'a': (GI, {test_storage_expiry.OLD})})
        f = test_storage_expiry.ExpHost(self.tmp, vgs={'retained': retained, 'regen': vg()},
                                        targets={'a': {'dev': '/dev/retained/a', 'acls': set()}})
        f.install(self)
        conf = self.tmp / 'config.json'
        conf.write_text(json.dumps(config()))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(reconcile.main(['expire', '--id', 'a', '--config', str(conf)]), 0)
        self.assertTrue(f.find('lvremove'))
        self.assertScoped(f)


class FencerScopeTest(test_storage_fencer.Base, Scoped):
    def test_every_lvm_argv_scoped_fence(self):
        f = self.hung_w2()
        self.loop()
        self.assertTrue(f.fence_tags())
        self.assertScoped(f)

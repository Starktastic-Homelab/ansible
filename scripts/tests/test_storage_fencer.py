"""Offline checks for the fencer executor, loop and CLI: host, LIO, nft, ss and the API faked, every call recorded."""
import contextlib
import copy
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_storage_reconcile import FILES, FakeHost, FakeLio, config, vg  # noqa: E402

from homelab_storage import fence_policy, fencer, host, kube, lio  # noqa: E402

SERVER_NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)
LOCAL_NOW = 1_800_000_000
A, B, C, D = ('aaaaaaaa-0000-4000-8000-00000000000a', 'bbbbbbbb-0000-4000-8000-00000000000b',
              'cccccccc-0000-4000-8000-00000000000c', 'dddddddd-0000-4000-8000-00000000000d')
UNSETTLED = 'fenced:w2:unknown'
W1, W2 = lio.worker_iqn('w1'), lio.worker_iqn('w2')
W2_ADDR = '192.0.2.22'
OOS = dict(kube.OUT_OF_SERVICE)
SS_LINE = '0 0 192.0.2.10:3260 192.0.2.22:51234'
WRITES = {'vgchange', 'nft add', 'nft delete', 'lio.delete_acl', 'lio.add_acl', 'lio.save', 'lio.ensure_target',
          'api.patch'}


class Crash(BaseException):
    """A kill at a checkpoint: not an Exception, so nothing in the fencer catches it."""


def node(name, ready=True, age=1000, bootid=A, taints=None):
    since = (SERVER_NOW - timedelta(seconds=age)).strftime('%Y-%m-%dT%H:%M:%SZ')
    return {'metadata': {'name': name, 'resourceVersion': '7'}, 'spec': {'taints': list(taints or [])},
            'status': {'nodeInfo': {'bootID': bootid},
                       'conditions': [{'type': 'Ready', 'status': 'True' if ready else 'Unknown',
                                       'lastTransitionTime': since}]}}


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def time(self):
        return LOCAL_NOW + self.t


class FakeApi:
    def __init__(self, fake):
        self.fake, self.nodes, self.vas, self.error, self.patch_error = fake, {}, [], None, None

    def get(self, path):
        self.fake.record('api.get', path)
        if self.error:
            raise self.error
        if path == fencer.NODES:
            return {'items': copy.deepcopy(list(self.nodes.values()))}, SERVER_NOW
        if path == fencer.VAS:
            return {'items': copy.deepcopy(self.vas)}, SERVER_NOW
        return copy.deepcopy(self.nodes[path.rsplit('/', 1)[1]]), SERVER_NOW

    def patch_node_taints(self, name, taints, resource_version):
        self.fake.record('api.patch', name, tuple(t['key'] for t in taints))
        if self.error or self.patch_error:
            raise self.error or self.patch_error
        self.nodes[name]['spec']['taints'] = list(taints)


class FenceLio(FakeLio):
    acl_sticks = False  # a delete that leaves the ACL in place

    def delete_acl(self, acl_iqn):
        self.fake.record('lio.delete_acl', acl_iqn)
        out = []
        for id, t in sorted(self.targets_.items()):
            if acl_iqn in t['acls']:
                out.append({'target': lio.iqn(id), 'info': 'InitiatorName: ' + acl_iqn})
                if not self.acl_sticks:
                    t['acls'].discard(acl_iqn)
        return out

    def add_acl(self, acl_iqn):
        self.fake.record('lio.add_acl', acl_iqn)
        for t in self.targets_.values():
            t['acls'].add(acl_iqn)

    def acl_iqns(self, id):
        self.fake.record('lio.acl_iqns', id)
        return set(self.targets_[id]['acls'])


class FenceHost(FakeHost):
    def __init__(self, tmp, tags=(), fenced_set=(), acls=(W1, W2)):
        super().__init__(tmp, vgs={'retained': vg(tags), 'regen': vg()}, fenced_set=fenced_set,
                         targets={'a': {'dev': '/dev/retained/a', 'acls': set(acls)},
                                  'p': {'dev': '/dev/regen/p', 'acls': set(acls)}})
        self.lio = FenceLio(self, self.lio.targets_)
        self.api, self.clock, self.ss, self.nft_sticks = FakeApi(self), FakeClock(), [], True
        self.ss_errors = 0  # the next N `ss` calls fail

    def run(self, argv, check=True):
        a = list(argv)
        if a[0] == 'ss':
            self.record(*argv)
            if self.ss_errors:
                self.ss_errors -= 1
                raise subprocess.CalledProcessError(1, argv, '', 'fake failure')
            return subprocess.CompletedProcess(argv, 0, ''.join(l + '\n' for l in self.ss), '')
        if a[:3] == ['nft', 'delete', 'element']:
            self.record(*argv)
            self.fenced_set.discard(a[-1].strip('{} '))
            return subprocess.CompletedProcess(argv, 0, '', '')
        if a[:3] == ['nft', 'add', 'element'] and not self.nft_sticks:
            self.record(*argv)
            return subprocess.CompletedProcess(argv, 0, '', '')
        return super().run(argv, check)

    def install(self, test):
        super().install(test)
        for target, name, value in [(fencer, 'time', self.clock), (kube, 'Client', lambda *a: self.api),
                                    (fence_policy, 'HISTORY_PATH', self.tmp / 'bootids.json')]:
            p = mock.patch.object(target, name, value)
            p.start()
            test.addCleanup(p.stop)

    def steps(self):
        """The mutating calls and checkpoints, in order, as short names."""
        out = []
        for call, held in self.calls:
            name = f'{call[0]}:{call[1]}' if call[0] == 'checkpoint' else ' '.join(call[:2]) if call[0] == 'nft' else call[0]
            if name in WRITES or name.startswith('checkpoint:'):
                out.append((name, held))
        return out

    def writes(self):
        return [n for n, _ in self.steps() if n in WRITES]

    def tags(self):
        return self.vgs['retained']['tags']

    def fence_tags(self):
        return {t for t in self.tags() if t.startswith('fenced:')}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def fake(self, nodes=(), history=None, **kw):
        f = FenceHost(self.tmp, **kw)
        f.install(self)
        f.api.nodes = {n['metadata']['name']: n for n in nodes}
        if history is not None:
            (self.tmp / 'bootids.json').write_text(json.dumps(history))
        self.f = f
        return f

    def running(self, **cfg):
        """A Fencer past its first loop; a fresh one un-settles every tag on its first loop."""
        fz = fencer.Fencer(config(**cfg))
        fz.started = True
        return fz

    def loop(self, fencer_=None, **cfg):
        f = fencer_ or fencer.Fencer(config(**cfg))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            f.loop_once()
        self.err = err.getvalue()
        return f

    def cli(self, *argv):
        conf = self.tmp / 'config.json'
        conf.write_text(json.dumps(config()))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fencer.main(['--config', str(conf), *argv])
        self.err = err.getvalue()
        return rc, out.getvalue()

    def history(self):
        return json.loads((self.tmp / 'bootids.json').read_text())

    def prom(self):
        return (self.tmp / 'textfile/storage-fencer.prom').read_text()

    def hung_w2(self, **kw):
        """w1 healthy, w2 NotReady past the window: the loop fences w2."""
        return self.fake(nodes=[node('w1'), node('w2', ready=False, age=301)], **kw)

    def w2(self, **kw):
        """w2 posts a new status; its taints stay as the fencer left them."""
        self.f.api.nodes['w2'] = node('w2', taints=self.f.api.nodes['w2']['spec']['taints'], **kw)

    def vgchanges_in(self, fz):
        n = len(self.f.find('vgchange'))
        self.loop(fz)
        return self.f.find('vgchange')[n:]

    def assertFencedLive(self, f):
        self.assertIn(W2_ADDR, f.fenced_set)
        self.assertFalse(any(W2 in t['acls'] for t in f.lio.targets_.values()))
        self.assertEqual(f.api.nodes['w2']['spec']['taints'], [OOS])

    def assertUnfenced(self, f):
        self.assertEqual(f.fence_tags(), set())
        self.assertNotIn(W2_ADDR, f.fenced_set)
        self.assertTrue(all(W2 in t['acls'] for t in f.lio.targets_.values()))
        self.assertEqual(f.api.nodes['w2']['spec']['taints'], [])


class FenceTest(Base):
    def test_fence_step_order(self):
        f = self.hung_w2()
        self.loop()
        self.assertEqual([n for n, _ in f.steps()], [
            'vgchange', 'checkpoint:fence:1', 'nft add', 'checkpoint:fence:2', 'lio.delete_acl', 'checkpoint:fence:3',
            'checkpoint:fence:4', 'lio.save', 'checkpoint:fence:5', 'api.patch', 'checkpoint:fence:6'])
        self.assertTrue(all(held for _, held in f.steps()), f.steps())
        self.assertEqual(f.find('vgchange'), [('vgchange', '--addtag', UNSETTLED, 'retained')])
        self.assertIn('fence w2: tagged, bootID not settled yet', self.err)
        self.assertEqual(f.find('lio.delete_acl'), [('lio.delete_acl', W2)])
        for id in ('a', 'p'):  # both VGs' targets, each ACL's session logged before the delete
            self.assertIn(f'{lio.iqn(id)}: InitiatorName: {W2}', self.err)
        self.assertFencedLive(f)

    def test_proof_failure_no_taint(self):
        f = self.hung_w2()
        f.ss = [SS_LINE]
        fz = self.loop()
        self.assertEqual(f.find('api.patch'), [])
        self.assertEqual(f.find('lio.save'), [])
        self.assertGreaterEqual(f.clock.t, 60)
        self.assertIn('storage_fence_failures_total 1\n', self.prom())
        self.assertIn(UNSETTLED, f.tags())  # the tag and the drop stay; the next loop retries
        f.ss = []
        self.loop(fz)
        self.assertEqual(len(f.find('api.patch')), 1)
        self.assertIn('storage_fence_failures_total 1\n', self.prom())
        self.assertFencedLive(f)

    def test_proof_checks_all_three(self):
        def acl_present(f):
            f.lio.acl_sticks = True

        def addr_missing(f):
            f.nft_sticks = False

        def established(f):
            f.ss = [SS_LINE]
        for leg in (acl_present, addr_missing, established):
            with self.subTest(leg=leg.__name__):
                self.tmp = Path(tempfile.mkdtemp())
                self.addCleanup(shutil.rmtree, self.tmp)
                f = self.hung_w2()
                leg(f)
                self.loop()
                self.assertEqual(f.find('api.patch'), [])
                self.assertIn(('ss', '-Htn', 'state', 'established', '( sport = :3260 )', 'dst', W2_ADDR), f.find('ss'))

    def test_resume_after_crash_each_step(self):
        for step in range(1, 6):
            with self.subTest(step=step):
                self.tmp = Path(tempfile.mkdtemp())
                self.addCleanup(shutil.rmtree, self.tmp)
                f = self.hung_w2()

                def crash(name, at=f'fence:{step}'):
                    f.record('checkpoint', name)
                    if name == at:
                        raise Crash(name)
                with mock.patch.object(host, 'checkpoint', crash), self.assertRaises(Crash):
                    self.loop()
                self.assertEqual(f.find('api.patch'), [])
                self.loop(self.loop())  # a fresh fencer process, from the state the crash left; it settles next
                self.assertEqual(len(f.find('api.patch')), 1)
                self.assertEqual({t for t in f.tags() if t.startswith('fenced:')}, {f'fenced:w2:{A}'})
                self.assertFencedLive(f)

    def continue_under(self, hold):
        # ADR 0017 "It is level-triggered": a hold blocks new fences only, so a started fence still gets its taint.
        f = self.fake(nodes=[node('w1'), node('w2', ready=False)], tags=[hold, f'fenced:w2:{A}'])
        self.loop()
        self.assertFencedLive(f)
        self.assertEqual(len(f.find('api.patch')), 1)
        self.assertIn('storage_fence_active{node="w2"} 1\n', self.prom())
        self.assertIn('storage_fence_failures_total 0\n', self.prom())

    def test_continue_fence_taints_under_hold_fence(self):
        self.continue_under('hold_fence')

    def test_continue_fence_taints_under_hold_create(self):
        self.continue_under('hold_create')

    def test_continue_fence_no_taint_with_foreign_va(self):
        # A foreign VA blocks step 6, hold or not: the taint would force-detach a disk the fence proves nothing about.
        for tags in ([], ['hold_fence']):
            with self.subTest(tags=tags):
                self.tmp = Path(tempfile.mkdtemp())
                self.addCleanup(shutil.rmtree, self.tmp)
                f = self.fake(nodes=[node('w1'), node('w2', ready=False)], tags=[*tags, f'fenced:w2:{A}'])
                f.api.vas = [{'spec': {'nodeName': 'w2', 'attacher': 'csi.proxmox.sinextra.dev'}}]
                self.loop()
                self.assertEqual(f.find('api.patch'), [])
                self.assertIn(W2_ADDR, f.fenced_set)
                self.assertEqual(len(f.find('lio.save')), 1)  # steps 2-5 still run
                f.api.vas = []
                self.loop()
                self.assertEqual(len(f.find('api.patch')), 1)

    def test_new_fence_refused_under_hold(self):
        for hold in ('hold_fence', 'hold_create'):
            with self.subTest(hold=hold):
                self.tmp = Path(tempfile.mkdtemp())
                self.addCleanup(shutil.rmtree, self.tmp)
                f = self.hung_w2(tags=[hold])
                self.loop()
                self.assertEqual(f.find('vgchange'), [])
                self.assertEqual(f.find('api.patch'), [])
                self.assertEqual(f.fenced_set, set())
                self.assertIn(f'w2: {hold}', self.err)


class SettleTest(Base):
    def test_settle_after_proof(self):
        f = self.hung_w2()
        fz = self.loop()
        self.assertEqual(f.fence_tags(), {UNSETTLED})
        self.assertEqual(self.vgchanges_in(fz),
                         [('vgchange', '--deltag', UNSETTLED, '--addtag', f'fenced:w2:{A}', 'retained')])
        self.assertEqual(f.fence_tags(), {f'fenced:w2:{A}'})
        self.assertIn(f'fence w2: settled on bootID {A}', self.err)

    def test_opening_proof_before_api_read(self):
        f = self.fake(nodes=[node('w1', bootid=B), node('w2', ready=False)], tags=[f'fenced:w2:{A}'],
                      fenced_set=[W2_ADDR], acls=[W1])
        self.loop(self.running())
        calls = [c for c, _ in f.calls]
        first_read = calls.index(('api.get', fencer.NODES))
        before = {c[0] if c[0] != 'nft' else ' '.join(c[:3]) for c in calls[:first_read]}
        self.assertLessEqual({'lio.acl_iqns', 'nft -j list', 'ss'}, before)
        self.assertTrue(all(held for _, held in f.calls[:first_read + 1]))

    def test_brief_ready_then_notready_stays_fenced(self):
        # The hole: w2 rebooted to C, was Ready (and could mount) between two reads, then hung. C must not unfence it.
        f = self.fake(nodes=[node('w1', bootid=B), node('w2', bootid=A)])
        fz = self.loop()
        self.w2(ready=False, age=301, bootid=C)
        self.loop(fz)
        self.assertEqual(f.fence_tags(), {UNSETTLED})
        self.loop(fz)
        self.assertEqual(f.fence_tags(), {f'fenced:w2:{C}'})
        self.w2(bootid=C)
        self.loop(fz)
        self.assertEqual(f.fence_tags(), {f'fenced:w2:{C}'})
        self.assertFencedLive(f)
        self.w2(bootid=D)
        self.loop(fz)
        self.assertUnfenced(f)

    def test_reboot_before_fence_stays_fenced(self):
        # A slow boot C posted before the fence: the fencer cannot tell it from one that mounted, so one more reboot.
        f = self.fake(nodes=[node('w1', bootid=B), node('w2', bootid=A)])
        fz = self.loop()
        self.w2(ready=False, age=100, bootid=C)
        self.loop(fz)
        self.w2(ready=False, age=301, bootid=C)
        self.loop(fz)
        self.loop(fz)
        self.assertEqual(f.fence_tags(), {f'fenced:w2:{C}'})
        self.w2(bootid=C)
        self.loop(fz)
        self.assertFencedLive(f)
        self.w2(bootid=D)
        self.loop(fz)
        self.assertUnfenced(f)

    def test_reboot_after_fence_unfences(self):
        f = self.hung_w2()
        fz = self.loop()
        self.loop(fz)
        self.assertEqual(f.fence_tags(), {f'fenced:w2:{A}'})
        self.w2(ready=False, bootid=C)
        self.loop(fz)
        self.assertEqual(f.fence_tags(), {f'fenced:w2:{A}'})
        self.assertIn('w2: not-ready', self.err)
        self.w2(bootid=C)
        self.loop(fz)
        self.assertUnfenced(f)

    def test_boot_during_proof_is_settled(self):
        f = self.hung_w2()

        def hook(name):
            f.record('checkpoint', name)
            if name == 'fence:3':
                self.w2(ready=False, age=301, bootid=C)  # w2 reset and posted C while the cut was being proven
        with mock.patch.object(host, 'checkpoint', hook):
            fz = self.loop()
        self.loop(fz)
        self.assertEqual(f.fence_tags(), {f'fenced:w2:{C}'})
        self.w2(bootid=C)
        self.loop(fz)
        self.assertFencedLive(f)

    def test_restart_unsettles(self):
        # Also covers a tag written by the old fencer: the new one's first loop un-settles it.
        for still_on_a in (False, True):
            with self.subTest(still_on_a=still_on_a):
                self.tmp = Path(tempfile.mkdtemp())
                self.addCleanup(shutil.rmtree, self.tmp)
                w2 = node('w2', ready=False, bootid=A, taints=[OOS]) if still_on_a else node('w2', bootid=C,
                                                                                             taints=[OOS])
                f = self.fake(nodes=[node('w1', bootid=B), w2], tags=[f'fenced:w2:{A}'], fenced_set=[W2_ADDR],
                              acls=[W1], history={'w2': {'seen': [A]}})
                fz = fencer.Fencer(config())
                self.assertEqual(self.vgchanges_in(fz),
                                 [('vgchange', '--deltag', f'fenced:w2:{A}', '--addtag', UNSETTLED, 'retained')])
                self.assertNotIn('lio.add_acl', f.writes())
                self.assertFencedLive(f)
                self.loop(fz)
                settled = A if still_on_a else C
                self.assertEqual(f.fence_tags(), {f'fenced:w2:{settled}'})
                self.w2(bootid=settled)
                self.loop(fz)
                self.assertFencedLive(f)
                self.w2(bootid=C if still_on_a else D)
                self.loop(fz)
                self.assertUnfenced(f)

    def test_restart_before_settle(self):
        f = self.hung_w2()
        self.loop()
        fz = fencer.Fencer(config())
        self.assertEqual(self.vgchanges_in(fz), [])
        self.loop(fz)
        self.assertEqual(f.fence_tags(), {f'fenced:w2:{A}'})
        self.w2(bootid=A)
        self.loop(fz)
        self.assertFencedLive(f)

    def test_fall_off_unsettles(self):
        f = self.fake(nodes=[node('w1', bootid=B), node('w2', ready=False, taints=[OOS])], tags=[f'fenced:w2:{A}'],
                      fenced_set=[W2_ADDR], acls=[W1], history={'w2': {'seen': [A]}})
        fz = self.running()
        f.fenced_set.discard(W2_ADDR)
        for t in f.lio.targets_.values():
            t['acls'].add(W2)
        self.w2(bootid=C)
        self.assertEqual(self.vgchanges_in(fz),
                         [('vgchange', '--deltag', f'fenced:w2:{A}', '--addtag', UNSETTLED, 'retained')])
        self.assertIn('fence w2: unsettled', self.err)
        self.assertFencedLive(f)
        self.loop(fz)
        self.assertEqual(f.fence_tags(), {f'fenced:w2:{C}'})
        self.loop(fz)
        self.assertFencedLive(f)

    def test_failed_unsettle_is_sticky(self):
        # The un-settle vgchange fails once: the fell-off signal must survive until it lands, or C unfences on A's tag.
        for restart in (False, True):
            with self.subTest(restart=restart):
                self.tmp = Path(tempfile.mkdtemp())
                self.addCleanup(shutil.rmtree, self.tmp)
                f = self.fake(nodes=[node('w1', bootid=B), node('w2', ready=False, taints=[OOS])],
                              tags=[f'fenced:w2:{A}'], fenced_set=[W2_ADDR], acls=[W1], history={'w2': {'seen': [A]}})
                fz = fencer.Fencer(config()) if restart else self.running()
                if not restart:
                    f.fenced_set.discard(W2_ADDR)
                    for t in f.lio.targets_.values():
                        t['acls'].add(W2)
                real, fails = fencer.lvm.vg_tags, [1]

                def vg_tags(vg, add=(), delete=()):
                    if fails and f'fenced:w2:{A}' in delete:
                        fails.pop()
                        raise subprocess.CalledProcessError(5, ['vgchange'], '', 'fake failure')
                    real(vg, add, delete)
                with mock.patch.object(fencer.lvm, 'vg_tags', vg_tags):
                    self.loop(fz)
                    self.assertEqual(f.fence_tags(), {f'fenced:w2:{A}'})
                    self.assertFencedLive(f)
                    self.w2(bootid=C)  # C mounted in the open window, hung, and is Ready again
                    self.loop(fz)
                    self.assertEqual(f.fence_tags(), {UNSETTLED})
                    self.assertFencedLive(f)
                    self.loop(fz)
                    self.assertEqual(f.fence_tags(), {f'fenced:w2:{C}'})
                    self.loop(fz)
                    self.assertFencedLive(f)
                    self.w2(bootid=D)
                    self.loop(fz)
                    self.assertUnfenced(f)

    def test_proof_error_is_not_a_fall_off(self):
        f = self.fake(nodes=[node('w1', bootid=B), node('w2', bootid=C, taints=[OOS])], tags=[f'fenced:w2:{A}'],
                      fenced_set=[W2_ADDR], acls=[W1], history={'w2': {'seen': [A]}})
        fz = self.running()
        f.ss_errors = 1
        self.assertEqual(self.vgchanges_in(fz), [])
        self.assertNotIn('lio.add_acl', f.writes())
        self.assertIn('w2: proof-error', self.err)
        self.loop(fz)
        self.assertUnfenced(f)

    def test_api_error_after_fence_keeps_unknown(self):
        f = self.hung_w2()
        fz = self.loop()
        f.api.error = kube.ApiUnreachable('down')
        self.assertEqual(self.vgchanges_in(fz), [])
        self.assertEqual(f.fence_tags(), {UNSETTLED})
        f.api.error = None
        self.loop(fz)
        self.assertEqual(f.fence_tags(), {f'fenced:w2:{A}'})

    def test_proof_failure_stays_unsettled(self):
        f = self.hung_w2()
        f.ss = [SS_LINE]
        fz = self.loop()
        self.w2(bootid=C)
        for _ in range(3):
            self.loop(fz)
            self.assertEqual(f.fence_tags(), {UNSETTLED})
        self.assertEqual(f.find('api.patch'), [])

    def test_bad_tag_blocks_settle_not_unsettle(self):
        f = self.fake(nodes=[node('w1', bootid=B), node('w2', ready=False)], tags=['fenced:w1:bad', UNSETTLED],
                      fenced_set=[W2_ADDR], acls=[W1])
        self.assertEqual(self.vgchanges_in(self.running()), [])
        self.assertEqual(f.fence_tags(), {'fenced:w1:bad', UNSETTLED})
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        f = self.fake(nodes=[node('w1', bootid=B), node('w2', ready=False)], tags=['fenced:w1:bad', f'fenced:w2:{A}'])
        self.assertEqual(self.vgchanges_in(self.running()),
                         [('vgchange', '--deltag', f'fenced:w2:{A}', '--addtag', UNSETTLED, 'retained')])

    def test_node_missing_stays_unsettled(self):
        f = self.hung_w2()
        fz = self.loop()
        del f.api.nodes['w2']
        self.loop(fz)
        self.assertEqual(f.fence_tags(), {UNSETTLED})
        self.assertIn('w2: unsettled', self.err)
        f.api.nodes['w2'] = node('w2', ready=False, age=301)
        self.loop(fz)
        self.assertEqual(f.fence_tags(), {f'fenced:w2:{A}'})

    def test_rebuild_empty_history(self):
        f = self.fake(nodes=[node('w1', bootid=B), node('w2', taints=[OOS])], tags=[f'fenced:w2:{A}'],
                      fenced_set=[W2_ADDR], acls=[W1])
        self.assertFalse((self.tmp / 'bootids.json').exists())
        fz = self.running()
        self.loop(fz)
        self.assertFencedLive(f)
        self.w2(bootid=C)
        self.loop(fz)
        self.assertUnfenced(f)


class UnfenceTest(Base):
    def fenced(self, w2, history={'w2': {'seen': [A]}}, **kw):
        return self.fake(nodes=[node('w1', bootid=B), w2], tags=[f'fenced:w2:{A}'], fenced_set=[W2_ADDR], acls=[W1],
                         history=history, **kw)

    def test_unfence_order_taint_first(self):
        f = self.fenced(node('w2', bootid=C, taints=[OOS]))
        self.loop(self.running())
        self.assertEqual([n for n, _ in f.steps()], [
            'api.patch', 'checkpoint:unfence:1', 'lio.add_acl', 'checkpoint:unfence:2', 'nft delete',
            'checkpoint:unfence:3', 'lio.save', 'checkpoint:unfence:4', 'vgchange', 'checkpoint:unfence:5'])
        self.assertTrue(all(held for _, held in f.steps()))
        self.assertEqual(f.find('vgchange'), [('vgchange', '--deltag', f'fenced:w2:{A}', 'retained')])
        self.assertUnfenced(f)
        self.assertEqual(self.history()['w2'], {'seen': [A, C]})

    def test_unfence_keeps_other_taints(self):
        other = {'key': 'example.com/x', 'effect': 'NoSchedule'}
        f = self.fenced(node('w2', bootid=C, taints=[other, OOS]))
        self.loop(self.running())
        self.assertEqual(f.api.nodes['w2']['spec']['taints'], [other])

    def test_unfence_api_error_retries(self):
        f = self.fenced(node('w2', bootid=C, taints=[OOS]))
        fz = self.running()
        f.api.patch_error = kube.ApiUnreachable('down')
        self.loop(fz)
        self.assertNotIn('lio.add_acl', f.writes())
        self.assertEqual(f.fence_tags(), {f'fenced:w2:{A}'})
        f.api.patch_error = None
        self.loop(fz)
        self.assertUnfenced(f)

    def test_unfence_crash_after_reopen_refences(self):
        # Storage open under a tag looks like a fence that fell off: cut again, settle on C, one more reboot (D).
        for restart in (False, True):
            for step in range(1, 5):
                with self.subTest(restart=restart, step=step):
                    self.tmp = Path(tempfile.mkdtemp())
                    self.addCleanup(shutil.rmtree, self.tmp)
                    f = self.fenced(node('w2', bootid=C, taints=[OOS]))
                    fz = self.running()

                    def crash(name, at=f'unfence:{step}'):
                        f.record('checkpoint', name)
                        if name == at:
                            raise Crash(name)
                    with mock.patch.object(host, 'checkpoint', crash), self.assertRaises(Crash):
                        self.loop(fz)
                    self.assertEqual(f.fence_tags(), {f'fenced:w2:{A}'})  # the tag goes last
                    if restart:
                        fz = fencer.Fencer(config())
                    self.loop(fz)
                    if step == 1 and not restart:  # nothing was reopened: the unfence just finishes
                        self.assertUnfenced(f)
                        continue
                    self.assertEqual(f.fence_tags(), {UNSETTLED})
                    self.assertFencedLive(f)
                    self.loop(fz)
                    self.assertEqual(f.fence_tags(), {f'fenced:w2:{C}'})
                    self.loop(fz)
                    self.assertFencedLive(f)
                    self.w2(bootid=D)
                    self.loop(fz)
                    self.assertUnfenced(f)

    def test_unfence_unknown_needs_confirmation(self):
        f = self.fake(nodes=[node('w1'), node('w2', bootid=C)], tags=['fenced:w2:unknown'], fenced_set=[W2_ADDR],
                      acls=[W1])
        rc, out = self.cli('unfence', 'w2')
        self.assertEqual(rc, 1)
        self.assertIn('qm status <vmid> --verbose', out)
        self.assertEqual(f.writes(), [])
        rc, out = self.cli('unfence', 'w2', '--confirm-stopped')
        self.assertEqual(rc, 0, self.err)
        self.assertEqual(f.writes(), ['lio.add_acl', 'nft delete', 'lio.save', 'vgchange'])  # no taint to remove
        self.assertNotIn('fenced:w2:unknown', f.tags())

    def test_api_read_under_the_lock(self):
        # A worker that recovered while the fencer waited for the lock must not be fenced on the old reading.
        f = self.hung_w2()
        self.loop()
        self.assertTrue(all(held for c, held in f.calls if c[0] == 'api.get'))

    def test_bad_fence_tag_reapplies_valid_fences_only(self):
        # w2 is validly tagged and its fence fell off (address gone, ACL back); w1 has a malformed tag and w2 is
        # Ready on a new bootID. The loop re-applies w2's fence, unfences nothing and counts a failure each loop.
        f = self.fake(nodes=[node('w1', bootid=B), node('w2', bootid=C)], tags=['fenced:w1:bad', f'fenced:w2:{A}'],
                      history={'w2': {'seen': [A]}})
        fz = self.loop()
        self.assertIn(W2_ADDR, f.fenced_set, self.err)
        self.assertFalse(any(W2 in t['acls'] for t in f.lio.targets_.values()))
        self.assertIn(UNSETTLED, f.tags())  # the fall-off un-settles it; the bad tag then blocks the settle
        self.assertNotIn('lio.add_acl', f.writes())
        self.assertIn('malformed fence tag', self.err)
        self.loop(fz)
        self.assertIn(UNSETTLED, f.tags())
        self.assertIn('storage_fence_failures_total 2\n', self.prom())
        self.assertEqual(self.cli('unfence', 'w2')[0], 1)
        self.assertIn(UNSETTLED, f.tags())
        rc, out = self.cli('status')
        self.assertEqual((rc, json.loads(out)['errors']), (1, ["malformed fence tag 'fenced:w1:bad'"]))


class LoopTest(Base):
    def test_api_error_does_nothing(self):
        for err, code in ((kube.ApiUnreachable('down'), 'unreachable'), (kube.ApiAuthError('401'), 'auth')):
            with self.subTest(code=code):
                self.tmp = Path(tempfile.mkdtemp())
                self.addCleanup(shutil.rmtree, self.tmp)
                f = self.hung_w2(tags=[f'fenced:w1:{A}'])
                f.api.error = err
                fz = self.loop()
                self.loop(fz)
                self.assertEqual([c[0] for c, _ in f.calls], ['vgs', 'lvs', 'api.get'] * 2)  # tags read, no proof yet
                self.assertIn(f'storage_fencer_api_errors_total{{code="{code}"}} 2\n', self.prom())
                self.assertIn('storage_fencer_last_api_success_timestamp_seconds 0\n', self.prom())

    def test_history_records_any_ready_state_and_freezes(self):
        # w2 is Ready again with a bootID it had before the fence: it stays fenced and its history must not move.
        frozen = {'seen': [A, C]}
        f = self.fake(nodes=[node('w1', bootid=B), node('w2', bootid=C, taints=[OOS])], tags=[f'fenced:w2:{A}'],
                      fenced_set=[W2_ADDR], acls=[W1], history={'w2': frozen})
        fz = self.loop(self.running())
        self.assertEqual(f.fence_tags(), {f'fenced:w2:{A}'})
        self.assertEqual(self.history(), {'w1': {'seen': [B]}, 'w2': frozen})
        f.api.nodes['w1'] = node('w1', ready=False, age=10, bootid=C)
        self.loop(fz)
        self.assertEqual(self.history(), {'w1': {'seen': [B, C]}, 'w2': frozen})

    def test_manual_fence_tags_unknown(self):
        f = self.fake(nodes=[node('w1'), node('w2', ready=False, age=10)])
        f.api.error = kube.ApiUnreachable('down')
        rc, out = self.cli('fence', 'w2')
        self.assertEqual(rc, 0, self.err)
        self.assertEqual(json.loads(out), {'acl_present': {'a': False, 'p': False}, 'addr_in_set': True,
                                           'established': []})
        self.assertEqual(f.fence_tags(), {UNSETTLED})
        self.assertEqual(f.writes(), ['vgchange', 'nft add', 'lio.delete_acl', 'lio.save'])  # steps 1-5, no taint
        self.assertEqual(f.find('api.get'), [])
        with self.assertRaises(SystemExit):
            self.cli('fence', 'w2', '--boot-id', 'x')
        f.api.error = None
        self.loop(self.running())
        self.assertEqual(f.fence_tags(), {f'fenced:w2:{A}'})

    def test_metrics(self):
        self.hung_w2()
        self.loop()
        text = self.prom()
        for line in ('storage_fence_active{node="w1"} 0', 'storage_fence_active{node="w2"} 1',
                     'storage_fence_failures_total 0', f'storage_fencer_last_api_success_timestamp_seconds {LOCAL_NOW}',
                     'storage_fencer_api_errors_total{code="auth"} 0',
                     'storage_fencer_api_errors_total{code="unreachable"} 0'):
            self.assertIn(line + '\n', text)

    def test_cli_commands_never_write_metrics(self):
        self.fake(nodes=[node('w1'), node('w2', ready=False, age=10)])
        self.cli('fence', 'w2')
        self.cli('status')
        self.assertFalse((self.tmp / 'textfile/storage-fencer.prom').exists())  # the loop's counters are its own

    def test_sigterm_between_loops(self):
        self.fake()
        old = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, old)
        loops = []

        def once(self_):
            loops.append(1)
            os.kill(os.getpid(), signal.SIGTERM)  # arrives mid-loop; the loop still finishes
            loops.append(2)
        with mock.patch.object(fencer.Fencer, 'loop_once', once):
            self.assertEqual(fencer.Fencer(config()).run(), 0)
        self.assertEqual(loops, [1, 2])


class CliTest(Base):
    def test_prove_cli_read_only(self):
        f = self.fake(nodes=[node('w1'), node('w2')], tags=[f'fenced:w2:{A}'], fenced_set=[W2_ADDR], acls=[W1])
        rc, out = self.cli('prove', 'w2')
        self.assertEqual(rc, 0, self.err)
        self.assertEqual(json.loads(out), {'acl_present': {'a': False, 'p': False}, 'addr_in_set': True,
                                           'established': []})
        f.ss = [SS_LINE]
        rc, out = self.cli('prove', 'w2')
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out)['established'], [SS_LINE])
        rc, out = self.cli('prove', 'w1')
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out)['acl_present'], {'a': True, 'p': True})
        self.assertEqual([n for n, _ in f.steps()], [])  # no writes, no checkpoints

    def test_restore_set(self):
        f = self.fake(tags=[f'fenced:w2:{A}'])
        rc, _ = self.cli('restore-set')
        self.assertEqual(rc, 0, self.err)
        self.assertEqual(f.fenced_set, {W2_ADDR})

    def test_status(self):
        self.fake(tags=[f'fenced:w2:{A}', 'hold_fence'], history={'w1': {'seen': [B]}})
        rc, out = self.cli('status')
        self.assertEqual(rc, 0, self.err)
        self.assertEqual(json.loads(out), {'fenced': {'w2': A}, 'holds': ['hold_fence'],
                                           'history': {'w1': {'seen': [B]}}})

    def test_unconfigured_node_refused(self):
        f = self.fake()
        for cmd in (('fence', 'ghost'), ('unfence', 'ghost'), ('prove', 'ghost')):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.cli(*cmd)[0], 1)
        self.assertEqual(f.writes(), [])

    def test_wrapper_runs_isolated(self):
        dest = self.tmp / 'pkg'
        shutil.copytree(FILES / 'homelab_storage', dest / 'homelab_storage')
        shutil.copy(FILES / 'storage-fencer', dest)
        r = subprocess.run([sys.executable, '-I', str(dest / 'storage-fencer'), '--help'], cwd=self.tmp,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('restore-set', r.stdout)


if __name__ == '__main__':
    unittest.main()

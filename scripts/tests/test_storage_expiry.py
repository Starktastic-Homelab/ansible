"""Offline checks for expire-candidates and expire; reuses the reconcile test's host fakes."""
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_storage_reconcile import NOW, Base, FakeHost, FakeLio, config, vg  # noqa: E402
from homelab_storage import lio, reconcile  # noqa: E402

Q = 259200
OLD, FRESH = f'pending-delete:{NOW - Q - 1}', f'pending-delete:{NOW - Q + 60}'


class ExpLio(FakeLio):
    def delete_target(self, id):
        self.fake.record('lio.delete_target', id)
        self.targets_.pop(id, None)

    def sessions(self, id):
        if self.sessions_.get(id) == 'error':
            raise lio.InUse(f'{id}: cannot read sessions')
        return super().sessions(id)


class ExpHost(FakeHost):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.lio = ExpLio(self, kw.get('targets'), kw.get('sessions'))

    def run(self, argv, check=True, **kw):
        r = super().run(argv, check, **kw)
        if argv[0] == 'lvremove':
            vgn, i = argv[-1].split('/')
            del self.vgs[vgn]['lvs'][i]
        return r


class ExpiryTest(Base):
    def fake(self, tag=OLD, holds=(), **kw):
        retained = vg({'ledger:a:created', *holds}, {'a': (1 << 30, {tag})})
        f = ExpHost(self.tmp, vgs={'retained': retained, 'regen': vg()}, targets={'a': {'dev': '/dev/retained/a', 'acls': set()}}, **kw)
        f.install(self)
        self.f = f
        return f

    def run_cli(self, *args):
        (self.tmp / 'config.json').write_text(json.dumps(config()))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = reconcile.main([*args, '--config', str(self.tmp / 'config.json')])
        self.err = err.getvalue()
        return rc, out.getvalue()

    def candidates(self):
        rc, out = self.run_cli('expire-candidates')
        self.assertEqual(rc, 0, self.err)
        return json.loads(out)

    def test_not_old_enough(self):
        self.fake(tag=FRESH)
        self.assertEqual(self.candidates(), [])
        self.assertEqual(self.run_cli('expire', '--id', 'a')[0], 1)
        self.assertEqual(self.f.find('lvremove'), [])

    def test_hold_create_skips(self):
        self.fake(holds=['hold_create'])
        self.assertEqual(self.candidates(), [])
        self.assertEqual(self.run_cli('expire', '--id', 'a')[0], 1)
        self.assertEqual(self.f.find('lvremove'), [])

    def test_candidate_shape(self):
        self.fake()
        self.assertEqual(self.candidates(), [{'id': 'a', 'vg': 'retained', 'since': NOW - Q - 1, 'volume_handle': 'a',
                                              'iqn': lio.iqn('a'), 'sessions': False}])

    def test_session_read_error_counts_in_use(self):
        self.fake(sessions={'a': 'error'})
        self.assertTrue(self.candidates()[0]['sessions'])
        self.assertEqual(self.run_cli('expire', '--id', 'a')[0], 1)
        self.assertEqual(self.f.find('lio.delete_target'), [])

    def test_expire_order(self):
        f = self.fake()
        rc, _ = self.run_cli('expire', '--id', 'a')
        self.assertEqual(rc, 0, self.err)
        self.assertEqual([n for n in f.names() if n not in ('vgs', 'lvs')],
                         ['lio.delete_target', 'lio.save', 'blkdiscard', 'lvremove', 'vgchange'])
        # No -f, so the exclusive open still refuses a busy LV (the tty refusal is covered by test_run_stdin_is_devnull).
        self.assertEqual(f.find('blkdiscard')[0], ('blkdiscard', '/dev/retained/a'))
        self.assertEqual(f.find('lvremove')[0], ('lvremove', '-y', 'retained/a'))
        self.assertTrue(all(held for call, held in f.calls if call[0] in ('lio.delete_target', 'blkdiscard', 'lvremove')))

    def test_expire_rechecks_session(self):
        f = self.fake()
        self.assertFalse(self.candidates()[0]['sessions'])
        f.lio.sessions_['a'] = ['iqn.2026-11.net.starktastic:w1']  # a login after the listing
        self.assertEqual(self.run_cli('expire', '--id', 'a')[0], 1)
        self.assertEqual(f.find('lio.delete_target') + f.find('blkdiscard') + f.find('lvremove'), [])

    def test_tombstone_written(self):
        f = self.fake()
        self.assertEqual(self.run_cli('expire', '--id', 'a')[0], 0, self.err)
        self.assertEqual(f.vgs['retained']['tags'], {'tombstone:a'})
        self.assertEqual(f.find('vgchange')[0], ('vgchange', '--deltag', 'ledger:a:created', '--addtag', 'tombstone:a', 'retained'))

    def test_failure_exits_1_and_keeps_ledger(self):
        f = self.fake()
        orig = f.run

        def run(a, check=True, **kw):
            if a[0] == 'blkdiscard':
                raise RuntimeError('boom')
            return orig(a, check, **kw)
        p = mock.patch.object(reconcile.host, 'run', run)
        p.start()
        self.addCleanup(p.stop)
        self.assertEqual(self.run_cli('expire', '--id', 'a')[0], 1)
        self.assertEqual(f.vgs['retained']['tags'], {'ledger:a:created'})


if __name__ == '__main__':
    unittest.main()

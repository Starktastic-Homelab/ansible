"""Offline checks for the pure reconcile decisions, one per row of the ADR reconcile table."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'roles/storage_vm/files'))
from homelab_storage import ledger, plan  # noqa: E402
from homelab_storage.inventory import Volume  # noqa: E402

GI = 1024 ** 3


def vol(id='a', cls='retained', origin='new', size=GI, ns='n', claim='c'):
    return Volume(id, ns, claim, size, cls, origin)


def vg(tags=(), lvs=None, present=True, free=None):
    return plan.VgState(ledger.Ledger.from_tags(tags), {k: plan.LvState(*v) if isinstance(v, tuple) else plan.LvState(v) for k, v in (lvs or {}).items()},
                        present, free_bytes=free)


def run(volumes, retained=None, regen=None, tags=(), cluster=True, **kw):
    c = ledger.Cluster.from_tags(list(tags), 'retained') if cluster else None
    return plan.decide(volumes, {'retained': retained or vg(), 'regen': regen or vg()}, c, **kw)


def kinds(d):
    return [(a.kind, a.id) for a in d.actions]


def refused(d):
    return {(r.id, r.reason) for r in d.refusals}


class PlanTest(unittest.TestCase):
    def test_created_and_present_exports(self):
        d = run([vol()], vg([ledger.tag_created('a')], {'a': GI}))
        self.assertEqual(kinds(d), [('export', 'a')])
        self.assertFalse(d.fail)

    def test_grow_extends(self):
        d = run([vol(size=2 * GI)], vg([ledger.tag_created('a')], {'a': GI}))
        self.assertIn(('extend', 'a'), kinds(d))
        self.assertEqual([a.size_bytes for a in d.actions if a.kind == 'extend'], [2 * GI])

    def test_shrink_refused(self):
        d = run([vol(size=GI)], vg([ledger.tag_created('a')], {'a': 2 * GI}))
        self.assertEqual(refused(d), {('a', 'shrink')})
        self.assertEqual(kinds(d), [('export', 'a')])

    def test_new_origin_new(self):
        self.assertEqual(kinds(run([vol()])), [('create_new', 'a')])

    def test_new_origin_migrated(self):
        self.assertEqual(kinds(run([vol(origin='migrated')])), [('create_raw', 'a')])

    def test_regenerable_goes_to_regen(self):
        d = run([vol(cls='regenerable')])
        self.assertEqual(d.actions[0].vg, 'regen')

    def test_ledgered_lv_missing(self):
        d = run([vol()], vg([ledger.tag_created('a')]))
        self.assertEqual(refused(d), {('a', 'lv-missing')})
        self.assertTrue(d.fail)

    def test_orphan_refused(self):
        d = run([vol()], vg(lvs={'a': GI}))
        self.assertEqual(refused(d), {('a', 'orphan')})

    def test_creating_refused(self):
        d = run([vol()], vg([ledger.tag_creating('a')], {'a': GI}))
        self.assertEqual(refused(d), {('a', 'crashed-create')})

    def test_tombstone_refused(self):
        d = run([vol()], vg([ledger.tag_tombstone('a')]))
        self.assertEqual(refused(d), {('a', 'tombstoned')})

    def test_regen_missing_keeps_retained_lifecycle(self):
        d = run([vol('a', size=2 * GI), vol('p', cls='regenerable')], vg([ledger.tag_created('a')], {'a': GI}),
                vg(present=False))
        self.assertEqual(kinds(d), [('export', 'a'), ('extend', 'a')])
        self.assertEqual(refused(d), {('p', 'vg-missing')})
        self.assertTrue(d.fail)

    def test_both_vgs_missing_refuses_all(self):
        d = run([vol('a'), vol('p', cls='regenerable')], vg(present=False), vg(present=False))
        self.assertEqual(refused(d), {('a', 'vg-missing'), ('p', 'vg-missing')})
        self.assertEqual(d.actions, [])
        self.assertTrue(d.fail)

    def test_one_vg_missing_fails_with_no_entry_of_its_class(self):
        d = run([vol('a')], vg([ledger.tag_created('a')], {'a': GI}), vg(present=False))
        self.assertEqual((kinds(d), refused(d)), ([('export', 'a')], set()))
        self.assertTrue(d.fail)

    def test_other_vg_unread_refuses_entry_only(self):
        d = run([vol('a'), vol('b')], vg([ledger.tag_created('a')], {'a': GI}), vg(present=False))
        self.assertEqual(kinds(d), [('export', 'a')])
        self.assertEqual(refused(d), {('b', 'other-vg-unread')})

    def test_class_move_keeps_live_export(self):
        d = run([vol('a', cls='regenerable')], vg([ledger.tag_created('a')], {'a': GI}), vg(present=False))
        self.assertEqual(refused(d), {('a', 'vg-missing')})
        self.assertEqual([(a.kind, a.id, a.vg) for a in d.actions], [('export', 'a', 'retained')])
        self.assertTrue(d.fail)

    def test_retained_missing_refuses_everything(self):
        d = run([vol('a'), vol('b', cls='regenerable')], cluster=False)
        self.assertEqual({r.id for r in d.refusals}, {'a', 'b'})
        self.assertTrue(d.fail)
        self.assertEqual(d.actions, [])

    def test_hold_create(self):
        d = run([vol('a'), vol('b'), vol('c', size=2 * GI)],
                vg([ledger.tag_created('a'), ledger.tag_created('c')], {'a': GI, 'c': GI}), tags=[ledger.HOLD_CREATE])
        self.assertEqual(kinds(d), [('export', 'a'), ('export', 'c')])
        self.assertEqual(refused(d), {('b', 'hold-create')})
        self.assertTrue(d.fail)

    def test_hold_create_fails_without_creates(self):
        d = run([vol('a', size=2 * GI)], vg([ledger.tag_created('a')], {'a': GI}), tags=[ledger.HOLD_CREATE])
        self.assertEqual(kinds(d), [('export', 'a')])  # the extend is held back
        self.assertEqual(refused(d), set())
        self.assertTrue(d.fail)

    def test_removed_marks_pending(self):
        d = run([], vg([ledger.tag_created('a')], {'a': GI}))
        self.assertEqual(kinds(d), [('export', 'a'), ('mark_pending', 'a')])

    def test_removed_already_pending_stays_exported(self):
        d = run([], vg([ledger.tag_created('a')], {'a': (GI, ('pending-delete:1',))}))
        self.assertEqual(kinds(d), [('export', 'a')])

    def test_readded_clears_pending(self):
        d = run([vol()], vg([ledger.tag_created('a')], {'a': (GI, ('pending-delete:1',))}))
        self.assertEqual(kinds(d), [('export', 'a'), ('clear_pending', 'a')])

    def test_more_than_two_removals_refused(self):
        ids = ['a', 'b', 'c']
        d = run([vol('k')], vg([ledger.tag_created(i) for i in ids + ['k']], {i: GI for i in ids + ['k']}))
        self.assertEqual({a.kind for a in d.actions}, {'export'})
        self.assertEqual({a.id for a in d.actions}, {'a', 'b', 'c', 'k'})
        self.assertTrue(d.fail)
        self.assertEqual({r.id for r in d.refusals}, {'a', 'b', 'c', 'k'})
        d = run([], vg([ledger.tag_created(i) for i in ids], {i: GI for i in ids}), removed_ok=3)
        self.assertFalse(d.fail)

    def test_removed_ledgered_lv_missing_fails(self):
        d = run([], vg([ledger.tag_created('a')], {}))
        self.assertEqual(refused(d), {('a', 'lv-missing')})
        self.assertTrue(d.fail)

    def test_id_in_other_vg_refused(self):
        d = run([vol()], regen=vg([ledger.tag_created('a')], {'a': GI}))
        self.assertEqual(refused(d), {('a', 'other-vg')})
        self.assertEqual([(a.kind, a.id, a.vg) for a in d.actions], [('export', 'a', 'regen')])

    def test_create_refused_when_vg_too_small(self):
        d = run([vol('a', size=2 * GI)], vg(free=GI))
        self.assertEqual(refused(d), {('a', 'no-space')})
        self.assertEqual(kinds(d), [])
        self.assertEqual(kinds(run([vol('a', size=2 * GI)], vg(free=2 * GI))), [('create_new', 'a')])

    def test_free_space_is_cumulative(self):
        d = run([vol('a'), vol('b')], vg(free=GI))
        self.assertEqual(kinds(d), [('create_new', 'a')])
        self.assertEqual(refused(d), {('b', 'no-space')})
        d = run([vol('a'), vol('p', cls='regenerable')], vg(free=GI), vg(free=GI))
        self.assertEqual(kinds(d), [('create_new', 'a'), ('create_new', 'p')])
        self.assertEqual(refused(d), set())

    def test_extend_refused_when_vg_too_small(self):
        d = run([vol(size=3 * GI)], vg([ledger.tag_created('a')], {'a': GI}, free=GI))
        self.assertEqual(kinds(d), [('export', 'a')])
        self.assertEqual(refused(d), {('a', 'no-space')})
        d = run([vol(size=3 * GI)], vg([ledger.tag_created('a')], {'a': GI}, free=2 * GI))
        self.assertEqual(kinds(d), [('export', 'a'), ('extend', 'a')])
        self.assertEqual(refused(d), set())

    def test_hold_create_consumes_no_space(self):
        d = run([vol('a', size=2 * GI)], vg(free=GI), tags=[ledger.HOLD_CREATE])
        self.assertEqual(refused(d), {('a', 'hold-create')})
        self.assertEqual(kinds(d), [])

    def test_unread_free_skips_check(self):
        self.assertEqual(kinds(run([vol('a', size=2 * GI)], vg(free=None))), [('create_new', 'a')])

    def test_namespace_change_is_render_only(self):
        r = vg([ledger.tag_created('a')], {'a': GI})
        self.assertEqual(kinds(run([vol(ns='x', claim='y')], r)), kinds(run([vol()], r)))


if __name__ == '__main__':
    unittest.main()

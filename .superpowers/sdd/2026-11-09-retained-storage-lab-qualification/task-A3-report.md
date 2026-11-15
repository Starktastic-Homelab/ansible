# Task A3 report
Status: DONE_WITH_CONCERNS. Base 45abca4a, one commit.
Files: roles/storage_vm/files/homelab_storage/plan.py, scripts/tests/test_storage_plan.py, validate.yml (1 step). 19 tests pass.
Rulings: R4 (VG missing or cluster None refuses every entry, reason vg-missing, fail), R5 (pending LVs stay exported; brake counts newly removed ids only; removed_ok parameter), R6 (fail only for lv-missing, VG missing, brake, hold_create; per-entry refusals do not set fail, the executor must exit 1 on any refusal).
Choices: refusal reasons are lv-missing, orphan, crashed-create, tombstoned, shrink, other-vg, vg-missing, hold-create, removal-brake. Grow emits export then extend. Removed LV not ledgered or still creating is refused (orphan / crashed-create). Under hold_create only export actions remain (no extend, clear/mark_pending); entries needing creation get refusal hold-create. Brake refuses all inventory entries and removed ids with removal-brake and drops all actions (including exports). Action carries vg and size_bytes. Tests written with impl; red step not observed separately (ran impl first).

## Fix round 1
1. Removed ledgered `created` ids with no LV now refuse `lv-missing` with fail=True (new `Ledger.created_ids()`); test `test_removed_ledgered_lv_missing_fails`.
2. Per-entry refusals on a created+present LV (shrink, other-vg) and the removal brake now keep `export` actions and refuse only mutations; tests `test_shrink_refused`, `test_id_in_other_vg_refused`, `test_more_than_two_removals_refused` updated.
Command: `for t in inventory ledger plan; do python3 scripts/tests/test_storage_$t.py; done` -> all OK (plan: 20 tests).

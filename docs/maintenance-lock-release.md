# Releasing a maintenance lock after a failed run

Also linked from `maintenance-coordination.md` (failed operations). Revised 2026-11-23 after three reviews (correctness, safety, operability). Written offline; nothing live was touched. Refs:
- **PIN** = ansible `887269798650e88fed791bb4f22a47bf50ad17be` (short `8872697`). CI checks the lock helper out at this commit (terraform `apply.yml@main:130-136`, ansible `deploy.yml@main:56-62`). Only `origin/codex/storage-maintenance` contains it.
- The release uses `scripts/maintenance_lock_op.sh` and its sibling `scripts/maintenance_lock.py`, both from one ansible `main` commit (4.1). Line cites `maintenance_lock.py:N` are that file on main. Main's helper is PIN's plus `execution.guard`; lines 1-82 are the same in both.
- ansible `origin/main` = `1eab126`, terraform `origin/main` = `a8aff78` when this was written.

Tested offline:
- `scripts/tests/test_maintenance_lock_op.py` runs the script and the sibling helper as real processes against a temp root and a frozen schema-1 record in the format the PIN helper writes. It covers every exit code, hold then release at `held`, a held `guard` under `timeout` (exit 124, record unchanged, no process left), release refused while `execution.guard` is held, the NEW-record message, and the nonce never in any output. CI runs it (`validate.yml`).
- Checked by hand on 2026-11-23: main's helper verifies, advances and releases a record the PIN helper wrote, and the PIN helper reads main's result.
- The 4.1 row builder ran offline with these file names. Not tried on real QGA.
- Every JSON row block below is ASCII and has no `$`.

Mark meanings:
- **Ben OK**: a live change. Ben approves each one, every time.
- **UNVERIFIED**: not proven from code. Section 7 says how to settle each one.

Precedent: one failed-run lock has been released by hand. That was ansible deploy 42144282965 attempt 1 (push), at stage `acquired`, on 2026-11-06, with Ben's approval. Both attempts ran the same SHA `1a21268`. Attempt 1 ran 01:30-01:34Z and failed; attempt 2 ran 01:43-01:48Z and passed (`gh run view --attempt 1|2`). The role fix #304 (`902db94`) landed later, at 08:53Z, so attempt 2 passed without it. Why it passed is not known. The release itself is per transcript research, not re-checked. **No terraform-held lock has ever been released by hand**, at any stage.

---

## 1. When to use this, and when not

Use it when all of these are true:
- `/var/lib/homelab-maintenance/operation.json` exists on VM300.
- Its `owner` is a CI run: `Starktastic-Homelab/terraform/<run_id>/<attempt>` or `Starktastic-Homelab/ansible/<run_id>/<attempt>`. Both workflows set this (terraform `apply.yml@main:112`, ansible `deploy.yml@main:47`).
- Its `stage` is `acquired`, `applying` or `recovering`. Those are the only stages CI writes (`maintenance_lock.py:135`; `maintenance_apply.py@main:152,161`). `held` means a person took it over: stop and find out who, unless your own `held-by-me` marker (4.4) is there.
- That run attempt has ended (failed or cancelled), and no process of it is left on VM300 (2.3).

Do not use it:
- **While the owning run is still running or queued.** Wait. A cancel is Ben's call and has a cost: it leaves the 3.4 or 3.6 mess (a deploy cut mid `helm upgrade` leaves `pending-*` releases, `deploy.yml@main:27-28`).
- **Because the lock is old.** Age means nothing. The helper never takes over an old lock (`maintenance_lock.py:2`), and the docs forbid clearing one for its age or on a timer (ansible `maintenance-coordination.md:132`, terraform `storage-safe-maintenance.md:33-34`).
- **To get a pipeline moving.** A release is the last step of a reconcile, not a bypass. Releasing does not authorize targeted applies or skipping the pipeline (terraform `storage-safe-maintenance.md:131-134`).
- **For a lock a person took by hand** (owner `claude-code/...`, `lab/...`). Whoever took it keeps their own owner and nonce files, and releases it with those.
- **For a run that failed at "Acquire" with `FileExistsError: ... '/maintenance/operation.json'`.** That run holds nothing (`maintenance_lock.py:57`). Deal with it after the real holder is released (5.2).
- **For a corrupt or empty record.** The helper refuses it with "Corrupt maintenance record; inspect manually without clearing it" (`maintenance_lock.py:74-75`). This runbook has no step that removes a record, corrupt or not. Bring it to Ben.

## 2. Identify the holder

### 2.1 Read the record without printing the nonce

Never `cat operation.json`. The file holds the nonce in plain text (`maintenance_lock.py:134-137`). Print only the safe keys.

Through the live helper, `/opt/azure-cli/bin/python3 -I ~/.cache/claude-homelab-live/bin/live.py rows.json`:
- `live.py` has only `pve` (SSH) and `tn` (QGA on VM100) targets (`live.py:15-18`), so VM300 is reached as root with `qm guest exec 300` from pve.
- pve expands `$` before the guest sees it, so no row contains `$`.
- Every row is ASCII only (`live.py:14`).
- The row timeout (last field) is always the `qm --timeout` plus 30. `live.py` has no `try` (`live.py:13-19`), so a row that hits its own timeout aborts every row after it.

```json
[
 ["pve","qm guest exec 300 --timeout 30 -- python3 -I -c 'import json,os; p=\"/var/lib/homelab-maintenance/operation.json\"; print(\"NO LOCK\") if not os.path.exists(p) else print({k: json.load(open(p)).get(k) for k in (\"schema\",\"owner\",\"operation\",\"stage\",\"created_at\",\"instance\")})' | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get(\"out-data\",\"\")); print(d.get(\"err-data\",\"\")); print(\"exit\", d.get(\"exitcode\"))'",60],
 ["pve","qm guest exec 300 --timeout 30 -- ls -la /var/lib/homelab-maintenance | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get(\"out-data\",\"\")); print(\"exit\", d.get(\"exitcode\"))'",60]
]
```

From Ben's own shell on VM300, the same read is:

```bash
sudo python3 -I -c 'import json; d=json.load(open("/var/lib/homelab-maintenance/operation.json")); print({k: d.get(k) for k in ("schema","owner","operation","stage","created_at","instance")})'
```

Write down `owner`, `operation`, `stage` and `created_at` (UTC, written at acquire, `maintenance_lock.py:137`).
- Possible `operation` values: `ansible-deploy` (`deploy.yml@main:94`), and `terraform-normal`, `terraform-drain` or `terraform-destroy` (terraform `maintenance_apply.py@main:113`).
- `instance` must be `cc1aeeb7-4827-466c-9d4b-5dc6c881f193` (`apply.yml@main:111`, `deploy.yml@main:43`). If it is anything else, stop.
- If the read fails with a JSON error, the record is corrupt. Stop (section 1).
- Stray `stage-<hex>.json` files are left over from an interrupted `advance` (`maintenance_lock.py:160-162`). They are harmless. Leave them.

### 2.2 Match it to a run and an attempt

The owner names the repo, the run id and the attempt. A "Re-run" is a new attempt, so it gets a new owner: a rerun can never release the attempt before it.

```bash
gh run view <run_id> -R Starktastic-Homelab/<repo> --attempt <attempt> --json status,conclusion,event,headSha,headBranch,displayTitle,createdAt,updatedAt
gh run view <run_id> -R Starktastic-Homelab/<repo> --attempt <attempt> --log | tail -200
```

- Use `--log`, not `--log-failed`: a cancelled step is not `failure`, so `--log-failed` can show nothing.
- `status` must be `completed`. Note the `event` and `headSha`.
- For ansible, `repository_dispatch` means the run carried a payload, possibly `[]` (normal and destroy modes send `[]`, `maintenance_apply.py@main:124,168`; `deploy.yml@main:44-46`). Read it from the sender's `client-payload:` line (3.5).
- Find the last step and the last command that ran (grep for the last `Run ` header). **The stage alone does not tell you how far the run got** (section 3).
- `MAINTENANCE_OWNER` appears in the step's env block in the log. It must equal the record's `owner` exactly.

Check that nothing is in flight on the runner. Every repo here has self-hosted jobs:

```bash
for r in terraform ansible packer apps; do gh run list -R Starktastic-Homelab/$r --status in_progress; gh run list -R Starktastic-Homelab/$r --status queued; done
```

Only `deploy.yml@main:93-105` and `apply.yml@main:181-188` use the lock. These jobs also run on `self-hosted` and ignore it (all `@main`, line of `runs-on`):
- ansible: `i915-sriov-upgrade.yml:20` (reboots the Proxmox host, 3.8), `i915-acceptance.yml:39`, `csi-activation-preflight.yml:21`, `proxmox-backup.yml:16`, `proxmox-guest-startup.yml:17`, `ser2net.yml:16`, `validate.yml:98`;
- packer: `build.yml:18`;
- terraform: `validate-and-plan.yml:50` (the plan job), `drift.yml:11` (daily 08:00 UTC, `:5`);
- apps: `refresh.yaml:15`.

### 2.3 Check that no process of the owner is left

A completed run in GitHub does not prove its container is gone. The PIN helper CI runs has no execution mutex. Its `guard` flock is held only for the length of one helper call (`maintenance_lock.py:42-45`), so a dead job holds no flock. Main's helper, which this release uses (4.1), also takes `execution.guard` for advance and release (`maintenance_lock.py:87-112`); if another process holds it, the release fails at once with `Maintenance execution is busy`. No CI path holds `execution.guard` today. Node retirement does call main's helper from the deploy checkout (`roles/k3s_node_retirement/tasks/main.yml@main:64-72,139-147`; enabled by `group_vars/all/proxmox_csi.yml@main:20`, though the role default `defaults/main.yml:3` and `maintenance-coordination.md:65` still say `false`), but only `verify`, which takes only `guard` (`maintenance_lock.py:141-143`).

Live-helper rows (read-only):

```json
[
 ["pve","qm guest exec 300 --timeout 30 -- pgrep -a -x Runner.Worker | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get(\"out-data\",\"\")); print(\"exit\", d.get(\"exitcode\"))'",60],
 ["pve","qm guest exec 300 --timeout 30 -- docker ps -a --format '{{.ID}} {{.Image}} {{.Status}} {{.CreatedAt}} {{.Names}}' | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get(\"out-data\",\"\")); print(d.get(\"err-data\",\"\")); print(\"exit\", d.get(\"exitcode\"))'",60],
 ["pve","qm guest exec 300 --timeout 30 -- stat -c '%i %U:%G %a %n' /var/lib/homelab-maintenance/guard /var/lib/homelab-maintenance/execution.guard | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get(\"out-data\",\"\")); print(d.get(\"err-data\",\"\"))'",60],
 ["pve","qm guest exec 300 --timeout 30 -- cat /proc/locks | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get(\"out-data\",\"\"))'",60],
 ["pve","qm guest exec 300 --timeout 30 -- python3 -I -c 'import time; print(\"vm300 boot\", time.strftime(\"%Y-%m-%dT%H:%M:%SZ\", time.gmtime(time.time()-float(open(\"/proc/uptime\").read().split()[0]))))' | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get(\"out-data\",\"\")); print(\"exit\", d.get(\"exitcode\"))'",60],
 ["pve","python3 -I -c 'import time; print(\"pve boot\", time.strftime(\"%Y-%m-%dT%H:%M:%SZ\", time.gmtime(time.time()-float(open(\"/proc/uptime\").read().split()[0]))))'",30]
]
```

In Ben's shell, the same checks are `pgrep -a -x Runner.Worker; sudo docker ps -a; stat -c '%i %U:%G %a %n' /var/lib/homelab-maintenance/*guard; cat /proc/locks; uptime -s` (local time).

What you want to see:
- `pgrep` exit 1: no job is running.
- No job container from the owner run, running or exited. Both jobs run in `container:` (`apply.yml@main:103-106`, `deploy.yml@main:37-41`).
- No line in `/proc/locks` with the inode of `guard` or `execution.guard`. The inode is the last number of the `MAJ:MIN:INODE` field.
- `execution.guard` either missing (the first release creates it, root-owned, 0660) or mode `660`. Another owner with another mode makes the release fail with `Execution guard group permissions changed` (`maintenance_lock.py:100-103`): stop and bring it to Ben.

**After a reboot:** if VM300's boot time is later than the record's `created_at`, no process of the owner can exist. A `Runner.Worker` or job container you see then belongs to another job. Wait for it to end; never override the check. If pve's boot time is later than `created_at`, it is the 3.8 shape.

If anything of the owner is still there, stop. The owner is not gone.

### 2.4 Leftover secrets after a dead runner (runner death, SIGKILL, reboot)

Cleanup steps are `if: always()`, which do not run when the runner itself dies:
- deploy writes `private_key.pem` and `.vault_pass` in the workspace and `~/.kube/config` in the container home (`deploy.yml@main:84-91`); it removes them at `:125-127`;
- terraform's `plan.tfplan` holds prior state (`apply.yml@main:146-147`); it is removed at `:190-192`; `/tmp/kubeconfig` lives in the job container (`apply.yml@main:20,176`).

Look for them by name only, never read them. `<work>` is the runner's work directory, shown in the failed run's checkout log (UNVERIFIED: not in any repo):

```json
[
 ["pve","qm guest exec 300 --timeout 60 -- find <work> -maxdepth 6 -name private_key.pem -o -name .vault_pass -o -name plan.tfplan -o -name handoff.enc -o -path '*_github_home/.kube/config' | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get(\"out-data\",\"\")); print(d.get(\"err-data\",\"\")); print(\"exit\", d.get(\"exitcode\"))'",90]
]
```

**By hand (Ben OK, each one):** `rm -f` each file found, and `docker rm` the owner's exited container. Keep the runner workspace out of any evidence bundle.

## 3. Reconcile, by failure shape

Rules for every shape:
- Reconcile **while the lock is still held**. Only after every uncertain effect is resolved may the original owner release (`maintenance-coordination.md:141-142`).
- No GUI actions on VMs while you reconcile (`storage-safe-maintenance.md:32`).
- Hold every merge and push to terraform, ansible, packer and apps, and every manual dispatch of a self-hosted workflow (2.2 list), until section 5 is done. The next deploy or apply takes the lock straight after a release, and the others ignore it.

Read-only checks used below:
- **[P]**: `qm list`, `qm status <id>` and `qm config <id>` on pve (a `pve` row).
- **KUBE**: `k3s kubectl <args>` as root on the master, through a `pve` row running `qm guest exec 200 --timeout 60 -- k3s kubectl <args>` with row timeout 90 (Part 1 convention; storage-migration `part-4.md:27-28`). The k3s VMs are 200 (master), then 201-202 (workers) (terraform `variables.tf@main:23-27` default 200, not overridden in `terraform.tfvars`; `main.tf@main:25,58`; `terraform.tfvars:1,5`).
- Nodes: `KUBE get nodes -o wide`. A cordon shows as `SchedulingDisabled`.
- Pods: `KUBE get pods -A --field-selector=status.phase!=Running,status.phase!=Succeeded`. The `!` is not `$`, so it is safe.
- ArgoCD: `KUBE -n argocd get applications`.

### 3.1 How terraform moves the lock

`maintenance_apply.py@main`:
- acquire (`:113`);
- then `terraform init`, plan and show (`:127-132`);
- wwn guard: on refusal it releases itself (`:133-136`); an acknowledged run prints `::warning::Acknowledged:` and goes on (`:137-138`);
- drain mode only: `get nodes`, then cordon all nodes, then drain all nodes, workers before masters (`:139-151`);
- `advance acquired -> applying` (`:152`);
- graceful shutdown of the VMs the plan deletes, by vmid (`:153`, `:30-47`, `:65-83`), which sits **outside** the `try`;
- destroy or apply (`:154-157`), with `restart_vms` on any exception (`:158-160`);
- `advance applying -> recovering` (`:161`);
- release (`:162`);
- only then write `has_changes` and `drained_nodes` (`:165-168`).

There is no `finally` around this. Every failure or cancel after acquire keeps the lock, except the wwn refusal.

The nonce lives only in process memory (`:113-115`). After a failure it exists only in `operation.json`.

kubectl output is not captured (`run()` at `:120-122`), so cordon and drain lines reach the Actions log.

### 3.2 terraform failed before drain (stage `acquired`)

The log's last command is `terraform init`, `plan` or `show`, or `kubectl get nodes` in drain mode.
- **State:** nothing changed.
- **Check:**
  - [P] `qm list` shows 200-202 running, on the same names as before.
  - `KUBE get nodes`: all `Ready`, and no `SchedulingDisabled` that this run caused.
- **By hand:** nothing.
- **Ben OK:** the release (section 4).
- **Never:** rerun the run before the release. It gets `FileExistsError` and adds noise.

### 3.3 terraform failed during drain (stage `acquired`, operation `terraform-drain`)

The log's last command is a `kubectl cordon <node>` or a `kubectl drain <node>`.
- **State:**
  - A cordon failed: the nodes before it are cordoned.
  - A drain failed: **every** node is cordoned, the earlier nodes are drained, and one node is partly drained.
  - Nothing is applied, because `advance` never ran.
- **The lost list:**
  - `drained_nodes` was only in memory. It is written after release (`:165-168`), so it is gone.
  - A later drain run skips nodes that are already cordoned (`:146-147`), so ansible would never uncordon them.
  - Rebuild the list from the log: every node with a `cordon` line that is not "already cordoned". The exact kubectl wording is UNVERIFIED.
  - Nodes that were cordoned before the run must stay cordoned.
- **Check:** `KUBE get nodes`, and the pods and ArgoCD checks above.
- **By hand (Ben OK, each node):** `KUBE uncordon <node>` for each node in the rebuilt list. Then wait until the pods and ArgoCD are healthy.
- **Ben OK:** the release.
- **Never:**
  - Release with nodes still cordoned and then rerun drain. That loses the list for good.
  - Guess the list from the current cordons.

### 3.4 terraform failed during or after apply (stage `applying`, or `recovering`)

The log shows "Graceful shutdown before Terraform: ..." (`:77`), then a terraform destroy or apply error, or a cancel.

**Possible states:**
- **Killed in graceful shutdown** (`:153`, outside the `try`). Target VMs can be shut down with **no restart**.
- **Apply failed:**
  - `restart_vms` tried `POST .../status/start` by vmid (`:88-97`). This is best effort.
  - It may boot a half-built replacement that reused the vmid (`:86-87`).
  - Fresh guests have no k3s (`:163`).
- **Destroy mode where destroy succeeded and apply failed:** the VMs are gone.
- **SIGKILL, runner death or host reboot:** no restart. VMs may be stopped, deleted or half-created. State may not be saved: the backend has no state locking (README `:96`). UNVERIFIED. Do 2.4.
- **Drain mode:** add everything from 3.3. All nodes are cordoned, and the list is lost.
- **Stage `applying`, but the log shows apply complete:** the run died at `:161`. Treat it like `recovering`.
- **`recovering`:** the apply succeeded but the release failed (`:162`). VMs are fine. No `GITHUB_OUTPUT` was written and no dispatch was sent.

**Check:**
- [P] `qm list`; `qm status 200`, `201` and `202`; and `qm config <id>` (`name`, `smbios1`, disks) for every vmid in the plan.
- KUBE may be dead here. The only route is VM 200 (`terraform.tfvars@main:1` has one master), and with `rebuild_workers_with_control_plane = true` (`terraform.tfvars@main:29`) a control-plane replace takes every node. If KUBE does not answer, use [P] only: `qm list`, `qm config 200|201|202`, and `qm guest exec <id> --timeout 30 -- systemctl is-active k3s k3s-agent` (row timeout 60).
- If KUBE answers: `KUBE get nodes -o custom-columns=NAME:.metadata.name,UUID:.status.nodeInfo.systemUUID,UNSCHED:.spec.unschedulable`, and compare `UUID` with `smbios1` to tell an old VM from a fresh one. A fresh guest has no k3s, so it has no Node yet.
- Pods and ArgoCD, as above, when KUBE answers.
- Do not read terraform state. Get the plan shape from the failed run's log, or from the next PR's plan comment.

**Before any `qm start` of an old VM, check its disks and stop on any doubt:**
- If the log has `::warning::Acknowledged:` (`:138`), the in-place update may have rewritten CSI slots without `wwn=` (`csi_wwn_guard.py@main:4-8`; `storage-safe-maintenance.md@main:67-73`). Every CSI `scsiN:` line in its `qm config` must still carry `wwn=`. If not, restore as `storage-safe-maintenance.md@main:84-87` says (Ben OK) before it boots.
- No `vm-9999-*` or other retained volume in its `qm config` may also appear in another VM's `qm config`. Two VMs on one volume is two writers (`storage-safe-maintenance.md@main:133-134`).

**By hand, each one Ben OK, and only after Ben has read the log:**
- `qm start <id>` for an **old** VM the run left stopped, after the disk check above. Do not start a half-built replacement unless Ben decides to.
- In drain mode, rebuild the list as in 3.3 and **write it down** while the lock is held. Then, still under the lock, uncordon each node on the list that survived (old VM, `Ready`) and that the failed run's plan did not replace (the plan's VM list is in its log, "Graceful shutdown before Terraform: ...", `:77`). Leave the others for 5.2.
- Nodes that will be replaced need no uncordon: a recreated Node starts schedulable, because deploy's retirement deletes the stale Node first (`k3s.yml@main:70-83`; `maintenance-coordination.md@main:56-59`). Check that after the resume deploy (5.2).

**Ben OK:** the release.

**Never:**
- "Re-run" the failed run. Before Terraform even looks at the plan, a rerun takes the lock again, re-cordons and re-drains every node in drain mode (and computes `drained_nodes` as `[]`, because they are all cordoned already, `:146-147`), and shuts down the old plan's target vmids again (`:153`). Those vmids can now be the new VMs. Then it either fails on a stale plan (which runs `restart_vms`, `:158-160`, and holds the lock again), or, if state was not saved, re-runs the replace against half-built VMs.
- Run targeted applies.
- Dispatch ansible by hand after a failed replace (draft 5.13 D50(i) step 1).

Resume is in 5.2.

### 3.5 Dispatch failed after release (no lock held)

- **What happened:** the `apply` job succeeded, then `trigger-ansible` failed or was cancelled (`apply.yml@main:194-208`).
- **Check:** section 2.1 shows `NO LOCK`. If a lock is shown, it is someone else's.
- **Payload:** it is in the `trigger-ansible` step log, on the `client-payload:` line (`apply.yml@main:208`), for example `{"drained_nodes": []}` in terraform run 44822075349. Job outputs cannot be read back after the run, so the log is the only copy.
- **By hand (Ben OK):** resend the original payload, unchanged, one quoted string per node:
  ```bash
  cat > /tmp/dispatch.json <<'JSON'
  {"event_type":"infrastructure-changed","client_payload":{"drained_nodes":["k3s-worker-01","k3s-worker-02"]}}
  JSON
  gh api -X POST repos/Starktastic-Homelab/ansible/dispatches --input /tmp/dispatch.json
  ```
  Use the exact names from the log; for an empty list, use `"drained_nodes":[]`. The dispatched deploy runs ansible `main` as it is now.
- **Never:** rerun drain or apply to rebuild the list (`storage-safe-maintenance.md:27-29`, `maintenance-coordination.md:60-62`).
- Re-running only the failed `trigger-ansible` job may reuse the apply outputs. That is UNVERIFIED. If it does not, it reruns `apply` (see 3.4, Never). Do not use it until it is settled.

### 3.6 ansible deploy failed before Release (stage always `acquired`)

- **Why the lock stays:** deploy never advances (`deploy.yml@main:93-105`), and Release has no `if:`, so any failure after Acquire keeps the lock by design. The step log shows how far it got.
- **Find the play:** look up the last play in `k3s.yml@main`:
  - "Wait for SSH" (`:2-15`): no change.
  - "Initialize k3s cluster", "Join additional masters" (`:17-66`): control plane may be half-done.
  - "Retire verified replaced worker Nodes" (`:70-83`): live (retirement is enabled, 2.3). Stale Node objects may be deleted.
  - "Join workers" (`:85-94`): agents may be half-installed.
  - "Recover nodes drained by Terraform" (`:98-143`): **drained nodes stay cordoned**.
  - "Bootstrap cluster" (`:145-155`): nodes are already uncordoned; ArgoCD or helm may be half-applied.
  - The role internals behind these effects were not read.
- **Check:**
  - `KUBE get nodes`, pods, ArgoCD.
  - Helm stuck releases (the warning is at `deploy.yml@main:27-28`): `KUBE get secret -A -l owner=helm,status=pending-upgrade`, then the same with `pending-install` and `pending-rollback`.
- **Know where the cause lives.** A rerun runs the run's own SHA: `deploy.yml@main:54` checks out with no `ref`. So:
  - Cause outside the ansible repo (runner resources, a transient error): no commit is needed.
  - Cause needs an ansible commit: merge the fix while the lock is held (Ben OK; this one merge is the exception to the hold). Its push deploy fails at Acquire (`maintenance_lock.py:57`), which is expected and holds nothing. Do not rerun the original run: it would run the unfixed code.
- Whether a full rerun of `k3s.yml` is safe after every play's failure is UNVERIFIED: the docs only say retirement is a no-op on an unchanged generation (`maintenance-coordination.md:66-68`), and the 11-06 precedent covers one failure in "Run K3s Playbook". Bring the failed play to Ben before the resume.
- **Dispatched runs:**
  - The payload is only on that run. A later push or manual run uses `[]` and never uncordons (`deploy.yml@main:44-46`).
  - Keep the run's `drained_nodes` from the terraform run log (3.5).
- **Ben OK:** the release.
- **Never:** run `ansible-playbook` by hand against the cluster to "finish" a deploy.

### 3.7 ansible deploy failed after Release (no lock held)

- **What happened:** `Encrypt kubeconfig`, `Upload` or the `handoff` job failed (`deploy.yml@main:110-150`). The cluster is deployed, but `KUBECONFIG_RAW` was not rewritten.
- **Check:** 2.1 shows `NO LOCK`.
- **Resume (Ben OK):** `gh run rerun <run_id> -R Starktastic-Homelab/ansible` (the whole run). It takes the lock again and runs all of `k3s.yml`. Do not use `--failed` here: if only `handoff` failed it reruns `handoff` alone, and the artifact it needs is kept 1 day (`deploy.yml@main:123`); whether a new attempt can read it is UNVERIFIED.
- For a dispatched run, send the 3.5 dispatch with its payload instead.

### 3.8 Killed by a host reboot

- **Signs:** pve's boot time (2.3) is later than the record's `created_at`. One known cause: `i915-sriov-upgrade.yml` runs on `self-hosted` with no lock (`i915-sriov-upgrade.yml@main:18-27`), and its play schedules `shutdown -r +1` (`roles/i915_sriov/handlers/main.yml@main:20-26`, on by default, `defaults/main.yml@main:27`, `tasks/main.yml@main:285-287`). A job the runner starts in that minute dies in the reboot. Look for an `i915-sriov-upgrade` run that ended just before `created_at`.
- **Reconcile:** like 3.4 (or 3.6 for deploy) with no restart. The k3s VMs come back only by their own onboot setting (`modules/vm/main.tf@main:16`), after VM 100 (`group_vars/proxmox_hosts/guest_startup.yml@main:7-8`). Do 2.4.

## 4. Release

Only after section 3 is done for the shape. 4.1 is prep; 4.3 to 4.5 change the lock. Both routes put `maintenance_lock_op.sh` and `maintenance_lock.py` from one ansible commit in one fresh directory on VM300; the script calls the helper next to itself.

### 4.1 Stage the files (Ben OK: this writes files on VM300)

On the workstation, in bash (in zsh `$S:scripts` breaks, so write `"${S}:..."`). `S` is the ansible commit Ben read the two files at. It must be on `origin/main`:

```bash
W=$(mktemp -d)        # 0700
A=~/Projects/homelab/ansible
git -C "$A" fetch origin
S=<sha>               # the commit Ben read; for example: git -C "$A" rev-parse origin/main
git -C "$A" merge-base --is-ancestor "$S" origin/main && echo "on main"
for f in maintenance_lock.py maintenance_lock_op.sh; do git -C "$A" show "${S}:scripts/$f" > "$W/$f"; done
sha256sum "$W"/maintenance_lock.py "$W"/maintenance_lock_op.sh    # write both hashes down
```

- Stop if `on main` is not printed. Unmerged code was not reviewed.
- Take both files from the same commit. The script's test runs it against that commit's helper only.
- The hashes come from the commit, not from this page. VM300 must print the same two (last row below, or Route B's `sha256sum`).
- This is main's helper, not the PIN. It reads and writes the same schema-1 record, takes the same `guard`, and adds `execution.guard` (`maintenance_lock.py:83-127`, `:132`, `:147`, `:156`), so a release also refuses while another process holds execution.

**Route A, the live helper (root over QGA, `/root/lock-release`).** Build the rows file, then run it through `live.py`:

```bash
python3 -I - "$W" <<'PY'
import base64, json, pathlib, sys
w = pathlib.Path(sys.argv[1])
parse = "python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get(\"out-data\",\"\")); print(d.get(\"err-data\",\"\")); print(\"exit\", d.get(\"exitcode\"))'"
rows = []
for name in ('maintenance_lock.py', 'maintenance_lock_op.sh'):
    b64 = base64.b64encode((w / name).read_bytes()).decode()
    mk = 'os.mkdir("/root/lock-release",0o700); ' if not rows else ''
    code = 'import base64,os; %sopen("/root/lock-release/%s","xb").write(base64.b64decode("%s"))' % (mk, name, b64)
    rows.append(['pve', "qm guest exec 300 --timeout 30 -- python3 -I -c '%s' | %s" % (code, parse), 60])
rows.append(['pve', 'qm guest exec 300 --timeout 30 -- sha256sum /root/lock-release/maintenance_lock.py /root/lock-release/maintenance_lock_op.sh | ' + parse, 60])
assert all(r[1].isascii() and '$' not in r[1] for r in rows)
(w / 'stage-rows.json').write_text(json.dumps(rows, indent=1))
print('rows:', [len(r[1]) for r in rows])
PY
```

- The first row refuses if `/root/lock-release` already exists, and each file is opened with `xb`. So only one operator can be staged at a time. If it refuses, either someone else is mid-release (stop and find out who) or your own earlier staging failed half way (clean up as in 4.5, Ben OK, and stage again).
- The last row must print the two hashes you wrote down.
- The helper row is about 10.8 KB (helper at `1eab126`). The QGA argument size limit is UNVERIFIED. If that row fails, nothing ran in the guest; stop and use Route B.

**Route B, Ben's own shell on VM300 (`~/lock-release`).**

```bash
ssh ben@10.9.9.10 'mkdir -m 700 ~/lock-release'      # fails if it exists: stop and ask why
scp "$W"/maintenance_lock.py "$W"/maintenance_lock_op.sh ben@10.9.9.10:lock-release/
ssh ben@10.9.9.10 'sha256sum ~/lock-release/*'
```

- VM300 is `10.9.9.10` (qualification doc `:9`). This SSH route is the one used on 11-06; it is UNVERIFIED for this use.
- Ben runs `sudo` himself in 4.3-4.5. Claude never types a password.

### 4.2 The release script (`scripts/maintenance_lock_op.sh`)

Ben reads it, and its test, at `S` before staging. Usage, as root: `timeout 20 bash <dir>/maintenance_lock_op.sh <expected owner> <expected stage> verify|hold|release`.
- It never prints the nonce, and never passes it in argv or through an inner `sudo env`.
- It never uses `set -x`, and never uses `cmd && echo ok` (that does not stop under `set -e`).

Exit codes:

| Exit | Output | Meaning |
|---|---|---|
| 0 | `verify ok: ...`, then `advanced to held` or `released: ...` | done |
| 1 | a helper error, or `pgrep failed` | refused; see the 4.3 stop list |
| 2 | `UNKNOWN STAGE`, `unknown action`, `usage` | bad arguments |
| 3 | `OWNER MISMATCH` | the record is not the run you named |
| 4 | `RUNNER JOB ALIVE` | a job is running; wait, then run again |
| 5 | `HELD BY SOMEONE ELSE` | `held` without your own `held-by-me` marker |
| 6 | `NO LOCK` | no `operation.json` |
| 124 | (none) | `timeout 20` fired, most likely waiting on `guard` |

Why it is shaped like this:
- **The owner and nonce come from the record itself, so the helper's ownership check proves nothing here.** The real gates are:
  - the literal `want_owner`, which Ben copies from `gh run view` (2.2);
  - `verify` at the exact recorded stage;
  - the `held` marker, the runner check and the exclusive staging directory (4.1);
  - Ben's OK.
- The stage list (`acquired`, `applying`, `recovering`, `held`) is the stages section 1 covers. It is policy, not read from CI; terraform's stages live in terraform `maintenance_apply.py`.
- The owner needs no quoting: owners match `[a-zA-Z0-9_./:-]` (`maintenance_lock.py:130`).
- `HOMELAB_RUNNER_INSTANCE` is VM300's own `/sys/class/dmi/id/product_uuid`, lowercased: the value `roles/maintenance_runner/tasks/main.yml:5` checks against the reviewed instance. Every call checks it against `runner-instance` (`maintenance_lock.py:29-31`), and the record's `instance` must match it. Never create a second marker to get past a mismatch (`maintenance-coordination.md:144-145`).
- The root is fixed at `/var/lib/homelab-maintenance` (the role's `maintenance_runner_root`; the test ties the two). The helper's default is the container path `/maintenance` (`maintenance_lock.py:169`).
- Test hooks: `LOCK_OP_TEST_ROOT`, `LOCK_OP_TEST_INSTANCE_FILE` and `LOCK_OP_TEST_WORKER` are read only when the script is not root. As root it ignores them and sets a fixed `PATH`. Not as root, it fails on the root-only DMI file. So in production no environment value changes the root, the instance or the runner check.
- It runs as root on both routes, so group membership does not matter. `hold` written by root keeps the group: `_write` sets 0660 (`maintenance_lock.py:56-59`) in the setgid 2770 directory (role `tasks/main.yml@main:21-27`).
- `timeout 20` ends the script and its helper child if the helper waits on `guard` (`maintenance_lock.py:42`); tested.
- The nonce lives only in this root shell's environment and its python children. The shell exits when the script ends.
- If a Python traceback appears, it shows code lines, not values. The helper's error messages never include the owner or the nonce (`maintenance_lock.py:31`, `:75`, `:77`, `:79`, `:99`, `:103`, `:107`).

### 4.3 Dry check (no change)

Route A row (`live.py`, in a rows file of its own):

```json
[
 ["pve","qm guest exec 300 --timeout 40 -- timeout 20 bash /root/lock-release/maintenance_lock_op.sh Starktastic-Homelab/<repo>/<run_id>/<attempt> <stage> verify | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get(\"out-data\",\"\")); print(d.get(\"err-data\",\"\")); print(\"exit\", d.get(\"exitcode\"))'",90]
]
```

Route B: `sudo timeout 20 bash ~/lock-release/maintenance_lock_op.sh Starktastic-Homelab/<repo>/<run_id>/<attempt> <stage> verify`

Expect `verify ok: ...`. Stop on any of:
- `OWNER MISMATCH`, `HELD BY SOMEONE ELSE`, `RUNNER JOB ALIVE` (wait, then retry), `NO LOCK`, `UNKNOWN STAGE`;
- staged hashes that differ from the ones you wrote down in 4.1 (wrong files staged);
- `Maintenance ownership mismatch`, `Maintenance stage mismatch`, `Corrupt maintenance record`;
- `Maintenance execution is busy` (another process holds `execution.guard`; check `/proc/locks`, 2.3), `Invalid maintenance execution guard`, `Execution guard group permissions changed` (`maintenance_lock.py:99-107`);
- `pgrep failed`;
- `Missing or mismatched runner instance; check the host bind mount` (`maintenance_lock.py:31`). Never "fix the marker" (`maintenance-coordination.md:144-145`).

**Timeouts (Route A):**
- `exit None` (no exit code) means the outcome is unknown: `qm` stopped waiting, and the guest process may still be running (UNVERIFIED on real QGA). Re-run the 2.1 read. Never resend a `hold` or `release`.
- `exit 124` means the in-guest `timeout 20` fired, most likely while waiting on `guard`. Check `/proc/locks` (2.3) before trying `verify` again.

### 4.4 Stage handling (optional, Ben OK)

- `release` does not check the stage (`maintenance_lock.py:146-150`). **No advance is needed to release.**
- The doc shows `advance --stage <recorded> --next-stage held` as an optional marker that a person has taken over (`maintenance-coordination.md:136-141`). The 11-06 precedent skipped it.
- Use `hold` only if reconciling will take a long time and you want the record to say so. It also writes `held-by-me` in your staging directory; after that, pass `held` as the stage in 4.5.
- `held` is not a fence. terraform's own checks verify without a stage (`maintenance_apply.py@main:117-118`).
- If `hold` dies between the `advance` and the `touch`, the record says `held` with no marker, and the script refuses it from then on (exit 5). Do not create the marker by hand: bring it to Ben.

Same row or command as 4.3, with `hold` for `verify`.

### 4.5 Release (Ben OK)

Same row or command as 4.3, with `<stage-or-held> release` for `<stage> verify`. Send the Route A row in a rows file of its own. Expect `verify ok`, then `released: operation.json gone`.

- `released; a NEW record exists now` is a success: a queued job took the lock straight after. Read it with 2.1. Do not retry the release.

Clean up (Ben OK):
- Route A: `["pve","qm guest exec 300 --timeout 30 -- rm -rf /root/lock-release | python3 -c 'import json,sys; d=json.load(sys.stdin); print(\"exit\", d.get(\"exitcode\"))'",60]`
- Route B: `rm -rf ~/lock-release`
- Workstation: `rm -rf "$W"`

## 5. Verify, then resume

### 5.1 Lock free

- Run the row from 2.1 again. Expect `NO LOCK`.
- `runner-instance`, `guard` and `operations/` must still be there. A release unlinks only `operation.json` (`maintenance_lock.py:149`). `execution.guard` may now exist too; leave it.

### 5.2 Resume, by shape

Rules for every resume:
- **One ansible resume deploy, at ansible `main` as it is now.** Each deploy runs the whole `k3s.yml` and the bootstrap at its own SHA (`deploy.yml@main:54,96-102`), so rerunning several old runs can end on an older SHA.
  - If there is a `drained_nodes` payload to deliver: the 3.5 dispatch, with that payload (Ben OK). A dispatch runs current `main`, so it also carries any fix.
  - Otherwise: rerun only the newest failed deploy run whose `headSha` is ansible `main` now (`gh run view <id> --json headSha`): `gh run rerun <id> -R Starktastic-Homelab/ansible --failed` (Ben OK). If `main` has not moved, that is the original run.
  - If no failed deploy run sits at `main` now (for example the fix commit was `[skip ci]`): a manual run on `main`, `gh workflow run deploy.yml -R Starktastic-Homelab/ansible --ref main` (Ben OK; `deploy.yml@main:4`; it carries `[]`, `:44-46`).
  - Every other deploy run that failed at Acquire stays as it is.
- **A terraform run (the failed one, or one that failed at Acquire behind it) may be rerun only if** no other `apply.yml` run printed "Graceful shutdown before Terraform" after that PR's `validate-and-plan` ran (`gh run list -R Starktastic-Homelab/terraform -w apply.yml`, then `--log` of each). A rerun shuts VMs down before Terraform checks the plan (3.4, Never). Otherwise open a fresh PR (Ben OK).
- **Never rerun a drain-mode run while a node cordoned by a failed run is still cordoned.**
- **Any uncordon after the release:** right before it, run 2.1 (expect `NO LOCK`) and the 2.2 in-flight check, with merges still held.
- A failed or cancelled saved-plan run leaves `pr-<N>/` in the plan bucket: `cleanup` runs only on success (`apply.yml@main:210-217`). A fresh-PR resume leaves it there. There is no tool to remove it by hand (section 7).

By shape:
- **terraform 3.2 (nothing applied):**
  - Ben chooses one of two ways.
  - Re-run the failed run, if the rule above allows it. It gets a new attempt and owner, and re-applies the same saved plan, which is still valid because state did not move.
  - Or open a fresh PR so a new plan is reviewed.
- **terraform 3.3 (drain, nothing applied):** after the uncordons, the same choice as 3.2. The new drain computes a correct list.
- **terraform 3.4 (partial apply), on main today (telmate):**
  - Do not re-run the failed run.
  - Open a new terraform PR. Its `validate-and-plan` plan, made against live state, shows what is left to replace. Ben reviews it and merges it (Ben OK).
  - For a master-then-workers replace, the leftover worker replace is designed to replan: workers keep the master's UUID in `force_recreate_on_change_of` (`modules/vm/main.tf@main:21`; `main.tf@main:49-53`, "a mismatch remains after an interrupted apply"). Whether telmate replans any other unfinished replace is UNVERIFIED.
  - After its deploy, check that replaced nodes came back schedulable, and uncordon by hand (Ben OK, with the recheck above) each node from the 3.4 list that is still `SchedulingDisabled`.
- **terraform 3.4, after 5.13 (bpg):**
  - `rebuild_nonce` does not exist on terraform main today (grep).
  - When it exists, follow draft 5.13 D50(i): start the stopped VMs (Ben OK, disk check from 3.4), wait for every node `Ready`, release, open a PR that bumps `rebuild_nonce`, then uncordon by hand after its deploy (with the recheck above).
- **terraform `recovering`, or `applying` with apply done:**
  - Nothing to re-apply.
  - Rebuild `drained_nodes` from the log: `[]` outside drain mode; the 3.3 method in drain mode.
  - Send the 3.5 dispatch (Ben OK).
- **3.5:** the dispatch was the resume. Watch the deploy run.
- **ansible 3.6:** the one resume deploy from the rules above. With a fix commit, that is the fix's push run (it failed at Acquire), not the original.
- **ansible 3.7:** the whole-run rerun in 3.7.
- **Never use `workflow_dispatch` on `apply.yml` as a shortcut.** It plans and applies in one step, without review, and always dispatches (`apply.yml@main:47-50`, `maintenance_apply.py@main:103-104`, README `:105-106`). Use it only if Ben chooses it.

### 5.3 Done when

- There is no `operation.json`.
- The resume run is green.
- `KUBE get nodes` shows every node `Ready`, with no unexpected `SchedulingDisabled`.
- ArgoCD is healthy.
- The staging directories (4.5) and any 2.4 leftovers are gone.
- Record what was done, in one or two lines, for the next incident.

## 6. Never

- `cat`, `tee`, copy or upload `operation.json`. An evidence bundle must leave it out or redact the nonce. Keep the runner workspace out too (2.4).
- Pass the nonce in argv, through `sudo env NONCE=...`, under `set -x`, or in a live-helper row.
- Release while the owner run, its container or `Runner.Worker` is still alive.
- Release because the lock is old, on a timer, or to unblock a merge.
- Release before reconciling (no cordons left unexplained, no VM left stopped unexplained).
- Remove `operation.json` by any means other than the helper's `release`. That includes a corrupt record: bring it to Ben.
- Resend a `hold` or `release` after `exit None`.
- Create a second `runner-instance` marker, or edit the existing one.
- Re-run a terraform run that failed at `applying` or `recovering`.
- Rerun drain or apply to rebuild `drained_nodes`.
- Use `workflow_dispatch` on `apply.yml` or a targeted apply to "finish" a failed apply.
- Stage `maintenance_lock_op.sh` and `maintenance_lock.py` from different commits, from a commit not on `origin/main`, or from a copy of this page.

## 7. Open questions and unverified items

1. **What exactly does kubectl print for an already-cordoned node?** The output does reach the Actions log (3.1). Settle: read a past drain-mode `apply.yml` log (none in the last 30 runs), or run `kubectl cordon` twice on a lab node.
2. **Does "Re-run" of a `repository_dispatch` run keep `client_payload`?** No longer needed by 5.2 (a dispatched resume uses a fresh dispatch). Settle: GitHub docs.
3. **Does re-running only a failed `trigger-ansible` job reuse the `apply` job outputs, or rerun `apply`?** Settle: GitHub docs on re-running a single job with `needs`. Until then, use the `gh api` dispatch.
4. **Does a GitHub cancel reach `maintenance_apply.py` inside the container as SIGINT (giving `restart_vms`) or as SIGTERM or SIGKILL (no restart)?** Settle: a lab job that traps signals. Until then, treat a cancel like 3.4 with no restart.
5. **Does terraform save state when killed mid-apply?** Settle: the next PR's plan comment. Do not read state.
6. **Route checks on VM300:** does `ssh ben@10.9.9.10` work from the workstation, and does `sudo` there want a password (Ben types it)? Is QGA happy with an 11 KB argument? Does `qm guest exec --timeout` leave the guest process running? Settle: a harmless `qm guest exec 300 -- python3 -I -c '<11 KB comment>'` and a `sleep 60` with `--timeout 5`, read-only, Ben OK.
7. **Where is the runner's work directory on VM300?** Needed for 2.4. Settle: the "Checkout repository" log of any self-hosted run.
8. **Is a full `k3s.yml` rerun safe after a failure in each play?** Settle: read `roles/k3s_init`, `k3s_masters`, `k3s_node_retirement`, `k3s_workers` and `bootstrap_cluster` for non-idempotent steps. Retirement is live (2.3), so it is in scope.
9. **Does telmate replan an unfinished replace other than master-then-workers after a failed apply?** Settle: a lab apply, or the plan comment of the next PR after a real failure.
10. **Can a new attempt of a deploy run read the `kubeconfig-handoff` artifact an earlier attempt uploaded?** Matters only for 3.7 with `--failed`; 3.7 uses a whole-run rerun until then. Settle: GitHub docs.
11. **How long does GitHub take to mark a run `completed` after its runner died?** Until then, 2.3's boot-time rule is the way past a reboot.
12. **Should `i915-sriov-upgrade.yml` (and other self-hosted writers) take the maintenance lock?** Its reboot can kill a locked job (3.8). Ben decides.
13. **Should a stale `pr-<N>/` plan be removed from the bucket after a fresh-PR resume, and with what tool?** A rerun of that old run would read it. Ben decides.
14. **Can pve's `meta: ctime=` in `qm config` date a VM's creation**, as a KUBE-free way to tell old from new? Not proven from code.
15. **Who held the lock on 09-29 and 09-30?** Deploys 36618663331 and 36767121240 failed at Acquire (per research, not re-checked). Not needed for this runbook, but it shows a hand-held lock blocked CI with no record of who held it. Settle: Codex-era notes, if any.
16. **Should a hand release leave a receipt in `operations/`?** The role creates that directory as "durable operation evidence" (role `tasks/main.yml:62-68`), and the helper never writes there. Ben decides.
17. **Does `execution.guard` exist on VM300, and with what owner and mode?** The retired executor took execution exclusion and was installed on VM300 (`maintenance-executor.md:10-11,29-31`), so it probably exists at 0660. Settle: the 2.3 `stat` row.

## Review notes

Findings not taken, or taken in part:
- **Correctness I1, the reason the 11-06 precedent worked** ("its fix was outside the ansible tree"): taken in part. Both attempts ran `1a21268`, so any fix between them was outside the repo; but the named helm-diff fix #304 (`902db94`, 08:53Z) is an ansible commit that landed after attempt 2 passed (01:48Z). Whether a by-hand change came between the attempts was not checked. The mechanism (a rerun keeps the old SHA) is taken.
- **Safety I2, a person-owned lock around late uncordons:** not taken. It needs nonce handling at a terminal. The recheck before each uncordon (5.2) is used instead.
- **Safety I3, a `held-by-me` marker alone:** in a shared `/root/lock-release` it cannot tell two QGA operators apart. Taken together with the staging directory that refuses to exist twice (4.1).
- **Correctness M5, `gh run rerun --failed` for a failure after Release:** if only `handoff` failed, `--failed` reruns `handoff` alone, not `provision`. 3.7 uses a whole-run rerun instead.
- **Correctness M8, "remove `pr-<N>/` from the bucket (Ben OK)" as a step:** no repo tool does this by hand. Recorded in 5.2 and question 13 instead.
- **Operability C1, "Section 4 cannot be run":** taken as a second route. Route B (Ben's own shell) stays, since this runbook is for Ben.
- **Operability M1, group membership settled from code:** the qualification proves it for 09-21 only. It is moot now: the script runs as root on both routes.

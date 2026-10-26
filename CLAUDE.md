# ansible

Configures the Proxmox host (i915 SR-IOV, ser2net) and the K3s cluster on the Terraform VMs: K3s with
kube-vip HA, the sealed-secrets key, TLS restore, and ArgoCD pointed at `apps`. Stage 3 of
packer → terraform → ansible → apps; the system overview is `../CLAUDE.md`.

## What runs live

- **Every push to `main` is a full deploy** (`deploy.yml` → `k3s.yml`, ArgoCD helm upgrade, rewrite of
  org secret `KUBECONFIG_RAW`). Path-ignored: `README.md`, `renovate.json`, `group_vars/proxmox_hosts/**`,
  `roles/ser2net/**`, `ser2net.yml`. Docs-only commits (including this file) need `[skip ci]`.
- `deploy.yml` also runs on Terraform's `repository_dispatch: infrastructure-changed`. Deploys queue and
  are never cancelled, since an interrupted `helm upgrade` leaves a pending release.
- **A change to `group_vars/proxmox_hosts/i915_sriov.yml` on `main` reboots the Proxmox host**
  (`i915-sriov-upgrade.yml`), runner included, without taking the maintenance lock.
- Pushing ser2net files deploys ser2net to the host; `format.yml` pushes Prettier commits to PR branches.

## Commands

```sh
# Python 3.14: Ansible 2.21 fails on 3.15, so don't merge renovate/python-3.x
pip install -r requirements.txt && ansible-galaxy collection install -r requirements.yml
python3 -m unittest discover -s scripts/tests -p 'test_maintenance*.py' -v
python3 scripts/tests/test_<name>.py  # proxmox_csi_bootstrap, node_retirement, csi_preflight, drain_recovery,
                                      # proxmox_csi_storage, deploy_lock, i915_compat, i915_acceptance
ansible-lint --offline                # no path argument: `.` silently skips roles/
```

Lint needs a `.vault_pass` (`ansible.cfg`): create a dummy only if none exists, never overwrite it.
Playbook runs need the real vault, SSH key and `PROXMOX_*` env; the inventory queries the live API.

## Safe vs ask first

- Safe: the tests, `ansible-lint --offline`, `yamllint`, `pre-commit run --files <changed files>`.
- Ask first (settings prompt for these): any `ansible-playbook`, including `--check` and `--syntax-check`,
  which change nothing but connect to live hosts with the vault and SSH key; `ansible-vault`; `gh workflow
  run`; merging PRs; `kubectl`/`helm` against the cluster; `scripts/maintenance_lock.py` acquire/release.
- `proxmox-csi-storage.yml` runs only from the maintenance runner under an existing lock, never from here.

## Before calling a change done

Run the touched area's tests and `ansible-lint --offline`; for playbook changes, ask to run `--check`.
Report what ran. GPU work is done only when `i915-acceptance.yml` passes post-reboot; never reboot twice.

## Gotchas and past decisions

- Bumping `k3s_version` doesn't upgrade existing nodes: install runs only when `/usr/local/bin/k3s` is
  absent (`roles/k3s_common/tasks/main.yml`).
- kube-vip manifests stay out of the k3s auto-deploy dir, or the addon controller revokes their RBAC.
  The long leader-election timings are deliberate (etcd disk stalls; see the role defaults comments).
- i915 handlers keep "Reboot host" last and use `shutdown -r +1` because the runner lives on that host.
- Node retirement is enabled: one host at a time, skipped in check mode. Drain recovery uncordons only
  nodes in both the dispatch payload and the inventory, after all are Ready (`k3s.yml`).
- CSI: 29 attachment slots per worker; OS disks stay on virtio0/ide2, never scsi1-29
  (`group_vars/all/proxmox_csi.yml`, README "Proxmox CSI bootstrap"). Changing the budget means changing
  apps' capacity alert too. The TLS anchor `files/proxmox-api-trust.pem` expires 2027-06-14.
- Maintenance lock (README "External maintenance coordination"): failed runs keep it; never remove one for being old.
- apps owns ArgoCD's notifications ConfigMap/Secret (`create: false`, `bootstrap_cluster/tasks/argocd.yml`).
- Keep host i915 pins as double-quoted YAML (`key: "value"`): packer regex-reads them.
- `docs/iscsi-*.md` and `docs/superpowers/` are history; #286 retired the iSCSI executor.

## Contracts

Changing a contract means changing both sides together; see `../CLAUDE.md`.

- ← terraform dispatch: `deploy.yml` (`repository_dispatch`, `K3S_DRAIN_RECOVERY_NODES`) and the drain
  recovery play in `k3s.yml`. ← terraform VM tags: groups in `inventory/proxmox.yml`.
- Maintenance lock ↔ terraform: `scripts/maintenance_lock.py`. `deploy.yml` checks out a pinned SHA that
  exists only on branch `codex/storage-maintenance`, while `roles/k3s_node_retirement` uses this copy.
- → terraform, apps: org secret `KUBECONFIG_RAW` (`handoff` job in `deploy.yml`).
- ↔ packer: `group_vars/proxmox_hosts/i915_sriov.yml`, `roles/i915_sriov/defaults/main.yml`,
  `.github/workflows/i915-compat.yml`; `scripts/i915_compat.py` and its test match packer's byte for byte.
- → apps: `roles/bootstrap_cluster/tasks/{argocd,main,restore-certs}.yml`, worker labels in
  `roles/k3s_workers/tasks/main.yml`, CSI topology in `group_vars/all/proxmox_csi.yml`, NFS in
  `group_vars/all/vars.yml`.

## Git

Commit only in this repo. Never push or open PRs unless asked. Never commit secrets, `.vault_pass`, keys
or decrypted vault content.

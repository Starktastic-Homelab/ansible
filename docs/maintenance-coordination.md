# Maintenance coordination

> Moved from the README on 2026-11-16. The text is unchanged.

## External maintenance coordination

VM300 was bootstrapped and checked across real containers on September 21, 2026.
See the [qualification record](maintenance-runner-qualification-2026-09-21.md)
for evidence and the remaining workflow/reboot test limits.

Infrastructure replacement and configuration share VM300's
`/var/lib/homelab-maintenance`, mounted as `/maintenance` in mutating job
containers. An absent/mismatched runner marker blocks execution. The record is
independent of K3s and NAS availability; it survives job cancellation and reboot.
No age-based takeover or unconditional unlock exists. Prohibit concurrent manual
Proxmox GUI start/recreate operations during a maintenance operation.

VM300 has 4096 MiB. Before this role pinned it, it had a balloon floor of 2048 MiB (VM300 is
not in Terraform; when and by whom 2048 was set is not recorded), the host's only
ballooned VM, and with the host near full, auto-ballooning could hold it at the
floor. At the old 1024 MiB floor that OOM-killed helm-diff during the ArgoCD 3.6
chart upgrade: the runner still had helm-diff 3.15.7, whose diff needed about
3.4 GB for the ArgoCD CRDs. `bootstrap_cluster` now reinstalls helm-diff whenever
it differs from the pin. `proxmox_guest_startup` now sets VM300's balloon equal to
its memory (`proxmox_guest_startup_balloon` in
`group_vars/proxmox_hosts/guest_startup.yml`), so it never balloons. The
minimum changes live; the actual size follows through pvestatd, or a one-off `qm balloon 300 4096`. The same role caps the ZFS ARC at 8 GiB
(`proxmox_guest_startup_zfs_arc_max_bytes`) through
`/etc/tmpfiles.d/zfs-arc-max.conf`, applied live when the running value differs.
`/etc/modprobe.d/zfs.conf` (installer-owned, 13481541632) is left as is; it sets
the value at module load and the tmpfiles value wins after boot.

Before merging workflows, run `maintenance-runner.yml` against an explicitly
reviewed `maintenance_runner` inventory host for **VM300**, after confirming its
address, SSH route, service user and SMBIOS UUID. Set `maintenance_runner_user`
to that service user. The role checks UUID
`cc1aeeb7-4827-466c-9d4b-5dc6c881f193`; it neither guesses an IP nor creates a
runner. Restart the runner service in an approved window if supplementary group
membership changed. This bootstrap is manual, never part of `k3s.yml`.

Deploy uses a pinned helper, acquires before configuration, verifies immediately
before the playbook, and releases only after success. Terraform's companion PR
holds the same lock through drain/apply and releases before dispatching Ansible.
Ansible installs k3s before performing the requested drain recovery and bootstrap. Existing workflow concurrency alone does not serialize repositories.
The mutation job must run on this external runner; a container-local directory
without the matching host marker cannot acquire ownership.

The optional `infrastructure-changed` payload `drained_nodes` is a JSON list of
nodes Terraform made unschedulable. After all k3s installation plays succeed,
Ansible waits for every current inventory node to become Ready, then uncordons
only listed names still in the inventory, before ArgoCD bootstrap. Installation
or recovery failure stops the playbook and retains maintenance ownership.
Ordinary push/manual runs and older dispatches without the list do not change
node scheduling. This receiver must merge before the paired Terraform sender.

Nodes already cordoned before Terraform started are omitted from the list, so
surviving Node objects keep those operator holds. Recreated Node objects retain
the normal Kubernetes scheduling defaults; this mechanism does not restore
operator cordons after object deletion. Removed inventory nodes need no recovery.
If the sender's dispatch fails after apply/release, inspect both runs and retry
the failed dispatch with its original payload. Do not repeat drain/apply to infer
the original list. Integrated replacement remains subject to disposable-lab
qualification before Proxmox CSI activation.

Worker retirement is staged separately with `k3s_node_retirement_enabled: false`.
When enabled after disposable-lab qualification, Ansible compares the current
worker's SMBIOS UUID with its Kubernetes Node before joining the agent. A missing
Node or unchanged generation is a no-op. A stale generation requires a matching
replacement VM and complete current Proxmox inventory proving the old UUID is
absent, including stopped VMs and templates. Native Kubernetes deletion uses the
observed Node UID as a precondition; K3s removes its own node-password Secret.
The role never stops VMs, forces storage detach, or deletes password Secrets.

Verification uses the existing `PROXMOX_URL`, `PROXMOX_USER`, `PROXMOX_TOKEN_ID`
and `PROXMOX_TOKEN_SECRET` environment settings. The read-only prerequisites are
propagated `VM.Audit` at `/vms`, propagated `Pool.Audit` at `/pool`, and `Sys.Audit`
at `/access`. It checks the full ACL list and effective child permissions, then
refuses incomplete visibility, missing/locked configs, duplicate UUIDs or drift.
A restrictive pool override without inherited `Pool.Audit` conservatively blocks
the check. It does not grant privileges or use the CSI runtime token. API requests
verify TLS by default. The homelab configuration sets
`k3s_node_retirement_ca_path` to the public `files/proxmox-api-trust.pem` on the
Ansible runner. This trusts the exact PVE API leaf certificate: the existing PVE
CA lacks the key-usage extension required by Python's strict verifier, while
native partial-chain validation accepts this explicitly trusted leaf. Certificate
expiry and IP/hostname verification remain enabled; no TLS flags or upstream
client code are changed. Other installations may override the path with their
own valid CA bundle. Disabling verification remains an isolated-lab override only.

The leaf was independently read through authenticated Proxmox SSH and matched
to a strict TLS handshake from VM300 on 2026-10-05 (Asia/Jerusalem). Its SHA256 is
`991939462464635f91d2a089924eab1055637e8bd48d7771a26cb2b378b41a1c`, and it expires
2027-06-14. It covers API address `10.9.9.20`. Before renewing/replacing the API
certificate, review and deploy its new public trust anchor through the same
authenticated route, then verify the actual deployment client's connection.
An unrecognized replacement certificate or expired trust anchor must block
retirement. This file contains no private key and does not enable retirement,
CSI, topology changes or VM replacement. Ordinary k3s rebuilds do not replace
the Proxmox host's API certificate.

The successful handshake checked stock Python3.13 TLS behavior; production
deployment credentials, full inventory visibility and the complete Ansible
retirement path remain activation checks. Python documents these default
[strict and partial-chain flags](https://docs.python.org/3.13/library/ssl.html#ssl.create_default_context).

Before enabling retirement, the manually dispatched **Read-only CSI activation
preflight** workflow uses the same deployment API and Vault secrets on VM300.
It checks strict API TLS, the existing complete-visibility filter, readable QEMU
configurations and stable permissions/inventory. It also seals and recovers a
synthetic Secret with native kubeseal and the external Vault key, requiring its
certificate to match the pinned Apps public certificate. It does not use the
live Kubernetes sealing key, delete Nodes or run a cluster deployment. A failure
blocks activation; it never broadens API privileges. Temporary private-key files
and the job's Vault password file are removed, and secret-bearing tasks suppress
output. The workflow pins the public certificate to Apps commit
`9a32641c261fb3d72ed2bfc2598f5acc57c891d6`; review that pin when the bootstrap key
is intentionally rotated. This is a one-time activation check, not a new routine
rebuild checkpoint.

The role verifies existing maintenance ownership before evidence collection and
immediately before deletion. The normal root is `/maintenance`; disposable labs
may set `k3s_node_retirement_maintenance_root` to their externally coordinated lab
root. Read failures, changed Node UID or uncertain evidence stop worker joining,
scheduling recovery and bootstrap; workflow ownership stays held on failure.
No concurrent manual VM/ACL mutation is allowed during that operation. Check mode
skips retirement and cannot qualify recovery. This role does not restore cordons
after Node recreation or replace the full-control-plane cohort safety policy.

After a failed/cancelled operation: inspect `operation.json` on VM300 (protect its
nonce from logs), identify the exact repository/run/attempt and current stage,
ensure the owner process is gone, inspect infrastructure/workload state, and
reconcile the failed operation explicitly. Never delete a lock just because it is
old. An operator continuation requires the original `MAINTENANCE_OWNER` and
`MAINTENANCE_NONCE`, the expected `HOMELAB_RUNNER_INSTANCE`, and an exact stage:

```sh
python3 scripts/maintenance_lock.py verify --stage acquired
python3 scripts/maintenance_lock.py advance --stage acquired --next-stage held
```

Use the recorded stage, not necessarily the example above. Only after resolving
all uncertain effects may the original owner release. Losing the runner disk or
its identity blocks maintenance until the shared lock domain is recovered and
all possible writers/infrastructure jobs have been reconciled. Do not provision
a second independent runner marker to bypass a held operation.

The retired iSCSI status/preflight executor, request dispatcher/installer and
user-service template are removed. `maintenance-runner.yml` retains the VM300
instance marker, operation directory and runner group access. Preserve installed
immutable runtimes, manifests and operation receipts as historical evidence; do
not modify them in place or reuse old receipts as current writer authority.
The generic maintenance helper and native deployment locking remain in use.

### Proxmox CSI bootstrap

`group_vars/all/proxmox_csi.yml` enables the native topology prerequisites for the
[accepted shared integration plan](https://github.com/Starktastic-Homelab/apps/blob/main/docs/superpowers/plans/2026-10-03-proxmox-csi-shared-integration.md).
The homelab configuration sets `proxmox_csi_enabled: true`; an explicit false
override keeps the generated k3s configuration free of CSI labels.
It does not install CSI, register NFS storage, allocate images, publish credentials,
or migrate Jellyfin.

The nonempty `proxmox_csi_region` matches the stock driver's `clusters[].region`. Each VM's zone
comes from the dynamic inventory's `proxmox_node`; no service or VM-to-zone map is
maintained here. Both values must be valid Kubernetes label values. `k3s_common`
validates them before configuring that node and writes native `node-label` options.
Bootstrap also waits for and patches existing Node objects before installing ArgoCD,
because registration flags do not update labels on already registered nodes.
Workers additionally register and reconcile the stock driver's
`csi.proxmox.sinextra.dev/max-volume-attachments: "29"` label. The unmodified
v0.20.0 [attachment loop](https://github.com/sergelogvinov/proxmox-csi-plugin/blob/v0.20.0/pkg/csi/utils.go)
uses only `scsi1` through `scsi29`; advertising 30 would allow an attachment that
cannot succeed. The worker OS disk uses `virtio0` and cloud-init uses `ide2`,
leaving those SCSI slots for CSI. Do not add other disks in that range without
reducing the allowance. Control-plane labels remain topology-only.

For the first rollout on existing workers, merging this change runs the normal
deployment, updates the registration configuration and patches the live labels.
The existing serial worker rollout can restart each k3s agent. After successful
deployment, refresh the CSI node plugin Pods one at a time (ordinary Pod deletion,
never a disk detach), waiting for the recreated plugin to become Ready and its
`CSINode.spec.drivers[].allocatable.count` to report 29 before continuing.
Existing application Pods and disks stay in place. A label change alone does not
guarantee that kubelet has refreshed the advertised count. Fresh cluster rebuilds
receive the label before the plugin is first registered and need no extra step.

The matching Apps capacity alerts use this 29-slot budget. Activate them only
after both live counts have been verified. Two workers provide 58 total slots;
they cannot accommodate more than 29 volumes on one surviving worker. Alerts
provide advance notice, not automatic worker creation. The worker count stays
under Terraform's existing policy.

Changing these labels updates the k3s configuration and can restart existing k3s services.
Disabling the integration later does not
uninstall CSI or remove existing Kubernetes labels; it is not a rollback procedure.

The stock v0.20.0 [node image](https://github.com/sergelogvinov/proxmox-csi-plugin/blob/v0.20.0/Dockerfile)
already carries mount, filesystem check/format/resize and device tools. No additional
guest filesystem package installation or driver wrapper is added here. The qualified
Debian guests supply the kernel/device support; the integrated deployment still needs
its synthetic-volume qualification before production use.

Replacement-ordering review: Terraform's locked `destroy` path completes all destroys
before applying the new VMs. Ansible is dispatched only after a successful changed
apply. Packer guests do not install k3s; this playbook starts it afterwards. The existing
worker install guard only checks whether the k3s binary exists. A fresh control plane
with surviving old workers is therefore **not qualified for CSI activation**: this
PR adds neither fencing nor a safe partial-datastore-replacement workflow. The full
rebuild lab does not prove that scenario safe. Retain the existing maintenance lock
and the verified Node-retirement/full-cohort replacement safeguards.

Before activation, the remaining shared scope is: a dedicated NAS export and Proxmox
NFS registration, an external reserved image-owner ID, scoped API permissions and
recoverable credentials, the stock Apps controller, safe replacement ordering, and
PVC-first resize reconciliation. Apps remains the sole per-volume inventory. No
production choices or resources are inferred from the disposable lab.

Offline regression check (Ansible and PyYAML installed):

```sh
python3 scripts/tests/test_proxmox_csi_bootstrap.py
```

It renders with Ansible's own templating engine, checks exact disabled master/worker
configuration bytes, inventory-specific new/existing Node labels, and rejects empty,
malformed or overlong topology values, and checks bootstrap tag selection. It never connects to a host or Kubernetes API.

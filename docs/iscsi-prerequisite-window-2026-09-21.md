# Worker enrollment and fencing qualification: next live window

Prepared 2026-09-21 after the approved NFS durability correction. This document
proposes the next operation; it is not an enrollment or fencing receipt.

## Current evidence

Read-only preflight at 19:02:50 UTC confirms both workers and the master Ready,
with the expected Proxmox SMBIOS UUIDs and current Kubernetes node UIDs. Both
workers lack open-iscsi and have no initiator identity, sessions or stored targets.
The current kernel already provides iscsi_tcp. Both routes to 10.9.8.30 select
eth1 and their expected storage IP. Free root space is 26.6 GiB on worker 201 and
49.1 GiB on worker 202; repeat the 25 GiB minimum check immediately before work.
The maintenance lock is free. The fencing user, role and grants do not exist.
VMID 900 belongs to the Packer template and must not be used as a test VM.

Ansible's merged map used k3s-worker-* keys although inventory and Node names are
kube-worker-*. The companion correction changes only those keys, preserving the
planned iqn.2026-09.net.starktastic:k3s-worker-01/-02 identifiers. Before the fix,
the actual worker lookup fails with KeyError. With the fix, both captured worker
generations pass the existing enrollment validator. This is an offline check,
not an assertion that unused-IQN review or production enrollment has occurred.

## Scope for the next approval

1. Use the existing durable maintenance lock on VM300 for every mutation phase.
   Pin the reviewed Ansible commit and helper; recheck worker generations, Node UIDs, active operations,
   free space, idle sessions, stored target records and IQN non-use. Save the
   reviewed first-enrollment records outside Kubernetes under ownership, then
   release that preparation operation before dispatching the deployment workflow.
   No concurrent Proxmox
   GUI start/recreate or infrastructure apply during this window.
2. Enroll workers 201 and 202 with the merged iscsi_initiator role: install
   open-iscsi/e2fsprogs, load/persist iscsi_tcp, assign the distinct reviewed IQNs,
   start iscsid, verify routes and current generation, and publish prerequisite
   labels/observations. Use the existing opt-in deployment workflow after the
   correction merges; it also performs the normal K3s/ArgoCD reconciliation.
   Routine merge deployment retains enrollment disabled. No VM replacement,
   planned reboot, target login, filesystem mount or Jellyfin storage change.
3. After enrollment succeeds, acquire a distinct account-qualification operation
   and apply storage-fencing.yml under that ownership. Create
   iscsi-fence@pve!retained with privilege separation and HomelabFence containing
   only VM.Audit and VM.PowerMgmt. Grant both user and token those privileges at
   /vms/201 and /vms/202 with propagation disabled. Save the new token privately
   at /var/lib/homelab-maintenance/private/pve-fence.json on VM300. Inside the
   runner container, pass storage_fencing_secret_destination as
   /maintenance/private/pve-fence.json, using the existing bind mount. Store an
   independently trusted PVE CA and leaf pin alongside it. Never place token material in Git,
   Kubernetes or logs. VM.PowerMgmt also permits start/reboot at the API level;
   the reviewed command exposes only stop and inspection/reconciliation.
4. Verify effective user and token permissions and actual worker config/status
   reads. Test denied READ access to protected VMs 100, 200 and 300. Never issue
   negative power tests against a production VM.
5. Recheck VMID 990 unused immediately before creation. Create one VM named
   owned-fence-test-jellyfin-20260921, with an operation-recorded random SMBIOS
   UUID, one CPU, 128 MiB RAM, no disks, no network and onboot disabled. Refuse
   any pre-existing occupant; do not adopt it. Temporarily add the same exact
   user/token ACLs on /vms/990. Verify its actual configuration before starting
   it. Demonstrate wrong-UUID refusal with no stop request, one successful
   token stop with task polling. Restart only this disposable VM for the
   intentionally suppressed stop-response case, using a distinct receipt/intent
   path. Reconcile that case read-only against its own original durable intent.
   Only this disposable VM may be started/stopped for qualification.
6. Remove only the temporary /vms/990 user/token grants and the identity-checked,
   stopped, diskless test VM. Recheck permanent permissions, unchanged production
   VM identities/power states and app readiness. Save sanitized qualification
   evidence, then release the maintenance lock. Failure or ambiguity retains
   ownership and journals for explicit recovery; never retry a blind stop,
   silently rotate the token or clear the lock on age alone.

Enrollment's deployment workflow acquires/releases its own ownership. Run that
as a complete first phase; acquire a distinct operation for account/test-VM
qualification after successful enrollment and recheck all identities. Do not
nest locks or pass an unowned continuation into the deploy workflow.

## Gates after this window

This qualifies worker prerequisites and the restricted permission route. It
creates no NAS iSCSI service/export, retained PV or writable Jellyfin target.
Native target onboarding, sealed CHAP and the scheduled cold migration remain
separate reviewed operations. Keep the source-held outage PR unmerged.

Packer #109 still supplies these packages in future images; merging it starts a
normal template build. Current workers can be enrolled with Ansible without a
Packer build or Terraform replacement. No image build is part of this window.

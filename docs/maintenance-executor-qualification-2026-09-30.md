# Disposable supervised executor qualification — 2026-09-30

The supervised executor passed seven synthetic systemd checks inside temporary
VM990 on Proxmox. The guest and all its disks were removed after evidence export.
No Ansible invocation, container or systemd test ran on the operator workstation.
No production VM was stopped or reconfigured.

## Environment and identity

- Full clone of Debian template VM900, one vCPU, 1024 MiB RAM, 4 GiB on vm-pool.
- VM name: `owned-maintenance-qualification-20260930`.
- SMBIOS UUID: `8bbf6024-b312-4068-8781-070e772d5249`.
- Machine ID: `8bbf6024b31240688781070e772d5249`.
- No network interfaces; onboot disabled; commands transported by QEMU guest agent.
- Python 3.13.5; systemd 257.13-1~deb13u1; dedicated unprivileged `qualification` user.
- Root bootstrap marked this exact disposable machine, granted read access to its
  DMI UUID and enabled the test user's manager/lingering. These changes existed
  only in VM990. No packages were installed and no production credentials entered it.

Sources were copied from reviewed Ansible revision `9acbeac`, plus the added
supervisor-SIGKILL harness case in this PR. Exact source hashes are recorded in
[the sanitized evidence](evidence/maintenance-executor-2026-09-30.json).
The fixed synthetic adapter exercises execution and supervision; it does not
contact storage or substitute for application acceptance.

## Results

1. Submission client exits while supervised work continues; duplicate submission
   preserves the same started record.
2. Concurrent execution is refused. Forced service stop cleans the descendant,
   leaves an unknown request and cannot grant replay permission.
3. An accepted request refused while busy can be explicitly started later.
4. SIGKILL of the supervisor cleans its descendant, leaves no terminal receipt
   and cannot be explicitly restarted.
5. Nonzero child exit produces a failed receipt.
6. A stale started receipt is neither success nor permission to replay.
7. Source drift refuses execution before started evidence or adapter invocation.

Original ownership bytes were unchanged. Exported evidence includes request and
receipt records, file modes and hashes, source hashes, removed temporary unit and
final process inventory containing only the user's systemd manager and PAM helper.
Credentials are omitted. The initial six-case run passed; the expanded seven-case
run used a fresh synthetic state directory and also passed.

## Cleanup and boundary

Cleanup rechecked VM990's name, UUID, disk and disconnected network, stopped it,
verified stopped state, then deleted it and its owned volumes. A final inventory
confirmed VM990 absent and zero remaining VM990 volumes. VMs 100, 200, 201, 202 and
300 remained running with continuing uptimes; VM900 remained a stopped template.

This qualifies the synthetic execution protocol on the recorded guest runtime.
Ansible installation, root-owned release assembly on VM300, actual runner reboot,
production Apps runtime/dependencies, live storage and mutation recovery remain
unqualified. Merging source does not enable the executor. A separate reviewed
installation plan is required before production use; Ansible main merges still
trigger that repository's existing K3s deployment workflow.

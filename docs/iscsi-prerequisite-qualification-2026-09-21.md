# Worker iSCSI and restricted fencing qualification

Completed September 21, 2026. Both workers now have verified initiator
prerequisites. The restricted Proxmox account passed actual-token qualification.
All 97 Deployments/StatefulSets and all three nodes are ready. Maintenance
ownership is released; the temporary VM, grants, containers and tool image are
removed. Jellyfin remains online on its original NFS configuration.

## Enrollment and reconciled workflow failure

[Ansible #268](https://github.com/Starktastic-Homelab/ansible/pull/268) corrected
the worker inventory keys while preserving the planned IQNs. Its ordinary merge
deployment passed. The explicit enrollment
[run 35643849523](https://github.com/Starktastic-Homelab/ansible/actions/runs/35643849523)
installed open-iscsi, configured the IQNs, loaded/persisted iscsi_tcp, started and
enabled iscsid, and verified both storage routes.

That run then failed: iscsi_initiator_review_root was a role default unavailable
in the later worker play. Its failed status remains part of the evidence. The
fix moves the shared path to worker group variables. A real two-play Ansible
regression reproduced the undefined variable before the fix and passed afterward.

Under the failed run's original owner and nonce, manual reconciliation compared
the canonical reviews, rechecked current VM/Node identities, installed packages,
module/service state, idle sessions, routes and disk space, and completed only
the missing generation observations and prerequisite labels. Each Node patch
tested its UID and SMBIOS UUID. Final reads confirmed Ready state, unchanged
boot IDs and exact labels before recording completion and releasing the lock.
The subsequent corrected opt-in workflow has not yet been run on main.

| Worker | VMID | Storage address | Initiator |
| --- | ---: | --- | --- |
| kube-worker-01 | 201 | 10.9.8.51 | iqn.2026-09.net.starktastic:k3s-worker-01 |
| kube-worker-02 | 202 | 10.9.8.52 | iqn.2026-09.net.starktastic:k3s-worker-02 |

Both use open-iscsi 2.1.11-1+deb13u2, with no sessions or stored targets.
Both exceed the 25 GiB free-root-space gate. These records and labels certify
prerequisites only; neither authorizes an application writer or placement move.

## Restricted account and actual-token tests

The separately owned account operation applied the reviewed Ansible role.
iscsi-fence@pve!retained is privilege-separated. Effective **user and token**
permissions contain only VM.Audit and VM.PowerMgmt on /vms/201 and /vms/202,
with propagation disabled. Actual token reads verified both workers. Read-only
negative tests returned HTTP 403 for VMs 100, 200 and 300. No production power
test was attempted. VM.PowerMgmt also allows start/reboot through the raw API;
the reviewed fencing client exposes stop, inspection and reconciliation.

The first TLS read failed because the Proxmox-generated CA lacks keyUsage and
modern Python enables strict X.509 checks. The client now allows the legacy
certificate encoding by clearing VERIFY_X509_STRICT. Chain and expiry validation
remain required, and the independently obtained exact leaf fingerprint is checked
before authentication. This restores the broader legacy-X.509 workaround set;
it is not a certificate-validation bypass or an automatic trust retry.
[Python documents this compatibility setting](https://docs.python.org/3/library/ssl.html#ssl.create_default_context).
Real TLS fixtures verify successful pinned access and that a wrong pin, untrusted
CA or expired certificate receives no credentials.

The owned qualification VM990 had one CPU, 128 MiB, no disks, no network and
onboot disabled. VM900, the Packer template, was untouched. A pre-start check
initially held VM990 because Proxmox returns memory as the string "128". The
qualification guard now accepts only integer 128 or that exact string; different
sizes and composite settings are refused. Its regression failed before the fix
and passed afterward. Creation was not repeated: execution resumed against the
original intent and matching UUID while the VM remained stopped.

Actual-token qualification then passed:

- Wrong SMBIOS UUID refused, no stop request and no receipt.
- One normal stop, same-token task polling, exact stopped identity and durable receipt.
- A separate test-only restart followed by deliberate suppression of a successful
  stop reply. No receipt was issued until read-only reconciliation confirmed the
  exact stopped VM. No second stop request was sent for that case.
- Temporary user/token grants removed, owned stopped VM deleted, and cluster
  inventory, ACLs and both effective permission sets reread to confirm cleanup.

The suppressed-response case is controlled client-side fault injection against
an actual stop, not an induced network partition or production worker fence.
Only the scoped token and its trusted CA/pin remain privately on VM300. The
operation's root-password copy was removed. The original and qualified client
hashes and each operation intent/receipt are retained outside Kubernetes.

## Final checks and remaining scope

All five production/runner VMs remain running. The three K3s Node identities and
boot IDs are unchanged. TrueNAS iSCSI remains stopped/disabled with zero targets
and extents; apps/pv remains STANDARD. No filesystem initialization, retained PV,
Jellyfin outage or application migration occurred. Packer images were not built.

Local verification: 81 tests, one existing opt-in live-hardware test skipped;
relevant pre-commit and offline Ansible lint passed. Independent review covered
the scope fix/reconciliation and TLS/test-VM procedure. Live qualification used
the recorded corrected client hash, not the earlier incompatible client.

The companion fix must land before future opt-in enrollment or workflow-based
fencing uses these corrections. Production fencing still requires a current
reviewed generation record, exact maintenance ownership and explicit operator
approval. The qualified token is not authorization for unattended failover.
Next stages remain held-platform deployment, explicit target onboarding, then
the separately scheduled cold Jellyfin migration and application acceptance.

[Sanitized execution evidence](evidence/iscsi-prerequisites-2026-09-21.json) records
actual identities, permission checks, tests and final state. Detailed private
evidence accompanies the existing encrypted independent backup; no token or
maintenance nonce is included in the repository evidence.

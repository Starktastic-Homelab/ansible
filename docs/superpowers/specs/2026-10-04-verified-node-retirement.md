# Verified worker Node retirement

The user selected verified retirement in Ansible on 2026-10-04 after stock CCM
assessment showed that a permission-filtered Proxmox inventory could authorize
false retirement. Keep stock CSI, the existing Terraform drain handoff and the
control-plane replacement cohort. No additional cluster controller or backup gate.

Before worker installation, compare the existing Kubernetes Node system UUID
with the SSH-connected guest's SMBIOS UUID. No Node or the same generation needs
no deletion. A different generation requires current, complete Proxmox VM
inventory, matching replacement VMID/name/UUID and absence of the old UUID from
all VM configs, including stopped VMs and templates. Unknown or malformed data,
missing privileges, API failures, a surviving old generation or identity changes
must stop deployment. Do not stop/delete VMs, force storage detach or erase Secrets.

Use native Ansible URI reads and Kubernetes deletion with the observed Node UID
as an API precondition. Wait for that UID to disappear; K3s handles its own password
Secret cleanup. Recheck maintenance ownership immediately before deletion and
retain it on failure. This assumes no concurrent manual Proxmox/ACL mutation,
as required by the existing maintenance policy; independent APIs cannot provide
an atomic cross-system transaction.

Inventory completeness requires propagated VM.Audit at /vms, propagated Pool.Audit
at /pool and Sys.Audit at /access. Read the full ACL list and effective permissions;
check every relevant explicit VM/pool override as well. A filtered permissions
map can omit NoAccess paths, so the permissions map alone is insufficient. Repeat
permissions/ACL and VM resource reads after fetching configs; refuse drift. All
Proxmox operations are reads using the deployment's environment token. Never
broaden credentials automatically. Credentials/results carrying headers are no_log;
TLS verification defaults on with an optional supplied CA path.

Stage behind k3s_node_retirement_enabled=false until disposable-lab qualification.
Retirement affects worker Node identity only. Preserve existing drain recovery:
pre-existing cordons on surviving Node objects remain; recreated objects use
normal scheduling defaults. Control-plane replacement still replaces all workers.
No production activation, application migration or manual source-of-truth inventory.

# Proxmox CSI storage

> Moved from the README on 2026-11-16. The text is unchanged.

## Shared Proxmox CSI storage setup (manual, inactive)

`proxmox-csi-storage.yml` prepares shared infrastructure once, using native PVE
commands over the existing Proxmox SSH connection. It is **not** imported by
`k3s.yml` or dispatched automatically. NAS export allocation, controller activation
and application migration remain separate reviewed operations. Apps remains the
only per-volume inventory; this playbook allocates no service images.

The setup registers a dedicated NFS4.2 export with `images` content, creates a
persistent resource pool and a separated `kubernetes-csi@pve!retained` API token.
The role has only the stock non-replication privileges, granted to that pool,
that storage and the external image-owner ID. It has no VM power-management or
VM-allocation rights. The pool must contain only k3s QEMU VMs, never storage or
unrelated guests. Existing conflicting definitions are refused, not overwritten.
This setup requests full PVE user information
so its group-membership refusal can detect inherited privileges.

Proxmox removes VM-specific grants when a VM is deleted. The companion Terraform
`k3s_resource_pool` input gives replacement VMs native pool membership while the
pool and its grants stay outside disposable Terraform state. Leave that input
`null` until this setup and its access scope have been qualified. Do not destroy
the pool or reuse the image-owner ID as a VM/container. The reservation is an
operator convention recorded in the pool comment, not a Proxmox ID-allocation
lock; setup verifies the ID is unused, but cannot constrain later GUI actions.

Before execution, review a vars file containing these non-secret settings:

```yaml
proxmox_csi_storage_id: k3s-block
proxmox_csi_storage_nfs_server: 10.9.9.30
proxmox_csi_storage_nfs_export: /mnt/apps/k3s-block
proxmox_csi_storage_nodes: [pve]
proxmox_csi_storage_pool: k3s-csi
proxmox_csi_storage_owner_id: 9999
proxmox_csi_storage_api_url: https://10.9.9.20:8006/api2/json
proxmox_csi_storage_secret_destination: /maintenance/private/proxmox-csi.json
```

These are proposed names, **not allocated resources or approved live settings**.
The NAS export must already exist with the reviewed source restriction, quota,
permissions and durability settings. This first profile supports one Proxmox host.
Review its unused owner ID, existing role/user/ACLs and the exact storage identity.
Use the external VM300 runner, the maintenance helper from ansible `main` already used by
`deploy.yml`, and its existing ownership protocol. `/maintenance/private` must
already be a real owner-only directory with mode `0700` or `2700` (setgid), writable by the runner service user.
After acquiring and exporting the operation ownership, the source entry point is:

```sh
ansible-playbook -i inventory/hosts.yml --vault-password-file .vault_pass \
  --private-key private_key.pem -e @/maintenance/private/csi-setup-vars.yml \
  proxmox-csi-storage.yml
```

Run this only within the separately approved setup scope; never target this
workstation. The playbook verifies the external operation before inspection and
again before writes. It neither acquires nor releases ownership: the operation
owner reviews results and handles release. A failure leaves partial resources and
the existing operation owned; inspect them before retrying.

The token value goes directly into the protected external JSON file (`0600`),
with secret tasks hidden from logs. No token is committed or published to
Kubernetes by this playbook. Losing the response or failing between token creation
and persistence creates a mismatch that the next run refuses; it never silently
rotates or overwrites a secret. Token rotation/recovery requires explicit operator
reconciliation. Preserve an external recoverable copy and qualify authentication,
access denials, rebuilds and pool enrollment before controller activation. Stored
identity checks do not prove that a token's secret is valid after external rotation.

Offline checks: `python3 scripts/tests/test_proxmox_csi_storage.py`. These render
native Ansible conditions and verify refusal/opt-in wiring; they do not execute
PVE commands or qualify permissions and persistence on the live runner.

The read-only CSI activation preflight also accepts an optional `sealed_config_b64`
(the base64-encoded **encrypted SealedSecret**, never a plaintext Secret) and
`config_sha256` (SHA256 of the original `config.yaml` bytes). Supply both to prove
that the actual `csi-proxmox/proxmox-csi-config` payload can be recovered with the
external Vault key. The decrypted data stays inside the temporary runner process;
only success or a generic failure is logged. Omitting both retains the synthetic
probe and API visibility checks. This workflow does not activate CSI.

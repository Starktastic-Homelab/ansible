# Repository Guidelines

## Purpose and layout

Ansible configures Proxmox services and installs K3s, then bootstraps ArgoCD for
the sibling Apps repository. Read `README.md` and the relevant workflows before
changing cluster or host lifecycle behavior.

- `k3s.yml`: cluster installation and bootstrap orchestration.
- `i915-sriov.yml`, `i915-acceptance.yml`: host GPU lifecycle and runtime acceptance.
- `ser2net.yml`: serial-to-network service configuration.
- `roles/`: tasks, handlers, defaults, and templates grouped by responsibility.
- `inventory/`: static hosts and Proxmox dynamic inventory.
- `group_vars/`: shared/host configuration and Vault-encrypted secrets.
- `scripts/`: compatibility checks, GPU probes, and backup/restore utilities.

## Editing conventions

Follow existing role structure, use fully qualified module names, and keep tasks
idempotent. Use handlers for service restarts where appropriate. Preserve inventory
tags and the Terraform-to-Ansible handoff. Bootstrap ordering matters: pre-seed the
Sealed Secrets keypair and restore certificates as designed before application rollout.

Keep `group_vars/all/secrets.yml` encrypted. Never commit Vault passwords, private
SSH keys, kubeconfigs, or decrypted secrets. Do not overwrite an existing
`.vault_pass` with the dummy value used in isolated CI lint jobs.

GPU changes span the Proxmox host and Packer-built guests. Read the README's
compatibility and runtime acceptance procedures. Successful installation, healthy
nodes, or advertised GPU resources do not prove working hardware encoding.
Preserve baseline/boot-identity checks and the distinctions between `capture`,
`current-stack`, and `post-reboot` acceptance modes.

## Validation

Install Python requirements in a virtual environment and collections from
`requirements.yml`. Run relevant checks from the repository root:

```sh
ansible-galaxy collection install -r requirements.yml
ansible-lint --offline
python3 scripts/tests/test_i915_compat.py
python3 scripts/tests/test_i915_acceptance.py
bash scripts/tests/test-secrets-backup.sh
```

Run `ansible-lint` without a `.` path argument, as required by the workflow's
discovery behavior. Its syntax checks need collections installed and a readable
Vault password source because `ansible.cfg` names `.vault_pass`. Use
`pre-commit run --files <changed-files>` for configured formatting/lint hooks.
Full syntax checking follows `.github/workflows/validate.yml` and requires inventory,
Vault, and SSH configuration; report unavailable prerequisites instead of bypassing checks.

## Delivery

Playbooks and workflow dispatches may modify hosts, schedule reboots, deploy the
cluster, or update organization secrets. Keep live execution within the requested
operational scope. Report offline checks separately from inventory-backed syntax
checks and runtime acceptance, with affected roles and cross-repository dependencies.

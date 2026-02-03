# Homelab Ansible

Ansible playbooks for provisioning and managing a K3s Kubernetes cluster on Proxmox VMs.

## Prerequisites

- Python 3.10+
- Ansible 13.x (`pip install -r requirements.txt`)
- Proxmox VE with VMs tagged appropriately (`k3s`, `master`, `worker`)
- Vault password file at `.vault_password` (not committed to git)

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run the full playbook
ansible-playbook k3s.yml

# Run with specific tags
ansible-playbook k3s.yml --tags k3s           # K3s installation only
ansible-playbook k3s.yml --tags bootstrap     # Platform bootstrap only
ansible-playbook k3s.yml --tags workers       # Worker nodes only
```

## Playbook Structure

```
k3s.yml                     # Main playbook
├── Wait for SSH            # Ensures all nodes are reachable
├── Initialize cluster      # First master node (k3s_init role)
├── Harvest token           # Distributes join token to all nodes
├── Join masters            # Additional control plane nodes (k3s_masters role)
├── Join workers            # Worker nodes (k3s_workers role)
└── Bootstrap platform      # ArgoCD + sealed-secrets (bootstrap_platform role)
```

## Roles

| Role | Description |
|------|-------------|
| `k3s_common` | Shared tasks: checks K3s installation, downloads install script |
| `k3s_init` | Initializes first control plane node, deploys Kube-VIP |
| `k3s_masters` | Joins additional control plane nodes |
| `k3s_workers` | Joins worker nodes, applies node labels |
| `bootstrap_platform` | Deploys ArgoCD, sealed-secrets, configures OIDC |

## Tags

| Tag | Scope |
|-----|-------|
| `k3s` | All K3s-related tasks |
| `init` | First master initialization |
| `masters` | Additional master nodes |
| `workers` | Worker nodes |
| `kube-vip` | Kube-VIP deployment |
| `kubeconfig` | Kubeconfig setup |
| `bootstrap` | Platform bootstrapping |
| `argocd` | ArgoCD installation |
| `sealed-secrets` | Sealed-secrets key seeding |

## Configuration

### Variables (`group_vars/all/vars.yml`)

| Variable | Description |
|----------|-------------|
| `vip_address` | Virtual IP for K3s API server |
| `flannel_iface` | Network interface for Flannel CNI |
| `base_domain` | Base domain for services |
| `argocd_repo_url` | GitOps repository URL |

### Secrets (`group_vars/all/secrets.yml`)

Encrypted with ansible-vault. Contains:
- Sealed-secrets TLS key/cert
- ArgoCD admin password hash
- ArgoCD OIDC credentials

### Version Management

K3s and Kube-VIP versions are defined in `roles/k3s_init/defaults/main.yml` with Renovate comments for automated updates.

## Inventory

Uses dynamic inventory via `community.proxmox.proxmox` plugin. VMs are grouped by Proxmox tags:
- `all_k3s`: All K3s nodes (tag: `k3s`)
- `masters`: Control plane nodes (tags: `k3s` + `master`)
- `workers`: Worker nodes (tags: `k3s` + `worker`)

## Development

```bash
# Syntax check
ansible-playbook --syntax-check k3s.yml

# Dry run
ansible-playbook k3s.yml --check

# Limit to specific hosts
ansible-playbook k3s.yml --limit masters
```

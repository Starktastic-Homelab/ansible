<div align="center">

# ⚙️ Ansible — Cluster Configuration

**Zero-touch K3s provisioning with HA, GPU virtualization, and GitOps bootstrap**

[![Ansible](https://img.shields.io/badge/Ansible-EE0000?style=for-the-badge&logo=ansible&logoColor=white)](https://www.ansible.com/)
[![K3s](https://img.shields.io/badge/K3s-FFC61C?style=for-the-badge&logo=k3s&logoColor=black)](https://k3s.io/)
[![Kubernetes](https://img.shields.io/badge/Kubernetes-326CE5?style=for-the-badge&logo=kubernetes&logoColor=white)](https://kubernetes.io/)
[![Python](https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)

*Transforms bare Debian VMs into a production-grade Kubernetes cluster with a single playbook run*

</div>

---

## Table of Contents

- [Overview](#overview)
- [Deployment Pipeline](#deployment-pipeline)
- [Roles](#roles)
- [Inventory \& Discovery](#inventory--discovery)
- [Key Features](#key-features)
- [Additional Playbooks](#additional-playbooks)
- [CI/CD Automation](#cicd-automation)
- [Cross-Repo Integration](#cross-repo-integration)
- [Prerequisites](#prerequisites)
- [Usage](#usage)
- [License \& Contributing](#license--contributing)

---

## Overview

This repository takes freshly provisioned Debian VMs (from Terraform) and transforms them into a fully operational K3s Kubernetes cluster. The main `k3s.yml` playbook executes a 6-play pipeline that:

1. Waits for VMs to become reachable via SSH
2. Initializes the first control plane node with embedded etcd
3. Deploys **Kube-VIP** for a highly available virtual IP
4. Joins additional masters and workers to the cluster
5. Labels worker nodes with GPU capabilities
6. Bootstraps the cluster with **Sealed Secrets**, **TLS certificate recovery**, and **ArgoCD** with SSO

Beyond K3s, dedicated playbooks manage **Intel i915 SR-IOV GPU drivers** on the Proxmox host and a **ser2net bridge** for Zigbee home automation hardware.

---

## Deployment Pipeline

The main `k3s.yml` playbook orchestrates cluster creation through six sequential plays:

```mermaid
sequenceDiagram
    autonumber
    participant Runner as CI Runner
    participant M0 as Master-01
    participant M1 as Master-N
    participant W as Workers
    participant Cluster as K3s Cluster

    rect rgb(60, 60, 60)
    Note over Runner, W: Play 1 — Wait for SSH
    Runner->>M0: SSH probe (10s delay, 300s timeout)
    Runner->>M1: SSH probe
    Runner->>W: SSH probe
    end

    rect rgb(123, 66, 188)
    Note over M0: Play 2 — Initialize First Master
    Runner->>M0: Role: k3s_init
    M0->>M0: Install K3s (--cluster-init)
    M0->>M0: Deploy Kube-VIP DaemonSet
    M0->>M0: Advertise VIP (10.9.9.99)
    end

    rect rgb(50, 108, 229)
    Note over M0: Play 3 — Harvest Token
    M0-->>Runner: node-token
    Runner->>Runner: Set global fact
    Runner->>Cluster: Verify VIP responds (6443)
    end

    rect rgb(123, 66, 188)
    Note over M1: Play 4 — Join Additional Masters
    Runner->>M1: Role: k3s_masters
    M1->>Cluster: Join via VIP
    end

    rect rgb(50, 108, 229)
    Note over W: Play 5 — Join Workers
    Runner->>W: Role: k3s_workers
    W->>Cluster: Join as agents
    Runner->>Cluster: Label nodes (worker + GPU)
    end

    rect rgb(238, 0, 0)
    Note over Runner: Play 6 — Bootstrap
    Runner->>Cluster: Pre-seed Sealed Secrets key
    Runner->>Cluster: Mount NFS → Restore TLS certs
    Runner->>Cluster: Install ArgoCD (Helm + OIDC)
    Runner->>Cluster: Apply App-of-Apps bootstrap
    end
```

---

## Roles

Seven roles cover the full lifecycle from bare VM to GitOps-ready cluster:

| Role | Target | Purpose |
|------|--------|---------|
| **k3s_common** | All K3s nodes | Kernel tuning (inotify), Docker Hub auth, K3s binary download |
| **k3s_init** | First master | K3s `--cluster-init`, Kube-VIP DaemonSet, kubeconfig extraction |
| **k3s_masters** | Additional masters | Join control plane via VIP, wait for API readiness |
| **k3s_workers** | Worker nodes | Join as agents, apply `worker` + `gpu` node labels |
| **bootstrap_cluster** | CI runner (localhost) | Sealed Secrets key, TLS cert restore from NFS, ArgoCD + OIDC setup, App-of-Apps |
| **i915_sriov** | Proxmox host | Intel GPU SR-IOV driver lifecycle (install, upgrade, GRUB, sysfs VF config) |
| **ser2net** | Proxmox host | Expose USB Zigbee dongle as TCP socket for cluster consumption |

### Role Execution Order

The diagram below shows the order in which roles run across the `k3s.yml` plays. This sequence is enforced by the playbook's play order — only `k3s_common` is declared as an actual role `meta` dependency; the rest run in sequence because each play targets the next set of hosts.

```mermaid
flowchart LR
    KC([k3s_common]) ==> KI[[k3s_init]]
    KC ==> KM[[k3s_masters]]
    KC ==> KW[[k3s_workers]]
    KI ==> KM
    KM ==> KW
    KW ==> BC{{bootstrap_cluster}}

    I915["i915_sriov"] ~~~ SER["ser2net"]

    classDef common fill:#3C3C3C,stroke:#2D2D2D,color:#fff
    classDef init fill:#7B42BC,stroke:#6A35A3,color:#fff
    classDef bootstrap fill:#EE0000,stroke:#CC0000,color:#fff
    classDef sriov fill:#E57000,stroke:#CC6300,color:#fff
    classDef ser fill:#326CE5,stroke:#2B5FC2,color:#fff
    class KC common
    class KI init
    class BC bootstrap
    class I915 sriov
    class SER ser
```

---

## Inventory & Discovery

### Dynamic Inventory (Proxmox API)

VMs are discovered automatically via the `community.proxmox.proxmox` inventory plugin, using **Proxmox tags** set by Terraform:

| Ansible Group | Tag Filter | Description |
|--------------|------------|-------------|
| `all_k3s` | `k3s` | All Kubernetes nodes |
| `masters` | `k3s` + `master` | Control plane nodes |
| `workers` | `k3s` + `worker` | Worker nodes (GPU-labeled) |

IP addresses are extracted from cloud-init configuration (`proxmox_ipconfig0`), eliminating any hardcoded host lists.

### Static Inventory

The Proxmox hypervisor itself is in a static inventory for host-level operations (GPU drivers, ser2net):

```yaml
proxmox_hosts:
  hosts:
    pve:
      ansible_host: 10.9.9.20
      ansible_user: root
```

---

## Key Features

### High Availability — Kube-VIP

The cluster API is fronted by a **virtual IP (10.9.9.99)** managed by Kube-VIP running as a DaemonSet on control plane nodes. ARP-based leader election ensures seamless failover — all clients and workers connect through the VIP, never directly to a master node.

### Intel i915 SR-IOV — GPU Virtualization

The `i915_sriov` role manages the full GPU driver lifecycle on the Proxmox host:

- **Upstream compatibility validation** — before anything destructive, `scripts/i915_compat.py` checks the *exact* driver release against the target kernel using upstream release metadata; unknown or unsupported combinations abort the play with the host untouched
- **DKMS driver** install/upgrade from GitHub releases
- **Kernel pinning** — `i915_sriov_pinned_kernel` (e.g. `6.17.13-13-pve`) is the authoritative desired host kernel; the role builds and verifies the DKMS module for it *before* pinning it via `proxmox-boot-tool` and rebooting into it
- **GRUB parameters**: `intel_iommu=on i915.enable_guc=3 i915.max_vfs=7 module_blacklist=xe`
- **sysfs configuration** to create 7 virtual functions on boot

#### Host and guest driver versions differ on purpose

The Proxmox host (PF) and the Packer VM images (VF) run **different**
`i915-sriov-dkms` releases, because upstream maintains parallel lines and
states the two sides need not match:

| Side | Kernel | Driver | Upstream range |
|------|--------|--------|----------------|
| **Host** (this repo) | 6.17.13-13-pve | `2026.08.12.1` | 6.17 – 7.1 |
| **Guest** (packer repo) | 6.12 (Debian 13) | `2026.03.05.6` | 6.12 – 6.19 |

Three axes are validated, all from upstream data for the exact tags — never
from version ordering, equality or a curated allowlist:

1. host driver ↔ host kernel (release notes of the tag)
2. guest driver ↔ guest kernel (release notes of the tag)
3. PF ↔ VF IOV ABI — `IOV_VERSION_{BASE,LATEST}_{MAJOR,MINOR}` from each tag's
   `gt/iov/abi/iov_version_abi.h`, replayed through the upstream handshake

`scripts/i915_compat.py` (stdlib only, shared verbatim with the packer repo)
implements this and **fails closed**: a missing tag, unparsable release notes,
a missing ABI header or a network failure all exit non-zero instead of
approving an unverified combination.

```bash
python3 scripts/i915_compat.py \
  --host-version 2026.08.12.1 --host-kernel 6.17.13-13-pve \
  --guest-version 2026.03.05.6 --guest-kernel 6.12
# exit 0 = compatible · 1 = incompatible · 2 = cannot be established
```

Renovate is free to propose newer host releases, but the `i915-compat`
workflow re-runs the same check against the pinned kernel (and the guest state
on the packer repo's `main`) and blocks the PR with a sticky report if the
combination is not proven safe. Moving the guest to a newer release line is a
packer-repo change; the host only needs a driver whose range covers
`i915_sriov_pinned_kernel`.

### Sealed Secrets Bootstrap

The bootstrap role **pre-seeds the Sealed Secrets TLS keypair** before any workloads deploy. This enables a "secrets-in-git" workflow — encrypted SealedSecret manifests can be committed to the Apps repo and will decrypt correctly from day zero.

### ArgoCD with SSO

ArgoCD is deployed via Helm with:

- **Authentik OIDC integration** for single sign-on
- **Progressive syncs** enabled for phased rollout support
- **RBAC** with Authentik group mapping (Admins → admin role)
- **App-of-Apps bootstrap** pointing to the Apps repo, triggering full cluster reconciliation

### TLS Certificate Recovery

On cluster rebuild, TLS certificates are **recovered from NFS backup** rather than re-issued. This prevents Let's Encrypt rate limiting and ensures services come back online with valid certificates immediately. The restore process orchestrates data across three systems:

```mermaid
flowchart LR
    subgraph nfs["TrueNAS NFS Server"]
        BACKUP[(cert-backup/\n*.yaml)]
    end

    subgraph master["K3s Master Node"]
        MNT["/tmp/cert-backup\n(read-only mount)"]
    end

    subgraph runner["Ansible Runner"]
        FIND["Find *.yaml files"]
        FETCH["Fetch to localhost"]
        PARSE{{"Extract namespace\nfrom filename"}}
    end

    subgraph k8s["Kubernetes API"]
        NS["Ensure target\nnamespaces exist"]
        APPLY(["kubectl apply\neach TLS secret"])
    end

    BACKUP -.->|"NFS mount"| MNT
    MNT ==> FIND ==> FETCH
    FETCH ==> PARSE
    PARSE ==> NS ==> APPLY

    classDef storage fill:#326CE5,stroke:#2B5FC2,color:#fff
    classDef gate fill:#E57000,stroke:#CC6300,color:#fff
    class BACKUP,MNT storage
    class PARSE gate
```

On a fresh cluster where no NFS backup exists yet, the restore step **gracefully skips** without failing — certificates will be issued fresh by cert-manager and backed up on the next CronJob run.

---

## Additional Playbooks

| Playbook | Target | Purpose |
|----------|--------|---------|
| `i915-sriov.yml` | Proxmox host | Install/upgrade Intel SR-IOV GPU driver |
| `ser2net.yml` | Proxmox host | Configure TCP bridge for USB Zigbee dongle (port 3333) |

---

## CI/CD Automation

Six workflows cover deployment, validation, and specialized hardware management:

```mermaid
flowchart TD
    subgraph triggered["Triggered by Terraform"]
        DISPATCH{{repository_dispatch\ninfrastructure-changed}} ==> DEPLOY[deploy.yml\nRun k3s.yml playbook]
        DEPLOY ==> KUBECONFIG>Upload kubeconfig\nto org-wide secret]
    end

    subgraph pr["PR Phase"]
        PR([Pull Request]) --> LINT[validate.yml\nansible-lint + syntax-check]
        PR --> FMT[format.yml\nPrettier formatting]
        PR --> CMP{{i915-compat.yml\nHost ↔ guest driver compatibility}}
    end

    subgraph special["Specialized"]
        DRV_CHANGE["i915_sriov.yml change"] --> I915[i915-sriov-upgrade.yml\nGPU driver upgrade + reboot]
        SER_CHANGE["ser2net role change"] --> SER[ser2net.yml\nZigbee gateway deploy]
    end

    classDef deploy fill:#EE0000,stroke:#CC0000,color:#fff
    classDef dispatch fill:#7B42BC,stroke:#6A35A3,color:#fff
    classDef gatecheck fill:#E57000,stroke:#CC6300,color:#fff
    class DEPLOY deploy
    class DISPATCH dispatch
    class CMP gatecheck
```

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| **deploy** | Terraform dispatch / push to main | Full K3s deployment + kubeconfig upload |
| **validate** | PR | `ansible-lint` (production profile) + syntax check |
| **format** | PR | Prettier YAML/JSON formatting |
| **i915-sriov-upgrade** | i915 config change / manual | GPU driver lifecycle on Proxmox host |
| **i915-compat** | PR (i915 config/role changes) | Blocks merge unless upstream data proves the host ↔ guest driver combination works |
| **ser2net** | ser2net role change / manual | Zigbee serial bridge configuration |

---

## Cross-Repo Integration

Ansible sits in the middle of the infrastructure pipeline, receiving triggers from Terraform and bootstrapping the Apps deployment:

```mermaid
flowchart LR
    TF(["🏗️ Terraform\nVMs provisioned"]) ==>|"repository dispatch"| AN(["⚙️ Ansible\nK3s + Bootstrap"])
    AN ==>|"App-of-Apps"| APPS(["☸️ Apps\n60+ services deployed"])
    AN -.->|"Kubeconfig uploaded\nto org secret"| GH[(🔐 GitHub Org\nSecrets)]

    classDef terraform fill:#7B42BC,stroke:#6A35A3,color:#fff
    classDef ansible fill:#EE0000,stroke:#CC0000,color:#fff
    classDef apps fill:#326CE5,stroke:#2B5FC2,color:#fff
    class TF terraform
    class AN ansible
    class APPS apps
```

The handoff chain:
1. **Terraform apply** completes → sends `infrastructure-changed` dispatch event
2. **Ansible deploy** workflow runs the `k3s.yml` playbook
3. **ArgoCD** is installed and pointed at the Apps repo via App-of-Apps
4. **Kubeconfig** is uploaded to the GitHub org secrets for use by the Apps CI workflows

---

## Prerequisites

- **Python** ≥ 3.11 with `ansible`, `proxmoxer`, `kubernetes` packages
- **Helm** (for ArgoCD deployment)
- **Network access** to Proxmox API and K3s node SSH
- **Ansible Vault** password file (`.vault_pass`) for encrypted secrets
- **Proxmox API token** for dynamic inventory

---

## Usage

```bash
# Install Python dependencies
pip install -r requirements.txt

# Deploy K3s cluster
ansible-playbook -i inventory/ k3s.yml

# Upgrade GPU driver on Proxmox host
ansible-playbook -i inventory/ i915-sriov.yml

# Configure Zigbee serial bridge
ansible-playbook -i inventory/ ser2net.yml
```

> In practice, the `k3s.yml` playbook is triggered automatically by the Terraform pipeline via GitHub Actions `repository_dispatch`.

---

## License & Contributing

This is a personal homelab project. Feel free to use it as inspiration for your own infrastructure. If you spot an issue or have a suggestion, [open an issue](../../issues) — contributions and feedback are welcome.

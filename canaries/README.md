# Isolated iSCSI rebuild canary

`iscsi-rebuild.yml` prepares one **disposable Debian/systemd VM**, not a
production cluster. It installs `open-iscsi`, sets the caller's stable initiator
IQN, enables `iscsid`, and reuses `k3s_init` **only through `tasks_from: install`**.
That entry retains its `k3s_common` dependency. Production `k3s.yml`, Kube-VIP,
ArgoCD/bootstrap, production secrets and `$HOME/.kube/config` are never needed.
Do not use a production VM, inventory, image containing credentials, or vault.

## Authoring-only merge safety

The existing `deploy.yml` runs the **production** K3s playbook on qualifying
pushes to `main`. An authoring-only merge must retain `[skip ci]` in the
**resulting merge commit message**. Keep the marker in the PR title and check
the merge message; a marker only in the PR body does not skip this push
trigger. Run the PR checks before merging, without adding skip markers to the
implementation commits.

Do not dispatch `Deploy K3s Cluster` or `infrastructure-changed` to run this
canary. Those are production entry points, not the isolated invocation below.

## Disposable node requirements

The VM must have its final hostname, the storage interface, a cloud-init
resolver at `/run/systemd/resolve/resolv.conf`, and working SSH/sudo. Use a clean
VM; do not repurpose an existing cluster. A changed IQN is refused while iSCSI
sessions exist. An unchanged IQN does not restart `iscsid` or log sessions out.
Choose a unique IQN **once per logical node**, persist it in the fixture, and
reuse it when recreating that node; never bake it into a shared image.

## Explicit caller contract

Create a canonical absolute state directory, owned by the invoking user with
mode `0700`. No symlink components, `..`, `.kube` directories, home-directory
roots, linked output files, or destinations selected by the current
`KUBECONFIG` are accepted. Keep all state out of Git.

Supply a **single synthetic YAML/JSON inventory** outside the production
inventory. Its only VM must belong to both `iscsi_canary` and `masters`:
`k3s_common` uses `masters` to configure/restart the server rather than an agent.
For example (documentation addresses and paths; replace all connection inputs):

```yaml
all:
  children:
    iscsi_canary:
      hosts:
        iscsi-rebuild-canary-example:
          ansible_host: 192.0.2.10
          ansible_user: debian
          ansible_private_key_file: /absolute/canary-state/id_ed25519
          ansible_ssh_common_args: >-
            -o UserKnownHostsFile=/absolute/canary-state/known_hosts
            -o GlobalKnownHostsFile=/dev/null
            -o StrictHostKeyChecking=yes
    masters:
      hosts:
        iscsi-rebuild-canary-example: {}
```

Use a dedicated, verified canary known-hosts file. Supply these extra variables,
for example in the state's `ansible-vars.json`:

| Input | Required value |
| --- | --- |
| `canary_fixture_id` | 1–42 lowercase alphanumeric/hyphen characters, starting/ending alphanumeric |
| `canary_node_name` | `iscsi-rebuild-canary-` followed by the fixture ID; must equal the VM's actual hostname |
| `canary_iscsi_iqn` | A valid lowercase `iqn.YYYY-MM.reversed-domain:unique-logical-node` (at most 223 characters) |
| `canary_state_dir` | The reserved absolute private state directory |
| `k3s_version` | Exactly `v1.36.4+k3s1` |
| `flannel_iface` | The VM's storage interface, normally `eth1` |

`ansible_host` must be the canary's IPv4 address; it is also the TLS SAN, **not a
VIP**. A K3s config drop-in sets pod CIDR `10.242.0.0/16`, service CIDR
`10.243.0.0/16` and cluster DNS `10.243.0.10`. Public registry pulls are anonymous;
no Docker Hub credentials are requested. Production registry auth remains
enabled by default outside this entry.

## Invocation and outputs

From the Ansible repository root, after separately approving the disposable
fixture's provisioning:

```bash
state=/absolute/canary-state
env -u ANSIBLE_VAULT_PASSWORD_FILE -u ANSIBLE_VAULT_IDENTITY_LIST \
    -u ANSIBLE_VARS_ENABLED -u ANSIBLE_INVENTORY -u ANSIBLE_ROLES_PATH \
    -u KUBECONFIG \
    ANSIBLE_CONFIG="$PWD/canaries/ansible.cfg" \
    ansible-playbook -i "$state/inventory.yml" canaries/iscsi-rebuild.yml \
      --extra-vars "@$state/ansible-vars.json"
```

**Always select `canaries/ansible.cfg` before starting Ansible.** The repository's
normal config selects a production vault password file at process startup.
The canary config disables implicit inventory and `group_vars`/`host_vars`
loading and permits only static inventory plugins. Do not add vault arguments,
production variables, tag selection, or `--skip-tags`. A controller-only
assertion rejects even an empty inventory; all remote work targets only
`iscsi_canary`, and hostname verification precedes privilege escalation.

Outputs are replaced on each successful run, both with mode `0600`:

- `state/kubeconfig`: fetched from this VM's new K3s cluster, with its server
  rewritten to `https://<ansible_host>:6443`. Its contents are not logged.
- `state/node-identity.json`: `fixture_id`, `node_name`, `machine_id` and
  `boot_id`. The IDs are public identifiers of this disposable VM.

The rebuild runner must preserve the first generation's identities before
recreating the VM and compare them with the second generation. Always pass
`--kubeconfig "$state/kubeconfig"` explicitly to canary Kubernetes commands;
never bootstrap production applications or merge this file into a real config.

## Non-live regression checks

```bash
python3 scripts/tests/test_iscsi_canary.py
```

This uses existing Ansible/PyYAML plus Python's standard library. It runs only
controller assertions and synthetic artifact copies, never SSH, package or
service changes, the installer, or a real kubeconfig read. For syntax checking,
use the same isolated config and synthetic inventory with `--syntax-check`;
do not use the production validation inventory or vault credentials.

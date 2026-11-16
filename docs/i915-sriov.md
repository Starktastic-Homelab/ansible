# Intel i915 SR-IOV: driver versions, recovery and acceptance

> Moved from the README on 2026-11-16. The text is unchanged.

## Host and guest driver versions differ on purpose

The Proxmox host (PF) and the Packer VM images (VF) run **different**
`i915-sriov-dkms` releases, because upstream maintains parallel lines and
states the two sides need not match:

| Side | Kernel | Driver | Upstream range |
|------|--------|--------|----------------|
| **Host** (this repo) | 7.0.14-29-pve | `2026.11.05` | 6.18 – 7.3 |
| **Guest** (packer repo) | 6.12 (Debian 13) | `2025.12.10.2` | 6.12 – 6.18 |

Three axes are validated, all from upstream data for the exact tags — never
from version ordering, equality or a curated allowlist:

1. host driver ↔ host kernel (release notes of the tag)
2. guest driver ↔ guest kernel (release notes of the tag)
3. PF ↔ VF IOV ABI — `IOV_VERSION_{BASE,LATEST}_{MAJOR,MINOR}` from each tag's
   `gt/iov/abi/iov_version_abi.h`, replayed through the upstream handshake

`scripts/i915_compat.py` (stdlib only, shared verbatim with the packer repo)
implements this and **fails closed**: a missing tag, unparsable release notes,
a missing ABI header or a network failure all exit non-zero instead of
passing an unverified metadata combination. The independent local exclusion
for host release `2026.09.16` rejects the reproduced runtime regression even
though its declared kernel/ABI metadata agrees. This is a small deny list of
observed failures, not a host/guest compatibility allowlist.

```bash
python3 scripts/i915_compat.py \
  --host-version 2026.11.05 --host-kernel 7.0.14-29-pve \
  --guest-version 2025.12.10.2 --guest-kernel 6.12
# exit 0 = metadata/policy passed, hardware NOT TESTED
# exit 1 = unsupported or blocked · 2 = metadata cannot be established
```

Renovate is free to propose newer host releases, but the `i915-compat`
workflow re-runs the same check against the pinned kernel (and the guest state
on the packer repo's `main`) and fails with a sticky report if metadata or
known-bad policy rejects it. Every report explicitly says hardware acceptance
was not tested. A green metadata check is not approval to call an upgrade
operationally verified. Moving the guest to a newer release line is a
packer-repo change; the host only needs a driver whose range covers
`i915_sriov_pinned_kernel`.

The versions may differ, but the guest image assumes the PF runs Xe_LP CCS:
the packer repo's `scripts/bootstrap.sh` boots the guests with
`i915.xelp_enable_ccs=1`, and the host must match. Without that parameter the
`2025.12.10.x` guest ran a 48-bit PPGTT with no `ccs0` and hung `rcs0` under
OpenCL and OpenVINO (`GPU HANG ecode 12:1`, 2026-11-06). Host releases from
`2026.09.16` leave CCS off unless the host cmdline adds
`i915.xelp_enable_ccs=1`, so `i915_sriov_grub_params` carries it. Dropping it
here means dropping the guest parameter in packer in the same change.
`i915_compat.py` checks neither; the acceptance probe's OpenCL arm (below)
catches the mismatch at runtime.

The required-check names are versioned in `.github/required-status-checks.json`.
After the named checks exist and pass, an authorized administrator can apply
that exact list without replacing other branch protections:

```sh
gh api --method PATCH \
  repos/Starktastic-Homelab/ansible/branches/main/protection/required_status_checks \
  --input .github/required-status-checks.json
```

## Host-driver recovery and GPU acceptance

The host pin `2026.09.14` once restored hardware encoding after the VA-API
initialization failures observed on both workers with `2026.09.16`.
Renovate excludes the bad release and disables automerge for host-driver
updates; the checker also rejects it in CI and before direct Ansible
installation. Later releases remain visible for manual review. Kernel/IOV
metadata alone does not prove usable hardware encoding.

**Merge only during an approved Proxmox maintenance window.** A push to
`main` changing `group_vars/proxmox_hosts/i915_sriov.yml` automatically runs
the upgrade workflow. The role schedules `sync && shutdown -r +1`, which
interrupts the hosted VMs and potentially the whole cluster, not just
Jellyfin. Confirm working console access and recoverable critical-workload
backups first. Do not issue a second manual reboot.

After the host returns, verify the running kernel and **loaded** module on
the Proxmox console, not just the installed package:

```sh
uname -r
cat /sys/module/i915/version
dkms status
proxmox-boot-tool kernel list
```

Expect the pinned kernel (`7.0.14-29-pve`), loaded module `2026.11.05-sriov`
and `/sys/module/i915/parameters/xelp_enable_ccs` = `1` (the guests show `Y`). Rolling back to a
6.17 kernel also means reinstalling `2026.09.14`: `2026.11.05` builds only
for 6.18 – 7.3, so the old kernel boots the stock i915 with no VFs.
Keep the guest images, GPU device plugin, and application
encoding settings unchanged. Wait for the nodes and workloads to recover,
then confirm Jellyfin and Immich run on different workers with
`kubectl -n media get pods -o wide`.

Readiness and advertised GPU resources can remain healthy while encoding
is broken. Require exit zero from actual synthetic hardware encodes:

```sh
kubectl -n media exec deployment/jellyfin -c main -- \
  s6-setuidgid abc timeout 30 /usr/lib/jellyfin-ffmpeg/ffmpeg \
  -hide_banner -loglevel error \
  -init_hw_device vaapi=va:/dev/dri/renderD128 -filter_hw_device va \
  -f lavfi -i testsrc2=size=128x128:rate=30 -t 1 \
  -vf format=nv12,hwupload -c:v h264_vaapi -low_power 1 -f null -

kubectl -n media exec deployment/immich-main -- \
  timeout 30 ffmpeg -hide_banner -loglevel error \
  -init_hw_device vaapi=va:/dev/dri/renderD128 -filter_hw_device va \
  -f lavfi -i testsrc2=size=128x128:rate=30 -t 1 \
  -vf format=nv12,hwupload -c:v h264_vaapi -f null -
```

Repeat the Jellyfin command with `-c:v hevc_vaapi` to cover its other enabled
low-power encoder. Then verify a real forced hardware transcode, HDR tone
mapping, and seeking in Jellyfin, and inspect the new worker boots for GuC
CT errors. Direct play is not proof. If the rollback does not restore
encoding, stop and investigate before another driver/kernel change or
reboot. This recovery does not address the separate SQLite contention.

## Reproducible runtime acceptance

`i915-acceptance.yml` reads the loaded kernel/module, boot ID and machine ID
over the existing infrastructure inventory. It then checks that Kubernetes
reports those same node identities and runs a short GPU Job on **every**
inventory worker. The jobs run the pinned official `jellyfin/jellyfin` image
(its jellyfin-ffmpeg and bundled Intel OpenCL runtime) as UID 1000, request
the existing `gpu.intel.com/i915` device-plugin resource, and mount no
application volumes. They do not depend on Jellyfin or Immich deployments.
Each job also joins the group that owns its worker's `/dev/dri/by-path/*-render`
nodes, read from the worker. The device plugin bind-mounts those nodes with the
worker's `render` group, Intel OpenCL opens only them, and without the group it
reports no platform (`-1001`).

Masters and workers come from the Proxmox tags (`inventory/proxmox.yml`), so
adding masters needs no change here. Every inventory machine, the Proxmox host
and masters included, must answer over SSH in every mode. In current-stack and
post-reboot every Kubernetes node must also be `Ready` before the jobs run. An
unreachable master therefore fails every mode rather than being skipped: the
snapshot must hold evidence for every tagged machine, and post-reboot also needs
a fresh boot on every machine that capture recorded. Every mode also lists the
nodes Kubernetes knows from a master and fails if they differ from the tagged
masters and workers. A guest whose Proxmox API call fails drops out of its tag
groups with only a warning, and its node would otherwise pass without a probe.
A Node left over from a VM that Terraform removed fails the same check; delete it
from the runner (`k3s kubectl delete node <name>`) before an upgrade.

Each worker must encode at least 30 frames through low-power H.264, low-power
HEVC, synthetic HEVC Main10/PQ hardware decoding plus VA-API tone mapping and
hardware encoding, and a synthetic 10-bit PQ clip tone-mapped by
`tonemap_opencl` (VA-API to OpenCL interop) and encoded with `h264_vaapi`.
Exit zero without the required frame evidence is not acceptance. The OpenCL
arm drives the compute path that a PF/VF CCS mismatch hangs (`rcs0`,
2026-11-06), which the VA-API arms pass. The playbook also reads each worker's
`/sys/module/i915/parameters/xelp_enable_ccs` and its since-boot
`GPU HANG` count from `journalctl -k -b` before and after the jobs, and fails
if the count changes. OpenVINO (Immich ML) is still untested, so after a guest
image or host driver change also run an Immich ML job on each worker.
Temporary namespaces/jobs and private kubeconfigs are cleaned in
nested `always` blocks; diagnostic artifacts contain no kubeconfig or SSH/Vault
credentials.

There are three explicit scopes:

| Mode | Meaning |
|------|---------|
| `capture` | Save pre-change identities and the intended target; no GPU probes, installation or reboot. The guest fleet must match its declared target before changing the host. |
| `current-stack` | Verify the declared target is actually loaded and run the GPU probes. Does **not** claim a reboot occurred or validate an uninstalled candidate. |
| `post-reboot` | Require the successful installation run's saved baseline, unchanged machine identities, changed boot IDs on the host and all cluster nodes, the frozen target's loaded versions, and every GPU probe. |

The installation workflow captures and uploads `i915-before` **before** changing
the host. Its successful conclusion means installation/reboot scheduling only.
The runner itself reboots with Proxmox, so runtime acceptance is a separate,
explicitly invoked workflow after recovery, not a wait inside installation.

Current-stack diagnostics can also be dispatched through the existing validation
workflow:

```sh
gh workflow run i915-compat.yml --repo Starktastic-Homelab/ansible \
  -f runtime_mode=current-stack
```

For post-upgrade acceptance, set `INSTALLATION_RUN_ID` to the successful
`i915-sriov-upgrade.yml` run that saved the baseline:

```sh
gh workflow run i915-acceptance.yml --repo Starktastic-Homelab/ansible \
  -f mode=post-reboot -f installation_run_id="$INSTALLATION_RUN_ID"
```

The workflow verifies the producer run and reuses its frozen target. An expired
or missing baseline is a failure, not permission to assume fresh boots. A
no-op installation that did not reboot can receive current-stack diagnostics,
not post-reboot acceptance. If an emergency prevents baseline capture, use a
separately approved recovery procedure and document that limitation rather than
manufacturing before/after evidence.

The diagnostic image is deliberately pinned independently of the application
catalog. These synthetic probes test the hardware paths, not visual quality,
application upgrades, seeking, or database/probe stability; retain the real
application-level playback acceptance above.

Offline guard tests, including stale boots, wrong loaded versions, missing
workers, command failures and zero-frame false positives:

```sh
python3 scripts/tests/test_i915_compat.py
python3 scripts/tests/test_i915_acceptance.py
```

# VM300 maintenance coordination qualification

Qualified September 21, 2026 using the manually reviewed
`maintenance-runner.yml` and `maintenance_runner` role at Ansible commit
`0c61d10bcd786cd75a582e071d4959d2aa04c4d5`.

## Verified host and bootstrap

- Proxmox VM300 is `runner`, running at `10.9.9.10` on the management network.
- Both Proxmox configuration and guest SMBIOS report UUID
  `cc1aeeb7-4827-466c-9d4b-5dc6c881f193`.
- The existing GitHub Actions service runs as `ben` (UID/GID 1000).
- The manual role created `homelab-maintenance` (GID 1001), added `ben`, and
  created `/var/lib/homelab-maintenance` and `operations` as root/group 2770,
  with the matching `runner-instance` marker as root/group 0640.
- Apply: 9 tasks OK, 5 changed, 2 skipped, zero failures. Subsequent check mode:
  11 OK, zero changes, zero failures. Initial fresh-install check mode stopped
  at user membership because simulated group creation does not create the group;
  actual apply and subsequent check mode succeeded.
- No `Runner.Worker` process was present before the brief runner-service restart.
  The new service process includes GID 1001; logs confirm connection to GitHub
  and `Listening for Jobs` at 15:17:00 UTC. Garage remained running.

## Actual cross-container checks

Used separate network-disabled, disposable containers with the host directory
bound to `/maintenance`. The runner's cached `python:trixie` image ID was
`sha256:0876e54cf728d89fd9d0fdaf5837b9ee879ea5fbbd6fd0cddbe5eb0cce3f5f9e`.
The helper exactly matched pinned commit
`887269798650e88fed791bb4f22a47bf50ad17be`, SHA256
`0e1c5ebf1f9e0c088d40fe2cf939406d2971f857844e9c05e3d3fdf636855299`.

Passed:

- Root container acquired; a separate UID 1000 container with the maintenance
  group verified the same ownership.
- A competing owner could not acquire while the first operation persisted.
- Wrong nonce, wrong runner identity, and missing host mount were rejected.
- A container verified ownership and exited 143 to model a failed/cancelled job;
  its operation persisted. A later container resumed with the original credentials,
  advanced the exact stage, and released ownership.
- A non-root container acquired the next operation; a root container verified it;
  the original non-root owner released it.
- No operation remained. The permanent guard is root/group 0660.

Nonces and credentials were kept out of logs and Git. The temporary SSH public
key used for the bootstrap was removed after testing; existing authorized-key
lines were preserved. Temporary qualification containers and guest files were
removed. The supplied Proxmox password file was restricted to mode 0600.

## Limits and next gates

These tests used real VM300 bind mounts and containers, but did not dispatch
competing GitHub workflows, cancel an actual Actions run, or reboot VM300.
Cancellation was modeled by exit 143. This establishes persistence across
container lifetimes, not completed reboot or end-to-end workflow qualification.
Normal coordinated workflows must still be observed after their reviewed merges.

No K3s VM, NAS setting, target, fencing account, or Jellyfin deployment changed.
Worker enrollment, fencing qualification, fresh backup/restore coverage, and the
NAS durability change remain separate steps. No storage writer was authorized.

[Packer #109](https://github.com/Starktastic-Homelab/packer/pull/109) adds clone-safe
image prerequisites; no image build or cluster replacement ran for that PR.

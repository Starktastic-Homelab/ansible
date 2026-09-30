# Supervised read-only maintenance runner

Status: the synthetic executor passed disposable-VM systemd qualification on
2026-09-30. See [qualification evidence](maintenance-executor-qualification-2026-09-30.md).
The original read-only executor was installed and accepted on VM300 on
2026-10-01 (local time), with diagnostic findings preserved. The dependency-bundle
upgrade described below is source-only and has not been installed or qualified
on VM300. No production workflow switch is included.

The executor accepts only `status` and `preflight` for Jellyfin. It has no API for
shell commands, arbitrary environment variables, source paths, or mutations.
The service's root-owned manifest fixes interpreter, sources and state root.
The public CLI manifest argument is an operator installation reference, not a
field that a request may choose. Treat installation configuration as trusted.

## Durable protocol

Each schema-1 request names a UUID, operation, service, exact ownership stage,
Apps commit and runtime ID. Credentials (original owner and nonce) arrive only
through private stdin and are stored in a `0600` file inside a `0700` request
directory. The final acceptance marker binds request and credential hashes.
Existing IDs, including incomplete directories, are never overwritten. Duplicate
submission only inspects existing state and never starts another attempt.

The executor verifies source/interpreter identity and original ownership, takes
execution exclusion, and durably records `started` before invoking the fixed
adapter. Started requests cannot run again. Completion requires a matching
terminal receipt and a zero child exit code; inactive systemd state alone is
never success. Failed, missing or contradictory evidence remains failed/unknown.
Unknown outcomes require inspection; there is no automatic replay after reboot.

Output lives in private `stdout.log`/`stderr.log` files under the request. Reports
contain allowlisted fields and fixed diagnostics. `attempt-*.json` records fixed
validation/start errors when writable state is available. If runtime verification
fails before state can safely be located, the CLI exits nonzero without claiming
completion. A retained `.pending-*` marker makes the entire request unknown after a failed
publication, including directory-sync failure. Do not erase files to make a
request eligible again.

## Opt-in installation (later reviewed rollout)

`maintenance_runner_executor_enabled` defaults to false. Enabling it requires reviewed
40-character Apps and Ansible commit IDs and SHA-256 checksums of their GitHub
archives. Merge the adapter PR first, then select final reviewed commits for both
repositories; no mutable branch archive is accepted.

The existing role first checks the reviewed VM300 hardware identity and explicit
runner service user. Installation downloads and verifies the archives, creates a
root-owned content-addressed release, records the interpreter executable hash and
version, and installs `/etc/systemd/user/homelab-maintenance@.service`. Updating
Python invalidates qualification until a new reviewed runtime is installed.
No pip installation occurs during request execution.

Opt-in installation also enables lingering for the reviewed runner user. It does
not start an operation or enable instances at boot. After a reviewed install or
upgrade, the operator must reload that user's systemd manager before submission;
no manager reload or service start is hidden in this source delivery. Never update
a runtime while a prior operation is active. Old request IDs remain bound to the
original runtime and cannot silently migrate to another source revision.

The manifest path/runtime ID are recorded in
`/etc/homelab-maintenance/installation.json`. Use that managed installation for:

```text
python3 -B <release>/ansible/scripts/maintenance_executor.py --manifest <manifest> submit
python3 -B <release>/ansible/scripts/maintenance_executor.py --manifest <manifest> inspect <request-uuid>
python3 -B <release>/ansible/scripts/maintenance_executor.py --manifest <manifest> start <request-uuid>
```

`submit` reads exactly `{"request": {...}, "credentials": {...}}` from private
stdin. Do not put credentials in argv, shell history, Git or CI artifacts.
`start` is an explicit recovery step for accepted intent with no started evidence;
it is never available for an interrupted started request. The service rechecks
eligibility under exclusion. `inspect` reports recorded history separately from
current service state; service-query failure means unobserved, not healthy.

## Qualification and workstation boundary

The operator reported workstation freezes during local testing. No Ansible
playbook deployment was dispatched, but privileged local systemd containers were
attempted. Causation is unproven. The test container was stopped and removed.
**Do not run privileged systemd containers, Ansible installation, or runtime
qualification on that workstation.** Local verification is limited to lightweight
Python tests, static checks and review; unit tests mock systemctl.

Use `scripts/tests/qualify_maintenance_executor.py` only inside an explicitly
provisioned disposable VM, as an unprivileged test user with a working systemd
user manager. Provisioning is outside this script. An administrator must first
create root-owned `/etc/homelab-maintenance-disposable` containing that VM's exact
`/etc/machine-id`, with no group/other write permission. The script also requires
that exact ID as an argument. The unprivileged user must be able to read
`/sys/class/dmi/id/product_uuid`; provisioning granted read permission only inside
the isolated test VM. The script refuses non-VM/container environments and production
VM300's UUID, and requires a new `/var/tmp/maintenance-qualification-*` state root.

The script installs only a temporary user unit if none already exists, runs a
fixed synthetic adapter, stops its own units and removes its unit afterward.
It preserves synthetic receipts and results. It never uses production credentials
or acquires the real maintenance operation. It covers client exit, duplicate
submission, concurrency, explicit start, forced stop/child cleanup, nonzero exit,
supervisor SIGKILL, stale evidence and runtime drift. Actual runner reboot and mutating-operation
recovery remain separate qualification work.

The script passed on the identified disposable VM and the guest was removed
after evidence export. This qualifies the synthetic supervised execution protocol;
it does not validate Ansible installation, the production runtime, runner reboot,
or live storage. Production adoption, backup integration and mutation-specific
reconciliation need later reviewed plans and deployment approval.

## Managed preflight dependencies (opt-in upgrade)

New runtime manifests use schema 2. They bind the Apps requirements file and
exact source/dependency inventory as well as file hashes and interpreter identity.
Added, removed, modified, or symlinked dependency files invalidate the runtime.
Schema-1 manifests and their original request/runtime IDs remain readable; do not
rewrite old receipts or replay a started request during upgrade.

The fixed executor passes its verified `ansible/scripts` directory to the Apps
adapter. Requests cannot supply helper/dependency paths. The adapter only discovers
the helper; it does not import it. Install a commit containing the matching Apps
adapter options before enabling this Ansible revision.

Set `maintenance_runner_executor_dependencies_enabled: true` only in a reviewed
installation inventory, alongside the existing executor opt-in and immutable
Apps/Ansible archive pins. Supply all of:

- `maintenance_runner_executor_kubectl_version`: exact `vX.Y.Z`, compatible with
  the observed API server version; no `latest` lookup during installation.
- `maintenance_runner_executor_kubectl_arch`: `amd64` (default) or `arm64`, checked
  against the target host architecture.
- `maintenance_runner_executor_kubectl_sha256`: reviewed publisher checksum.
- `maintenance_runner_executor_websocket_version`: exact Apps requirements pin.
- `maintenance_runner_executor_websocket_url`: reviewed pure-Python wheel URL
  under `https://files.pythonhosted.org/packages/`.
- `maintenance_runner_executor_websocket_sha256`: reviewed wheel checksum.

The role downloads kubectl from the official versioned Kubernetes URL into
`dependencies/bin/kubectl`, and retains the checksum-verified wheel under
`dependencies/python/`. It installs no OS packages, changes no host Python
packages, and runs no pip/build hooks. The release validator checks wheel metadata
against the pinned requirement and checks that the transport module is present.
The read-only adapter examines wheel metadata without importing transport code;
there is no fallback to host packages or host kubectl for a managed bundle.
The immutable wheel is available to a future explicitly qualified transport
consumer; this change does not introduce a mutating consumer.

The manifest covers wheel bytes and kubectl bytes; publication preserves kubectl's
executable mode and makes the wheel read-only to the service account. A new bundle
produces a new runtime ID. No service reload, request submission, or NAS mutation
occurs during source delivery. Installation is still a separately reviewed VM300
operation with new acceptance requests and preserved original evidence.

Checks only establish local prerequisites. Executable presence/version metadata
are not live cluster access, functional transport qualification, journal
reconciliation, or writer release authorization. Existing journals continue to
report `unknown` until a separately designed evidence-consumption protocol exists.

Installation references: [Kubernetes checksum verification](https://kubernetes.io/docs/tasks/tools/install-kubectl-linux/)
and [Python ZIP imports](https://docs.python.org/3/library/zipimport.html).

Source validation on 2026-10-01: 49 synthetic maintenance tests and 110 Apps
storage tests passed. A combined executor/actual-adapter smoke test using synthetic
state and the verified wheel passed helper, transport and kubectl discovery and
wrote a terminal receipt. It used a fake kubectl that must not execute. No local
Ansible/systemd/container qualification ran. The Apps suite emitted existing
SQLite fixture ResourceWarnings. Full pre-commit was unavailable locally;
changed YAML was parsed and formatted with cached tools. CI must supply
Ansible lint/syntax validation, and VM300 upgrade acceptance is still pending.

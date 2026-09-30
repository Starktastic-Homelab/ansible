# Supervised read-only maintenance runner

Status: source and unit-test preparation. **Disposable-VM systemd qualification is
pending. Do not enable this on VM300 yet.** No production Apps helper pin or
workflow changes are part of this increment.

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
that exact ID as an argument, refuses non-VM/container environments and production
VM300's UUID, and requires a new `/var/tmp/maintenance-qualification-*` state root.

The script installs only a temporary user unit if none already exists, runs a
fixed synthetic adapter, stops its own units and removes its unit afterward.
It preserves synthetic receipts and results. It never uses production credentials
or acquires the real maintenance operation. It covers client exit, duplicate
submission, concurrency, explicit start, forced stop/child cleanup, nonzero exit,
stale evidence and runtime drift. Actual runner reboot and mutating-operation
recovery remain separate qualification work.

Until this script passes on the identified disposable VM, keep the coordinated
PRs draft. Unit results do not substitute for systemd qualification. Production
adoption, backup integration and mutation-specific reconciliation need later
reviewed plans and deployment approval.

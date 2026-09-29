# Maintenance execution exclusion

The `execution` context manager in `scripts/maintenance_lock.py` adds an advisory,
per-runner execution mutex alongside the existing persistent ownership record.
This is a library prerequisite for a future supervised executor. Merging it does
not update Apps' pinned helper, install a service, restart a VM or run maintenance.

## Contract

`execution(root, owner, nonce, stage)` requires the original ownership and exact
current stage. It yields an integer descriptor for the permanent
`<root>/execution.guard` file. A competing adopter raises `ExecutionBusy` promptly,
including when it supplies the same owner and nonce. Identity mismatches retain
the existing exceptions; no credentials are included in the busy diagnostic.

The file is regular, no-follow, opened nonblocking and shared as mode `0660`.
Do not remove or replace its inode. All callers must use the same protected local
runner directory. Advisory exclusion assumes trusted directory administrators;
it cannot prevent privileged manual modification or lock-inode replacement.

The execution mutex is acquired before the short metadata guard. `verify` only
uses the metadata guard, so it can run inside the operation. `acquire`, `advance`
and `release` also acquire execution exclusion before changing ownership. They
raise `ExecutionBusy` if execution is active. The context is not reentrant: do
not call these mutation functions from within it. Close the context and wait for
all children before a separately validated stage transition.

## Child lifetime

The descriptor is close-on-exec by default. Pass it explicitly only to trusted
children that participate in the operation. The following is a library sketch;
`reviewed_argv` and `private_env` must come from the future validated executor,
not arbitrary user input. Obtain the owner/nonce privately, never as command-line
arguments or in logs.

```python
with execution(root, owner, nonce, expected_stage) as execution_fd:
    with subprocess.Popen(
        reviewed_argv,
        env=private_env,
        pass_fds=(execution_fd,),
    ) as child:
        result = child.wait()
```

Normal callers must wait for children. A child that outlives the parent and keeps
its descriptor also keeps execution exclusion. Context exit closes its descriptor
without `LOCK_UN`; an explicit unlock would also unlock the inherited open file
description. Exclusion ends only after the last holder closes it. Trusted children
must propagate the descriptor to any descendants whose work also requires exclusion.

## What this does not establish

Persistent `operation.json` is never cleared by context exit or process death.
A free mutex is not proof that a failed operation is safe to retry. Lost replies,
partial work, mounts and intent receipts still require reviewed reconciliation.
There is no automatic retry or restart behavior in this helper.

Older pinned helpers and callers that only invoke `verify` do not acquire this
mutex for their operation body. Production adoption must coordinate all writers
with the future executor and durable intent/exit records; updating one consumer
alone does not establish universal exclusion. No Apps helper pin changes here.

## Qualification

Run the unit suite using only temporary local state:

```bash
python3 -m unittest discover -s scripts/tests -p 'test_maintenance*.py' -v
```

The Linux process tests cover competing same-owner starts, release/start races,
blocked stage/release transitions, identity failure, unsafe lock paths, restrictive
umask, normal/exceptional exit, and child exclusion after normal parent exit,
SIGTERM and SIGKILL. They use bounded synchronization, adopt and reap orphaned test
children, and preserve the ownership record. An intentionally broken test copy
with explicit `LOCK_UN` fails the child-lifetime regression.

These are local process tests, not a runner reboot, live host qualification or
proof of interrupted storage recovery. The next stage installs and qualifies the
runtime/supervisor and its recovery protocol in a disposable environment.

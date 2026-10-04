# Verified worker Node retirement implementation plan

> **For agentic workers:** Use superpowers:executing-plans for inline implementation and a final independent review.

**Goal:** Recover same-name worker replacement without deleting a Node while its old VM generation exists.
**Architecture:** Native Ansible reads and UID-conditional deletion, with small pure filters validating evidence. The role runs before worker installation and stays disabled until lab qualification.
**Tech Stack:** Existing Ansible, kubernetes.core, Python standard library; no new runtime dependency.
**Spec:** ../specs/2026-10-04-verified-node-retirement.md

## Global constraints

- Stock CSI and Terraform drain handoff remain unchanged; no production activation.
- No workstation deployments; only offline source tests locally.
- Existing maintenance ownership is required immediately before a destructive Node API call.
- Complete inventory, replacement identity and absent old UUID are mandatory; unknown means refuse.
- Keep API secrets out of output/argv, verify TLS by default, never grant privileges automatically.

## Review focus

- Child ACL overrides and pool NoAccess must not hide a live old VM.
- Same VMID/name with a new UUID must succeed only after the old UUID is absent everywhere.
- Malformed/partial API responses, templates and stopped guests must not evade scanning.
- A Node replaced during the check must survive conditional deletion; failures retain ownership.
- Disabled/default, absent Node and unchanged-generation paths must avoid deletion and unnecessary API access.

## Task 1: Evidence validation and native retirement role

Files: filter_plugins/k3s_node_retirement.py; roles/k3s_node_retirement/{defaults,tasks}/main.yml;
k3s.yml; scripts/tests/test_node_retirement.py; .github/workflows/validate.yml; README.md.
Interfaces: pure filters validate Kubernetes/guest identities, complete permissions,
and a full list of Proxmox resources/config responses; native modules own all I/O.

- [ ] Write failing safety cases for missing/unchanged/stale Nodes, UUID validation,
  full permissions including hidden overrides, surviving old UUID anywhere, wrong
  replacement, duplicate/missing configs and changed inventory.
- [ ] Run tests and confirm the missing behavior fails.
- [ ] Add pure filters and the disabled role, with secret-protected native URI reads,
  final ownership check and UID-conditional deletion before worker join.
- [ ] Verify role arguments/order offline; add the test to CI and explain permissions,
  failure behavior and the surviving-cordon boundary in README.
- [ ] Run the source suite, offline lint and syntax checks; fix relevant failures.
- [ ] Commit and obtain one fresh whole-branch review; resolve substantive findings.

## Task 2: Deliver and qualify

- [ ] Create the Ansible source PR with exact validation and disabled-state boundary.
- [ ] Stop for its merge, then resume the approved disposable lab with fresh identity
  checks and narrowly scoped temporary access. Any new permission grant needs its
  own concrete review; source approval does not grant production API access.
- [ ] Qualify same-name worker replacement, refusal with uncertain evidence and
  original data/attachment identity, then resume remaining integrated cases.

User authorization: continuous inline execution and this architectural choice are
already explicit. No additional plan approval is required for this source work.

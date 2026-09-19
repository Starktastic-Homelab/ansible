#!/usr/bin/env python3
"""Validate collected i915 runtime evidence; never install drivers or reboot.

The Ansible playbook collects machine identities and runs isolated GPU probes.
This stdlib-only validator keeps current-stack diagnostics distinct from
post-reboot acceptance, which requires the deployment's pre-change snapshot.
"""

import argparse
import json
import re
import sys
import uuid
from pathlib import Path

from i915_compat import BLOCKED_HOST_RELEASES, Unknown, http_get, parse_series

PROBE_PREFIX = "I915_GPU_RESULT="
GPU_CHECKS = {"h264_low_power", "hevc_low_power", "hdr_decode_tonemap_encode"}
EXPECTED_FIELDS = {"host_version", "host_kernel", "guest_version", "guest_kernel_series"}
ROLES = {"hosts", "masters", "workers"}
GUEST_CONFIG_URL = "https://raw.githubusercontent.com/Starktastic-Homelab/packer/main/debian.auto.pkrvars.hcl"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def release_name(value):
    return value.strip().removeprefix("v").removesuffix("-sriov")


def read_target(host_config, host_defaults, guest_config, host_version=None):
    def field(text, key, separator):
        matches = re.findall(r'^%s\s*%s\s*"([^"]+)"' % (key, separator), text, re.MULTILINE)
        require(len(matches) == 1, "Cannot read one declared %s" % key)
        return matches[0]

    target = {
        "host_version": field(host_config, "i915_sriov_version", ":"),
        "host_kernel": field(host_defaults, "i915_sriov_pinned_kernel", ":"),
        "guest_version": field(guest_config, "i915_sriov_version", "="),
        "guest_kernel_series": field(guest_config, "i915_sriov_kernel_series", "="),
    }
    if host_version is not None:
        require(re.fullmatch(r"v?[0-9]+(?:\.[0-9]+)+", host_version) is not None, "Invalid host driver target")
        target["host_version"] = host_version
    require(
        release_name(target["host_version"]) not in BLOCKED_HOST_RELEASES,
        "Blocked host driver target: %s" % target["host_version"],
    )
    return target


def validate_structure(snapshot):
    require(isinstance(snapshot, dict) and snapshot.get("schema") == 1, "Unknown snapshot schema")
    expected = snapshot.get("expected")
    require(isinstance(expected, dict) and set(expected) == EXPECTED_FIELDS, "Incomplete expected target")
    require(
        all(isinstance(value, str) and value.strip() for value in expected.values()),
        "Expected target fields must be nonempty strings",
    )
    inventory = snapshot.get("inventory")
    require(isinstance(inventory, dict) and set(inventory) == ROLES, "Incomplete machine inventory")
    names = []
    for role, members in inventory.items():
        require(isinstance(members, list) and members, "Empty %s inventory" % role)
        require(all(isinstance(name, str) and name for name in members), "Invalid inventory name")
        names.extend(members)
    require(len(set(names)) == len(names), "Duplicate machine inventory entries")
    machines = snapshot.get("machines")
    require(isinstance(machines, dict) and set(machines) == set(names), "Missing or unexpected machine evidence")
    for name, machine in machines.items():
        require(isinstance(machine, dict), "Invalid machine evidence for %s" % name)
        for field in ("node_name", "boot_id", "machine_id", "kernel"):
            require(
                isinstance(machine.get(field), str) and machine[field],
                "Missing %s for machine %s" % (field, name),
            )
        try:
            parsed_boot = uuid.UUID(machine["boot_id"])
        except ValueError:
            raise ValueError("Invalid boot ID for %s" % name) from None
        require(str(parsed_boot) == machine["boot_id"], "Invalid boot ID for %s" % name)
        require(
            re.fullmatch(r"[0-9a-f]{32}", machine["machine_id"]) is not None,
            "Invalid machine ID for %s" % name,
        )
        require(isinstance(machine.get("module"), str), "Missing loaded module evidence for %s" % name)
    worker_names = [machines[name]["node_name"] for name in inventory["workers"]]
    require(len(set(worker_names)) == len(worker_names), "Duplicate Kubernetes worker names")


def validate_capture(snapshot):
    validate_structure(snapshot)
    expected, machines = snapshot["expected"], snapshot["machines"]
    host_version = release_name(expected["host_version"])
    require(host_version not in BLOCKED_HOST_RELEASES, "Blocked host driver target: %s" % host_version)
    try:
        guest_series = parse_series(expected["guest_kernel_series"])
        for name in snapshot["inventory"]["workers"]:
            machine = machines[name]
            require(
                release_name(machine["module"]) == release_name(expected["guest_version"]),
                "Wrong loaded guest driver/module on %s: %s" % (name, machine["module"]),
            )
            require(
                parse_series(machine["kernel"]) == guest_series,
                "Wrong running guest kernel on %s: %s" % (name, machine["kernel"]),
            )
    except Unknown as error:
        raise ValueError(str(error)) from None


def validate_snapshot(snapshot, baseline=None):
    validate_capture(snapshot)
    expected, machines = snapshot["expected"], snapshot["machines"]
    for name in snapshot["inventory"]["hosts"]:
        machine = machines[name]
        require(
            release_name(machine["module"]) == release_name(expected["host_version"]),
            "Wrong loaded host driver/module on %s: %s" % (name, machine["module"]),
        )
        require(
            machine["kernel"] == expected["host_kernel"],
            "Wrong running host kernel on %s: %s" % (name, machine["kernel"]),
        )
    if baseline is not None:
        validate_structure(baseline)
        require(expected == baseline["expected"], "Expected target differs from the deployment baseline")
        require(
            all(set(snapshot["inventory"][role]) == set(baseline["inventory"][role]) for role in ROLES),
            "The deployment machine inventory changed",
        )
        for name, machine in machines.items():
            before = baseline["machines"][name]
            require(
                machine["machine_id"] == before["machine_id"]
                and machine["node_name"] == before["node_name"],
                "Runtime machine identity changed instead of rebooting: %s" % name,
            )
            require(machine["boot_id"] != before["boot_id"], "No fresh boot observed for %s" % name)


def parse_probe_log(log):
    require(isinstance(log, str), "GPU probe log must be text")
    records = [line[len(PROBE_PREFIX):] for line in log.splitlines() if line.startswith(PROBE_PREFIX)]
    require(len(records) == 1, "Expected exactly one complete GPU result record")
    try:
        result = json.loads(records[0])
    except json.JSONDecodeError:
        raise ValueError("Malformed GPU result record") from None
    require(isinstance(result, dict), "GPU result must be an object")
    return result


def build_report(snapshot, gpu_results, baseline=None):
    validate_snapshot(snapshot, baseline)
    require(isinstance(gpu_results, list), "GPU results must be a list")
    expected_workers = {
        snapshot["machines"][name]["node_name"]: snapshot["machines"][name]
        for name in snapshot["inventory"]["workers"]
    }
    observed = set()
    for result in gpu_results:
        require(isinstance(result, dict), "Invalid GPU probe result")
        node = result.get("node")
        require(isinstance(node, str) and node in expected_workers, "Unexpected GPU worker: %s" % node)
        require(node not in observed, "Duplicate GPU worker result: %s" % node)
        observed.add(node)
        machine = expected_workers[node]
        for field in ("kernel", "module", "boot_id"):
            require(
                result.get(field) == machine[field],
                "GPU probe %s disagrees with machine %s" % (field, node),
            )
        require(result.get("uid") == 1000, "GPU probe did not run as the intended nonroot user on %s" % node)
        frames = result.get("frames")
        require(isinstance(frames, dict) and set(frames) == GPU_CHECKS, "Incomplete GPU frame evidence on %s" % node)
        for check, count in frames.items():
            require(
                type(count) is int and count >= 30,
                "Insufficient encoded frames for %s on %s" % (check, node),
            )
    require(observed == set(expected_workers), "Missing GPU worker results")
    return {
        "schema": 1,
        "status": "passed",
        "mode": "post-reboot" if baseline is not None else "current-stack",
        "fresh_boots_verified": baseline is not None,
        "workers": sorted(expected_workers),
        "runtime": snapshot,
        "gpu_results": gpu_results,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("target", "capture", "preflight", "accept"))
    parser.add_argument("--snapshot")
    parser.add_argument("--host-version", help="explicit target override when reading declared versions")
    parser.add_argument("--mode", choices=("current-stack", "post-reboot"))
    parser.add_argument("--baseline")
    parser.add_argument("--probe-logs", help="JSON array of complete GPU pod log strings")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    if args.host_version is not None and args.action != "target":
        parser.error("--host-version is only valid when reading the target; acceptance uses the frozen snapshot")
    if args.action != "target" and not args.snapshot:
        parser.error("capture/preflight/accept requires --snapshot")
    if args.action in ("preflight", "accept") and not args.mode:
        parser.error("preflight/accept requires an explicit --mode")
    if args.mode == "post-reboot" and not args.baseline:
        parser.error("post-reboot mode requires the deployment's --baseline")
    if args.mode == "current-stack" and args.baseline:
        parser.error("a reboot baseline requires --mode post-reboot")
    if args.action == "accept" and not args.probe_logs:
        parser.error("accept requires --probe-logs from every expected worker")
    try:
        snapshot = json.loads(Path(args.snapshot).read_text()) if args.snapshot else None
        baseline = json.loads(Path(args.baseline).read_text()) if args.baseline else None
        if args.action == "target":
            root = Path(__file__).resolve().parents[1]
            result = {"i915_acceptance_expected": read_target(
                (root / "group_vars/proxmox_hosts/i915_sriov.yml").read_text(),
                (root / "roles/i915_sriov/defaults/main.yml").read_text(),
                http_get(GUEST_CONFIG_URL),
                args.host_version,
            )}
            print(json.dumps(result, indent=2))
        elif args.action == "capture":
            validate_capture(snapshot)
            result = snapshot
            print("Captured runtime baseline. Hardware acceptance: NOT TESTED.")
        elif args.action == "preflight":
            validate_snapshot(snapshot, baseline)
            result = {"status": "preflight-only", "hardware_acceptance": "NOT TESTED"}
            print("Runtime identities passed. Hardware acceptance: NOT TESTED.")
        else:
            logs = json.loads(Path(args.probe_logs).read_text())
            require(isinstance(logs, list), "Probe logs must be a JSON array")
            result = build_report(snapshot, [parse_probe_log(log) for log in logs], baseline)
            print(json.dumps(result, indent=2))
            if baseline is None:
                print("Current-stack diagnostics only; reboot freshness was NOT CHECKED.")
        if args.output:
            Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    except (OSError, ValueError, Unknown) as error:
        print("i915 acceptance rejected: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

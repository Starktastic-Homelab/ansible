#!/usr/bin/env python3
"""Offline acceptance-evidence checks; no SSH, Kubernetes or GPU access."""

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def snapshot():
    machines = {}
    for index, name in enumerate(("pve", "kube-master-01", "kube-worker-01", "kube-worker-02"), 1):
        machines[name] = {
            "node_name": name,
            "boot_id": f"00000000-0000-4000-8000-{index:012d}",
            "machine_id": f"{index:032x}",
            "kernel": "6.17.13-13-pve" if name == "pve" else "6.12.107+deb13-amd64",
            "module": "2026.09.14-sriov" if name == "pve" else "2026.03.05.7-sriov",
        }
    machines["kube-master-01"]["module"] = ""
    return {
        "schema": 1,
        "expected": {
            "host_version": "2026.09.14",
            "host_kernel": "6.17.13-13-pve",
            "guest_version": "2026.03.05.7",
            "guest_kernel_series": "6.12",
        },
        "inventory": {
            "hosts": ["pve"],
            "masters": ["kube-master-01"],
            "workers": ["kube-worker-01", "kube-worker-02"],
        },
        "machines": machines,
    }


def probes(state):
    return [
        {
            "node": state["machines"][name]["node_name"],
            "kernel": state["machines"][name]["kernel"],
            "module": state["machines"][name]["module"],
            "boot_id": state["machines"][name]["boot_id"],
            "uid": 1000,
            "frames": {
                "h264_low_power": 30,
                "hevc_low_power": 30,
                "hdr_decode_tonemap_encode": 30,
            },
        }
        for name in state["inventory"]["workers"]
    ]


def after_reboot(before):
    result = copy.deepcopy(before)
    for index, item in enumerate(result["machines"].values(), 1):
        item["boot_id"] = f"11111111-1111-4111-8111-{index:012d}"
    return result


class TestAcceptance(unittest.TestCase):
    def setUp(self):
        path = SCRIPTS / "i915_acceptance.py"
        self.assertTrue(path.is_file(), "The runtime evidence validator has not been implemented")
        spec = importlib.util.spec_from_file_location("i915_acceptance", path)
        self.runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.runtime)

    def test_current_stack_does_not_claim_a_verified_reboot(self):
        state = snapshot()
        report = self.runtime.build_report(state, probes(state))
        self.assertEqual(report["mode"], "current-stack")
        self.assertFalse(report["fresh_boots_verified"])
        self.assertEqual(report["workers"], ["kube-worker-01", "kube-worker-02"])

    def test_post_reboot_requires_fresh_boots_and_all_gpu_checks(self):
        before = snapshot()
        before["machines"]["pve"]["module"] = "2026.08.12.1-sriov"
        after = after_reboot(before)
        after["machines"]["pve"]["module"] = "2026.09.14-sriov"
        report = self.runtime.build_report(after, probes(after), before)
        self.assertEqual(report["mode"], "post-reboot")
        self.assertTrue(report["fresh_boots_verified"])

    def test_one_stale_boot_blocks_acceptance(self):
        before = snapshot()
        for name in before["machines"]:
            with self.subTest(name=name):
                after = after_reboot(before)
                after["machines"][name]["boot_id"] = before["machines"][name]["boot_id"]
                with self.assertRaisesRegex(ValueError, "boot"):
                    self.runtime.build_report(after, probes(after), before)

    def test_wrong_loaded_host_driver_is_rejected(self):
        state = snapshot()
        state["machines"]["pve"]["module"] = "2026.09.16-sriov"
        with self.assertRaisesRegex(ValueError, "driver|module"):
            self.runtime.validate_snapshot(state)

    def test_known_bad_target_cannot_be_accepted(self):
        state = snapshot()
        state["expected"]["host_version"] = "2026.09.16"
        state["machines"]["pve"]["module"] = "2026.09.16-sriov"
        with self.assertRaisesRegex(ValueError, "blocked|Blocked"):
            self.runtime.validate_snapshot(state)

    def test_wrong_host_kernel_is_rejected(self):
        state = snapshot()
        state["machines"]["pve"]["kernel"] = "6.17.13-12-pve"
        with self.assertRaisesRegex(ValueError, "kernel"):
            self.runtime.validate_snapshot(state)

    def test_wrong_guest_driver_or_kernel_is_rejected(self):
        for field, value in (("module", "2026.03.05.6-sriov"), ("kernel", "6.120.1")):
            with self.subTest(field=field):
                state = snapshot()
                state["machines"]["kube-worker-02"][field] = value
                with self.assertRaises(ValueError):
                    self.runtime.validate_snapshot(state)

    def test_missing_machine_or_empty_worker_inventory_is_rejected(self):
        missing = snapshot()
        del missing["machines"]["kube-worker-02"]
        empty = snapshot()
        empty["inventory"]["workers"] = []
        for state in (missing, empty):
            with self.subTest(state=state), self.assertRaises(ValueError):
                self.runtime.validate_snapshot(state)

    def test_changed_machine_identity_is_not_a_driver_reboot(self):
        before = snapshot()
        after = after_reboot(before)
        after["machines"]["kube-worker-02"]["machine_id"] = "f" * 32
        with self.assertRaisesRegex(ValueError, "machine"):
            self.runtime.build_report(after, probes(after), before)

    def test_different_target_from_deployment_evidence_is_rejected(self):
        before = snapshot()
        after = after_reboot(before)
        after["expected"]["host_version"] = "2026.09.15"
        after["machines"]["pve"]["module"] = "2026.09.15-sriov"
        with self.assertRaisesRegex(ValueError, "target|expected"):
            self.runtime.build_report(after, probes(after), before)

    def test_missing_or_duplicate_worker_probe_is_rejected(self):
        state = snapshot()
        results = probes(state)
        for bad in (results[:1], [results[0], results[0]]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.runtime.build_report(state, bad)

    def test_zero_frames_or_missing_codec_is_not_success(self):
        state = snapshot()
        for check in probes(state)[0]["frames"]:
            with self.subTest(check=check):
                results = probes(state)
                results[0]["frames"][check] = 0
                with self.assertRaisesRegex(ValueError, "frame"):
                    self.runtime.build_report(state, results)
        results = probes(state)
        del results[0]["frames"]["hdr_decode_tonemap_encode"]
        with self.assertRaises(ValueError):
            self.runtime.build_report(state, results)

    def test_probe_must_match_the_observed_node_boot_and_nonroot_user(self):
        state = snapshot()
        for field, value in (("boot_id", "stale"), ("kernel", "6.12.1"), ("module", "wrong"), ("uid", 0)):
            with self.subTest(field=field):
                results = probes(state)
                results[0][field] = value
                with self.assertRaises(ValueError):
                    self.runtime.build_report(state, results)

    def test_probe_log_requires_one_complete_result_record(self):
        expected = probes(snapshot())[0]
        log = "Probe starting\nI915_GPU_RESULT=" + json.dumps(expected) + "\n"
        self.assertEqual(self.runtime.parse_probe_log(log), expected)
        for invalid in ("", "Everything looked healthy", log + log, "I915_GPU_RESULT={bad}\n"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.runtime.parse_probe_log(invalid)

    def test_target_is_read_from_declared_versions_without_observed_value_fallback(self):
        self.assertTrue(hasattr(self.runtime, "read_target"), "Declared target reader is missing")
        target = self.runtime.read_target(
            'i915_sriov_version: "2026.09.14"\n',
            'i915_sriov_pinned_kernel: "6.17.13-13-pve"\n',
            'i915_sriov_version = "2026.03.05.7"\ni915_sriov_kernel_series = "6.12"\n',
        )
        self.assertEqual(target, snapshot()["expected"])
        for host, defaults, guest in (
            ("", 'i915_sriov_pinned_kernel: "6.17.13-13-pve"', 'i915_sriov_version = "2026.03.05.7"'),
            ('i915_sriov_version: "2026.09.14"', "", 'i915_sriov_version = "2026.03.05.7"'),
            ('i915_sriov_version: "2026.09.14"', 'i915_sriov_pinned_kernel: "6.17.13-13-pve"', ""),
        ):
            with self.subTest(host=host, defaults=defaults, guest=guest), self.assertRaises(ValueError):
                self.runtime.read_target(host, defaults, guest)

    def test_capture_allows_the_old_host_driver_but_not_unexpected_guests(self):
        self.assertTrue(hasattr(self.runtime, "validate_capture"), "Baseline capture validation is missing")
        state = snapshot()
        state["machines"]["pve"]["module"] = "2026.08.12.1-sriov"
        self.runtime.validate_capture(state)
        state["machines"]["kube-worker-01"]["module"] = "2026.03.05.6-sriov"
        with self.assertRaisesRegex(ValueError, "guest"):
            self.runtime.validate_capture(state)

    def test_acceptance_cannot_silently_ignore_a_driver_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = snapshot()
            (root / "runtime.json").write_text(json.dumps(state))
            (root / "logs.json").write_text(json.dumps([
                "I915_GPU_RESULT=" + json.dumps(result) for result in probes(state)
            ]))
            result = subprocess.run([
                sys.executable, str(SCRIPTS / "i915_acceptance.py"), "accept",
                "--snapshot", str(root / "runtime.json"), "--mode", "current-stack",
                "--probe-logs", str(root / "logs.json"), "--host-version", "2026.09.16",
                "--output", str(root / "acceptance.json"),
            ], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertFalse((root / "acceptance.json").exists())


class TestGpuProbeFailure(unittest.TestCase):
    def setUp(self):
        self.script = SCRIPTS / "i915_gpu_probe.sh"
        self.assertTrue(self.script.is_file(), "The generic GPU probe has not been implemented")

    def test_ffmpeg_failure_cannot_emit_success_evidence(self):
        result = subprocess.run(
            ["sh", str(self.script), "/bin/false"], capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("I915_GPU_RESULT=", result.stdout)

    def test_zero_encoded_frames_cannot_emit_success_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "ffmpeg"
            fake.write_text(
                "#!/bin/sh\n"
                'while [ "$#" -gt 0 ]; do\n'
                '  if [ "$1" = "-progress" ]; then printf "frame=0\\nprogress=end\\n" > "$2"; fi\n'
                "  shift\n"
                "done\n"
            )
            fake.chmod(0o700)
            result = subprocess.run(
                ["sh", str(self.script), str(fake)], capture_output=True, text=True,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("frames", result.stderr)
        self.assertNotIn("I915_GPU_RESULT=", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)

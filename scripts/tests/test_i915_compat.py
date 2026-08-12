#!/usr/bin/env python3
"""Offline tests for i915_compat.py. Run: python3 scripts/tests/test_i915_compat.py

Network is stubbed with fixtures captured from real upstream data, so the suite
is deterministic. Set I915_COMPAT_LIVE=1 to additionally hit GitHub.
"""

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import i915_compat as c  # noqa: E402

NOTES_LATEST = """**Required kernel**: 6.17.x ~ 7.1.x

For older kernel (**v6.12 ~ v6.19**), please use the [2026.03.05.6](https://github.com/strongtz/i915-sriov-dkms/releases/tag/2026.03.05.6) release.

**Note:** The Host and Guest are not strictly required to use the same module version.
"""

NOTES_BACKPORT = """**Supported kernel**: 6.12.x ~ 6.19.x

## What's Changed
* Add conftest for v6.18.44
"""

ABI_1_0 = """/* SPDX-License-Identifier: MIT */
#define IOV_VERSION_LATEST_MAJOR\t\t1u
#define IOV_VERSION_LATEST_MINOR\t\t0u
/* XXX In future we need to have major.minor base versions per platform */
#define IOV_VERSION_BASE_MAJOR\t\t\t1u
#define IOV_VERSION_BASE_MINOR\t\t\t0u
"""

GUC_1_1 = """#define GUC_VF_VERSION_LATEST_MAJOR\t1u
#define GUC_VF_VERSION_LATEST_MINOR\t1u
"""

LATEST, BACKPORT = "2026.08.12.1", "2026.03.05.6"


def fake_world(releases, abis, guc=GUC_1_1):
    """Build an http_get stub. Missing keys raise Unknown, like a 404 would."""

    def http_get(url):
        for tag, notes in releases.items():
            if url == c.RELEASE_API.format(repo=c.REPO, tag=tag):
                return json.dumps({"tag_name": tag, "body": notes})
        for tag, header in abis.items():
            if url == c.RAW.format(repo=c.REPO, tag=tag, path=c.IOV_ABI_PATH):
                return header
        if url.endswith(c.GUC_ABI_PATH):
            return guc
        raise c.Unknown("%s -> HTTP 404" % url)

    return http_get


CURRENT = fake_world(
    {LATEST: NOTES_LATEST, BACKPORT: NOTES_BACKPORT},
    {LATEST: ABI_1_0, BACKPORT: ABI_1_0},
)


class Base(unittest.TestCase):
    def use(self, http_get):
        real = c.http_get
        c.http_get = http_get
        self.addCleanup(lambda: setattr(c, "http_get", real))

    def run_cli(self, *args):
        with redirect_stdout(io.StringIO()) as out:
            code = c.main(list(args))
        return code, out.getvalue()


class TestParsing(Base):
    def test_kernel_ranges_from_real_release_notes(self):
        self.assertEqual(c.parse_kernel_range(NOTES_LATEST), ((6, 17), (7, 1)))
        self.assertEqual(c.parse_kernel_range(NOTES_BACKPORT), ((6, 12), (6, 19)))

    def test_unparsable_notes_fail_closed(self):
        for notes in ("", "no kernel information here", "**Required kernel**: soon"):
            with self.assertRaises(c.Unknown):
                c.parse_kernel_range(notes)

    def test_kernel_series_is_numeric_not_lexicographic(self):
        order = [c.parse_series(k) for k in ("6.9", "6.12", "6.17", "6.19", "7.1")]
        self.assertEqual(order, sorted(order))
        self.assertLess(c.parse_series("6.9"), c.parse_series("6.12"))
        self.assertLess(c.parse_series("6.19"), c.parse_series("7.1"))
        self.assertEqual(c.parse_series("6.17.13-13-pve"), (6, 17))

    def test_abi_macros(self):
        self.assertEqual(c.parse_iov_abi(ABI_1_0), {"base": (1, 0), "latest": (1, 0)})

    def test_malformed_abi_fails_closed(self):
        for header in ("", "#define IOV_VERSION_LATEST_MAJOR 1u", "garbage"):
            with self.assertRaises(c.Unknown):
                c.parse_iov_abi(header)


class TestNegotiation(Base):
    def negotiate(self, host, guest):
        return c.negotiate_iov(host, guest)

    def test_identical_ranges(self):
        agreed, _ = self.negotiate(
            {"base": (1, 0), "latest": (1, 0)}, {"base": (1, 0), "latest": (1, 0)}
        )
        self.assertEqual(agreed, (1, 0))

    def test_overlapping_minor_ranges_pick_the_common_minor(self):
        agreed, _ = self.negotiate(
            {"base": (1, 0), "latest": (1, 2)}, {"base": (1, 1), "latest": (1, 5)}
        )
        self.assertEqual(agreed, (1, 2))

    def test_pf_below_vf_base_is_incompatible(self):
        agreed, detail = self.negotiate(
            {"base": (1, 0), "latest": (1, 1)}, {"base": (1, 4), "latest": (2, 0)}
        )
        self.assertIsNone(agreed)
        self.assertIn("outside the VF range", detail)

    def test_vf_major_below_pf_base_is_rejected(self):
        agreed, detail = self.negotiate(
            {"base": (2, 0), "latest": (3, 0)}, {"base": (1, 0), "latest": (1, 0)}
        )
        self.assertIsNone(agreed)
        self.assertIn("below its base major", detail)

    def test_older_major_still_served_by_newer_pf(self):
        agreed, _ = self.negotiate(
            {"base": (1, 0), "latest": (2, 3)}, {"base": (1, 0), "latest": (1, 7)}
        )
        self.assertEqual(agreed, (1, 7))


class TestCli(Base):
    def test_intended_split_combination_passes(self):
        self.use(CURRENT)
        code, out = self.run_cli(
            "--host-version", LATEST, "--host-kernel", "6.17.13-13-pve",
            "--guest-version", BACKPORT, "--guest-kernel", "6.12",
        )
        self.assertEqual(code, c.EXIT_OK, out)
        self.assertIn("Overall: ✅", out)
        self.assertIn("6.17-7.1", out)
        self.assertIn("6.12-6.19", out)
        self.assertIn("common/negotiated ABI: 1.0", out)

    def test_guest_on_latest_line_with_612_fails_the_guest_axis(self):
        self.use(CURRENT)
        code, out = self.run_cli(
            "--host-version", LATEST, "--host-kernel", "6.17.13-13-pve",
            "--guest-version", LATEST, "--guest-kernel", "6.12",
        )
        self.assertEqual(code, c.EXIT_INCOMPATIBLE, out)
        self.assertIn("outside the release's supported range 6.17-7.1", out)
        self.assertIn("Overall: ❌", out)

    def test_unparsable_release_notes_fail_closed(self):
        self.use(fake_world({LATEST: "release notes rewritten upstream"}, {LATEST: ABI_1_0}))
        code, out = self.run_cli("--host-version", LATEST, "--host-kernel", "6.17.13-13-pve")
        self.assertEqual(code, c.EXIT_UNKNOWN, out)
        self.assertIn("Cannot be established", out)

    def test_missing_tag_fails_closed(self):
        self.use(CURRENT)
        code, out = self.run_cli("--guest-version", "2099.01.01.1", "--guest-kernel", "6.12")
        self.assertEqual(code, c.EXIT_UNKNOWN, out)
        self.assertIn("404", out)

    def test_missing_abi_header_fails_closed(self):
        self.use(fake_world({LATEST: NOTES_LATEST, BACKPORT: NOTES_BACKPORT}, {LATEST: ABI_1_0}))
        code, out = self.run_cli(
            "--host-version", LATEST, "--host-kernel", "6.17.13-13-pve",
            "--guest-version", BACKPORT, "--guest-kernel", "6.12",
        )
        self.assertEqual(code, c.EXIT_UNKNOWN, out)

    def test_incompatible_abi_fails(self):
        broken = ABI_1_0.replace("IOV_VERSION_BASE_MAJOR\t\t\t1u", "IOV_VERSION_BASE_MAJOR\t\t\t3u")
        broken = broken.replace("IOV_VERSION_LATEST_MAJOR\t\t1u", "IOV_VERSION_LATEST_MAJOR\t\t3u")
        self.use(
            fake_world(
                {LATEST: NOTES_LATEST, BACKPORT: NOTES_BACKPORT},
                {LATEST: broken, BACKPORT: ABI_1_0},
            )
        )
        code, out = self.run_cli(
            "--host-version", LATEST, "--host-kernel", "6.17.13-13-pve",
            "--guest-version", BACKPORT, "--guest-kernel", "6.12",
        )
        self.assertEqual(code, c.EXIT_INCOMPATIBLE, out)
        self.assertIn("PF ↔ VF", out)

    def test_network_failure_fails_closed(self):
        def dead(url):
            raise c.Unknown("%s -> connection refused" % url)

        self.use(dead)
        code, _ = self.run_cli("--host-version", LATEST, "--host-kernel", "6.17.13-13-pve")
        self.assertEqual(code, c.EXIT_UNKNOWN)

    def test_single_axis_skips_abi(self):
        self.use(CURRENT)
        code, out = self.run_cli("--guest-version", BACKPORT, "--guest-kernel", "6.12")
        self.assertEqual(code, c.EXIT_OK, out)
        self.assertNotIn("PF ↔ VF", out)

    def test_token_is_never_printed(self):
        os.environ["GITHUB_TOKEN"] = "ghp_secret_value"
        self.addCleanup(os.environ.pop, "GITHUB_TOKEN", None)
        self.use(CURRENT)
        _, out = self.run_cli("--guest-version", BACKPORT, "--guest-kernel", "6.12")
        self.assertNotIn("ghp_secret_value", out)


@unittest.skipUnless(os.environ.get("I915_COMPAT_LIVE"), "set I915_COMPAT_LIVE=1")
class TestLive(Base):
    def test_real_upstream_current_combination(self):
        code, out = self.run_cli(
            "--host-version", LATEST, "--host-kernel", "6.17.13-13-pve",
            "--guest-version", BACKPORT, "--guest-kernel", "6.12",
        )
        self.assertEqual(code, c.EXIT_OK, out)


if __name__ == "__main__":
    unittest.main(verbosity=2)

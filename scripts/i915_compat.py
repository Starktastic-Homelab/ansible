#!/usr/bin/env python3
"""Validate an i915-sriov-dkms host/guest combination against upstream data.

Host (PF) and guest (VF) do NOT have to run the same release. Upstream says so
explicitly, and the releases have split into kernel-compatible lines. What must
hold instead is three independent axes:

  1. host release supports the host kernel     (upstream release notes)
  2. guest release supports the guest kernel   (upstream release notes)
  3. host PF and guest VF can negotiate a common IOV ABI
     (drivers/gpu/drm/i915/gt/iov/abi/iov_version_abi.h of each exact tag)

Everything is derived from the exact upstream tag; nothing is hardcoded and
there is no curated host/guest allowlist. Anything that cannot be fetched or
parsed fails closed.

Usage:
  i915_compat.py --host-version T --host-kernel K --guest-version T --guest-kernel K
  i915_compat.py --host-version T --host-kernel K     # host axis only
  i915_compat.py --guest-version T --guest-kernel K   # guest axis only

Exit codes: 0 compatible, 1 incompatible, 2 compatibility could not be
established (network/parse/missing tag -> fail closed).

NOTE: this file is duplicated verbatim in the Starktastic-Homelab/ansible and
Starktastic-Homelab/packer repositories so each repo stays independently
usable. Keep the two copies identical.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

REPO = "strongtz/i915-sriov-dkms"
RELEASE_API = "https://api.github.com/repos/{repo}/releases/tags/{tag}"
RAW = "https://raw.githubusercontent.com/{repo}/{tag}/{path}"
IOV_ABI_PATH = "drivers/gpu/drm/i915/gt/iov/abi/iov_version_abi.h"
GUC_ABI_PATH = "drivers/gpu/drm/i915/gt/uc/abi/guc_version_abi.h"

EXIT_OK, EXIT_INCOMPATIBLE, EXIT_UNKNOWN = 0, 1, 2

# "**Required kernel**: 6.17.x ~ 7.1.x" / "**Supported kernel**: 6.12.x ~ 6.19.x"
KERNEL_RANGE_RE = re.compile(
    r"(?:required|supported)[\s*_]+kernel[\s*_]*:?[\s*_]*"
    r"v?(\d+)\.(\d+)(?:\.x)?\s*(?:~|--|-|–|—|to)\s*"
    r"v?(\d+)\.(\d+)(?:\.x)?",
    re.IGNORECASE,
)
SERIES_RE = re.compile(r"^v?(\d+)\.(\d+)")


class Unknown(Exception):
    """Compatibility cannot be established -> fail closed."""


def http_get(url):
    """Fetch a URL as text. Raises Unknown on any failure (fail closed)."""
    request = urllib.request.Request(url, headers={"User-Agent": "i915-compat"})
    token = os.environ.get("GITHUB_TOKEN", "")
    if token and url.startswith("https://api.github.com/"):
        # Only ever sent to the API host, and never echoed anywhere.
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        raise Unknown("%s -> HTTP %s" % (url, err.code)) from None
    except Exception as err:  # network down, DNS, TLS, timeout
        raise Unknown("%s -> %s" % (url, err)) from None


def parse_series(text):
    """'6.17.13-13-pve' or '6.12' -> (6, 17) / (6, 12). Numeric, not lexical."""
    match = SERIES_RE.match(text.strip())
    if not match:
        raise Unknown("cannot parse kernel version %r" % text)
    return int(match.group(1)), int(match.group(2))


def parse_kernel_range(body):
    """Extract the supported kernel range from upstream release notes."""
    match = KERNEL_RANGE_RE.search(body or "")
    if not match:
        raise Unknown(
            "release notes do not state a supported kernel range in a known format"
        )
    low = (int(match.group(1)), int(match.group(2)))
    high = (int(match.group(3)), int(match.group(4)))
    if low > high:
        raise Unknown("release notes state an inverted kernel range")
    return low, high


def parse_iov_abi(header):
    """Parse the four IOV_VERSION_{BASE,LATEST}_{MAJOR,MINOR} macros."""
    found = {
        "%s_%s" % (kind.lower(), part.lower()): int(value)
        for kind, part, value in re.findall(
            r"#define\s+IOV_VERSION_(BASE|LATEST)_(MAJOR|MINOR)\s+(\d+)u?", header
        )
    }
    if len(found) != 4:
        raise Unknown("IOV ABI header is missing or malformed")
    return {
        "base": (found["base_major"], found["base_minor"]),
        "latest": (found["latest_major"], found["latest_minor"]),
    }


def negotiate_iov(host, guest):
    """Replay the upstream PF/VF handshake; return the negotiated ABI or None.

    Mirrors reply_handshake() in gt/iov/intel_iov_service.c: the VF asks for its
    own LATEST, the PF answers within its [BASE, LATEST], and the VF must be
    able to speak whatever came back.
    """
    wanted, pf_base, pf_latest = guest["latest"], host["base"], host["latest"]
    if wanted == (0, 0) or wanted[0] > pf_latest[0]:
        agreed = pf_latest
    elif wanted[0] < pf_base[0]:
        return None, "host PF rejects VF major %d (below its base major %d)" % (
            wanted[0],
            pf_base[0],
        )
    elif wanted[0] < pf_latest[0]:
        agreed = wanted
    else:
        agreed = (wanted[0], min(pf_latest[1], wanted[1]))
    if not guest["base"] <= agreed <= guest["latest"]:
        return None, "PF offers %s, outside the VF range %s-%s" % (
            fmt(agreed),
            fmt(guest["base"]),
            fmt(guest["latest"]),
        )
    return agreed, ""


def fmt(version):
    return "%d.%d" % version


def guc_latest(tag):
    """VF<->GuC interface version. Informational only; never fatal."""
    try:
        header = http_get(RAW.format(repo=REPO, tag=tag, path=GUC_ABI_PATH))
        found = dict(
            (part.lower(), int(value))
            for part, value in re.findall(
                r"#define\s+GUC_VF_VERSION_LATEST_(MAJOR|MINOR)\s+(\d+)u?", header
            )
        )
        return "%d.%d" % (found["major"], found["minor"])
    except Exception:
        return "unknown"


def check_kernel(role, tag, kernel):
    """Validate one driver/kernel axis against the exact upstream release."""
    raw = http_get(RELEASE_API.format(repo=REPO, tag=tag))
    try:
        notes = json.loads(raw).get("body") or ""
    except ValueError:
        raise Unknown("release %s returned unreadable metadata" % tag) from None
    low, high = parse_kernel_range(notes)
    series = parse_series(kernel)
    ok = low <= series <= high
    return {
        "role": role,
        "driver": tag,
        "kernel": kernel,
        "series": fmt(series),
        "range": "%s-%s" % (fmt(low), fmt(high)),
        "ok": ok,
        "detail": ""
        if ok
        else "kernel %s is outside the release's supported range %s-%s"
        % (fmt(series), fmt(low), fmt(high)),
    }


def check_abi(host_tag, guest_tag):
    host = parse_iov_abi(http_get(RAW.format(repo=REPO, tag=host_tag, path=IOV_ABI_PATH)))
    guest = parse_iov_abi(http_get(RAW.format(repo=REPO, tag=guest_tag, path=IOV_ABI_PATH)))
    agreed, detail = negotiate_iov(host, guest)
    return {
        "host": "%s-%s" % (fmt(host["base"]), fmt(host["latest"])),
        "guest": "%s-%s" % (fmt(guest["base"]), fmt(guest["latest"])),
        "agreed": fmt(agreed) if agreed else "none",
        "ok": agreed is not None,
        "detail": detail,
    }


def mark(ok):
    return "✅" if ok else "❌"


def report(axes, abi, guc):
    lines = ["## i915 SR-IOV Compatibility", ""]
    for axis in axes:
        lines += [
            "%s:" % axis["role"].capitalize(),
            "- driver: `%s`" % axis["driver"],
            "- kernel: %s (%s)" % (axis["kernel"], axis["series"]),
            "- supported range: %s" % axis["range"],
            "- result: %s%s" % (mark(axis["ok"]), " " + axis["detail"] if axis["detail"] else ""),
            "",
        ]
    if abi:
        lines += [
            "PF ↔ VF:",
            "- host IOV ABI: %s" % abi["host"],
            "- guest IOV ABI: %s" % abi["guest"],
            "- common/negotiated ABI: %s" % abi["agreed"],
            "- result: %s%s" % (mark(abi["ok"]), " " + abi["detail"] if abi["detail"] else ""),
            "",
        ]
    if guc:
        lines += ["_GuC VF interface (informational): %s_" % guc, ""]
    ok = all(axis["ok"] for axis in axes) and (abi is None or abi["ok"])
    lines.append("Overall: %s %s" % (mark(ok), "Compatible" if ok else "Incompatible"))
    return ok, "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host-version")
    parser.add_argument("--host-kernel")
    parser.add_argument("--guest-version")
    parser.add_argument("--guest-kernel")
    parser.add_argument("--markdown", help="also write the report to this file")
    args = parser.parse_args(argv)

    host = bool(args.host_version and args.host_kernel)
    guest = bool(args.guest_version and args.guest_kernel)
    if not host and not guest:
        parser.error("give --host-version/--host-kernel and/or --guest-version/--guest-kernel")

    try:
        axes = []
        if host:
            axes.append(check_kernel("host", args.host_version, args.host_kernel))
        if guest:
            axes.append(check_kernel("guest", args.guest_version, args.guest_kernel))
        abi = check_abi(args.host_version, args.guest_version) if host and guest else None
        guc = None
        if host and guest:
            guc = "host %s, guest %s" % (
                guc_latest(args.host_version),
                guc_latest(args.guest_version),
            )
        ok, text = report(axes, abi, guc)
    except Unknown as err:
        text = (
            "## i915 SR-IOV Compatibility\n\n"
            "Overall: %s Cannot be established — failing closed.\n\n- %s\n" % (mark(False), err)
        )
        ok, code = False, EXIT_UNKNOWN
    else:
        code = EXIT_OK if ok else EXIT_INCOMPATIBLE

    sys.stdout.write(text)
    if args.markdown:
        with open(args.markdown, "w", encoding="utf-8") as handle:
            handle.write(text)
    return code


if __name__ == "__main__":
    sys.exit(main())

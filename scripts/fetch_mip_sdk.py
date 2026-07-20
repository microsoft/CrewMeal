"""Fetch and stage the Microsoft Information Protection (MIP) File SDK native libraries.

CrewMeal decrypts MIP-protected documents by shelling out to a MIP SDK CLI (see
:mod:`crewmeal.search_enhancement.mip_sdk`). The CLI depends on the MIP File
SDK's native libraries, which Microsoft distributes as a NuGet package. Those
binaries are **not** committed to this repository; they are fetched at build/
deploy time by this script and pinned by version + SHA-256.

Usage (all values may also come from the environment)::

    python scripts/fetch_mip_sdk.py \
        --version 1.14.107 \
        --sha256 <hex digest of the .nupkg> \
        --runtime linux-x64 \
        --dest /opt/mip/lib

Environment fallbacks:
    CREWMEAL_MIP_SDK_VERSION   package version to fetch
    CREWMEAL_MIP_SDK_URL       full override URL for the .nupkg (skips the NuGet template)
    CREWMEAL_MIP_SDK_SHA256    expected SHA-256 of the downloaded .nupkg
    CREWMEAL_MIP_SDK_RUNTIME   NuGet runtime id (e.g. linux-x64, win-x64, osx-x64)
    CREWMEAL_MIP_SDK_LIB_DIR   destination directory for the native libraries

The destination directory is what you then point ``CREWMEAL_MIP_SDK_LIB_DIR`` at
so the runner adds it to ``PATH``/``LD_LIBRARY_PATH``. The CLI entrypoint itself
(``CREWMEAL_MIP_SDK_CLI``) is a deployment-provided thin wrapper around the SDK
and is out of scope for this fetcher.

.. important::

   The MIP SDK is licensed by Microsoft under its own terms (the Microsoft
   Software License Terms / EULA that accompany the SDK). By running this script
   you are downloading Microsoft-licensed binaries; review and accept those
   terms for your deployment. This script does not grant any license.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import sys
import zipfile
from pathlib import Path

import requests

PACKAGE_NAME = "Microsoft.InformationProtection.File"
NUGET_URL_TEMPLATE = "https://www.nuget.org/api/v2/package/{package}/{version}"
DEFAULT_RUNTIME = "linux-x64"
DOWNLOAD_TIMEOUT_SECONDS = 300

LICENSE_NOTICE = (
    "NOTE: The Microsoft Information Protection SDK is licensed by Microsoft "
    "under its own Software License Terms. These binaries are Microsoft "
    "property; review and accept the SDK EULA for your deployment. This script "
    "grants no license."
)


class FetchError(RuntimeError):
    """A recoverable failure while fetching or staging the SDK."""


def resolve_source(args: argparse.Namespace) -> tuple[str, str]:
    """Return the (url, version-label) to download."""

    url_override = args.url or os.getenv("CREWMEAL_MIP_SDK_URL", "").strip()
    version = args.version or os.getenv("CREWMEAL_MIP_SDK_VERSION", "").strip()
    if url_override:
        return url_override, version or "custom-url"
    if not version:
        raise FetchError(
            "No version specified. Pass --version (or set CREWMEAL_MIP_SDK_VERSION), "
            "or provide a full --url."
        )
    return (
        NUGET_URL_TEMPLATE.format(package=PACKAGE_NAME, version=version),
        version,
    )


def download(url: str) -> bytes:
    print(f"Downloading MIP File SDK from {url} ...", file=sys.stderr)
    response = requests.get(url, timeout=DOWNLOAD_TIMEOUT_SECONDS)
    if response.status_code != 200:
        raise FetchError(
            f"Download failed: HTTP {response.status_code} for {url}"
        )
    return response.content


def verify_checksum(data: bytes, expected: str | None, *, allow_unverified: bool) -> None:
    digest = hashlib.sha256(data).hexdigest()
    if not expected:
        if allow_unverified:
            print(
                f"WARNING: no --sha256 provided; skipping integrity check. "
                f"Computed sha256={digest}",
                file=sys.stderr,
            )
            return
        raise FetchError(
            "No expected SHA-256 provided. Pass --sha256 (or set "
            "CREWMEAL_MIP_SDK_SHA256) to pin the artifact, or pass "
            f"--allow-unverified to bypass. Computed sha256={digest}"
        )
    if digest.lower() != expected.strip().lower():
        raise FetchError(
            f"Checksum mismatch: expected {expected}, computed {digest}. "
            "Refusing to stage an unverified artifact."
        )
    print(f"Checksum OK (sha256={digest}).", file=sys.stderr)


def extract_native(nupkg: bytes, runtime: str, dest: Path) -> list[Path]:
    """Extract ``runtimes/<runtime>/native/*`` from the .nupkg into ``dest``."""

    prefix = f"runtimes/{runtime}/native/"
    written: list[Path] = []
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(nupkg)) as archive:
        members = [
            name
            for name in archive.namelist()
            if name.startswith(prefix) and not name.endswith("/")
        ]
        if not members:
            available = sorted(
                {
                    name.split("/")[1]
                    for name in archive.namelist()
                    if name.startswith("runtimes/") and "/" in name[len("runtimes/") :]
                }
            )
            raise FetchError(
                f"No native libraries found for runtime {runtime!r}. "
                f"Available runtimes in package: {', '.join(available) or '<none>'}"
            )
        for name in members:
            target = dest / Path(name).name
            target.write_bytes(archive.read(name))
            written.append(target)
    return written


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--version", default=None, help="MIP File SDK NuGet version to fetch.")
    parser.add_argument("--url", default=None, help="Full override URL for the .nupkg.")
    parser.add_argument(
        "--sha256",
        default=None,
        help="Expected SHA-256 of the downloaded .nupkg (pins the artifact).",
    )
    parser.add_argument(
        "--runtime",
        default=os.getenv("CREWMEAL_MIP_SDK_RUNTIME", "").strip() or DEFAULT_RUNTIME,
        help=f"NuGet runtime id to extract (default: {DEFAULT_RUNTIME}).",
    )
    parser.add_argument(
        "--dest",
        default=os.getenv("CREWMEAL_MIP_SDK_LIB_DIR", "").strip() or None,
        help="Destination directory for native libraries (CREWMEAL_MIP_SDK_LIB_DIR).",
    )
    parser.add_argument(
        "--allow-unverified",
        action="store_true",
        help="Proceed without a checksum (NOT recommended).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(LICENSE_NOTICE, file=sys.stderr)
    try:
        if not args.dest:
            raise FetchError(
                "No destination directory. Pass --dest or set CREWMEAL_MIP_SDK_LIB_DIR."
            )
        url, version = resolve_source(args)
        data = download(url)
        expected = args.sha256 or os.getenv("CREWMEAL_MIP_SDK_SHA256", "").strip() or None
        verify_checksum(data, expected, allow_unverified=args.allow_unverified)
        written = extract_native(data, args.runtime, Path(args.dest))
    except FetchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"ERROR: network failure: {exc}", file=sys.stderr)
        return 1

    print(
        f"Staged {len(written)} MIP SDK native file(s) for {PACKAGE_NAME} "
        f"{version} ({args.runtime}) into {args.dest}:",
        file=sys.stderr,
    )
    for path in written:
        print(f"  {path.name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

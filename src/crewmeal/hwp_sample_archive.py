from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = REPOSITORY_ROOT / "result" / "hwp-sample-archive"
HWP5_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
HWP_LEGACY_MAGIC = b"HWP Document File"
MAX_ARCHIVE_BYTES = 5 * 1024 * 1024 * 1024
MAX_SAMPLE_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class RepositorySource:
    repository: str
    commit: str
    version: str
    license: str
    provenance: str


SOURCES: tuple[RepositorySource, ...] = (
    RepositorySource(
        repository="chrisryugj/kordoc",
        commit="0794f162ca028a252a76f9e579eb6d107f15398d",
        version="v4.2.3",
        license="MIT",
        provenance="All tracked HWP/HWPX occurrences at the kordoc v4.2.3 tag.",
    ),
    RepositorySource(
        repository="edwardkim/rhwp",
        commit="8d3bfa4b92174b16bac587fe1409975cf34ba566",
        version="pinned commit",
        license="MIT",
        provenance="All tracked HWP/HWPX occurrences in the rhwp repository.",
    ),
    RepositorySource(
        repository="KimDaehyeon6873/hwp-hwpx-parser",
        commit="339d1290ed46f90dc5d02a72622eb21df4b8925c",
        version="v1.0.0-era commit",
        license="Apache-2.0",
        provenance="All tracked HWP/HWPX occurrences in parser tests and examples.",
    ),
    RepositorySource(
        repository="mete0r/pyhwp",
        commit="83239f0d3bdf438b2c9f7dcff455a6e841154a39",
        version="0.1b15-era commit",
        license="AGPL-3.0",
        provenance="All tracked HWP/HWPX occurrences in the pyhwp repository.",
    ),
    RepositorySource(
        repository="sxa-lab/openhanji",
        commit="e3ea65dad5fd779917be6bdc63dcdfe04d08e60e",
        version="v0.1.0-era commit",
        license="Apache-2.0",
        provenance="All tracked HWP/HWPX occurrences in OpenHanji tests.",
    ),
    RepositorySource(
        repository="airmang/python-hwpx",
        commit="1bf38e74a965ee940cf9aa69882f02cf891c78bc",
        version="v3.4.1-era commit",
        license="Apache-2.0",
        provenance="All tracked HWP/HWPX occurrences, including attributed corpora.",
    ),
)


class SampleArchiveError(RuntimeError):
    """Raised when the complete sample archive cannot be verified."""


def _github_api_json(endpoint: str) -> Mapping[str, Any]:
    gh = shutil.which("gh")
    if gh:
        completed = subprocess.run(
            [gh, "api", endpoint],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            shell=False,
        )
        if completed.returncode != 0:
            raise SampleArchiveError(
                f"GitHub API failed for {endpoint}: {completed.stderr.strip()[-2000:]}"
            )
        payload = json.loads(completed.stdout)
    else:
        request = urllib.request.Request(
            f"https://api.github.com/{endpoint}",
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "CrewMeal-HWP-Sample-Collector/1",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=180) as response:
            payload = json.load(response)
    if not isinstance(payload, dict):
        raise SampleArchiveError(f"Unexpected GitHub API response for {endpoint}.")
    return payload


def enumerate_repository(source: RepositorySource) -> tuple[dict[str, Any], ...]:
    endpoint = (
        f"repos/{source.repository}/git/trees/{source.commit}?recursive=1"
    )
    payload = _github_api_json(endpoint)
    if payload.get("truncated") is not False:
        raise SampleArchiveError(
            f"Recursive Git tree was truncated for {source.repository}."
        )
    tree = payload.get("tree")
    if not isinstance(tree, list):
        raise SampleArchiveError(
            f"Git tree is missing for {source.repository}@{source.commit}."
        )
    entries: list[dict[str, Any]] = []
    for item in tree:
        if not isinstance(item, dict) or item.get("type") != "blob":
            continue
        path = item.get("path")
        blob_sha = item.get("sha")
        size = item.get("size")
        if not isinstance(path, str) or Path(path).suffix.lower() not in {
            ".hwp",
            ".hwpx",
        }:
            continue
        if not isinstance(blob_sha, str) or not re.fullmatch(
            r"[0-9a-f]{40}", blob_sha
        ):
            raise SampleArchiveError(
                f"Invalid Git blob SHA for {source.repository}:{path}."
            )
        entries.append(
            {
                "repository": source.repository,
                "commit": source.commit,
                "path": path,
                "git_blob_sha": blob_sha,
                "git_blob_bytes": int(size) if isinstance(size, int) else None,
            }
        )
    return tuple(sorted(entries, key=lambda entry: str(entry["path"]).casefold()))


def _download(
    url: str,
    destination: Path,
    *,
    max_bytes: int,
    attempts: int = 4,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(f"{destination.suffix}.part")
    last_error: Exception | None = None
    for attempt in range(attempts):
        temporary.unlink(missing_ok=True)
        downloaded = 0
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "CrewMeal-HWP-Sample-Collector/1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > max_bytes:
                    raise SampleArchiveError(
                        f"Download is larger than {max_bytes} bytes: {url}"
                    )
                with temporary.open("wb") as output:
                    while chunk := response.read(1024 * 1024):
                        downloaded += len(chunk)
                        if downloaded > max_bytes:
                            raise SampleArchiveError(
                                f"Download exceeded {max_bytes} bytes: {url}"
                            )
                        output.write(chunk)
            temporary.replace(destination)
            return
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as error:
            last_error = error
            if isinstance(error, urllib.error.HTTPError) and error.code < 500 and error.code != 429:
                break
            time.sleep(2**attempt)
        finally:
            temporary.unlink(missing_ok=True)
    raise SampleArchiveError(f"Download failed after {attempts} attempts: {url}") from last_error


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _git_blob_sha(path: Path) -> str:
    size = path.stat().st_size
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(f"blob {size}\0".encode("ascii"))
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _lfs_pointer(path: Path) -> tuple[str, int] | None:
    if path.stat().st_size > 2048:
        return None
    content = path.read_text(encoding="utf-8", errors="replace")
    if not content.startswith("version https://git-lfs.github.com/spec/v1"):
        return None
    oid_match = re.search(r"^oid sha256:([0-9a-f]{64})$", content, re.MULTILINE)
    size_match = re.search(r"^size (\d+)$", content, re.MULTILINE)
    if oid_match is None or size_match is None:
        raise SampleArchiveError(f"Malformed Git LFS pointer: {path}")
    return oid_match.group(1), int(size_match.group(1))


def classify_document(path: Path) -> tuple[str, bool, str | None]:
    suffix = path.suffix.lower()
    with path.open("rb") as stream:
        magic = stream.read(32)
    if suffix == ".hwp":
        if magic.startswith(HWP5_MAGIC):
            return "hwp5-ole", True, None
        if magic.startswith(HWP_LEGACY_MAGIC):
            return "hwp-legacy", True, "Legacy HWP binary; not HWP5 OLE."
        if zipfile.is_zipfile(path):
            return (
                "extension-mismatch-hwpx",
                False,
                "The .hwp extension contains a ZIP/HWPX-style package.",
            )
        return "hwp-unknown", False, f"Unexpected HWP magic: {magic[:8].hex()}"
    if magic.startswith(HWP5_MAGIC):
        return (
            "extension-mismatch-hwp5",
            False,
            "The .hwpx extension contains an HWP5 OLE document.",
        )
    if not zipfile.is_zipfile(path):
        return "hwpx-invalid", False, f"Unexpected HWPX magic: {magic[:8].hex()}"
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            has_section = any(
                name.startswith("Contents/section") and name.endswith(".xml")
                for name in names
            )
            has_mimetype = "mimetype" in names
    except zipfile.BadZipFile as error:
        return "hwpx-invalid", False, str(error)
    if has_section and has_mimetype:
        return "hwpx-zip", True, None
    missing = [
        label
        for label, present in (
            ("mimetype", has_mimetype),
            ("Contents/section*.xml", has_section),
        )
        if not present
    ]
    return "hwpx-partial", False, f"Missing package parts: {', '.join(missing)}"


def _safe_destination(root: Path, relative_path: str) -> Path:
    destination = (root / Path(relative_path)).resolve()
    root_resolved = root.resolve()
    if root_resolved != destination and root_resolved not in destination.parents:
        raise SampleArchiveError(f"Unsafe repository path: {relative_path}")
    return destination


def _raw_url(source: RepositorySource, path: str) -> str:
    return (
        f"https://raw.githubusercontent.com/{source.repository}/"
        f"{source.commit}/{quote(path, safe='/')}"
    )


def _source_url(source: RepositorySource, path: str) -> str:
    return (
        f"https://github.com/{source.repository}/blob/"
        f"{source.commit}/{quote(path, safe='/')}"
    )


def _record_for_file(
    source: RepositorySource,
    entry: Mapping[str, Any],
    destination: Path,
    output_dir: Path,
    *,
    lfs_oid: str | None,
) -> dict[str, Any]:
    classification, valid, validation_note = classify_document(destination)
    return {
        "repository": source.repository,
        "commit": source.commit,
        "version": source.version,
        "license": source.license,
        "path": entry["path"],
        "format": destination.suffix.lower().lstrip("."),
        "bytes": destination.stat().st_size,
        "sha256": _sha256_file(destination),
        "git_blob_sha": entry["git_blob_sha"],
        "git_lfs_oid": lfs_oid,
        "classification": classification,
        "valid": valid,
        "validation_note": validation_note,
        "relative_path": destination.relative_to(output_dir).as_posix(),
        "source_url": _source_url(source, str(entry["path"])),
        "raw_url": _raw_url(source, str(entry["path"])),
        "duplicate_count": 1,
        "unique_relative_path": None,
    }


def _cached_record(
    previous: Mapping[str, Any] | None,
    source: RepositorySource,
    entry: Mapping[str, Any],
    destination: Path,
) -> dict[str, Any] | None:
    if previous is None or not destination.is_file():
        return None
    if (
        previous.get("repository") != source.repository
        or previous.get("commit") != source.commit
        or previous.get("path") != entry["path"]
        or previous.get("git_blob_sha") != entry["git_blob_sha"]
        or previous.get("bytes") != destination.stat().st_size
    ):
        return None
    expected_sha256 = previous.get("sha256")
    if not isinstance(expected_sha256, str) or _sha256_file(destination) != expected_sha256:
        return None
    record = dict(previous)
    classification, valid, validation_note = classify_document(destination)
    record.update(
        {
            "classification": classification,
            "valid": valid,
            "validation_note": validation_note,
        }
    )
    return record


def _load_previous_records(output_dir: Path) -> dict[tuple[str, str], Mapping[str, Any]]:
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.is_file():
        return {}
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return {}
    return {
        (str(record["repository"]), str(record["path"])): record
        for record in records
        if isinstance(record, dict)
        and isinstance(record.get("repository"), str)
        and isinstance(record.get("path"), str)
    }


def collect_repository(
    source: RepositorySource,
    entries: Sequence[Mapping[str, Any]],
    output_dir: Path,
    previous_records: Mapping[tuple[str, str], Mapping[str, Any]],
) -> list[dict[str, Any]]:
    repository_dir = (
        output_dir
        / "by-repository"
        / source.repository.replace("/", "__")
    )
    records_by_path: dict[str, dict[str, Any]] = {}
    missing_entries: list[Mapping[str, Any]] = []
    for entry in entries:
        destination = _safe_destination(repository_dir, str(entry["path"]))
        cached = _cached_record(
            previous_records.get((source.repository, str(entry["path"]))),
            source,
            entry,
            destination,
        )
        if cached is None:
            missing_entries.append(entry)
        else:
            records_by_path[str(entry["path"])] = cached

    if missing_entries:
        downloads_dir = output_dir / ".downloads"
        archive_path = downloads_dir / (
            source.repository.replace("/", "__") + f"-{source.commit}.zip"
        )
        if not archive_path.is_file():
            print(
                f"Downloading {source.repository}@{source.commit[:8]} "
                f"({len(entries)} HWP/HWPX files)...",
                flush=True,
            )
            _download(
                f"https://codeload.github.com/{source.repository}/zip/{source.commit}",
                archive_path,
                max_bytes=MAX_ARCHIVE_BYTES,
            )
        with zipfile.ZipFile(archive_path) as archive:
            members = {
                member.filename.split("/", 1)[1]: member
                for member in archive.infolist()
                if "/" in member.filename and not member.is_dir()
            }
            for index, entry in enumerate(missing_entries, start=1):
                path = str(entry["path"])
                member = members.get(path)
                if member is None:
                    raise SampleArchiveError(
                        f"{source.repository} archive is missing {path}."
                    )
                destination = _safe_destination(repository_dir, path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_suffix(f"{destination.suffix}.part")
                temporary.unlink(missing_ok=True)
                try:
                    with archive.open(member) as input_stream:
                        with temporary.open("wb") as output_stream:
                            shutil.copyfileobj(input_stream, output_stream, 1024 * 1024)
                    temporary.replace(destination)
                finally:
                    temporary.unlink(missing_ok=True)
                actual_git_sha = _git_blob_sha(destination)
                if actual_git_sha != entry["git_blob_sha"]:
                    raise SampleArchiveError(
                        f"Git blob mismatch for {source.repository}:{path}: "
                        f"expected {entry['git_blob_sha']}, got {actual_git_sha}."
                    )
                lfs = _lfs_pointer(destination)
                lfs_oid: str | None = None
                if lfs is not None:
                    lfs_oid, lfs_size = lfs
                    if lfs_size > MAX_SAMPLE_BYTES:
                        raise SampleArchiveError(
                            f"Git LFS sample exceeds {MAX_SAMPLE_BYTES} bytes: {path}"
                        )
                    _download(
                        _raw_url(source, path),
                        destination,
                        max_bytes=lfs_size,
                    )
                    if destination.stat().st_size != lfs_size:
                        raise SampleArchiveError(
                            f"Git LFS size mismatch for {source.repository}:{path}."
                        )
                    if _sha256_file(destination) != lfs_oid:
                        raise SampleArchiveError(
                            f"Git LFS SHA-256 mismatch for {source.repository}:{path}."
                        )
                records_by_path[path] = _record_for_file(
                    source,
                    entry,
                    destination,
                    output_dir,
                    lfs_oid=lfs_oid,
                )
                if index % 100 == 0:
                    print(
                        f"  {source.repository}: {index}/{len(missing_entries)} extracted",
                        flush=True,
                    )
        archive_path.unlink(missing_ok=True)
    print(
        f"Verified {source.repository}: {len(entries)} files "
        f"({len(entries) - len(missing_entries)} cached).",
        flush=True,
    )
    return [records_by_path[str(entry["path"])] for entry in entries]


def _safe_unique_name(sha256: str, filename: str) -> str:
    sanitized = re.sub(r'[<>:"/\\|?*]+', "_", filename).strip(" .")
    if not sanitized:
        sanitized = "sample"
    stem = Path(sanitized).stem[:100]
    suffix = Path(sanitized).suffix.lower()
    return f"{sha256[:16]}--{stem}{suffix}"


def build_unique_view(records: list[dict[str, Any]], output_dir: Path) -> None:
    unique_dir = output_dir / "unique"
    if unique_dir.exists():
        shutil.rmtree(unique_dir)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        groups[str(record["sha256"])].append(record)
    for sha256, group in sorted(groups.items()):
        canonical = min(
            group,
            key=lambda record: (
                str(record["repository"]).casefold(),
                str(record["path"]).casefold(),
            ),
        )
        source_path = output_dir / str(canonical["relative_path"])
        format_dir = unique_dir / str(canonical["format"])
        format_dir.mkdir(parents=True, exist_ok=True)
        unique_path = format_dir / _safe_unique_name(sha256, source_path.name)
        try:
            os.link(source_path, unique_path)
        except OSError:
            shutil.copy2(source_path, unique_path)
        relative_unique = unique_path.relative_to(output_dir).as_posix()
        for record in group:
            record["duplicate_count"] = len(group)
            record["unique_relative_path"] = relative_unique


def _summary(
    records: Sequence[Mapping[str, Any]],
    sources: Sequence[RepositorySource],
) -> dict[str, Any]:
    sha_sizes: dict[str, int] = {}
    for record in records:
        sha_sizes.setdefault(str(record["sha256"]), int(record["bytes"]))
    return {
        "occurrences": len(records),
        "unique_binaries": len(sha_sizes),
        "duplicate_occurrences": len(records) - len(sha_sizes),
        "occurrence_bytes": sum(int(record["bytes"]) for record in records),
        "unique_bytes": sum(sha_sizes.values()),
        "formats": dict(Counter(str(record["format"]) for record in records)),
        "classifications": dict(
            Counter(str(record["classification"]) for record in records)
        ),
        "repositories": {
            source.repository: sum(
                record["repository"] == source.repository for record in records
            )
            for source in sources
        },
        "valid": sum(bool(record["valid"]) for record in records),
        "invalid_or_partial": sum(not bool(record["valid"]) for record in records),
        "git_lfs": sum(record.get("git_lfs_oid") is not None for record in records),
    }


def _report_html(payload: Mapping[str, Any]) -> str:
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace(
        "</", "<\\/"
    )
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>HWP/HWPX 전체 샘플 아카이브</title>
  <link rel="icon" href="data:,">
  <script>
  (() => {{
    const param = new URLSearchParams(window.location.search).get("scoutTheme");
    const theme =
      param || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    document.documentElement.setAttribute("data-theme", theme);
  }})();
  </script>
  <style>
  :root {{
    color-scheme: light;
    --cp-bg: #f7f4ef;
    --cp-bg-elevated: #fcfbf8;
    --cp-surface: #ffffff;
    --cp-surface-soft: #f5f5f5;
    --cp-border: #dedede;
    --cp-border-strong: #919191;
    --cp-text: #242424;
    --cp-text-muted: #5c5c5c;
    --cp-text-soft: #6f6f6f;
    --cp-accent: #b11f4b;
    --cp-accent-hover: #9a1a41;
    --cp-accent-soft: rgba(177, 31, 75, 0.08);
    --cp-accent-fg: #ffffff;
    --cp-success: #16a34a;
    --cp-danger: #dc2626;
    --cp-warning: #f59e0b;
    --cp-link: #0078d4;
    --cp-shadow: 0 18px 48px rgba(0, 0, 0, 0.12);
    --cp-overlay: rgba(255, 255, 255, 0.8);
    --cp-panel: rgba(255, 255, 255, 0.86);
    --cp-panel-strong: rgba(255, 255, 255, 0.96);
    --cp-sheen: rgba(255, 255, 255, 0.55);
    --cp-highlight: rgba(177, 31, 75, 0.12);
  }}
  html[data-theme="dark"] {{
    color-scheme: dark;
    --cp-bg: #3d3b3a;
    --cp-bg-elevated: #343231;
    --cp-surface: #292929;
    --cp-surface-soft: #2e2e2e;
    --cp-border: #474747;
    --cp-border-strong: #5f5f5f;
    --cp-text: #dedede;
    --cp-text-muted: #919191;
    --cp-text-soft: #b0b0b0;
    --cp-accent: #fd8ea1;
    --cp-accent-hover: #fb7b91;
    --cp-accent-soft: rgba(253, 142, 161, 0.14);
    --cp-accent-fg: #1a1a1a;
    --cp-success: #4ade80;
    --cp-danger: #f87171;
    --cp-warning: #fbbf24;
    --cp-link: #4da6ff;
    --cp-shadow: 0 18px 48px rgba(0, 0, 0, 0.32);
    --cp-overlay: rgba(41, 41, 41, 0.88);
    --cp-panel: rgba(41, 41, 41, 0.72);
    --cp-panel-strong: rgba(41, 41, 41, 0.96);
    --cp-sheen: rgba(255, 255, 255, 0.04);
    --cp-highlight: rgba(253, 142, 161, 0.12);
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--cp-bg);
    color: var(--cp-text);
    font-family: "Segoe UI", Aptos, Calibri, -apple-system, BlinkMacSystemFont, sans-serif;
    line-height: 1.5;
  }}
  a {{ color: var(--cp-link); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  code {{
    font-family: Consolas, "Courier New", Courier, monospace;
    font-size: 0.86em;
  }}
  header {{
    border-bottom: 1px solid var(--cp-border);
    background: var(--cp-bg-elevated);
  }}
  .header-inner, main {{
    width: min(1500px, calc(100% - 32px));
    margin: 0 auto;
  }}
  .header-inner {{ padding: 32px 0 28px; }}
  h1 {{ margin: 0 0 8px; font-size: clamp(1.75rem, 4vw, 2.6rem); letter-spacing: -0.03em; }}
  h2 {{ margin: 0 0 16px; font-size: 1.25rem; }}
  p {{ margin: 0; }}
  .subtitle {{ color: var(--cp-text-muted); max-width: 900px; }}
  main {{ padding: 28px 0 48px; }}
  .stats {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
    gap: 12px;
    margin-bottom: 24px;
  }}
  .card {{
    background: var(--cp-surface);
    border: 1px solid var(--cp-border);
    border-radius: 16px;
    padding: 16px;
  }}
  .stat-label {{ color: var(--cp-text-muted); font-size: 0.82rem; }}
  .stat-value {{ display: block; margin-top: 4px; font-size: 1.55rem; font-weight: 700; }}
  .panel {{
    background: var(--cp-surface);
    border: 1px solid var(--cp-border);
    border-radius: 16px;
    padding: 20px;
    margin-bottom: 20px;
  }}
  .controls {{
    display: grid;
    grid-template-columns: minmax(240px, 2fr) repeat(3, minmax(140px, 1fr));
    gap: 12px;
  }}
  input, select, button {{
    width: 100%;
    min-height: 40px;
    border: 1px solid var(--cp-border-strong);
    border-radius: 0.625rem;
    background: var(--cp-surface);
    color: var(--cp-text);
    font: inherit;
    padding: 8px 12px;
  }}
  input:focus, select:focus, button:focus {{
    outline: 2px solid var(--cp-accent);
    outline-offset: 2px;
  }}
  button {{
    cursor: pointer;
    background: var(--cp-accent);
    color: var(--cp-accent-fg);
    border-color: var(--cp-accent);
    font-weight: 600;
  }}
  button:hover {{ background: var(--cp-accent-hover); }}
  .toggles {{ display: flex; flex-wrap: wrap; gap: 16px; margin-top: 14px; }}
  .toggle {{ display: inline-flex; align-items: center; gap: 8px; color: var(--cp-text-muted); }}
  .toggle input {{ width: auto; min-height: auto; accent-color: var(--cp-accent); }}
  .result-count {{ margin-top: 14px; color: var(--cp-text-muted); }}
  .table-wrap {{
    overflow: auto;
    border: 1px solid var(--cp-border);
    border-radius: 0.625rem;
  }}
  table {{ width: 100%; border-collapse: collapse; min-width: 1050px; }}
  th, td {{ padding: 10px 12px; border-bottom: 1px solid var(--cp-border); text-align: left; vertical-align: top; }}
  th {{
    position: sticky;
    top: 0;
    background: var(--cp-panel-strong);
    color: var(--cp-text-muted);
    font-size: 0.78rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    z-index: 1;
  }}
  tbody tr:hover {{ background: var(--cp-accent-soft); }}
  tbody tr:last-child td {{ border-bottom: 0; }}
  .path-cell {{ min-width: 340px; max-width: 580px; overflow-wrap: anywhere; }}
  .repo-cell {{ white-space: nowrap; }}
  .badge {{
    display: inline-flex;
    align-items: center;
    border: 1px solid var(--cp-border);
    border-radius: 0.625rem;
    background: var(--cp-surface-soft);
    padding: 2px 8px;
    font-size: 0.78rem;
    white-space: nowrap;
  }}
  .valid {{ color: var(--cp-success); }}
  .invalid {{ color: var(--cp-danger); }}
  .duplicate {{ color: var(--cp-accent); font-weight: 650; }}
  .muted {{ color: var(--cp-text-muted); }}
  .mono {{ font-family: Consolas, "Courier New", Courier, monospace; font-size: 0.82rem; }}
  .source-grid {{ display: grid; gap: 10px; }}
  .source-row {{
    display: grid;
    grid-template-columns: minmax(220px, 1fr) 120px 130px minmax(280px, 2fr);
    gap: 12px;
    padding: 10px 0;
    border-bottom: 1px solid var(--cp-border);
  }}
  .source-row:last-child {{ border-bottom: 0; }}
  .notice {{
    border-left: 4px solid var(--cp-warning);
    padding-left: 12px;
    color: var(--cp-text-muted);
  }}
  @media (max-width: 900px) {{
    .controls {{ grid-template-columns: 1fr 1fr; }}
    .source-row {{ grid-template-columns: 1fr; gap: 4px; }}
  }}
  @media (max-width: 560px) {{
    .header-inner, main {{ width: min(100% - 20px, 1500px); }}
    .controls {{ grid-template-columns: 1fr; }}
  }}
  </style>
</head>
<body>
  <header>
    <div class="header-inner">
      <h1>HWP/HWPX 전체 샘플 아카이브</h1>
      <p class="subtitle">6개 오픈소스 파서 저장소의 고정 커밋에서 추적 중인 모든 .hwp/.hwpx 파일을 원래 경로 그대로 수집하고, SHA-256 기준 중복 제거본을 함께 보관했습니다.</p>
    </div>
  </header>
  <main>
    <section class="stats" id="stats"></section>
    <section class="panel">
      <h2>파일 찾기</h2>
      <div class="controls">
        <input id="query" type="search" placeholder="저장소, 경로, SHA-256 검색" aria-label="파일 검색">
        <select id="repository" aria-label="저장소"></select>
        <select id="format" aria-label="형식"></select>
        <select id="classification" aria-label="분류"></select>
      </div>
      <div class="toggles">
        <label class="toggle"><input id="duplicates" type="checkbox"> 중복 파일만</label>
        <label class="toggle"><input id="invalid" type="checkbox"> 검증 실패·부분 패키지만</label>
      </div>
      <p class="result-count" id="result-count"></p>
    </section>
    <section class="panel">
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>형식</th>
              <th>검증</th>
              <th>크기</th>
              <th>저장소</th>
              <th>원본 경로 / 저장 파일</th>
              <th>SHA-256</th>
              <th>중복</th>
            </tr>
          </thead>
          <tbody id="rows"></tbody>
        </table>
      </div>
    </section>
    <section class="panel">
      <h2>출처와 이용 조건</h2>
      <div class="source-grid" id="sources"></div>
      <p class="notice">표시된 라이선스는 저장소 수준 정보입니다. 저장소 라이선스가 제3자 샘플 문서의 권리를 자동으로 재허여하지는 않습니다. 특히 공개기관 문서는 원 출처 조건을 함께 확인해야 합니다.</p>
    </section>
  </main>
  <script id="inventory-data" type="application/json">{data_json}</script>
  <script>
  const payload = JSON.parse(document.getElementById("inventory-data").textContent);
  const records = payload.records;
  const summary = payload.summary;
  const filters = {{
    query: document.getElementById("query"),
    repository: document.getElementById("repository"),
    format: document.getElementById("format"),
    classification: document.getElementById("classification"),
    duplicates: document.getElementById("duplicates"),
    invalid: document.getElementById("invalid"),
  }};

  function fileSize(bytes) {{
    const units = ["B", "KB", "MB", "GB"];
    let value = Number(bytes);
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {{
      value /= 1024;
      unit += 1;
    }}
    return `${{value.toFixed(unit === 0 ? 0 : 1)}} ${{units[unit]}}`;
  }}

  function pathHref(path) {{
    return path.split("/").map(encodeURIComponent).join("/");
  }}

  function option(select, value, label) {{
    const node = document.createElement("option");
    node.value = value;
    node.textContent = label;
    select.appendChild(node);
  }}

  function fillSelect(select, values, allLabel) {{
    option(select, "", allLabel);
    [...new Set(values)].sort((left, right) => left.localeCompare(right)).forEach((value) => option(select, value, value));
  }}

  fillSelect(filters.repository, records.map((record) => record.repository), "모든 저장소");
  fillSelect(filters.format, records.map((record) => record.format.toUpperCase()), "모든 형식");
  fillSelect(filters.classification, records.map((record) => record.classification), "모든 검증 분류");

  const statItems = [
    ["전체 파일", summary.occurrences.toLocaleString()],
    ["고유 바이너리", summary.unique_binaries.toLocaleString()],
    ["중복 발생", summary.duplicate_occurrences.toLocaleString()],
    ["HWP / HWPX", `${{summary.formats.hwp || 0}} / ${{summary.formats.hwpx || 0}}`],
    ["전체 용량", fileSize(summary.occurrence_bytes)],
    ["중복 제거 용량", fileSize(summary.unique_bytes)],
  ];
  const stats = document.getElementById("stats");
  statItems.forEach(([label, value]) => {{
    const card = document.createElement("article");
    card.className = "card";
    const labelNode = document.createElement("span");
    labelNode.className = "stat-label";
    labelNode.textContent = label;
    const valueNode = document.createElement("strong");
    valueNode.className = "stat-value";
    valueNode.textContent = value;
    card.append(labelNode, valueNode);
    stats.appendChild(card);
  }});

  const sources = document.getElementById("sources");
  payload.sources.forEach((source) => {{
    const row = document.createElement("div");
    row.className = "source-row";
    const repository = document.createElement("a");
    repository.href = `https://github.com/${{source.repository}}/tree/${{source.commit}}`;
    repository.textContent = source.repository;
    const version = document.createElement("span");
    version.textContent = source.version;
    const license = document.createElement("span");
    license.textContent = source.license;
    const note = document.createElement("span");
    note.className = "muted";
    note.textContent = source.provenance;
    row.append(repository, version, license, note);
    sources.appendChild(row);
  }});

  function render() {{
    const query = filters.query.value.trim().toLocaleLowerCase();
    const repository = filters.repository.value;
    const format = filters.format.value.toLocaleLowerCase();
    const classification = filters.classification.value;
    const shown = records.filter((record) => {{
      const searchable = `${{record.repository}} ${{record.path}} ${{record.sha256}}`.toLocaleLowerCase();
      return (!query || searchable.includes(query))
        && (!repository || record.repository === repository)
        && (!format || record.format === format)
        && (!classification || record.classification === classification)
        && (!filters.duplicates.checked || record.duplicate_count > 1)
        && (!filters.invalid.checked || !record.valid);
    }});
    document.getElementById("result-count").textContent = `${{shown.length.toLocaleString()}} / ${{records.length.toLocaleString()}}개 표시`;
    const body = document.getElementById("rows");
    body.replaceChildren();
    const fragment = document.createDocumentFragment();
    shown.forEach((record) => {{
      const row = document.createElement("tr");
      const formatCell = document.createElement("td");
      const formatBadge = document.createElement("span");
      formatBadge.className = "badge";
      formatBadge.textContent = record.format.toUpperCase();
      formatCell.appendChild(formatBadge);

      const validationCell = document.createElement("td");
      const validation = document.createElement("span");
      validation.className = record.valid ? "valid" : "invalid";
      validation.textContent = record.classification;
      validation.title = record.validation_note || "";
      validationCell.appendChild(validation);

      const sizeCell = document.createElement("td");
      sizeCell.textContent = fileSize(record.bytes);
      sizeCell.className = "mono";

      const repoCell = document.createElement("td");
      repoCell.className = "repo-cell";
      const repoLink = document.createElement("a");
      repoLink.href = record.source_url;
      repoLink.textContent = record.repository;
      repoCell.appendChild(repoLink);

      const pathCell = document.createElement("td");
      pathCell.className = "path-cell";
      const fileLink = document.createElement("a");
      fileLink.href = pathHref(record.relative_path);
      fileLink.textContent = record.path;
      const separator = document.createElement("span");
      separator.className = "muted";
      separator.textContent = " · ";
      const uniqueLink = document.createElement("a");
      uniqueLink.href = pathHref(record.unique_relative_path);
      uniqueLink.textContent = "고유본";
      pathCell.append(fileLink, separator, uniqueLink);

      const hashCell = document.createElement("td");
      hashCell.className = "mono";
      hashCell.textContent = record.sha256.slice(0, 16);
      hashCell.title = record.sha256;

      const duplicateCell = document.createElement("td");
      duplicateCell.textContent = record.duplicate_count.toLocaleString();
      if (record.duplicate_count > 1) duplicateCell.className = "duplicate";
      row.append(formatCell, validationCell, sizeCell, repoCell, pathCell, hashCell, duplicateCell);
      fragment.appendChild(row);
    }});
    body.appendChild(fragment);
  }}

  Object.values(filters).forEach((control) => {{
    control.addEventListener(control.type === "search" ? "input" : "change", render);
  }});
  render();
  </script>
</body>
</html>
"""


def collect_samples(
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    *,
    sources: Sequence[RepositorySource] = SOURCES,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    previous_records = _load_previous_records(output_dir)
    records: list[dict[str, Any]] = []
    source_counts: dict[str, int] = {}
    for source in sources:
        entries = enumerate_repository(source)
        source_counts[source.repository] = len(entries)
        records.extend(
            collect_repository(
                source,
                entries,
                output_dir,
                previous_records,
            )
        )
    records.sort(
        key=lambda record: (
            str(record["repository"]).casefold(),
            str(record["path"]).casefold(),
        )
    )
    build_unique_view(records, output_dir)
    generated_at = datetime.now(timezone.utc).isoformat()
    summary = _summary(records, sources)
    payload = {
        "schema_version": 1,
        "generated_at": generated_at,
        "platform": platform.platform(),
        "sources": [asdict(source) for source in sources],
        "source_counts": source_counts,
        "summary": summary,
        "records": records,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "report.html").write_text(
        _report_html(payload),
        encoding="utf-8",
    )
    downloads_dir = output_dir / ".downloads"
    if downloads_dir.is_dir() and not any(downloads_dir.iterdir()):
        downloads_dir.rmdir()
    return payload


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Collect every tracked HWP/HWPX sample from the pinned parser "
            "repositories and generate an HTML inventory."
        )
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser


def main() -> None:
    args = build_argument_parser().parse_args()
    payload = collect_samples(args.output_dir)
    print(
        json.dumps(
            {
                "report": str(args.output_dir / "report.html"),
                "manifest": str(args.output_dir / "manifest.json"),
                **payload["summary"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

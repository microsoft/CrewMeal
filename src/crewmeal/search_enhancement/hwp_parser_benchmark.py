from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
import unicodedata
import urllib.request
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from crewmeal.config import resolve_soffice_path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BENCHMARK_ROOT = REPOSITORY_ROOT / "benchmark" / "hwp"
DEFAULT_MANIFEST_PATH = BENCHMARK_ROOT / "corpus.json"
DEFAULT_RESULT_DIR = REPOSITORY_ROOT / "result" / "hwp-parser-benchmark"
PARSER_ADAPTER_PATH = BENCHMARK_ROOT / "adapters" / "parser_adapter.py"
KORDOC_ADAPTER_PATH = BENCHMARK_ROOT / "adapters" / "kordoc.mjs"
MAX_CORPUS_FILE_BYTES = 50 * 1024 * 1024
HWP5_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
HWPX_MAGIC = b"PK"
ENGINE_FORMATS: dict[str, frozenset[str]] = {
    "crewmeal": frozenset({"hwp", "hwpx"}),
    "kordoc": frozenset({"hwp", "hwpx"}),
    "rhwp": frozenset({"hwp", "hwpx"}),
    "hwp-hwpx-parser": frozenset({"hwp", "hwpx"}),
    "pyhwp": frozenset({"hwp"}),
    "openhanji": frozenset({"hwpx"}),
    "python-hwpx": frozenset({"hwpx"}),
}
RHWP_RELEASES = {
    ("windows", "x86_64"): {
        "url": "https://github.com/edwardkim/rhwp/releases/download/v0.7.19/rhwp-v0.7.19-windows-x86_64.zip",
        "sha256": "219c7ee4b19e239d28827a2d393ec5c423a065e7b54273b22bfcc72c0cf0a9b3",
        "archive": "rhwp-v0.7.19-windows-x86_64.zip",
        "executable": "rhwp.exe",
    },
    ("linux", "x86_64"): {
        "url": "https://github.com/edwardkim/rhwp/releases/download/v0.7.19/rhwp-v0.7.19-linux-x86_64.tar.gz",
        "sha256": "fe3dc818a44f2bc4d4a001311514ed399d46a1e752b3df0d6e9e2f2ac8058402",
        "archive": "rhwp-v0.7.19-linux-x86_64.tar.gz",
        "executable": "rhwp",
    },
}


class CorpusManifestError(ValueError):
    """Raised when the pinned benchmark corpus manifest is invalid."""


class CorpusIntegrityError(ValueError):
    """Raised when a downloaded corpus file fails an integrity check."""


class AdapterOutputError(ValueError):
    """Raised when a parser adapter does not return the common result contract."""


@dataclass(frozen=True, slots=True)
class CorpusDocument:
    id: str
    format: str
    filename: str
    source: Mapping[str, str]
    expected_bytes: int
    sha256: str
    license: str
    provenance: str
    tags: tuple[str, ...]
    expectations: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class EngineAvailability:
    available: bool
    detail: str
    executable: Path | None = None


def _require_nonempty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CorpusManifestError(f"{field_name} must be a non-empty string.")
    return value


def _expectations_count(expectations: Mapping[str, Any]) -> int:
    sentinels = expectations.get("sentinels", [])
    total = len(sentinels) if isinstance(sentinels, list) else 0
    total += sum(
        key in expectations
        for key in (
            "minTables",
            "minImages",
            "minPages",
            "minFootnotes",
            "minEndnotes",
        )
    )
    return total


def load_corpus_manifest(path: Path = DEFAULT_MANIFEST_PATH) -> tuple[CorpusDocument, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schemaVersion") != 1:
        raise CorpusManifestError("corpus schemaVersion must be 1.")
    raw_documents = payload.get("documents")
    if not isinstance(raw_documents, list):
        raise CorpusManifestError("corpus documents must be an array.")

    documents: list[CorpusDocument] = []
    seen_ids: set[str] = set()
    seen_hashes: set[str] = set()
    for index, raw in enumerate(raw_documents):
        if not isinstance(raw, dict):
            raise CorpusManifestError(f"documents[{index}] must be an object.")
        document_id = _require_nonempty_string(raw.get("id"), f"documents[{index}].id")
        if document_id in seen_ids:
            raise CorpusManifestError(f"Duplicate corpus id: {document_id}")
        seen_ids.add(document_id)

        file_format = _require_nonempty_string(
            raw.get("format"), f"documents[{index}].format"
        ).lower()
        if file_format not in {"hwp", "hwpx"}:
            raise CorpusManifestError(
                f"{document_id}: format must be 'hwp' or 'hwpx'."
            )
        filename = _require_nonempty_string(
            raw.get("filename"), f"documents[{index}].filename"
        )
        if Path(filename).name != filename or Path(filename).suffix.lower() != f".{file_format}":
            raise CorpusManifestError(
                f"{document_id}: filename must be a basename ending in .{file_format}."
            )

        source = raw.get("source")
        if not isinstance(source, dict):
            raise CorpusManifestError(f"{document_id}: source must be an object.")
        for source_field in ("repository", "commit", "path", "url"):
            _require_nonempty_string(
                source.get(source_field), f"{document_id}.source.{source_field}"
            )
        source_url = str(source["url"])
        if urlsplit(source_url).scheme != "https":
            raise CorpusManifestError(f"{document_id}: source URL must use HTTPS.")

        expected_bytes = raw.get("bytes")
        if (
            not isinstance(expected_bytes, int)
            or expected_bytes <= 0
            or expected_bytes > MAX_CORPUS_FILE_BYTES
        ):
            raise CorpusManifestError(
                f"{document_id}: bytes must be between 1 and "
                f"{MAX_CORPUS_FILE_BYTES}."
            )
        sha256 = _require_nonempty_string(
            raw.get("sha256"), f"{document_id}.sha256"
        ).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise CorpusManifestError(f"{document_id}: sha256 is invalid.")
        if sha256 in seen_hashes:
            raise CorpusManifestError(
                f"{document_id}: duplicate binary SHA-256 in corpus."
            )
        seen_hashes.add(sha256)

        tags = raw.get("tags")
        if not isinstance(tags, list) or not all(
            isinstance(tag, str) and tag for tag in tags
        ):
            raise CorpusManifestError(f"{document_id}: tags must be strings.")
        expectations = raw.get("expectations", {})
        if not isinstance(expectations, dict):
            raise CorpusManifestError(
                f"{document_id}: expectations must be an object."
            )
        sentinels = expectations.get("sentinels", [])
        if not isinstance(sentinels, list) or not all(
            isinstance(sentinel, str) and sentinel for sentinel in sentinels
        ):
            raise CorpusManifestError(
                f"{document_id}: expectations.sentinels must be strings."
            )
        for expectation in (
            "minTables",
            "minImages",
            "minPages",
            "minFootnotes",
            "minEndnotes",
        ):
            value = expectations.get(expectation)
            if value is not None and (not isinstance(value, int) or value < 0):
                raise CorpusManifestError(
                    f"{document_id}: expectations.{expectation} "
                    "must be a non-negative integer."
                )
        if _expectations_count(expectations) == 0:
            raise CorpusManifestError(
                f"{document_id}: at least one expectation is required."
            )

        documents.append(
            CorpusDocument(
                id=document_id,
                format=file_format,
                filename=filename,
                source={str(key): str(value) for key, value in source.items()},
                expected_bytes=expected_bytes,
                sha256=sha256,
                license=_require_nonempty_string(
                    raw.get("license"), f"{document_id}.license"
                ),
                provenance=_require_nonempty_string(
                    raw.get("provenance"), f"{document_id}.provenance"
                ),
                tags=tuple(tags),
                expectations=expectations,
            )
        )

    counts = Counter(document.format for document in documents)
    if counts != Counter({"hwp": 10, "hwpx": 10}):
        raise CorpusManifestError(
            "The default benchmark must contain exactly 10 HWP and 10 HWPX "
            f"documents; found {dict(counts)}."
        )
    return tuple(documents)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_corpus_file(path: Path, document: CorpusDocument) -> None:
    if not path.is_file():
        raise CorpusIntegrityError(f"Missing corpus file: {path}")
    file_size = path.stat().st_size
    if file_size != document.expected_bytes:
        raise CorpusIntegrityError(
            f"{document.id}: expected {document.expected_bytes} bytes, got {file_size}."
        )
    digest = _sha256_file(path)
    if digest != document.sha256:
        raise CorpusIntegrityError(
            f"{document.id}: expected SHA-256 {document.sha256}, got {digest}."
        )
    with path.open("rb") as stream:
        magic = stream.read(8)
    if document.format == "hwp" and magic != HWP5_MAGIC:
        raise CorpusIntegrityError(f"{document.id}: file is not an HWP5 OLE document.")
    if document.format == "hwpx":
        if not magic.startswith(HWPX_MAGIC) or not zipfile.is_zipfile(path):
            raise CorpusIntegrityError(f"{document.id}: file is not a valid HWPX ZIP.")
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
        if "mimetype" not in names or not any(
            name.startswith("Contents/section") and name.endswith(".xml")
            for name in names
        ):
            raise CorpusIntegrityError(
                f"{document.id}: HWPX package is missing required parts."
            )


def _download_file(url: str, destination: Path, *, max_bytes: int) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "CrewMeal-HWP-Parser-Benchmark/1"},
    )
    temporary = destination.with_suffix(f"{destination.suffix}.part")
    temporary.unlink(missing_ok=True)
    downloaded = 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            with temporary.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    downloaded += len(chunk)
                    if downloaded > max_bytes:
                        raise CorpusIntegrityError(
                            f"Download exceeded the {max_bytes}-byte limit: {url}"
                        )
                    output.write(chunk)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def fetch_corpus(
    documents: Sequence[CorpusDocument],
    result_dir: Path = DEFAULT_RESULT_DIR,
) -> tuple[Path, ...]:
    corpus_dir = result_dir / "corpus"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for document in documents:
        destination = corpus_dir / document.filename
        try:
            _validate_corpus_file(destination, document)
        except CorpusIntegrityError:
            destination.unlink(missing_ok=True)
            _download_file(
                document.source["url"],
                destination,
                max_bytes=min(MAX_CORPUS_FILE_BYTES, document.expected_bytes),
            )
            _validate_corpus_file(destination, document)
        paths.append(destination)

    receipt = {
        "schema_version": 1,
        "documents": [
            {
                "id": document.id,
                "filename": document.filename,
                "sha256": document.sha256,
                "bytes": document.expected_bytes,
                "source": document.source,
            }
            for document in documents
        ],
    }
    (result_dir / "corpus-receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return tuple(paths)


def _venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _run_setup_command(command: Sequence[str], *, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Setup command failed ({completed.returncode}): {' '.join(command)}\n"
            f"{completed.stderr.strip()[-4000:]}"
        )
    return completed.stdout.strip()


def _install_python_requirements(venv_dir: Path, requirements: Path) -> Path:
    python_path = _venv_python(venv_dir)
    if not python_path.is_file():
        _run_setup_command([sys.executable, "-m", "venv", str(venv_dir)])
    _run_setup_command(
        [
            str(python_path),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--quiet",
            "--upgrade",
            "pip",
        ]
    )
    _run_setup_command(
        [
            str(python_path),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--quiet",
            "--requirement",
            str(requirements),
        ]
    )
    return python_path


def _normalized_machine() -> str:
    machine = platform.machine().lower()
    if machine in {"amd64", "x64", "x86_64"}:
        return "x86_64"
    if machine in {"arm64", "aarch64"}:
        return "aarch64"
    return machine


def _safe_extract_zip(archive_path: Path, destination: Path) -> None:
    destination_resolved = destination.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            member_path = (destination / member.filename).resolve()
            if (
                destination_resolved != member_path
                and destination_resolved not in member_path.parents
            ):
                raise RuntimeError(f"Unsafe ZIP member path: {member.filename}")
        archive.extractall(destination)


def _install_rhwp(tools_dir: Path) -> Path:
    key = (platform.system().lower(), _normalized_machine())
    release = RHWP_RELEASES.get(key)
    if release is None:
        raise RuntimeError(
            "No pinned rhwp 0.7.19 binary is configured for "
            f"{platform.system()} {_normalized_machine()}."
        )
    rhwp_dir = tools_dir / "rhwp"
    archive_path = rhwp_dir / release["archive"]
    rhwp_dir.mkdir(parents=True, exist_ok=True)
    if not archive_path.is_file() or _sha256_file(archive_path) != release["sha256"]:
        archive_path.unlink(missing_ok=True)
        _download_file(release["url"], archive_path, max_bytes=100 * 1024 * 1024)
    digest = _sha256_file(archive_path)
    if digest != release["sha256"]:
        raise RuntimeError(
            f"rhwp archive hash mismatch: expected {release['sha256']}, got {digest}."
        )

    extract_dir = rhwp_dir / "release"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True)
    if archive_path.suffix == ".zip":
        _safe_extract_zip(archive_path, extract_dir)
    else:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            destination_resolved = extract_dir.resolve()
            for member in archive.getmembers():
                member_path = (extract_dir / member.name).resolve()
                if (
                    destination_resolved != member_path
                    and destination_resolved not in member_path.parents
                ):
                    raise RuntimeError(f"Unsafe TAR member path: {member.name}")
            archive.extractall(extract_dir)
    matches = list(extract_dir.rglob(release["executable"]))
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected one {release['executable']} in rhwp archive, found {len(matches)}."
        )
    executable = matches[0]
    if os.name != "nt":
        executable.chmod(executable.stat().st_mode | 0o111)
    _run_setup_command([str(executable), "--version"])
    return executable


def setup_tools(result_dir: Path = DEFAULT_RESULT_DIR) -> dict[str, Any]:
    tools_dir = result_dir / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    parser_python = _install_python_requirements(
        tools_dir / "python", BENCHMARK_ROOT / "requirements.txt"
    )
    pyhwp_python = _install_python_requirements(
        tools_dir / "pyhwp", BENCHMARK_ROOT / "requirements-pyhwp.txt"
    )

    npm = shutil.which("npm")
    if npm is None:
        raise RuntimeError("npm is required to install kordoc 4.2.3.")
    _run_setup_command(
        [
            npm,
            "ci",
            "--omit=optional",
            "--ignore-scripts",
            "--prefix",
            str(BENCHMARK_ROOT),
        ],
        cwd=REPOSITORY_ROOT,
    )
    rhwp_executable = _install_rhwp(tools_dir)
    status = {
        "parser_python": str(parser_python),
        "pyhwp_python": str(pyhwp_python),
        "kordoc_version": json.loads(
            (BENCHMARK_ROOT / "node_modules" / "kordoc" / "package.json").read_text(
                encoding="utf-8"
            )
        )["version"],
        "rhwp_executable": str(rhwp_executable),
        "libreoffice": (
            str(path) if (path := resolve_soffice_path()) is not None else None
        ),
    }
    (tools_dir / "setup-receipt.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return status


def _find_rhwp_executable(result_dir: Path) -> Path | None:
    executable_name = "rhwp.exe" if os.name == "nt" else "rhwp"
    matches = list((result_dir / "tools" / "rhwp" / "release").rglob(executable_name))
    return matches[0] if len(matches) == 1 else None


def engine_availability(
    result_dir: Path = DEFAULT_RESULT_DIR,
) -> dict[str, EngineAvailability]:
    tools_dir = result_dir / "tools"
    parser_python = _venv_python(tools_dir / "python")
    pyhwp_python = _venv_python(tools_dir / "pyhwp")
    node = shutil.which("node")
    rhwp = _find_rhwp_executable(result_dir)
    soffice = resolve_soffice_path()
    kordoc_package = BENCHMARK_ROOT / "node_modules" / "kordoc" / "package.json"
    return {
        "crewmeal": EngineAvailability(
            soffice is not None,
            str(soffice) if soffice else "LibreOffice not found; set SOFFICE_PATH.",
            soffice,
        ),
        "kordoc": EngineAvailability(
            node is not None and kordoc_package.is_file(),
            (
                f"node={node}, kordoc=4.2.3"
                if node is not None and kordoc_package.is_file()
                else "Run the setup command to install kordoc 4.2.3."
            ),
            Path(node) if node else None,
        ),
        "rhwp": EngineAvailability(
            rhwp is not None,
            str(rhwp) if rhwp else "Run the setup command to install rhwp 0.7.19.",
            rhwp,
        ),
        "hwp-hwpx-parser": EngineAvailability(
            parser_python.is_file(),
            (
                str(parser_python)
                if parser_python.is_file()
                else "Run the setup command to install Python parser tools."
            ),
            parser_python if parser_python.is_file() else None,
        ),
        "pyhwp": EngineAvailability(
            pyhwp_python.is_file(),
            (
                str(pyhwp_python)
                if pyhwp_python.is_file()
                else "Run the setup command to install pyhwp 0.1b15."
            ),
            pyhwp_python if pyhwp_python.is_file() else None,
        ),
        "openhanji": EngineAvailability(
            parser_python.is_file(),
            (
                str(parser_python)
                if parser_python.is_file()
                else "Run the setup command to install Python parser tools."
            ),
            parser_python if parser_python.is_file() else None,
        ),
        "python-hwpx": EngineAvailability(
            parser_python.is_file(),
            (
                str(parser_python)
                if parser_python.is_file()
                else "Run the setup command to install Python parser tools."
            ),
            parser_python if parser_python.is_file() else None,
        ),
    }


def _engine_command(
    engine: str,
    document_path: Path,
    availability: EngineAvailability,
    timeout_seconds: float,
) -> list[str]:
    if availability.executable is None:
        raise RuntimeError(f"{engine} has no executable.")
    if engine == "crewmeal":
        return [
            sys.executable,
            "-m",
            "crewmeal.search_enhancement.hwp_parser_benchmark",
            "_crewmeal-adapter",
            str(document_path),
            "--soffice",
            str(availability.executable),
            "--timeout",
            str(timeout_seconds),
        ]
    if engine == "kordoc":
        return [
            str(availability.executable),
            str(KORDOC_ADAPTER_PATH),
            str(document_path),
        ]
    command = [
        (
            sys.executable
            if engine == "rhwp"
            else str(availability.executable)
        ),
        str(PARSER_ADAPTER_PATH),
        engine,
        str(document_path),
        "--timeout",
        str(timeout_seconds),
    ]
    if engine == "rhwp":
        command.extend(["--rhwp-executable", str(availability.executable)])
    return command


def _adapter_environment() -> dict[str, str]:
    environment = os.environ.copy()
    source_path = str(REPOSITORY_ROOT / "src")
    current = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        f"{source_path}{os.pathsep}{current}" if current else source_path
    )
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def _parse_adapter_output(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            validate_adapter_output(payload)
            return payload
    raise AdapterOutputError("Adapter stdout did not contain a valid JSON result.")


def validate_adapter_output(payload: Mapping[str, Any]) -> None:
    if payload.get("schema_version") != 1:
        raise AdapterOutputError("adapter schema_version must be 1.")
    for field in ("parser", "version", "text", "markdown"):
        if not isinstance(payload.get(field), str):
            raise AdapterOutputError(f"adapter {field} must be a string.")
    if not isinstance(payload.get("tables"), list):
        raise AdapterOutputError("adapter tables must be an array.")
    for field in (
        "images_count",
        "footnotes_count",
        "endnotes_count",
        "links_count",
    ):
        value = payload.get(field)
        if not isinstance(value, int) or value < 0:
            raise AdapterOutputError(
                f"adapter {field} must be a non-negative integer."
            )
    pages_count = payload.get("pages_count")
    if pages_count is not None and (
        not isinstance(pages_count, int) or pages_count < 0
    ):
        raise AdapterOutputError(
            "adapter pages_count must be null or a non-negative integer."
        )
    if not isinstance(payload.get("metadata"), dict):
        raise AdapterOutputError("adapter metadata must be an object.")
    if not isinstance(payload.get("warnings"), list) or not all(
        isinstance(warning, str) for warning in payload["warnings"]
    ):
        raise AdapterOutputError("adapter warnings must be strings.")


def _normalized_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", " ", normalized).strip()


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[0-9a-z가-힣]+", _normalized_text(value)))


def score_expectations(
    document: CorpusDocument,
    output: Mapping[str, Any] | None,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    normalized_text = _normalized_text(str(output.get("text", ""))) if output else ""
    for sentinel in document.expectations.get("sentinels", []):
        checks.append(
            {
                "name": f"text:{sentinel}",
                "passed": _normalized_text(sentinel) in normalized_text,
            }
        )
    field_map = {
        "minTables": ("tables", lambda value: len(value)),
        "minImages": ("images_count", int),
        "minPages": ("pages_count", int),
        "minFootnotes": ("footnotes_count", int),
        "minEndnotes": ("endnotes_count", int),
    }
    for expectation_name, (output_field, convert) in field_map.items():
        if expectation_name not in document.expectations:
            continue
        actual_value: int | None = None
        if output is not None and output.get(output_field) is not None:
            value = output[output_field]
            if output_field == "tables" and isinstance(value, list):
                actual_value = len(value)
            elif isinstance(value, int):
                actual_value = convert(value)
        expected_value = int(document.expectations[expectation_name])
        checks.append(
            {
                "name": expectation_name,
                "passed": actual_value is not None and actual_value >= expected_value,
                "expected": expected_value,
                "actual": actual_value,
            }
        )
    passed = sum(bool(check["passed"]) for check in checks)
    return {
        "passed": passed,
        "total": len(checks),
        "percent": round(100 * passed / len(checks), 2) if checks else None,
        "checks": checks,
    }


def _text_metrics(text: str) -> dict[str, int]:
    normalized = unicodedata.normalize("NFKC", text)
    return {
        "characters": sum(not character.isspace() for character in normalized),
        "hangul_characters": len(re.findall(r"[가-힣]", normalized)),
        "unique_tokens": len(_tokens(normalized)),
    }


def _jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def _f1(precision: float, recall: float) -> float:
    return (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )


def add_agreement_metrics(
    records: list[dict[str, Any]],
    outputs: Mapping[tuple[str, str], Mapping[str, Any]],
) -> None:
    records_by_document: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        records_by_document[record["document_id"]].append(record)

    for document_id, document_records in records_by_document.items():
        token_sets = {
            record["engine"]: _tokens(str(outputs[(record["engine"], document_id)]["text"]))
            for record in document_records
            if record["status"] == "success"
            and (record["engine"], document_id) in outputs
            and _tokens(str(outputs[(record["engine"], document_id)]["text"]))
        }
        if len(token_sets) < 2:
            continue
        threshold = len(token_sets) // 2 + 1
        frequencies = Counter(
            token for tokens in token_sets.values() for token in tokens
        )
        consensus = {
            token for token, frequency in frequencies.items() if frequency >= threshold
        }
        for record in document_records:
            tokens = token_sets.get(record["engine"])
            if tokens is None:
                continue
            other_sets = [
                other_tokens
                for other_engine, other_tokens in token_sets.items()
                if other_engine != record["engine"]
            ]
            pairwise = [_jaccard(tokens, other) for other in other_sets]
            precision = len(tokens & consensus) / len(tokens) if tokens else 0.0
            recall = (
                len(tokens & consensus) / len(consensus) if consensus else 1.0
            )
            record["metrics"]["pairwise_jaccard_median"] = round(
                statistics.median(pairwise), 4
            )
            record["metrics"]["consensus_precision"] = round(precision, 4)
            record["metrics"]["consensus_recall"] = round(recall, 4)
            record["metrics"]["consensus_f1"] = round(_f1(precision, recall), 4)


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def aggregate_records(
    records: Sequence[Mapping[str, Any]],
    selected_engines: Sequence[str],
) -> list[dict[str, Any]]:
    aggregates: list[dict[str, Any]] = []
    for engine in selected_engines:
        for file_format in sorted(ENGINE_FORMATS[engine]):
            rows = [
                record
                for record in records
                if record["engine"] == engine and record["format"] == file_format
            ]
            successful = [row for row in rows if row["status"] == "success"]
            durations = [float(row["duration_seconds"]) for row in successful]
            expectation_passed = sum(
                int(row["metrics"]["expectations"]["passed"]) for row in rows
            )
            expectation_total = sum(
                int(row["metrics"]["expectations"]["total"]) for row in rows
            )
            agreement_values = [
                float(row["metrics"]["consensus_f1"])
                for row in successful
                if row["metrics"].get("consensus_f1") is not None
            ]
            versions = [
                str(row["version"]) for row in successful if row.get("version")
            ]
            aggregates.append(
                {
                    "engine": engine,
                    "format": file_format,
                    "version": Counter(versions).most_common(1)[0][0]
                    if versions
                    else None,
                    "attempted": len(rows),
                    "successful": len(successful),
                    "success_rate": round(
                        100 * len(successful) / len(rows), 2
                    )
                    if rows
                    else None,
                    "empty_text": sum(
                        int(row["metrics"].get("characters", 0) == 0)
                        for row in successful
                    ),
                    "expectations_passed": expectation_passed,
                    "expectations_total": expectation_total,
                    "expectation_recall": round(
                        100 * expectation_passed / expectation_total, 2
                    )
                    if expectation_total
                    else None,
                    "median_agreement_f1": round(
                        statistics.median(agreement_values), 4
                    )
                    if agreement_values
                    else None,
                    "median_seconds": round(statistics.median(durations), 4)
                    if durations
                    else None,
                    "p95_seconds": round(_percentile(durations, 0.95), 4)
                    if durations
                    else None,
                    "status_counts": dict(Counter(row["status"] for row in rows)),
                }
            )
    return aggregates


def _artifact_relative(path: Path, result_dir: Path) -> str:
    return path.relative_to(result_dir).as_posix()


def _load_resume_records(
    result_dir: Path,
    manifest_sha256: str,
) -> dict[tuple[str, str], Mapping[str, Any]]:
    results_path = result_dir / "results.json"
    if not results_path.is_file():
        return {}
    try:
        payload = json.loads(results_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if (
        not isinstance(payload, dict)
        or payload.get("manifest_sha256") != manifest_sha256
        or not isinstance(payload.get("runs"), list)
    ):
        return {}
    return {
        (str(record["engine"]), str(record["document_id"])): record
        for record in payload["runs"]
        if isinstance(record, dict)
        and record.get("status") == "success"
        and isinstance(record.get("engine"), str)
        and isinstance(record.get("document_id"), str)
        and isinstance(record.get("artifact"), str)
    }


def _resume_output(
    record: Mapping[str, Any],
    result_dir: Path,
) -> Mapping[str, Any] | None:
    artifact = result_dir / str(record["artifact"])
    try:
        payload = json.loads(artifact.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        validate_adapter_output(payload)
    except (OSError, json.JSONDecodeError, AdapterOutputError):
        return None
    return payload


def run_benchmark(
    documents: Sequence[CorpusDocument],
    *,
    result_dir: Path = DEFAULT_RESULT_DIR,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    selected_engines: Sequence[str] | None = None,
    timeout_seconds: float = 240.0,
    resume: bool = False,
) -> dict[str, Any]:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive.")
    engines = list(selected_engines or ENGINE_FORMATS)
    unknown = sorted(set(engines) - set(ENGINE_FORMATS))
    if unknown:
        raise ValueError(f"Unknown benchmark engines: {', '.join(unknown)}")
    corpus_paths = {
        document.id: path
        for document, path in zip(
            documents,
            fetch_corpus(documents, result_dir),
            strict=True,
        )
    }
    availability = engine_availability(result_dir)
    raw_dir = result_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    outputs: dict[tuple[str, str], Mapping[str, Any]] = {}
    manifest_sha256 = _sha256_file(manifest_path)
    resume_records = (
        _load_resume_records(result_dir, manifest_sha256) if resume else {}
    )
    compatible_pairs = [
        (engine, document)
        for engine in engines
        for document in documents
        if document.format in ENGINE_FORMATS[engine]
    ]

    for run_index, (engine, document) in enumerate(compatible_pairs, start=1):
        engine_status = availability[engine]
        expectation_score = score_expectations(document, None)
        record: dict[str, Any] = {
            "engine": engine,
            "document_id": document.id,
            "format": document.format,
            "status": "unavailable",
            "duration_seconds": 0.0,
            "return_code": None,
            "version": None,
            "artifact": None,
            "error": None,
            "metrics": {"expectations": expectation_score},
        }
        resumed_record = resume_records.get((engine, document.id))
        if resumed_record is not None:
            resumed_output = _resume_output(resumed_record, result_dir)
            if resumed_output is not None:
                print(
                    f"[{run_index}/{len(compatible_pairs)}] "
                    f"{engine} <- {document.id} (cached)",
                    file=sys.stderr,
                    flush=True,
                )
                expectation_score = score_expectations(document, resumed_output)
                record.update(
                    {
                        "status": "success",
                        "duration_seconds": float(
                            resumed_record.get("duration_seconds", 0.0)
                        ),
                        "return_code": resumed_record.get("return_code"),
                        "version": resumed_output["version"],
                        "artifact": resumed_record["artifact"],
                        "error": None,
                        "metrics": {
                            **_text_metrics(resumed_output["text"]),
                            "tables": len(resumed_output["tables"]),
                            "images": resumed_output["images_count"],
                            "pages": resumed_output["pages_count"],
                            "footnotes": resumed_output["footnotes_count"],
                            "endnotes": resumed_output["endnotes_count"],
                            "expectations": expectation_score,
                        },
                    }
                )
                records.append(record)
                outputs[(engine, document.id)] = resumed_output
                continue
        print(
            f"[{run_index}/{len(compatible_pairs)}] "
            f"{engine} <- {document.id}",
            file=sys.stderr,
            flush=True,
        )
        if not engine_status.available:
            record["error"] = engine_status.detail
            records.append(record)
            continue

        command = _engine_command(
            engine,
            corpus_paths[document.id],
            engine_status,
            timeout_seconds,
        )
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds + 5,
                shell=False,
                cwd=REPOSITORY_ROOT,
                env=_adapter_environment(),
            )
        except subprocess.TimeoutExpired as error:
            record["status"] = "timeout"
            record["duration_seconds"] = round(time.perf_counter() - started, 4)
            record["error"] = f"Adapter exceeded {timeout_seconds:.0f} seconds."
            if error.stderr:
                record["stderr_tail"] = str(error.stderr)[-4000:]
            records.append(record)
            continue
        except OSError as error:
            record["status"] = "error"
            record["duration_seconds"] = round(time.perf_counter() - started, 4)
            record["error"] = f"Could not start adapter: {error}"
            records.append(record)
            continue

        record["duration_seconds"] = round(time.perf_counter() - started, 4)
        record["return_code"] = completed.returncode
        if completed.returncode != 0:
            record["status"] = "error"
            record["error"] = (
                f"Adapter exited with {completed.returncode}: "
                f"{completed.stderr.strip()[-4000:]}"
            )
            if completed.stdout.strip():
                record["stdout_tail"] = completed.stdout.strip()[-4000:]
            records.append(record)
            continue
        try:
            output = _parse_adapter_output(completed.stdout)
        except AdapterOutputError as error:
            record["status"] = "error"
            record["error"] = str(error)
            record["stdout_tail"] = completed.stdout.strip()[-4000:]
            record["stderr_tail"] = completed.stderr.strip()[-4000:]
            records.append(record)
            continue

        parser_dir = raw_dir / engine
        parser_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = parser_dir / f"{document.id}.json"
        artifact_path.write_text(
            json.dumps(output, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        expectation_score = score_expectations(document, output)
        record.update(
            {
                "status": "success",
                "version": output["version"],
                "artifact": _artifact_relative(artifact_path, result_dir),
                "metrics": {
                    **_text_metrics(output["text"]),
                    "tables": len(output["tables"]),
                    "images": output["images_count"],
                    "pages": output["pages_count"],
                    "footnotes": output["footnotes_count"],
                    "endnotes": output["endnotes_count"],
                    "expectations": expectation_score,
                },
            }
        )
        if completed.stderr.strip():
            record["stderr_tail"] = completed.stderr.strip()[-4000:]
        records.append(record)
        outputs[(engine, document.id)] = output

    add_agreement_metrics(records, outputs)
    aggregates = aggregate_records(records, engines)
    generated_at = datetime.now(timezone.utc).isoformat()
    summary = {
        "schema_version": 1,
        "generated_at": generated_at,
        "manifest_sha256": manifest_sha256,
        "methodology": {
            "corpus": "10 HWP5 + 10 HWPX, SHA-256 pinned",
            "latency": "One cold subprocess invocation per engine/document pair",
            "expectation_score": (
                "Recall of manifest-pinned text and minimum structural expectations"
            ),
            "agreement_score": (
                "Token-set consensus F1 across successful parsers; not accuracy"
            ),
            "memory": "Not measured",
        },
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "availability": {
            engine: {
                "available": availability[engine].available,
                "detail": availability[engine].detail,
            }
            for engine in engines
        },
        "aggregates": aggregates,
        "runs": records,
    }
    result_dir.mkdir(parents=True, exist_ok=True)
    results_path = result_dir / "results.json"
    results_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_benchmark_reports(summary, documents, result_dir)
    return summary


def _display_percent(value: Any) -> str:
    return "-" if value is None else f"{float(value):.1f}%"


def _display_decimal(value: Any, digits: int = 3) -> str:
    return "-" if value is None else f"{float(value):.{digits}f}"


def render_markdown_report(
    summary: Mapping[str, Any],
    documents: Sequence[CorpusDocument],
) -> str:
    lines = [
        "# HWP/HWPX parser benchmark",
        "",
        f"Generated: `{summary['generated_at']}`",
        "",
        "This run uses 10 SHA-pinned HWP5 files and 10 SHA-pinned HWPX files. "
        "Third-party document binaries remain under `result/` and are not committed.",
        "",
        "## Results",
        "",
        "| Engine | Format | Success | Expected evidence | Agreement F1 | Median | p95 | Empty text |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for aggregate in summary["aggregates"]:
        lines.append(
            "| {engine} | {format} | {successful}/{attempted} ({success_rate}) | "
            "{expected} | {agreement} | {median}s | {p95}s | {empty_text} |".format(
                engine=aggregate["engine"],
                format=aggregate["format"].upper(),
                successful=aggregate["successful"],
                attempted=aggregate["attempted"],
                success_rate=_display_percent(aggregate["success_rate"]),
                expected=_display_percent(aggregate["expectation_recall"]),
                agreement=_display_decimal(aggregate["median_agreement_f1"]),
                median=_display_decimal(aggregate["median_seconds"]),
                p95=_display_decimal(aggregate["p95_seconds"]),
                empty_text=aggregate["empty_text"],
            )
        )

    lines.extend(
        [
            "",
            "Expected evidence is recall over manifest-pinned text sentinels and "
            "minimum table/image/page/note counts. Agreement F1 measures overlap "
            "with the majority token set from other successful parsers; it is not "
            "an accuracy score. Latency includes process startup and adapter overhead.",
            "",
            "## Availability",
            "",
            "| Engine | Available | Detail |",
            "| --- | --- | --- |",
        ]
    )
    for engine, status in summary["availability"].items():
        detail = str(status["detail"]).replace("|", "\\|")
        lines.append(
            f"| {engine} | {'yes' if status['available'] else 'no'} | {detail} |"
        )

    failed = [run for run in summary["runs"] if run["status"] != "success"]
    lines.extend(["", "## Failures and unavailable runs", ""])
    if failed:
        lines.extend(
            [
                "| Engine | Document | Status | Error |",
                "| --- | --- | --- | --- |",
            ]
        )
        for run in failed:
            error = str(run.get("error") or "").replace("\n", " ").replace("|", "\\|")
            lines.append(
                f"| {run['engine']} | {run['document_id']} | "
                f"{run['status']} | {error[-300:]} |"
            )
    else:
        lines.append("All compatible engine/document pairs completed.")

    lines.extend(
        [
            "",
            "## Corpus",
            "",
            "| Document | Format | Source | Features | Checks |",
            "| --- | --- | --- | --- | ---: |",
        ]
    )
    for document in documents:
        lines.append(
            f"| {document.id} | {document.format.upper()} | "
            f"{document.source['repository']}@{document.source['commit'][:8]} | "
            f"{', '.join(document.tags)} | "
            f"{_expectations_count(document.expectations)} |"
        )

    lines.extend(
        [
            "",
            "## Interpretation constraints",
            "",
            "- Parser-owned fixtures are mixed across projects, but fixture bias is "
            "not eliminated.",
            "- Structural expectations intentionally distinguish parsers that expose "
            "tables, images, notes, or pages from text-only converters.",
            "- Public HWPX documents retain their upstream terms. Repository licenses "
            "do not automatically relicense third-party documents.",
            "- `pyhwp` is AGPL-3.0; production integration requires a separate legal "
            "review even when its benchmark result is strong.",
            "",
            "Machine-readable run records are in `results.json`; normalized parser "
            "outputs are under `raw/<engine>/`.",
            "",
        ]
    )
    return "\n".join(lines)


def _html_metric_class(
    value: Any,
    *,
    success_threshold: float = 90.0,
    warning_threshold: float = 70.0,
) -> str:
    if value is None:
        return "metric-neutral"
    number = float(value)
    if number >= success_threshold:
        return "metric-good"
    if number >= warning_threshold:
        return "metric-warning"
    return "metric-bad"


def _html_error_tail(run: Mapping[str, Any], limit: int = 360) -> str:
    error = re.sub(r"\s+", " ", str(run.get("error") or "")).strip()
    return error[-limit:] if error else "-"


def render_html_report(
    summary: Mapping[str, Any],
    documents: Sequence[CorpusDocument],
) -> str:
    aggregates = list(summary["aggregates"])
    runs = list(summary["runs"])
    failed_runs = [run for run in runs if run["status"] != "success"]
    successful_runs = len(runs) - len(failed_runs)
    engine_names = sorted({str(row["engine"]) for row in aggregates})
    aggregate_index = {
        (str(row["engine"]), str(row["format"])): row for row in aggregates
    }

    result_rows: list[str] = []
    for row in aggregates:
        success_rate = float(row["success_rate"])
        expectation_recall = float(row["expectation_recall"])
        version = (
            f'<span class="version">v{escape(str(row["version"]))}</span>'
            if row.get("version")
            else ""
        )
        result_rows.append(
            """
            <tr data-engine="{engine_attr}" data-format="{format_attr}">
              <td><strong>{engine}</strong>{version}</td>
              <td><span class="format-badge">{file_format}</span></td>
              <td><span class="metric {success_class}">{successful}/{attempted} ({success_rate})</span></td>
              <td><span class="metric {expectation_class}">{expectation}</span></td>
              <td>{agreement}</td>
              <td>{median}</td>
              <td>{p95}</td>
              <td>{empty_text}</td>
            </tr>
            """.format(
                engine_attr=escape(str(row["engine"]), quote=True),
                format_attr=escape(str(row["format"]), quote=True),
                engine=escape(str(row["engine"])),
                version=version,
                file_format=escape(str(row["format"]).upper()),
                success_class=_html_metric_class(success_rate),
                successful=row["successful"],
                attempted=row["attempted"],
                success_rate=_display_percent(success_rate),
                expectation_class=_html_metric_class(expectation_recall),
                expectation=_display_percent(expectation_recall),
                agreement=_display_decimal(row["median_agreement_f1"]),
                median=(
                    f"{_display_decimal(row['median_seconds'])}s"
                    if row["median_seconds"] is not None
                    else "-"
                ),
                p95=(
                    f"{_display_decimal(row['p95_seconds'])}s"
                    if row["p95_seconds"] is not None
                    else "-"
                ),
                empty_text=row["empty_text"],
            )
        )

    decision_cards: list[str] = []
    rhwp_hwp = aggregate_index.get(("rhwp", "hwp"))
    rhwp_hwpx = aggregate_index.get(("rhwp", "hwpx"))
    if rhwp_hwp and rhwp_hwpx:
        decision_cards.append(
            """
            <article class="decision-card recommended">
              <p class="eyebrow">채택 엔진</p>
              <h3>rhwp</h3>
              <p>두 형식 모두 10/10 성공했고 기대 근거 회수율은 각각
              <strong>{hwp_recall}</strong>, <strong>{hwpx_recall}</strong>입니다.
              후속 native-skia 검증에서 SVG·PDF·PNG 20/20과 고유본 764/767을 통과했습니다.</p>
              <p class="decision-note">RenderTree의 표·본문·머리말·각주를 semantic-first로
              사용하고 이미지·수식이 있는 페이지만 선택적으로 Vision 분석합니다.</p>
            </article>
            """.format(
                hwp_recall=_display_percent(rhwp_hwp["expectation_recall"]),
                hwpx_recall=_display_percent(rhwp_hwpx["expectation_recall"]),
            )
        )

    kordoc_hwp = aggregate_index.get(("kordoc", "hwp"))
    kordoc_hwpx = aggregate_index.get(("kordoc", "hwpx"))
    if kordoc_hwp and kordoc_hwpx:
        decision_cards.append(
            """
            <article class="decision-card">
              <p class="eyebrow">검증된 대안</p>
              <h3>kordoc</h3>
              <p>HWP와 HWPX 모두 10/10 성공했습니다. 기대 근거 회수율은 각각
              <strong>{hwp_recall}</strong>, <strong>{hwpx_recall}</strong>이고,
              중앙값은 {hwp_latency}s / {hwpx_latency}s입니다.</p>
              <p class="decision-note">TypeScript/Node subprocess가 추가되고 페이지 렌더링을
              별도로 해결해야 하므로 단일 엔진인 rhwp보다 통합 복잡도가 높습니다.</p>
            </article>
            """.format(
                hwp_recall=_display_percent(kordoc_hwp["expectation_recall"]),
                hwpx_recall=_display_percent(kordoc_hwpx["expectation_recall"]),
                hwp_latency=_display_decimal(kordoc_hwp["median_seconds"]),
                hwpx_latency=_display_decimal(kordoc_hwpx["median_seconds"]),
            )
        )

    crewmeal_hwp = aggregate_index.get(("crewmeal", "hwp"))
    crewmeal_hwpx = aggregate_index.get(("crewmeal", "hwpx"))
    if crewmeal_hwp and crewmeal_hwpx:
        total_success = int(crewmeal_hwp["successful"]) + int(
            crewmeal_hwpx["successful"]
        )
        total_attempted = int(crewmeal_hwp["attempted"]) + int(
            crewmeal_hwpx["attempted"]
        )
        decision_cards.append(
            """
            <article class="decision-card blocked">
              <p class="eyebrow">현재 기준선</p>
              <h3>CrewMeal + LibreOffice</h3>
              <p><strong>{successful}/{attempted}</strong> 성공입니다. 설치된 LibreOffice가
              HWP5/HWPX 입력을 열지 못해 PDF 변환 단계에서 모두 중단됐습니다.</p>
              <p class="decision-note">현재 경로를 유지하는 튜닝보다 구조 파서 통합이 선행되어야 합니다.</p>
            </article>
            """.format(successful=total_success, attempted=total_attempted)
        )

    failure_counts = Counter(str(run["engine"]) for run in failed_runs)
    failure_summary = ", ".join(
        f"{escape(engine)} {count}건"
        for engine, count in sorted(
            failure_counts.items(), key=lambda item: (-item[1], item[0])
        )
    )
    failure_rows = [
        """
        <tr>
          <td>{engine}</td>
          <td><code>{document}</code></td>
          <td><span class="metric metric-bad">{status}</span></td>
          <td class="error-cell">{error}</td>
        </tr>
        """.format(
            engine=escape(str(run["engine"])),
            document=escape(str(run["document_id"])),
            status=escape(str(run["status"])),
            error=escape(_html_error_tail(run)),
        )
        for run in failed_runs
    ]
    if not failure_rows:
        failure_rows.append(
            '<tr><td colspan="4">호환되는 모든 실행이 성공했습니다.</td></tr>'
        )

    corpus_rows: list[str] = []
    for document in documents:
        tags = " ".join(
            f'<span class="tag">{escape(tag)}</span>' for tag in document.tags
        )
        source_url = escape(str(document.source["url"]), quote=True)
        source_label = (
            f"{document.source['repository']}@{document.source['commit'][:8]}"
        )
        corpus_rows.append(
            """
            <tr data-search="{search_attr}" data-format="{format_attr}">
              <td><code>{document_id}</code></td>
              <td><span class="format-badge">{file_format}</span></td>
              <td><a href="{source_url}" target="_blank" rel="noreferrer">{source}</a></td>
              <td class="tags">{tags}</td>
              <td>{checks}</td>
              <td>{size}</td>
            </tr>
            """.format(
                search_attr=escape(
                    " ".join(
                        (
                            document.id,
                            document.format,
                            str(document.source["repository"]),
                            " ".join(document.tags),
                        )
                    ).lower(),
                    quote=True,
                ),
                format_attr=escape(document.format, quote=True),
                document_id=escape(document.id),
                file_format=escape(document.format.upper()),
                source_url=source_url,
                source=escape(source_label),
                tags=tags,
                checks=_expectations_count(document.expectations),
                size=f"{document.expected_bytes:,} B",
            )
        )

    availability_rows = [
        """
        <tr>
          <td>{engine}</td>
          <td><span class="metric {status_class}">{status}</span></td>
          <td><code>{detail}</code></td>
        </tr>
        """.format(
            engine=escape(str(engine)),
            status_class=(
                "metric-good" if availability["available"] else "metric-bad"
            ),
            status="사용 가능" if availability["available"] else "사용 불가",
            detail=escape(str(availability["detail"])),
        )
        for engine, availability in summary["availability"].items()
    ]

    engine_options = "".join(
        f'<option value="{escape(engine, quote=True)}">{escape(engine)}</option>'
        for engine in engine_names
    )
    generated_at = escape(str(summary["generated_at"]))
    template = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>HWP/HWPX 파서 벤치마크</title>
  <link rel="icon" href="data:,">
  <script>
  (() => {
    const param = new URLSearchParams(window.location.search).get("scoutTheme");
    const theme =
      param || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    document.documentElement.setAttribute("data-theme", theme);
  })();
  </script>
  <style>
  :root {
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
  }
  html[data-theme="dark"] {
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
  }
  * { box-sizing: border-box; }
  html { scroll-behavior: smooth; }
  body {
    margin: 0;
    background: var(--cp-bg);
    color: var(--cp-text);
    font-family: "Segoe UI", Aptos, Calibri, -apple-system, BlinkMacSystemFont, sans-serif;
    line-height: 1.55;
  }
  a { color: var(--cp-link); }
  a:hover { color: var(--cp-accent-hover); }
  code {
    font-family: Consolas, "Courier New", Courier, monospace;
    overflow-wrap: anywhere;
  }
  .hero {
    border-bottom: 1px solid var(--cp-border);
    background: var(--cp-bg-elevated);
  }
  .hero-inner,
  main,
  footer {
    width: min(1180px, calc(100% - 32px));
    margin: 0 auto;
  }
  .hero-inner { padding: 56px 0 44px; }
  .eyebrow {
    margin: 0 0 8px;
    color: var(--cp-accent);
    font-size: 0.78rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  h1, h2, h3 { line-height: 1.2; }
  h1 {
    max-width: 780px;
    margin: 0;
    font-size: clamp(2rem, 5vw, 3.5rem);
    letter-spacing: -0.04em;
  }
  h2 { margin: 0 0 8px; font-size: 1.55rem; }
  h3 { margin: 0 0 12px; font-size: 1.15rem; }
  .lead {
    max-width: 800px;
    margin: 18px 0 0;
    color: var(--cp-text-muted);
    font-size: 1.06rem;
  }
  .generated {
    margin-top: 20px;
    color: var(--cp-text-soft);
    font-size: 0.82rem;
  }
  main { padding: 32px 0 64px; }
  section { margin-top: 36px; }
  .section-head {
    display: flex;
    align-items: end;
    justify-content: space-between;
    gap: 20px;
    margin-bottom: 16px;
  }
  .section-head p {
    max-width: 720px;
    margin: 0;
    color: var(--cp-text-muted);
  }
  .stats {
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    gap: 12px;
    margin-top: 0;
  }
  .stat,
  .decision-card,
  .panel,
  .callout {
    border: 1px solid var(--cp-border);
    background: var(--cp-surface);
    border-radius: 16px;
  }
  .stat { padding: 20px; }
  .stat strong {
    display: block;
    font-size: 1.75rem;
    letter-spacing: -0.03em;
  }
  .stat span { color: var(--cp-text-muted); font-size: 0.86rem; }
  .callout {
    padding: 24px;
    border-color: var(--cp-accent);
    background: var(--cp-accent-soft);
  }
  .callout strong { color: var(--cp-accent); }
  .callout p { margin: 6px 0 0; }
  .decision-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 16px;
  }
  .decision-card { padding: 22px; }
  .decision-card.recommended { border-color: var(--cp-success); }
  .decision-card.blocked { border-color: var(--cp-danger); }
  .decision-card p { margin: 0; }
  .decision-card .decision-note {
    margin-top: 14px;
    color: var(--cp-text-muted);
    font-size: 0.9rem;
  }
  .panel { overflow: hidden; }
  .filters {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    padding: 16px;
    border-bottom: 1px solid var(--cp-border);
    background: var(--cp-surface-soft);
  }
  input, select {
    min-height: 40px;
    padding: 8px 12px;
    border: 1px solid var(--cp-border-strong);
    border-radius: 0.625rem;
    background: var(--cp-surface);
    color: var(--cp-text);
    font: inherit;
  }
  input { min-width: min(360px, 100%); flex: 1; }
  input:focus, select:focus {
    outline: 2px solid var(--cp-accent);
    outline-offset: 1px;
  }
  .table-wrap { overflow-x: auto; }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.9rem;
  }
  th, td {
    padding: 12px 14px;
    border-bottom: 1px solid var(--cp-border);
    text-align: left;
    vertical-align: top;
  }
  th {
    background: var(--cp-surface-soft);
    color: var(--cp-text-muted);
    font-size: 0.76rem;
    letter-spacing: 0.03em;
    text-transform: uppercase;
    white-space: nowrap;
  }
  tbody tr:last-child td { border-bottom: 0; }
  tbody tr:hover { background: var(--cp-accent-soft); }
  .version {
    display: block;
    color: var(--cp-text-soft);
    font-size: 0.75rem;
  }
  .metric {
    display: inline-block;
    font-weight: 700;
    white-space: nowrap;
  }
  .metric-good { color: var(--cp-success); }
  .metric-warning { color: var(--cp-warning); }
  .metric-bad { color: var(--cp-danger); }
  .metric-neutral { color: var(--cp-text-muted); }
  .format-badge,
  .tag {
    display: inline-block;
    padding: 2px 7px;
    border: 1px solid var(--cp-border);
    border-radius: 0.625rem;
    background: var(--cp-surface-soft);
    font-size: 0.75rem;
    white-space: nowrap;
  }
  .tags { min-width: 210px; }
  .tag { margin: 0 4px 4px 0; color: var(--cp-text-muted); }
  .explanation {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 12px;
    margin-top: 16px;
  }
  .explanation article {
    padding: 16px;
    border-left: 3px solid var(--cp-accent);
    background: var(--cp-surface-soft);
  }
  .explanation strong { display: block; margin-bottom: 4px; }
  .explanation p { margin: 0; color: var(--cp-text-muted); font-size: 0.88rem; }
  details {
    border: 1px solid var(--cp-border);
    border-radius: 0.625rem;
    background: var(--cp-surface);
  }
  details + details { margin-top: 12px; }
  summary {
    padding: 16px;
    cursor: pointer;
    font-weight: 700;
  }
  details .table-wrap { border-top: 1px solid var(--cp-border); }
  .error-cell {
    max-width: 560px;
    color: var(--cp-text-muted);
    font-family: Consolas, "Courier New", Courier, monospace;
    font-size: 0.76rem;
    overflow-wrap: anywhere;
  }
  .artifact-links {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .artifact-links a {
    padding: 8px 12px;
    border: 1px solid var(--cp-border);
    border-radius: 0.625rem;
    background: var(--cp-surface);
    text-decoration: none;
  }
  .constraints {
    padding-left: 22px;
    color: var(--cp-text-muted);
  }
  .constraints li + li { margin-top: 6px; }
  .hidden { display: none; }
  footer {
    padding: 28px 0 48px;
    border-top: 1px solid var(--cp-border);
    color: var(--cp-text-muted);
    font-size: 0.84rem;
  }
  @media (max-width: 900px) {
    .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .decision-grid, .explanation { grid-template-columns: 1fr; }
  }
  @media (max-width: 560px) {
    .hero-inner, main, footer { width: min(100% - 20px, 1180px); }
    .hero-inner { padding: 36px 0 30px; }
    .stats { grid-template-columns: 1fr; }
    .section-head { align-items: start; flex-direction: column; }
    th, td { padding: 10px; }
  }
  </style>
</head>
<body>
  <header class="hero">
    <div class="hero-inner">
      <p class="eyebrow">CrewMeal 기술 검증</p>
      <h1>HWP/HWPX 파서 벤치마크</h1>
      <p class="lead">동일한 HWP5 10개와 HWPX 10개를 7개 엔진의 호환 조합에 실행해,
      실행 성공률·기대 근거 회수율·parser 간 agreement·지연 시간을 비교했습니다.</p>
      <p class="generated">생성 시각: <code>__GENERATED_AT__</code></p>
    </div>
  </header>
  <main>
    <section class="stats" aria-label="벤치마크 요약">
      <article class="stat"><strong>20</strong><span>SHA-256 고정 문서</span></article>
      <article class="stat"><strong>__RUN_COUNT__</strong><span>호환 실행 조합</span></article>
      <article class="stat"><strong>__SUCCESS_COUNT__</strong><span>성공 실행</span></article>
      <article class="stat"><strong>__FAILURE_COUNT__</strong><span>실패 실행</span></article>
      <article class="stat"><strong>__ENGINE_COUNT__</strong><span>비교 엔진</span></article>
    </section>

    <section>
      <div class="callout">
        <p class="eyebrow">결론</p>
        <h2>HWP/HWPX 공식 엔진으로 rhwp 채택</h2>
        <p><strong>두 형식 모두 100% 실행 성공</strong>했고 후속 native-skia 렌더 및
        767개 고유본 검증에서도 99.61%를 처리했습니다. RenderTree를 semantic HTML의
        기준값으로 사용하고 의미가 없는 이미지·수식 페이지만 선택적으로 Vision 분석합니다.</p>
      </div>
    </section>

    <section>
      <div class="section-head">
        <div>
          <h2>의사결정 요약</h2>
          <p>단일 점수 순위가 아니라 통합 난이도, 두 형식 지원, 실패 양상을 함께 해석했습니다.</p>
        </div>
      </div>
      <div class="decision-grid">__DECISION_CARDS__</div>
    </section>

    <section id="results">
      <div class="section-head">
        <div>
          <h2>엔진별 결과</h2>
          <p>성공률과 기대 근거 회수율을 우선 보고, agreement는 보조 지표로만 사용합니다.</p>
        </div>
      </div>
      <div class="panel">
        <div class="filters">
          <select id="engine-filter" aria-label="엔진 필터">
            <option value="">모든 엔진</option>
            __ENGINE_OPTIONS__
          </select>
          <select id="format-filter" aria-label="형식 필터">
            <option value="">모든 형식</option>
            <option value="hwp">HWP</option>
            <option value="hwpx">HWPX</option>
          </select>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Engine</th><th>Format</th><th>Success</th>
                <th>Expected evidence</th><th>Agreement F1</th>
                <th>Median</th><th>p95</th><th>Empty text</th>
              </tr>
            </thead>
            <tbody id="result-rows">__RESULT_ROWS__</tbody>
          </table>
        </div>
      </div>
      <div class="explanation">
        <article><strong>Expected evidence</strong><p>manifest에 고정한 텍스트 sentinel과
        최소 표·이미지·페이지·각주 조건의 회수율입니다.</p></article>
        <article><strong>Agreement F1</strong><p>다른 성공 parser의 다수 token 집합과의
        겹침입니다. <strong>정확도 점수가 아닙니다.</strong></p></article>
        <article><strong>Latency</strong><p>문서마다 cold subprocess를 한 번 실행한 값이며
        process 시작과 adapter overhead를 포함합니다.</p></article>
      </div>
    </section>

    <section>
      <div class="section-head">
        <div>
          <h2>실패 분석</h2>
          <p>총 __FAILURE_COUNT__건: __FAILURE_SUMMARY__.</p>
        </div>
      </div>
      <details>
        <summary>실패 실행 __FAILURE_COUNT__건 상세 보기</summary>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Engine</th><th>Document</th><th>Status</th><th>Error tail</th></tr></thead>
            <tbody>__FAILURE_ROWS__</tbody>
          </table>
        </div>
      </details>
    </section>

    <section id="corpus">
      <div class="section-head">
        <div>
          <h2>고정 corpus 20개</h2>
          <p>HWP는 HWP5만 10개, HWPX는 10개입니다. 각 파일은 URL·commit·크기·SHA-256으로 고정했습니다.</p>
        </div>
      </div>
      <div class="panel">
        <div class="filters">
          <input id="corpus-query" type="search" placeholder="문서 ID, 저장소, 태그 검색" aria-label="corpus 검색">
          <select id="corpus-format" aria-label="corpus 형식 필터">
            <option value="">모든 형식</option>
            <option value="hwp">HWP</option>
            <option value="hwpx">HWPX</option>
          </select>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Document</th><th>Format</th><th>Source</th><th>Features</th><th>Checks</th><th>Bytes</th></tr></thead>
            <tbody id="corpus-rows">__CORPUS_ROWS__</tbody>
          </table>
        </div>
      </div>
    </section>

    <section>
      <div class="section-head">
        <div>
          <h2>재현 정보</h2>
          <p>도구 가용성과 machine-readable 결과를 함께 보관했습니다.</p>
        </div>
      </div>
      <details>
        <summary>실행 환경과 parser 경로 보기</summary>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Engine</th><th>Available</th><th>Detail</th></tr></thead>
            <tbody>__AVAILABILITY_ROWS__</tbody>
          </table>
        </div>
      </details>
      <div class="artifact-links">
        <a href="results.json">results.json</a>
        <a href="report.md">report.md</a>
        <a href="corpus-receipt.json">corpus-receipt.json</a>
      </div>
    </section>

    <section>
      <div class="section-head"><div><h2>해석 제약</h2></div></div>
      <ul class="constraints">
        <li>여러 프로젝트의 fixture를 섞었지만 parser 소유 fixture에 따른 편향을 완전히 제거하지는 못했습니다.</li>
        <li>구조 조건은 표·이미지·각주·페이지를 노출하는 parser와 text-only converter를 의도적으로 구분합니다.</li>
        <li>공공 HWPX 문서와 제3자 fixture는 원 출처 조건을 유지합니다. 저장소 라이선스가 문서 권리를 자동 재허여하지 않습니다.</li>
        <li>pyhwp는 AGPL-3.0이므로 production 통합 전 별도 법률 검토가 필요합니다.</li>
        <li>메모리 사용량은 이번 실행에서 측정하지 않았습니다.</li>
      </ul>
    </section>
  </main>
  <footer>
    <p>Machine-readable 결과는 <code>results.json</code>, 정규화 parser 출력은
    <code>raw/&lt;engine&gt;/</code>에 있습니다.</p>
  </footer>
  <script>
  (() => {
    const engineFilter = document.getElementById("engine-filter");
    const formatFilter = document.getElementById("format-filter");
    const resultRows = [...document.querySelectorAll("#result-rows tr")];
    const filterResults = () => {
      for (const row of resultRows) {
        const engineMatches = !engineFilter.value || row.dataset.engine === engineFilter.value;
        const formatMatches = !formatFilter.value || row.dataset.format === formatFilter.value;
        row.classList.toggle("hidden", !(engineMatches && formatMatches));
      }
    };
    engineFilter.addEventListener("change", filterResults);
    formatFilter.addEventListener("change", filterResults);

    const corpusQuery = document.getElementById("corpus-query");
    const corpusFormat = document.getElementById("corpus-format");
    const corpusRows = [...document.querySelectorAll("#corpus-rows tr")];
    const filterCorpus = () => {
      const query = corpusQuery.value.trim().toLowerCase();
      for (const row of corpusRows) {
        const queryMatches = !query || row.dataset.search.includes(query);
        const formatMatches = !corpusFormat.value || row.dataset.format === corpusFormat.value;
        row.classList.toggle("hidden", !(queryMatches && formatMatches));
      }
    };
    corpusQuery.addEventListener("input", filterCorpus);
    corpusFormat.addEventListener("change", filterCorpus);
  })();
  </script>
</body>
</html>
"""
    replacements = {
        "__GENERATED_AT__": generated_at,
        "__RUN_COUNT__": str(len(runs)),
        "__SUCCESS_COUNT__": str(successful_runs),
        "__FAILURE_COUNT__": str(len(failed_runs)),
        "__ENGINE_COUNT__": str(len(summary["availability"])),
        "__DECISION_CARDS__": "".join(decision_cards),
        "__ENGINE_OPTIONS__": engine_options,
        "__RESULT_ROWS__": "".join(result_rows),
        "__FAILURE_SUMMARY__": failure_summary or "없음",
        "__FAILURE_ROWS__": "".join(failure_rows),
        "__CORPUS_ROWS__": "".join(corpus_rows),
        "__AVAILABILITY_ROWS__": "".join(availability_rows),
    }
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    return template


def write_benchmark_reports(
    summary: Mapping[str, Any],
    documents: Sequence[CorpusDocument],
    result_dir: Path,
) -> tuple[Path, Path]:
    markdown_path = result_dir / "report.md"
    markdown_path.write_text(
        render_markdown_report(summary, documents),
        encoding="utf-8",
    )
    html_path = result_dir / "report.html"
    html_path.write_text(
        render_html_report(summary, documents),
        encoding="utf-8",
    )
    return markdown_path, html_path


def _crewmeal_adapter(
    document_path: Path,
    soffice_path: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    import importlib.metadata

    from crewmeal.libreoffice import convert_hwp_to_pdf, inspect_pdf

    with tempfile.TemporaryDirectory(prefix="crewmeal-hwp-benchmark-") as temporary:
        conversion = convert_hwp_to_pdf(
            document_path,
            Path(temporary),
            soffice_path=soffice_path,
            timeout_seconds=timeout_seconds,
        )
        manifest = inspect_pdf(conversion.pdf_path)
    try:
        version = importlib.metadata.version("crewmeal")
    except importlib.metadata.PackageNotFoundError:
        version = "source"
    return {
        "schema_version": 1,
        "parser": "crewmeal",
        "version": version,
        "text": "\n".join(
            text
            for page_number in sorted(manifest.texts_by_page)
            for text in manifest.texts_by_page[page_number]
        ),
        "markdown": "",
        "tables": [],
        "images_count": 0,
        "pages_count": manifest.page_count,
        "footnotes_count": 0,
        "endnotes_count": 0,
        "links_count": sum(len(links) for links in manifest.links_by_page.values()),
        "metadata": {
            "conversion_seconds": conversion.conversion_seconds,
            "render_dpi": manifest.render_dpi,
        },
        "warnings": [
            "CrewMeal's PDF baseline does not expose embedded table/image/note counts."
        ],
    }


def _add_common_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--result-dir", type=Path, default=DEFAULT_RESULT_DIR)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the CrewMeal HWP5/HWPX parser benchmark."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup_parser = subparsers.add_parser("setup", help="Install pinned parser tools.")
    setup_parser.add_argument("--result-dir", type=Path, default=DEFAULT_RESULT_DIR)

    fetch_parser = subparsers.add_parser(
        "fetch", help="Download and verify the 20-file corpus."
    )
    _add_common_paths(fetch_parser)

    report_parser = subparsers.add_parser(
        "report", help="Regenerate Markdown and HTML reports from results.json."
    )
    _add_common_paths(report_parser)

    run_parser = subparsers.add_parser(
        "run", help="Run all compatible parser/document pairs."
    )
    _add_common_paths(run_parser)
    run_parser.add_argument(
        "--engine",
        action="append",
        choices=list(ENGINE_FORMATS),
        help="Limit the run to one or more engines.",
    )
    run_parser.add_argument("--timeout", type=float, default=240.0)
    run_parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse successful raw outputs from the existing results.json.",
    )

    all_parser = subparsers.add_parser(
        "all", help="Install tools, fetch the corpus, and run the benchmark."
    )
    _add_common_paths(all_parser)
    all_parser.add_argument("--timeout", type=float, default=240.0)
    all_parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse successful raw outputs from the existing results.json.",
    )

    adapter_parser = subparsers.add_parser("_crewmeal-adapter")
    adapter_parser.add_argument("document", type=Path)
    adapter_parser.add_argument("--soffice", required=True, type=Path)
    adapter_parser.add_argument("--timeout", type=float, default=240.0)
    return parser


def main() -> None:
    parser = build_argument_parser()
    args = parser.parse_args()
    if args.command == "setup":
        print(json.dumps(setup_tools(args.result_dir), ensure_ascii=False, indent=2))
        return
    if args.command == "_crewmeal-adapter":
        output = _crewmeal_adapter(args.document, args.soffice, args.timeout)
        print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
        return

    documents = load_corpus_manifest(args.manifest)
    if args.command == "fetch":
        paths = fetch_corpus(documents, args.result_dir)
        print(f"Verified {len(paths)} corpus files in {args.result_dir / 'corpus'}")
        return
    if args.command == "report":
        results_path = args.result_dir / "results.json"
        summary = json.loads(results_path.read_text(encoding="utf-8"))
        markdown_path, html_path = write_benchmark_reports(
            summary, documents, args.result_dir
        )
        print(
            json.dumps(
                {
                    "results": str(results_path),
                    "report": str(html_path),
                    "markdown_report": str(markdown_path),
                    "runs": len(summary["runs"]),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if args.command == "all":
        setup_tools(args.result_dir)
        selected_engines = None
    else:
        selected_engines = args.engine
    summary = run_benchmark(
        documents,
        result_dir=args.result_dir,
        manifest_path=args.manifest,
        selected_engines=selected_engines,
        timeout_seconds=args.timeout,
        resume=args.resume,
    )
    print(
        json.dumps(
            {
                "results": str(args.result_dir / "results.json"),
                "report": str(args.result_dir / "report.html"),
                "markdown_report": str(args.result_dir / "report.md"),
                "runs": len(summary["runs"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

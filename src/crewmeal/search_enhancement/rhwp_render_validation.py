from __future__ import annotations

import argparse
import concurrent.futures
import json
import platform
import re
import shutil
import subprocess
import time
import uuid
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence

import fitz

from crewmeal.search_enhancement.hwp_parser_benchmark import (
    DEFAULT_MANIFEST_PATH,
    load_corpus_manifest,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CORPUS_DIR = REPOSITORY_ROOT / "result" / "hwp-parser-benchmark" / "corpus"
DEFAULT_ARCHIVE_MANIFEST = (
    REPOSITORY_ROOT / "result" / "hwp-sample-archive" / "manifest.json"
)
DEFAULT_RESULT_DIR = REPOSITORY_ROOT / "result" / "rhwp-render-validation"
DEFAULT_DOCKER_IMAGE = "crewmeal-rhwp-native:0.7.19"
RHWP_VERSION = "0.7.19"
RHWP_COMMIT = "8d3bfa4b92174b16bac587fe1409975cf34ba566"
CONTROLLED_VLM_TARGET = "gpt4v-high"
SWEEP_VLM_TARGET = "gpt4v-low"
IN_SCOPE_CLASSIFICATIONS = {
    "hwp5-ole": "hwp",
    "hwpx-zip": "hwpx",
    "extension-mismatch-hwp5": "hwp",
    "extension-mismatch-hwpx": "hwpx",
}
WARNING_PATTERN = re.compile(
    r"LAYOUT_OVERFLOW|WARN(?:ING)?|오류|error",
    re.IGNORECASE,
)
SAFE_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


class RhwpValidationError(RuntimeError):
    """Raised when the rhwp validation harness cannot run safely."""


@dataclass(frozen=True, slots=True)
class ValidationDocument:
    id: str
    format: str
    path: Path
    bytes: int
    sha256: str
    classification: str
    source: str


@dataclass(frozen=True, slots=True)
class ProcessResult:
    status: str
    return_code: int | None
    duration_seconds: float
    stdout: str
    stderr: str


def _run_process(command: Sequence[str], timeout_seconds: float) -> ProcessResult:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        return ProcessResult(
            status="timeout",
            return_code=None,
            duration_seconds=round(time.perf_counter() - started, 4),
            stdout=str(error.stdout or ""),
            stderr=str(error.stderr or ""),
        )
    return ProcessResult(
        status="success" if completed.returncode == 0 else "error",
        return_code=completed.returncode,
        duration_seconds=round(time.perf_counter() - started, 4),
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


class DockerRhwpRunner:
    def __init__(
        self,
        *,
        image: str,
        workspace_root: Path,
        output_root: Path,
        startup_timeout_seconds: float = 60.0,
    ) -> None:
        self.image = image
        self.workspace_root = workspace_root.resolve()
        self.output_root = output_root.resolve()
        self.startup_timeout_seconds = startup_timeout_seconds
        self.container_name = f"crewmeal-rhwp-{uuid.uuid4().hex[:12]}"
        self.image_id: str | None = None
        self.version: str | None = None
        self._started = False

    def __enter__(self) -> DockerRhwpRunner:
        self.output_root.mkdir(parents=True, exist_ok=True)
        inspect = _run_process(
            ["docker", "image", "inspect", self.image, "--format", "{{.Id}}"],
            self.startup_timeout_seconds,
        )
        if inspect.status != "success":
            raise RhwpValidationError(
                f"Docker image {self.image!r} is unavailable: {inspect.stderr.strip()}"
            )
        self.image_id = inspect.stdout.strip()
        start = _run_process(
            [
                "docker",
                "run",
                "--detach",
                "--rm",
                "--name",
                self.container_name,
                "--entrypoint",
                "sleep",
                "--mount",
                (
                    f"type=bind,source={self.workspace_root},"
                    "target=/workspace,readonly"
                ),
                "--mount",
                f"type=bind,source={self.output_root},target=/output",
                self.image,
                "infinity",
            ],
            self.startup_timeout_seconds,
        )
        if start.status != "success":
            raise RhwpValidationError(
                f"Could not start rhwp container: {start.stderr.strip()}"
            )
        self._started = True
        version = self.run(["--version"], timeout_seconds=30.0)
        if version.status != "success":
            self.__exit__(None, None, None)
            raise RhwpValidationError(
                f"Could not execute rhwp in the container: {version.stderr.strip()}"
            )
        self.version = (version.stdout or version.stderr).strip()
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        if self._started:
            _run_process(
                ["docker", "stop", "--time", "2", self.container_name],
                timeout_seconds=15.0,
            )
            self._started = False

    def run(self, arguments: Sequence[str], timeout_seconds: float) -> ProcessResult:
        if not self._started:
            raise RhwpValidationError("The rhwp container is not running.")
        return _run_process(
            ["docker", "exec", self.container_name, "rhwp", *arguments],
            timeout_seconds,
        )

    def workspace_path(self, path: Path) -> str:
        try:
            relative = path.resolve().relative_to(self.workspace_root)
        except ValueError as error:
            raise RhwpValidationError(
                f"Input is outside the mounted workspace: {path}"
            ) from error
        return f"/workspace/{relative.as_posix()}"


def _clean_directory(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _command_record(result: ProcessResult) -> dict[str, Any]:
    combined = "\n".join(part for part in (result.stdout, result.stderr) if part)
    warnings = [
        line.strip()
        for line in combined.splitlines()
        if WARNING_PATTERN.search(line)
    ]
    return {
        "status": result.status,
        "return_code": result.return_code,
        "duration_seconds": result.duration_seconds,
        "warnings": warnings,
        "stdout_tail": result.stdout.strip()[-4000:],
        "stderr_tail": result.stderr.strip()[-4000:],
    }


def _pixmap_nonwhite_ratio(pixmap: fitz.Pixmap, max_samples: int = 50_000) -> float:
    total_pixels = pixmap.width * pixmap.height
    if total_pixels <= 0 or pixmap.n < 3:
        return 0.0
    samples = pixmap.samples
    pixel_step = max(1, total_pixels // max_samples)
    checked = 0
    nonwhite = 0
    for pixel_index in range(0, total_pixels, pixel_step):
        offset = pixel_index * pixmap.n
        if pixmap.n >= 4 and samples[offset + 3] < 8:
            continue
        checked += 1
        if min(samples[offset], samples[offset + 1], samples[offset + 2]) < 248:
            nonwhite += 1
    return round(nonwhite / checked, 6) if checked else 0.0


def inspect_png_directory(path: Path) -> dict[str, Any]:
    files = sorted(path.glob("*.png"))
    pages: list[dict[str, Any]] = []
    for file_path in files:
        pixmap = fitz.Pixmap(str(file_path))
        ratio = _pixmap_nonwhite_ratio(pixmap)
        pages.append(
            {
                "file": file_path.name,
                "bytes": file_path.stat().st_size,
                "width": pixmap.width,
                "height": pixmap.height,
                "channels": pixmap.n,
                "nonwhite_ratio": ratio,
                "blank": ratio < 0.00005,
            }
        )
    return {
        "valid": bool(pages),
        "page_count": len(pages),
        "total_bytes": sum(page["bytes"] for page in pages),
        "blank_pages": sum(page["blank"] for page in pages),
        "pages": pages,
    }


def inspect_svg_directory(path: Path) -> dict[str, Any]:
    files = sorted(path.glob("*.svg"))
    pages: list[dict[str, Any]] = []
    for file_path in files:
        root = ElementTree.parse(file_path).getroot()
        visible_nodes = 0
        for node in root.iter():
            local_name = node.tag.rsplit("}", 1)[-1]
            if local_name in {"text", "path", "image", "rect", "line", "polygon"}:
                visible_nodes += 1
        pages.append(
            {
                "file": file_path.name,
                "bytes": file_path.stat().st_size,
                "width": root.attrib.get("width"),
                "height": root.attrib.get("height"),
                "visible_nodes": visible_nodes,
                "blank": visible_nodes == 0,
            }
        )
    return {
        "valid": bool(pages),
        "page_count": len(pages),
        "total_bytes": sum(page["bytes"] for page in pages),
        "blank_pages": sum(page["blank"] for page in pages),
        "pages": pages,
    }


def inspect_pdf(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size == 0:
        return {
            "valid": False,
            "page_count": 0,
            "total_bytes": 0,
            "text_characters": 0,
            "blank_pages": 0,
            "pages": [],
        }
    pages: list[dict[str, Any]] = []
    text_characters = 0
    with fitz.open(path) as document:
        for page_index, page in enumerate(document):
            page_text_characters = sum(
                not character.isspace() for character in page.get_text()
            )
            text_characters += page_text_characters
            pixmap = page.get_pixmap(matrix=fitz.Matrix(0.25, 0.25), alpha=False)
            ratio = _pixmap_nonwhite_ratio(pixmap)
            pages.append(
                {
                    "page": page_index + 1,
                    "text_characters": page_text_characters,
                    "nonwhite_ratio": ratio,
                    "blank": ratio < 0.00005,
                }
            )
    return {
        "valid": bool(pages),
        "page_count": len(pages),
        "total_bytes": path.stat().st_size,
        "text_characters": text_characters,
        "blank_pages": sum(page["blank"] for page in pages),
        "pages": pages,
    }


def inspect_text_directory(path: Path) -> dict[str, Any]:
    files = sorted(path.glob("*.txt"))
    text = "\n".join(
        file_path.read_text(encoding="utf-8", errors="replace")
        for file_path in files
    )
    return {
        "valid": bool(files),
        "file_count": len(files),
        "total_bytes": sum(file_path.stat().st_size for file_path in files),
        "text_characters": sum(not character.isspace() for character in text),
    }


def load_controlled_documents(
    *,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    corpus_dir: Path = DEFAULT_CORPUS_DIR,
) -> tuple[ValidationDocument, ...]:
    documents = []
    for document in load_corpus_manifest(manifest_path):
        path = corpus_dir / document.filename
        if not path.is_file():
            raise RhwpValidationError(f"Controlled corpus file is missing: {path}")
        documents.append(
            ValidationDocument(
                id=document.id,
                format=document.format,
                path=path,
                bytes=document.expected_bytes,
                sha256=document.sha256,
                classification=(
                    "hwp5-ole" if document.format == "hwp" else "hwpx-zip"
                ),
                source=(
                    f"{document.source['repository']}@"
                    f"{document.source['commit'][:8]}"
                ),
            )
        )
    return tuple(documents)


def load_archive_documents(
    manifest_path: Path = DEFAULT_ARCHIVE_MANIFEST,
) -> tuple[ValidationDocument, ...]:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = payload.get("records")
    if not isinstance(records, list):
        raise RhwpValidationError("Archive manifest records must be an array.")
    root = manifest_path.parent
    documents: list[ValidationDocument] = []
    seen_hashes: set[str] = set()
    for record in records:
        sha256 = str(record["sha256"])
        if SHA256_PATTERN.fullmatch(sha256) is None:
            raise RhwpValidationError(
                "Archive manifest sha256 must contain 64 hexadecimal characters."
            )
        if sha256 in seen_hashes:
            continue
        seen_hashes.add(sha256)
        classification = str(record["classification"])
        actual_format = IN_SCOPE_CLASSIFICATIONS.get(classification)
        if actual_format is None:
            continue
        relative_path = Path(str(record["unique_relative_path"]))
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise RhwpValidationError(
                "Archived unique path must stay within the manifest directory."
            )
        path = (root / relative_path).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError as exc:
            raise RhwpValidationError(
                "Archived unique path must stay within the manifest directory."
            ) from exc
        if not path.is_file():
            raise RhwpValidationError(f"Archived unique file is missing: {path}")
        documents.append(
            ValidationDocument(
                id=sha256[:16],
                format=actual_format,
                path=path,
                bytes=int(record["bytes"]),
                sha256=sha256,
                classification=classification,
                source=f"{record['repository']}:{record['path']}",
            )
        )
    return tuple(sorted(documents, key=lambda document: document.sha256))


def _output_path(result_dir: Path, *parts: str) -> tuple[Path, str]:
    if any(SAFE_PATH_COMPONENT.fullmatch(part) is None for part in parts):
        raise RhwpValidationError("Artifact path contains an unsafe component.")
    relative = Path("artifacts").joinpath(*parts)
    artifact_root = (result_dir / "artifacts").resolve()
    resolved = (result_dir / relative).resolve()
    try:
        resolved.relative_to(artifact_root)
    except ValueError as exc:
        raise RhwpValidationError(
            "Artifact path escapes the configured result directory."
        ) from exc
    return resolved, f"/output/{relative.as_posix()}"


def _backend_status(command: Mapping[str, Any], inspection: Mapping[str, Any]) -> str:
    if command["status"] != "success":
        return str(command["status"])
    if not inspection.get("valid"):
        return "invalid-output"
    return "success"


def run_controlled_document(
    runner: DockerRhwpRunner,
    document: ValidationDocument,
    *,
    result_dir: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    input_path = runner.workspace_path(document.path)
    document_root, document_output = _output_path(
        result_dir, "controlled", document.id
    )
    _clean_directory(document_root)

    svg_host = document_root / "svg"
    svg_host.mkdir()
    svg_command = runner.run(
        [
            "export-svg",
            input_path,
            "-o",
            f"{document_output}/svg",
            "--font-style",
        ],
        timeout_seconds,
    )
    svg_inspection = inspect_svg_directory(svg_host)
    svg_record = {
        **_command_record(svg_command),
        **svg_inspection,
    }
    svg_record["status"] = _backend_status(
        _command_record(svg_command), svg_inspection
    )

    pdf_host = document_root / f"{document.id}.pdf"
    pdf_command = runner.run(
        [
            "export-pdf",
            input_path,
            "-o",
            f"{document_output}/{document.id}.pdf",
            "--font-path",
            "/usr/share/fonts/opentype/noto",
            "--fallback-serif",
            "Noto Serif CJK KR",
            "--fallback-sans",
            "Noto Sans CJK KR",
            "--fallback-mono",
            "DejaVu Sans Mono",
        ],
        timeout_seconds,
    )
    pdf_inspection = inspect_pdf(pdf_host)
    pdf_record = {
        **_command_record(pdf_command),
        **pdf_inspection,
    }
    pdf_record["status"] = _backend_status(
        _command_record(pdf_command), pdf_inspection
    )

    png_host = document_root / "png"
    png_host.mkdir()
    png_command = runner.run(
        [
            "export-png",
            input_path,
            "-o",
            f"{document_output}/png",
            "--font-path",
            "/usr/share/fonts/opentype/noto",
            "--vlm-target",
            CONTROLLED_VLM_TARGET,
        ],
        timeout_seconds,
    )
    png_inspection = inspect_png_directory(png_host)
    png_record = {
        **_command_record(png_command),
        **png_inspection,
    }
    png_record["status"] = _backend_status(
        _command_record(png_command), png_inspection
    )

    backends = {"svg": svg_record, "pdf": pdf_record, "png": png_record}
    page_counts = [
        int(record["page_count"])
        for record in backends.values()
        if record["status"] == "success"
    ]
    all_backends_succeeded = all(
        record["status"] == "success" for record in backends.values()
    )
    page_counts_agree = (
        len(page_counts) == len(backends) and len(set(page_counts)) == 1
    )
    return {
        "document_id": document.id,
        "format": document.format,
        "bytes": document.bytes,
        "sha256": document.sha256,
        "classification": document.classification,
        "source": document.source,
        "status": (
            "success"
            if all_backends_succeeded and page_counts_agree
            else "failed"
        ),
        "page_counts_agree": page_counts_agree,
        "backends": backends,
    }


def run_sweep_document(
    runner: DockerRhwpRunner,
    document: ValidationDocument,
    *,
    result_dir: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    input_path = runner.workspace_path(document.path)
    document_root, document_output = _output_path(result_dir, "sweep", document.id)
    _clean_directory(document_root)

    text_host = document_root / "text"
    text_host.mkdir()
    text_command = runner.run(
        [
            "export-text",
            input_path,
            "-p",
            "0",
            "-o",
            f"{document_output}/text",
        ],
        timeout_seconds,
    )
    text_inspection = inspect_text_directory(text_host)
    text_record = {
        **_command_record(text_command),
        **text_inspection,
    }
    text_record["status"] = _backend_status(
        _command_record(text_command), text_inspection
    )

    png_host = document_root / "png"
    png_host.mkdir()
    png_command = runner.run(
        [
            "export-png",
            input_path,
            "-p",
            "0",
            "-o",
            f"{document_output}/png",
            "--font-path",
            "/usr/share/fonts/opentype/noto",
            "--vlm-target",
            SWEEP_VLM_TARGET,
        ],
        timeout_seconds,
    )
    png_inspection = inspect_png_directory(png_host)
    png_record = {
        **_command_record(png_command),
        **png_inspection,
    }
    png_record["status"] = _backend_status(
        _command_record(png_command), png_inspection
    )
    succeeded = text_record["status"] == "success" and png_record["status"] == "success"
    return {
        "document_id": document.id,
        "format": document.format,
        "bytes": document.bytes,
        "sha256": document.sha256,
        "classification": document.classification,
        "source": document.source,
        "status": "success" if succeeded else "failed",
        "visual_only": (
            succeeded
            and text_record["text_characters"] == 0
            and png_record["blank_pages"] == 0
        ),
        "text": text_record,
        "png": png_record,
    }


def _empty_results(image: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": None,
        "rhwp": {
            "version": RHWP_VERSION,
            "commit": RHWP_COMMIT,
            "docker_image": image,
            "image_id": None,
        },
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "controlled": [],
        "sweep": [],
    }


def _load_results(result_dir: Path, image: str) -> dict[str, Any]:
    path = result_dir / "results.json"
    if not path.is_file():
        return _empty_results(image)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise RhwpValidationError("Unsupported rhwp validation results schema.")
    return payload


def _save_results(result_dir: Path, payload: Mapping[str, Any]) -> Path:
    result_dir.mkdir(parents=True, exist_ok=True)
    path = result_dir / "results.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def run_controlled(
    *,
    image: str = DEFAULT_DOCKER_IMAGE,
    result_dir: Path = DEFAULT_RESULT_DIR,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    corpus_dir: Path = DEFAULT_CORPUS_DIR,
    timeout_seconds: float = 600.0,
    limit: int | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    documents = list(
        load_controlled_documents(
            manifest_path=manifest_path,
            corpus_dir=corpus_dir,
        )
    )
    if limit is not None:
        documents = documents[:limit]
    payload = _load_results(result_dir, image)
    existing = {
        record["document_id"]: record
        for record in payload.get("controlled", [])
        if record.get("status") == "success"
    }
    records: list[dict[str, Any]] = []
    with DockerRhwpRunner(
        image=image,
        workspace_root=REPOSITORY_ROOT,
        output_root=result_dir,
    ) as runner:
        payload["rhwp"]["image_id"] = runner.image_id
        payload["rhwp"]["runtime_version"] = runner.version
        for index, document in enumerate(documents, start=1):
            if resume and document.id in existing:
                record = existing[document.id]
            else:
                record = run_controlled_document(
                    runner,
                    document,
                    result_dir=result_dir,
                    timeout_seconds=timeout_seconds,
                )
            records.append(record)
            payload["controlled"] = records
            payload["generated_at"] = datetime.now(timezone.utc).isoformat()
            _save_results(result_dir, payload)
            print(
                f"[controlled {index}/{len(documents)}] "
                f"{document.id}: {record['status']}",
                flush=True,
            )
    return payload


def run_sweep(
    *,
    image: str = DEFAULT_DOCKER_IMAGE,
    result_dir: Path = DEFAULT_RESULT_DIR,
    archive_manifest: Path = DEFAULT_ARCHIVE_MANIFEST,
    timeout_seconds: float = 120.0,
    workers: int = 4,
    limit: int | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    documents = list(load_archive_documents(archive_manifest))
    if limit is not None:
        documents = documents[:limit]
    payload = _load_results(result_dir, image)
    existing = {
        record["sha256"]: record
        for record in payload.get("sweep", [])
        if record.get("status") == "success"
    }
    retained = [
        existing[document.sha256]
        for document in documents
        if resume and document.sha256 in existing
    ]
    pending = [
        document
        for document in documents
        if not (resume and document.sha256 in existing)
    ]
    records = list(retained)
    with DockerRhwpRunner(
        image=image,
        workspace_root=REPOSITORY_ROOT,
        output_root=result_dir,
    ) as runner:
        payload["rhwp"]["image_id"] = runner.image_id
        payload["rhwp"]["runtime_version"] = runner.version
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, workers)
        ) as executor:
            futures = {
                executor.submit(
                    run_sweep_document,
                    runner,
                    document,
                    result_dir=result_dir,
                    timeout_seconds=timeout_seconds,
                ): document
                for document in pending
            }
            completed = 0
            for future in concurrent.futures.as_completed(futures):
                document = futures[future]
                record = future.result()
                records.append(record)
                completed += 1
                if completed % 10 == 0 or completed == len(pending):
                    records.sort(key=lambda row: row["sha256"])
                    payload["sweep"] = records
                    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
                    _save_results(result_dir, payload)
                if (
                    record["status"] != "success"
                    or completed % 10 == 0
                    or completed == len(pending)
                ):
                    print(
                        f"[sweep {completed}/{len(pending)}] "
                        f"{document.id}: {record['status']}",
                        flush=True,
                    )
    records.sort(key=lambda row: row["sha256"])
    payload["sweep"] = records
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    _save_results(result_dir, payload)
    return payload


def render_html_report(payload: Mapping[str, Any]) -> str:
    controlled = tuple(payload.get("controlled", ()))
    sweep = tuple(payload.get("sweep", ()))
    controlled_success = sum(
        record.get("status") == "success" for record in controlled
    )
    sweep_success = sum(record.get("status") == "success" for record in sweep)
    sweep_failed = len(sweep) - sweep_success
    visual_only = sum(bool(record.get("visual_only")) for record in sweep)
    controlled_pages = sum(
        int(record.get("backends", {}).get("png", {}).get("page_count", 0))
        for record in controlled
        if record.get("status") == "success"
    )
    page_agreement = sum(
        bool(record.get("page_counts_agree")) for record in controlled
    )
    blank_pages = sum(
        int(record.get("backends", {}).get(backend, {}).get("blank_pages", 0))
        for record in controlled
        for backend in ("svg", "pdf", "png")
    )

    latency_rows: list[str] = []
    for file_format in ("hwp", "hwpx"):
        records = [
            record
            for record in controlled
            if record.get("format") == file_format
            and record.get("status") == "success"
        ]
        for backend in ("svg", "pdf", "png"):
            durations = [
                float(record["backends"][backend]["duration_seconds"])
                for record in records
                if record.get("backends", {}).get(backend, {}).get("valid")
            ]
            latency_rows.append(
                "<tr>"
                f"<td>{escape(file_format.upper())}</td>"
                f"<td>{escape(backend.upper())}</td>"
                f"<td>{median(durations):.2f}s</td>"
                f"<td>{max(durations):.2f}s</td>"
                "</tr>"
                if durations
                else "<tr>"
                f"<td>{escape(file_format.upper())}</td>"
                f"<td>{escape(backend.upper())}</td>"
                "<td>-</td><td>-</td></tr>"
            )

    controlled_rows: list[str] = []
    for record in controlled:
        backends = record.get("backends", {})
        backend_status = " / ".join(
            f"{name.upper()} "
            f"{'OK' if backends.get(name, {}).get('valid') else 'FAIL'}"
            for name in ("svg", "pdf", "png")
        )
        warning_count = sum(
            len(backends.get(name, {}).get("warnings", ()))
            for name in ("svg", "pdf", "png")
        )
        row_class = "good" if record.get("status") == "success" else "bad"
        controlled_rows.append(
            f'<tr data-format="{escape(str(record.get("format", "")), quote=True)}" '
            f'data-status="{escape(str(record.get("status", "")), quote=True)}">'
            f'<td><strong>{escape(str(record.get("document_id", "")))}</strong></td>'
            f'<td>{escape(str(record.get("format", "")).upper())}</td>'
            f'<td class="{row_class}">{escape(str(record.get("status", "")))}</td>'
            f'<td>{escape(backend_status)}</td>'
            f'<td>{"yes" if record.get("page_counts_agree") else "no"}</td>'
            f"<td>{warning_count}</td>"
            "</tr>"
        )

    failure_rows: list[str] = []
    for record in sweep:
        if record.get("status") == "success":
            continue
        text = record.get("text", {})
        png = record.get("png", {})
        reason = (
            text.get("stderr_tail")
            or png.get("stderr_tail")
            or text.get("status")
            or png.get("status")
            or "unknown"
        )
        failure_rows.append(
            "<tr>"
            f"<td>{escape(str(record.get('source', '')))}</td>"
            f"<td>{escape(str(record.get('classification', '')))}</td>"
            f"<td><code>{escape(str(reason))}</code></td>"
            "</tr>"
        )
    if not failure_rows:
        failure_rows.append(
            '<tr><td colspan="3">실패 문서가 없습니다.</td></tr>'
        )

    rhwp = payload.get("rhwp", {})
    generated_at = payload.get("generated_at") or "not generated"
    controlled_rate = _report_percent(controlled_success, len(controlled))
    sweep_rate = _report_percent(sweep_success, len(sweep))
    decision = (
        "채택"
        if controlled_success == len(controlled)
        and len(controlled) > 0
        and sweep_rate >= 99.0
        else "추가 검토"
    )

    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>rhwp 도입 검증 보고서</title>
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
      line-height: 1.55;
    }}
    main {{ width: min(1180px, calc(100% - 32px)); margin: 0 auto; padding: 48px 0 80px; }}
    h1 {{ font-size: clamp(2rem, 5vw, 3.7rem); line-height: 1.08; margin: 8px 0 16px; }}
    h2 {{ margin: 0 0 12px; }}
    p {{ color: var(--cp-text-muted); }}
    a {{ color: var(--cp-link); }}
    code {{ font-family: Consolas, "Courier New", Courier, monospace; overflow-wrap: anywhere; }}
    .eyebrow {{ color: var(--cp-accent); font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }}
    .hero {{ padding: 28px 0 24px; border-bottom: 1px solid var(--cp-border); }}
    .decision {{
      margin: 28px 0;
      padding: 24px;
      background: var(--cp-accent-soft);
      border: 1px solid var(--cp-accent);
      border-radius: 16px;
    }}
    .decision strong {{ color: var(--cp-accent); font-size: 1.4rem; }}
    .stats {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin: 24px 0; }}
    .card, section.panel {{
      background: var(--cp-surface);
      border: 1px solid var(--cp-border);
      border-radius: 16px;
    }}
    .card {{ padding: 20px; }}
    .card b {{ display: block; font-size: 1.8rem; }}
    .card span {{ color: var(--cp-text-muted); }}
    section.panel {{ margin-top: 20px; padding: 24px; }}
    .flow {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }}
    .flow div {{ padding: 16px; background: var(--cp-surface-soft); border-radius: 0.625rem; }}
    .flow b {{ display: block; color: var(--cp-accent); }}
    .table-wrap {{ overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: 11px 12px; text-align: left; border-bottom: 1px solid var(--cp-border); vertical-align: top; }}
    th {{ color: var(--cp-text-muted); background: var(--cp-surface-soft); }}
    .good {{ color: var(--cp-success); font-weight: 700; }}
    .bad {{ color: var(--cp-danger); font-weight: 700; }}
    .muted {{ color: var(--cp-text-muted); }}
    input {{
      width: 100%;
      margin: 8px 0 14px;
      padding: 10px 12px;
      color: var(--cp-text);
      background: var(--cp-surface-soft);
      border: 1px solid var(--cp-border);
      border-radius: 0.625rem;
      font: inherit;
    }}
    @media (max-width: 760px) {{
      main {{ width: min(100% - 20px, 1180px); padding-top: 24px; }}
      .stats, .flow {{ grid-template-columns: 1fr 1fr; }}
      section.panel {{ padding: 16px; }}
    }}
  </style>
</head>
<body>
  <main>
    <header class="hero">
      <div class="eyebrow">CrewMeal 기술 의사결정</div>
      <h1>rhwp 도입 검증 보고서</h1>
      <p>HWP5/HWPX parser, semantic RenderTree, SVG·PDF·PNG 렌더링과 전체 샘플
      아카이브 호환성을 같은 고정 버전에서 검증했습니다.</p>
      <p class="muted">생성: <code>{escape(str(generated_at))}</code> · rhwp
      <code>{escape(str(rhwp.get("version", "")))}</code> · commit
      <code>{escape(str(rhwp.get("commit", "")))}</code></p>
    </header>

    <div class="decision">
      <div class="eyebrow">최종 결정</div>
      <strong>{decision}: HWP/HWPX 공식 엔진으로 rhwp 사용</strong>
      <p>RenderTree를 canonical semantic source로 사용합니다. 본문·표·머리말·꼬리말·각주는
      모델 없이 HTML로 정규화하고, 이미지·수식처럼 semantic payload가 없는 페이지만 PNG와
      Vision으로 보강합니다.</p>
    </div>

    <div class="stats">
      <div class="card"><b>{controlled_success}/{len(controlled)}</b><span>고정 corpus 성공 ({controlled_rate:.2f}%)</span></div>
      <div class="card"><b>{controlled_pages}</b><span>backend 일치 검증 페이지</span></div>
      <div class="card"><b>{sweep_success}/{len(sweep)}</b><span>고유 archive 성공 ({sweep_rate:.2f}%)</span></div>
      <div class="card"><b>{visual_only}</b><span>첫 페이지 text=0 · PNG nonblank</span></div>
    </div>

    <section class="panel">
      <h2>도입 아키텍처</h2>
      <div class="flow">
        <div><b>01 검증</b>확장자·magic·크기, HWP3 제외</div>
        <div><b>02 semantic</b>rhwp RenderTree → 본문·표·주석</div>
        <div><b>03 선택 렌더</b>visual-only 페이지만 PNG</div>
        <div><b>04 게시</b>허용 태그 HTML → externalItem</div>
      </div>
      <p><a href="../../docs/rhwp-architecture.md">상세 아키텍처 문서</a></p>
    </section>

    <section class="panel">
      <h2>렌더링 무결성</h2>
      <p>페이지 수 일치 {page_agreement}/{len(controlled)}, 세 backend 합산 빈 페이지
      {blank_pages}개. latency는 문서별 subprocess 실행 시간입니다.</p>
      <div class="table-wrap">
        <table>
          <thead><tr><th>형식</th><th>backend</th><th>중앙값</th><th>최대</th></tr></thead>
          <tbody>{"".join(latency_rows)}</tbody>
        </table>
      </div>
    </section>

    <section class="panel">
      <h2>고정 20문서 상세</h2>
      <input id="filter" type="search" placeholder="문서 ID, 형식, 상태 검색" aria-label="상세 결과 검색">
      <div class="table-wrap">
        <table id="controlled">
          <thead><tr><th>문서</th><th>형식</th><th>상태</th><th>backend</th><th>페이지 일치</th><th>경고</th></tr></thead>
          <tbody>{"".join(controlled_rows)}</tbody>
        </table>
      </div>
    </section>

    <section class="panel">
      <h2>전체 archive sweep</h2>
      <p>{len(sweep)}개 고유 HWP5/HWPX 중 {sweep_success}개 성공, {sweep_failed}개 실패.
      visual-only {visual_only}개는 <strong>첫 페이지만</strong> text가 없고 PNG가 비어 있지
      않다는 뜻이며 문서 전체가 이미지 전용이라는 뜻은 아닙니다.</p>
      <div class="table-wrap">
        <table>
          <thead><tr><th>실패 원본</th><th>분류</th><th>사유</th></tr></thead>
          <tbody>{"".join(failure_rows)}</tbody>
        </table>
      </div>
    </section>

    <section class="panel">
      <h2>제약과 운영 원칙</h2>
      <p>암호화 HWP와 필수 part가 빠진 HWPX는 Vision으로 해결할 수 없어 명시적으로
      실패합니다. 한컴 전용 폰트가 없으면 fallback 폰트가 사용되므로 픽셀 단위 완전 일치는
      보장하지 않습니다. 제3자 샘플 문서는 원 저장소 라이선스와 별개로 문서 권리를 확인해야
      하므로 CrewMeal 저장소에 커밋하지 않습니다.</p>
    </section>
  </main>
  <script>
    const filter = document.getElementById("filter");
    const rows = [...document.querySelectorAll("#controlled tbody tr")];
    filter.addEventListener("input", () => {{
      const query = filter.value.trim().toLocaleLowerCase();
      for (const row of rows) {{
        row.hidden = query !== "" && !row.textContent.toLocaleLowerCase().includes(query);
      }}
    }});
  </script>
</body>
</html>
"""


def write_html_report(
    result_dir: Path,
    payload: Mapping[str, Any],
) -> Path:
    result_dir.mkdir(parents=True, exist_ok=True)
    path = result_dir / "report.html"
    path.write_text(render_html_report(payload), encoding="utf-8")
    return path


def _report_percent(successful: int, attempted: int) -> float:
    return successful / attempted * 100 if attempted else 0.0


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--image", default=DEFAULT_DOCKER_IMAGE)
    parser.add_argument("--result-dir", type=Path, default=DEFAULT_RESULT_DIR)
    parser.add_argument("--resume", action="store_true")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate rhwp text and rendering backends for CrewMeal."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    controlled = commands.add_parser(
        "controlled", help="Render the pinned 10 HWP5 + 10 HWPX corpus."
    )
    _add_common_options(controlled)
    controlled.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    controlled.add_argument("--corpus-dir", type=Path, default=DEFAULT_CORPUS_DIR)
    controlled.add_argument("--timeout", type=float, default=600.0)
    controlled.add_argument("--limit", type=int)

    sweep = commands.add_parser(
        "sweep", help="Validate text and first-page PNG across the unique archive."
    )
    _add_common_options(sweep)
    sweep.add_argument(
        "--archive-manifest", type=Path, default=DEFAULT_ARCHIVE_MANIFEST
    )
    sweep.add_argument("--timeout", type=float, default=120.0)
    sweep.add_argument("--workers", type=int, default=4)
    sweep.add_argument("--limit", type=int)

    all_parser = commands.add_parser(
        "all", help="Run the controlled render benchmark and archive sweep."
    )
    _add_common_options(all_parser)
    all_parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    all_parser.add_argument("--corpus-dir", type=Path, default=DEFAULT_CORPUS_DIR)
    all_parser.add_argument(
        "--archive-manifest", type=Path, default=DEFAULT_ARCHIVE_MANIFEST
    )
    all_parser.add_argument("--controlled-timeout", type=float, default=600.0)
    all_parser.add_argument("--controlled-limit", type=int)
    all_parser.add_argument("--sweep-timeout", type=float, default=120.0)
    all_parser.add_argument("--workers", type=int, default=4)
    all_parser.add_argument("--limit", type=int)

    report = commands.add_parser(
        "report", help="Regenerate the self-contained HTML decision report."
    )
    _add_common_options(report)
    return parser


def main() -> None:
    args = build_argument_parser().parse_args()
    if args.command == "report":
        payload = _load_results(args.result_dir, args.image)
        report_path = write_html_report(args.result_dir, payload)
        print(
            json.dumps(
                {
                    "results": str(args.result_dir / "results.json"),
                    "report": str(report_path),
                    "controlled": len(payload.get("controlled", [])),
                    "sweep": len(payload.get("sweep", [])),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if args.command in {"controlled", "all"}:
        payload = run_controlled(
            image=args.image,
            result_dir=args.result_dir,
            manifest_path=args.manifest,
            corpus_dir=args.corpus_dir,
            timeout_seconds=(
                args.timeout
                if args.command == "controlled"
                else args.controlled_timeout
            ),
            limit=(
                args.limit
                if args.command == "controlled"
                else args.controlled_limit
            ),
            resume=args.resume,
        )
    else:
        payload = _load_results(args.result_dir, args.image)
    if args.command in {"sweep", "all"}:
        payload = run_sweep(
            image=args.image,
            result_dir=args.result_dir,
            archive_manifest=args.archive_manifest,
            timeout_seconds=(
                args.timeout if args.command == "sweep" else args.sweep_timeout
            ),
            workers=args.workers,
            limit=args.limit,
            resume=args.resume,
        )
    report_path = write_html_report(args.result_dir, payload)
    print(
        json.dumps(
            {
                "results": str(args.result_dir / "results.json"),
                "report": str(report_path),
                "controlled": len(payload.get("controlled", [])),
                "sweep": len(payload.get("sweep", [])),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

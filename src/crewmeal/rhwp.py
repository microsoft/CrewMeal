"""Safe subprocess boundary for the pinned rhwp document engine."""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RHWP_VERSION = "0.7.19"
RHWP_COMMIT = "8d3bfa4b92174b16bac587fe1409975cf34ba566"

_RENDER_TREE_NAME = re.compile(r"render_tree_(\d+)\.json")
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_WARNING_MARKERS = ("warning", "warn", "경고", "layout_overflow")


class RhwpError(RuntimeError):
    """Raised when rhwp cannot produce a complete, valid output."""


class RhwpEncryptedError(RhwpError):
    """Raised when rhwp reports an encrypted source document."""


class RhwpInvalidFileError(RhwpError):
    """Raised when rhwp reports a malformed or unsupported source document."""


class RhwpTimeoutError(RhwpError):
    """Raised when a bounded rhwp subprocess exceeds its deadline."""


@dataclass(frozen=True, slots=True)
class RhwpRenderTreeResult:
    pages: Mapping[int, Mapping[str, Any]]
    warnings: tuple[str, ...]
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class RhwpPngResult:
    page_images: Mapping[int, bytes]
    warnings: tuple[str, ...]
    elapsed_seconds: float


def extract_render_trees(
    source_path: Path,
    output_dir: Path,
    *,
    rhwp_path: Path,
    timeout_seconds: float,
) -> RhwpRenderTreeResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    completed = _run_rhwp(
        [
            str(rhwp_path),
            "export-render-tree",
            str(source_path),
            "-o",
            str(output_dir),
        ],
        timeout_seconds=timeout_seconds,
    )
    elapsed = time.perf_counter() - started
    output_text = _combined_output(completed)

    files = sorted(output_dir.glob("render_tree_*.json"))
    if completed.returncode != 0 or not files:
        _raise_for_failure(
            output_text,
            fallback="rhwp did not produce any RenderTree pages.",
        )

    pages: dict[int, Mapping[str, Any]] = {}
    for path in files:
        match = _RENDER_TREE_NAME.fullmatch(path.name)
        if match is None:
            continue
        page_number = int(match.group(1))
        if page_number in pages:
            raise RhwpError(f"rhwp produced duplicate page {page_number}.")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RhwpError(f"Cannot read rhwp RenderTree page {page_number}: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("type") != "Page":
            raise RhwpError(
                f"rhwp RenderTree page {page_number} has an invalid root node."
            )
        pages[page_number] = payload

    expected = set(range(1, len(pages) + 1))
    if set(pages) != expected:
        raise RhwpError(
            "rhwp RenderTree page numbers are incomplete: "
            f"expected {sorted(expected)}, got {sorted(pages)}."
        )

    return RhwpRenderTreeResult(
        pages=pages,
        warnings=_warning_lines(output_text),
        elapsed_seconds=elapsed,
    )


def export_png_pages(
    source_path: Path,
    output_dir: Path,
    page_numbers: Sequence[int],
    *,
    rhwp_path: Path,
    dpi: int,
    timeout_seconds: float,
) -> RhwpPngResult:
    if dpi <= 0:
        raise ValueError("dpi must be positive.")

    requested = tuple(sorted(set(page_numbers)))
    if any(page_number <= 0 for page_number in requested):
        raise ValueError("rhwp page numbers must be one-based and positive.")

    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    page_images: dict[int, bytes] = {}
    warnings: list[str] = []

    for page_number in requested:
        page_dir = output_dir / f"page-{page_number:04d}"
        page_dir.mkdir(parents=True, exist_ok=False)
        completed = _run_rhwp(
            [
                str(rhwp_path),
                "export-png",
                str(source_path),
                "-o",
                str(page_dir),
                "-p",
                str(page_number - 1),
                "--dpi",
                str(dpi),
            ],
            timeout_seconds=timeout_seconds,
        )
        output_text = _combined_output(completed)
        png_files = tuple(page_dir.glob("*.png"))
        if completed.returncode != 0 or len(png_files) != 1:
            _raise_for_failure(
                output_text,
                fallback=f"rhwp did not produce PNG page {page_number}.",
            )
        try:
            image = png_files[0].read_bytes()
        except OSError as exc:
            raise RhwpError(f"Cannot read rhwp PNG page {page_number}: {exc}") from exc
        if not image.startswith(_PNG_MAGIC):
            raise RhwpError(f"rhwp PNG page {page_number} is not a valid PNG file.")
        page_images[page_number] = image
        warnings.extend(_warning_lines(output_text))

    return RhwpPngResult(
        page_images=page_images,
        warnings=tuple(dict.fromkeys(warnings)),
        elapsed_seconds=time.perf_counter() - started,
    )


def _run_rhwp(
    command: Sequence[str],
    *,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RhwpTimeoutError(
            f"rhwp exceeded the {timeout_seconds:g} second timeout."
        ) from exc
    except OSError as exc:
        raise RhwpError(f"Cannot execute rhwp at '{command[0]}': {exc}") from exc


def _combined_output(completed: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(
        value.strip() for value in (completed.stdout, completed.stderr) if value.strip()
    )


def _warning_lines(output: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            line.strip()
            for line in output.splitlines()
            if line.strip()
            and any(marker in line.casefold() for marker in _WARNING_MARKERS)
        )
    )


def _raise_for_failure(output: str, *, fallback: str) -> None:
    message = output.strip() or fallback
    lowered = message.casefold()
    if "암호화" in message or "encrypted" in lowered or "password" in lowered:
        raise RhwpEncryptedError(message)
    invalid_markers = (
        "invalidfile",
        "invalid file",
        "유효하지 않은 파일",
        "필수 항목",
        "필수 파일",
        "unsupported",
        "지원하지 않습니다",
    )
    if any(marker in lowered for marker in invalid_markers):
        raise RhwpInvalidFileError(message)
    raise RhwpError(message)

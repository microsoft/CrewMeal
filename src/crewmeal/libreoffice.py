from __future__ import annotations

import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import fitz

from crewmeal.models import RendererManifest


class LibreOfficeConversionError(RuntimeError):
    """Raised when LibreOffice cannot produce a valid PDF."""


@dataclass(frozen=True, slots=True)
class ConversionResult:
    pdf_path: Path
    conversion_seconds: float
    stdout: str
    stderr: str


def convert_document_to_pdf(
    source_path: Path,
    output_dir: Path,
    *,
    soffice_path: Path,
    pdf_filter: str = "pdf",
    timeout_seconds: float = 180,
) -> ConversionResult:
    """Convert any LibreOffice-importable document to PDF via headless soffice.

    ``pdf_filter`` selects the export filter, e.g. ``pdf:impress_pdf_Export`` for
    presentations or ``pdf:writer_pdf_Export`` for word-processor documents
    (including HWP, which imports into Writer).
    """

    if not source_path.is_file():
        raise FileNotFoundError(f"Document not found: {source_path}")
    if not soffice_path.is_file():
        raise FileNotFoundError(f"LibreOffice not found: {soffice_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    output_pdf = output_dir / f"{source_path.stem}.pdf"
    if output_pdf.exists():
        output_pdf.unlink()

    with tempfile.TemporaryDirectory(prefix="crewmeal-lo-profile-") as profile:
        profile_uri = Path(profile).resolve().as_uri()
        command = [
            str(soffice_path),
            "--headless",
            "--nologo",
            "--nodefault",
            "--nolockcheck",
            "--nofirststartwizard",
            f"-env:UserInstallation={profile_uri}",
            "--convert-to",
            pdf_filter,
            "--outdir",
            str(output_dir),
            str(source_path),
        ]
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise LibreOfficeConversionError(
                f"LibreOffice conversion exceeded {timeout_seconds:.0f} seconds."
            ) from exc
        elapsed = time.perf_counter() - started

    if completed.returncode != 0:
        raise LibreOfficeConversionError(
            "LibreOffice conversion failed "
            f"(exit {completed.returncode}). stderr: {completed.stderr.strip()}"
        )
    if not output_pdf.is_file():
        raise LibreOfficeConversionError(
            "LibreOffice reported success but did not create the expected PDF. "
            f"stdout: {completed.stdout.strip()}"
        )
    if not output_pdf.read_bytes().startswith(b"%PDF-"):
        raise LibreOfficeConversionError(
            "LibreOffice created a file that is not a valid PDF."
        )

    return ConversionResult(
        pdf_path=output_pdf,
        conversion_seconds=elapsed,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def convert_pptx_to_pdf(
    pptx_path: Path,
    output_dir: Path,
    *,
    soffice_path: Path,
    timeout_seconds: float = 180,
) -> ConversionResult:
    return convert_document_to_pdf(
        pptx_path,
        output_dir,
        soffice_path=soffice_path,
        pdf_filter="pdf:impress_pdf_Export",
        timeout_seconds=timeout_seconds,
    )


def convert_hwp_to_pdf(
    hwp_path: Path,
    output_dir: Path,
    *,
    soffice_path: Path,
    timeout_seconds: float = 180,
) -> ConversionResult:
    """Run the legacy LibreOffice HWP baseline used by the parser benchmark.

    Production HWP/HWPX processing uses rhwp. This helper remains only to
    reproduce the pre-rhwp comparison result.
    """

    return convert_document_to_pdf(
        hwp_path,
        output_dir,
        soffice_path=soffice_path,
        pdf_filter="pdf:writer_pdf_Export",
        timeout_seconds=timeout_seconds,
    )


def inspect_pdf(
    pdf_path: Path,
    *,
    render_dpi: int = 144,
) -> RendererManifest:
    if render_dpi <= 0:
        raise ValueError("render_dpi must be positive.")

    texts_by_page: dict[int, tuple[str, ...]] = {}
    links_by_page: dict[int, tuple[str, ...]] = {}
    page_images: dict[int, bytes] = {}
    matrix = fitz.Matrix(render_dpi / 72, render_dpi / 72)

    try:
        document = fitz.open(pdf_path)
    except (fitz.FileDataError, RuntimeError) as exc:
        raise LibreOfficeConversionError(f"Cannot open rendered PDF: {exc}") from exc

    with document:
        if document.needs_pass:
            raise LibreOfficeConversionError("The rendered PDF is encrypted.")

        for page_index, page in enumerate(document, start=1):
            blocks = page.get_text("blocks", sort=True)
            texts_by_page[page_index] = tuple(
                text
                for block in blocks
                if len(block) >= 5
                and (text := str(block[4]).strip())
            )
            links_by_page[page_index] = tuple(
                uri
                for link in page.get_links()
                if (uri := str(link.get("uri", "")).strip())
            )
            page_images[page_index] = page.get_pixmap(
                matrix=matrix,
                alpha=False,
            ).tobytes("png")

        return RendererManifest(
            page_count=document.page_count,
            texts_by_page=texts_by_page,
            links_by_page=links_by_page,
            page_images=page_images,
            render_dpi=render_dpi,
        )

"""PDF document handler.

PDFs are already page-based, so there is no LibreOffice conversion step: the
source *is* the render target. We use PyMuPDF (via :func:`inspect_pdf`) to
render page images and extract per-page text evidence, then feed the shared
vision-analysis pipeline. Encrypted PDFs are rejected here and will be routed
through the decryption pipeline once that is enabled.
"""

from __future__ import annotations

import hashlib
import tempfile
import time
from pathlib import Path

import fitz

from crewmeal.config import AppConfig
from crewmeal.libreoffice import inspect_pdf
from crewmeal.models import SourceManifest
from crewmeal.search_enhancement.formats.base import (
    EncryptedDocumentError,
    InvalidDocumentError,
    PreparedDocument,
)
from crewmeal.search_enhancement.progress import ProgressReporter, Stage

PDF_CONTENT_TYPE = "application/pdf"


class PdfHandler:
    """Detect, validate, and prepare PDF documents."""

    format_id = "pdf"
    display_name = "PDF"
    extensions = frozenset({".pdf"})
    content_types = frozenset({PDF_CONTENT_TYPE})
    supported = True

    def validate(self, data: bytes, *, filename: str, max_bytes: int) -> None:
        if Path(filename).suffix.lower() != ".pdf":
            raise InvalidDocumentError("Only .pdf files are supported by the PDF handler.")
        if not data:
            raise InvalidDocumentError("The uploaded PDF is empty.")
        if len(data) > max_bytes:
            raise InvalidDocumentError(
                f"The PDF exceeds the {max_bytes // (1024 * 1024)} MB limit."
            )
        if not data.startswith(b"%PDF-"):
            raise InvalidDocumentError("The file is not a valid PDF document.")

    def fingerprint(self, data: bytes) -> str:
        return f"pdf-sha256:{hashlib.sha256(data).hexdigest()}"

    def prepare(
        self,
        data: bytes,
        *,
        source_name: str,
        config: AppConfig,
        reporter: ProgressReporter,
    ) -> PreparedDocument:
        source_started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="crewmeal-pdf-") as workspace:
            pdf_path = Path(workspace) / "input.pdf"
            pdf_path.write_bytes(data)
            page_count = self._inspect_header(pdf_path)
            source_seconds = time.perf_counter() - source_started

            rendering_started = time.perf_counter()
            reporter.stage(Stage.RENDERING, detail={"total": page_count})
            renderer_manifest = inspect_pdf(
                pdf_path,
                render_dpi=config.slide_image_render_dpi,
            )
            rendering_seconds = time.perf_counter() - rendering_started

        source_manifest = SourceManifest(
            slide_count=renderer_manifest.page_count,
            texts_by_slide=dict(renderer_manifest.texts_by_page),
            links_by_slide=dict(renderer_manifest.links_by_page),
            alt_text_by_slide={},
            notes_by_slide={},
        )

        return PreparedDocument(
            source_manifest=source_manifest,
            renderer_manifest=renderer_manifest,
            geometry_by_page={},
            stage_timings={
                "sourceInspectionSeconds": source_seconds,
                "conversionSeconds": 0.0,
                "renderingSeconds": rendering_seconds,
            },
        )

    def _inspect_header(self, pdf_path: Path) -> int:
        try:
            document = fitz.open(pdf_path)
        except (fitz.FileDataError, RuntimeError) as exc:
            raise InvalidDocumentError(f"Cannot open the PDF: {exc}") from exc
        with document:
            if document.needs_pass:
                raise EncryptedDocumentError(
                    "The PDF is encrypted. Enable a decryption provider to process it."
                )
            if document.page_count <= 0:
                raise InvalidDocumentError("The PDF contains no pages.")
            return document.page_count

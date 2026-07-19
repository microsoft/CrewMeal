"""PowerPoint (.pptx) document handler.

Wraps the existing PPTX-specific logic (OOXML source manifest, LibreOffice
PDF conversion, PyMuPDF rendering, Gantt/diagram geometry facts) behind the
generic :class:`~crewmeal.search_enhancement.formats.base.DocumentHandler`
protocol so the shared pipeline no longer hard-codes presentations.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from crewmeal.config import AppConfig
from crewmeal.libreoffice import convert_pptx_to_pdf, inspect_pdf
from crewmeal.search_enhancement.formats.base import (
    PreparedDocument,
    ProcessingFidelityError,
)
from crewmeal.search_enhancement.geometry_facts import geometry_facts_by_slide
from crewmeal.search_enhancement.progress import ProgressReporter, Stage
from crewmeal.source import (
    build_source_manifest,
    pptx_content_fingerprint,
    validate_pptx,
)

PPTX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)


class PptxHandler:
    """Detect, validate, and prepare PowerPoint presentations."""

    format_id = "pptx"
    display_name = "PowerPoint"
    extensions = frozenset({".pptx"})
    content_types = frozenset({PPTX_CONTENT_TYPE})
    supported = True

    def validate(self, data: bytes, *, filename: str, max_bytes: int) -> None:
        validate_pptx(data, filename=filename, max_bytes=max_bytes)

    def fingerprint(self, data: bytes) -> str:
        return pptx_content_fingerprint(data)

    def prepare(
        self,
        data: bytes,
        *,
        source_name: str,
        config: AppConfig,
        reporter: ProgressReporter,
    ) -> PreparedDocument:
        source_started = time.perf_counter()
        source_manifest = build_source_manifest(
            data,
            filename=source_name,
            max_bytes=config.max_upload_bytes,
        )
        source_seconds = time.perf_counter() - source_started

        with tempfile.TemporaryDirectory(prefix="crewmeal-search-") as workspace:
            workspace_path = Path(workspace)
            pptx_path = workspace_path / "input.pptx"
            pptx_path.write_bytes(data)
            reporter.stage(Stage.CONVERTING, message="LibreOffice PPTX→PDF")
            conversion = convert_pptx_to_pdf(
                pptx_path,
                workspace_path,
                soffice_path=config.require_soffice(),
            )
            rendering_started = time.perf_counter()
            reporter.stage(
                Stage.RENDERING,
                detail={"total": source_manifest.slide_count},
            )
            renderer_manifest = inspect_pdf(
                conversion.pdf_path,
                render_dpi=config.slide_image_render_dpi,
            )
            rendering_seconds = time.perf_counter() - rendering_started

        if renderer_manifest.page_count != source_manifest.slide_count:
            raise ProcessingFidelityError(
                "LibreOffice changed the presentation page count."
            )

        reporter.stage(Stage.GEOMETRY)
        geometry_by_page = geometry_facts_by_slide(data)

        return PreparedDocument(
            source_manifest=source_manifest,
            renderer_manifest=renderer_manifest,
            geometry_by_page=geometry_by_page,
            stage_timings={
                "sourceInspectionSeconds": source_seconds,
                "conversionSeconds": conversion.conversion_seconds,
                "renderingSeconds": rendering_seconds,
            },
        )

"""PowerPoint (.pptx) document handler.

Wraps the existing PPTX-specific logic (OOXML source manifest, LibreOffice
PDF conversion, PyMuPDF rendering, Gantt/diagram geometry facts) behind the
generic :class:`~crewmeal.search_enhancement.formats.base.DocumentHandler`
protocol so the shared pipeline no longer hard-codes presentations.
"""

from __future__ import annotations

import tempfile
import time
from dataclasses import replace
from pathlib import Path

from crewmeal.config import AppConfig
from crewmeal.libreoffice import convert_pptx_to_pdf, inspect_pdf
from crewmeal.models import RendererManifest, SourceManifest
from crewmeal.search_enhancement.formats.base import (
    PreparedDocument,
    ProcessingFidelityError,
)
from crewmeal.search_enhancement.geometry_facts import geometry_facts_by_slide
from crewmeal.search_enhancement.pptx_semantic import extract_semantic_slides
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

        if config.pptx_semantic_text_slides:
            return self._prepare_semantic_first(
                data,
                source_manifest=source_manifest,
                source_seconds=source_seconds,
                config=config,
                reporter=reporter,
            )
        return self._prepare_visual_first(
            data,
            source_manifest=source_manifest,
            source_seconds=source_seconds,
            config=config,
            reporter=reporter,
        )

    def _prepare_visual_first(
        self,
        data: bytes,
        *,
        source_manifest: SourceManifest,
        source_seconds: float,
        config: AppConfig,
        reporter: ProgressReporter,
    ) -> PreparedDocument:
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

    def _prepare_semantic_first(
        self,
        data: bytes,
        *,
        source_manifest: SourceManifest,
        source_seconds: float,
        config: AppConfig,
        reporter: ProgressReporter,
    ) -> PreparedDocument:
        reporter.stage(Stage.CONVERTING, message="OOXML 텍스트 슬라이드 판별")
        semantic_started = time.perf_counter()
        semantic = extract_semantic_slides(data)
        semantic_seconds = time.perf_counter() - semantic_started

        slide_count = source_manifest.slide_count
        if len(semantic.slides) != slide_count:
            raise ProcessingFidelityError(
                "OOXML semantic extraction changed the presentation slide count."
            )
        visual_pages = semantic.visual_pages

        if not visual_pages:
            reporter.stage(
                Stage.RENDERING,
                message="semantic coverage complete; Vision skipped",
                detail={"total": 0},
            )
            renderer_manifest = RendererManifest(
                page_count=slide_count,
                texts_by_page=dict(source_manifest.texts_by_slide),
                links_by_page=dict(source_manifest.links_by_slide),
                page_images={},
                render_dpi=config.slide_image_render_dpi,
            )
            return PreparedDocument(
                source_manifest=source_manifest,
                renderer_manifest=renderer_manifest,
                geometry_by_page={},
                stage_timings={
                    "sourceInspectionSeconds": source_seconds,
                    "conversionSeconds": 0.0,
                    "semanticExtractionSeconds": semantic_seconds,
                    "renderingSeconds": 0.0,
                },
                semantic_slides=semantic.slides,
            )

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
                message="visual 슬라이드만 렌더링",
                detail={"total": len(visual_pages)},
            )
            rendered = inspect_pdf(
                conversion.pdf_path,
                render_dpi=config.slide_image_render_dpi,
            )
            rendering_seconds = time.perf_counter() - rendering_started

        if rendered.page_count != slide_count:
            raise ProcessingFidelityError(
                "LibreOffice changed the presentation page count."
            )

        page_images = {
            page: image
            for page, image in rendered.page_images.items()
            if page in visual_pages
        }
        renderer_manifest = replace(rendered, page_images=page_images)

        reporter.stage(Stage.GEOMETRY)
        geometry_by_page = {
            page: facts
            for page, facts in geometry_facts_by_slide(data).items()
            if page in visual_pages
        }

        return PreparedDocument(
            source_manifest=source_manifest,
            renderer_manifest=renderer_manifest,
            geometry_by_page=geometry_by_page,
            stage_timings={
                "sourceInspectionSeconds": source_seconds,
                "conversionSeconds": conversion.conversion_seconds,
                "semanticExtractionSeconds": semantic_seconds,
                "renderingSeconds": rendering_seconds,
            },
            semantic_slides=semantic.slides,
        )

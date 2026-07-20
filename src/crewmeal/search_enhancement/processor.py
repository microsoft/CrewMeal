from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
from typing import Any

from crewmeal.config import AppConfig
from crewmeal.models import SourceManifest
from crewmeal.rhwp import RHWP_COMMIT, RHWP_VERSION
from crewmeal.search_enhancement.decryption import maybe_decrypt
from crewmeal.search_enhancement.formats import (
    ProcessingFidelityError,
    detect_handler,
)
from crewmeal.search_enhancement.html_renderer import (
    RenderedHtml,
    render_presentation_html,
)
from crewmeal.search_enhancement.mip_sdk import MipSdkRunner
from crewmeal.search_enhancement.models import (
    ContentSection,
    ContentTable,
    SlideContent,
    StructuredAnalysisResult,
)
from crewmeal.search_enhancement.progress import (
    NullProgressReporter,
    ProgressReporter,
    Stage,
)
from crewmeal.search_enhancement.structured_analysis import (
    StructuredSlideAnalysisService,
)
from crewmeal.search_enhancement.vision_model import VisionModelSettings

__all__ = [
    "PresentationProcessor",
    "ProcessedPresentation",
    "ProcessedDocument",
    "ProcessingFidelityError",
    "semantic_analysis_result",
    "merge_semantic_and_visual",
]


@dataclass(frozen=True, slots=True)
class ProcessedPresentation:
    rendered: RenderedHtml
    analysis: StructuredAnalysisResult
    stage_timings: dict[str, float]


# Generalized alias; the pipeline now handles more than presentations.
ProcessedDocument = ProcessedPresentation


class PresentationProcessor:
    def __init__(
        self,
        config: AppConfig,
        *,
        analysis_service: StructuredSlideAnalysisService | None = None,
        vision_model: VisionModelSettings | None = None,
        decryption_settings: Mapping[str, Any] | None = None,
        mip_runner: MipSdkRunner | None = None,
    ) -> None:
        self._config = config
        self._analysis_service = analysis_service
        self._vision_model = vision_model
        # Snapshot of admin decryption toggles (``decryption.<id>.enabled``),
        # read when the worker starts. Empty/None means every provider is off.
        self._decryption_settings = decryption_settings
        # Runtime backend for MIP decryption (shells out to the MIP SDK CLI).
        # ``None`` means MIP decryption, if enabled, fails loudly instead of
        # silently passing an encrypted payload through.
        self._mip_runner = mip_runner

    def decrypt_source(
        self,
        source_bytes: bytes,
        *,
        filename: str,
        content_type: str | None = None,
    ) -> bytes:
        """Decrypt an acquired source payload if a provider is enabled and matches.

        This is the decryption *boundary* for the worker: it runs right after the
        raw bytes are downloaded/loaded and **before** anything that assumes a
        readable document (content fingerprinting, format detection). Encrypted
        payloads are not valid Office/PDF containers, so fingerprinting them would
        fail — and even if it didn't, a fingerprint over ciphertext (with random
        per-encryption nonces) would defeat change detection. Decrypting here means
        every downstream step, including the stored ``source_etag``, sees the real
        plaintext content.

        Returns the (possibly decrypted) bytes. When no provider is enabled, or
        none recognizes the payload, the input is returned unchanged. Decryption is
        idempotent for plaintext, so :meth:`process` re-running it is a safe no-op.
        """

        return maybe_decrypt(
            source_bytes,
            filename=filename,
            content_type=content_type,
            settings=self._decryption_settings,
            mip_runner=self._mip_runner,
        )

    def process(
        self,
        source_bytes: bytes,
        *,
        source_name: str,
        progress: ProgressReporter | None = None,
        corrections: Sequence[str] | None = None,
    ) -> ProcessedPresentation:
        reporter = progress or NullProgressReporter()
        reporter.stage(Stage.VALIDATING, message=source_name)

        source_bytes = self.decrypt_source(source_bytes, filename=source_name)

        handler = detect_handler(source_name)
        handler.validate(
            source_bytes,
            filename=source_name,
            max_bytes=self._config.max_upload_bytes,
        )
        prepared = handler.prepare(
            source_bytes,
            source_name=source_name,
            config=self._config,
            reporter=reporter,
        )
        source_manifest = prepared.source_manifest
        renderer_manifest = prepared.renderer_manifest

        if prepared.semantic_slides is not None:
            analysis = self._analyze_semantic_document(
                prepared.semantic_slides,
                renderer_manifest.page_images,
                source_manifest=source_manifest,
                source_name=source_name,
                geometry_by_page=prepared.geometry_by_page,
                reporter=reporter,
                corrections=corrections,
            )
        else:
            with ExitStack() as services:
                analysis_service = self._analysis_service or services.enter_context(
                    StructuredSlideAnalysisService(
                        self._config, model=self._vision_model
                    )
                )
                reporter.stage(
                    Stage.ANALYZING,
                    detail={"completed": 0, "total": source_manifest.slide_count},
                )
                analysis = analysis_service.analyze(
                    renderer_manifest.page_images,
                    source_manifest=source_manifest,
                    source_name=source_name,
                    geometry_by_slide=dict(prepared.geometry_by_page),
                    progress=reporter,
                    corrections=corrections,
                )

        reporter.stage(Stage.RENDER_HTML)
        rendered = render_presentation_html(
            source_name=source_name,
            slides=analysis.slides,
            notes_by_slide=source_manifest.notes_by_slide,
            unit_label="슬라이드" if handler.format_id == "pptx" else "페이지",
        )
        return ProcessedPresentation(
            rendered=rendered,
            analysis=analysis,
            stage_timings={
                **prepared.stage_timings,
                "analysisSeconds": analysis.analysis_seconds,
            },
        )

    def _analyze_semantic_document(
        self,
        semantic_slides: tuple[SlideContent, ...],
        page_images: Mapping[int, bytes],
        *,
        source_manifest: SourceManifest,
        source_name: str,
        geometry_by_page: Mapping[int, str],
        reporter: ProgressReporter,
        corrections: Sequence[str] | None,
    ) -> StructuredAnalysisResult:
        if not semantic_slides:
            raise ProcessingFidelityError(
                "The semantic document contains no pages."
            )
        if corrections and not page_images:
            raise ProcessingFidelityError(
                "Tuning corrections cannot be applied to a semantic-only HWP/HWPX "
                "document because no visual-analysis pages were selected."
            )

        reporter.stage(
            Stage.ANALYZING,
            message=(
                "rhwp semantic evidence; targeted Vision"
                if page_images
                else "rhwp semantic evidence; Vision skipped"
            ),
            detail={
                "completed": 0,
                "total": len(page_images),
                "semanticPages": len(semantic_slides),
            },
        )
        semantic_result = semantic_analysis_result(
            source_name=source_name,
            slides=semantic_slides,
        )
        if not page_images:
            return semantic_result

        with ExitStack() as services:
            analysis_service = self._analysis_service or services.enter_context(
                StructuredSlideAnalysisService(
                    self._config, model=self._vision_model
                )
            )
            visual_result = analysis_service.analyze(
                page_images,
                source_manifest=source_manifest,
                source_name=source_name,
                geometry_by_slide=dict(geometry_by_page),
                progress=reporter,
                corrections=corrections,
                allow_partial_pages=True,
            )
        return merge_semantic_and_visual(semantic_result, visual_result)


def semantic_analysis_result(
    *,
    source_name: str,
    slides: tuple[SlideContent, ...],
) -> StructuredAnalysisResult:
    usage: dict[str, Any] = {
        "mode": "semantic-first",
        "semanticPages": len(slides),
        "slideImages": 0,
        "tokens": {},
    }
    warnings = _analysis_warnings(slides)
    return StructuredAnalysisResult(
        source_name=source_name,
        slides=slides,
        usage=usage,
        raw_result={
            "status": "Succeeded",
            "mode": "semantic-first",
            "engine": {
                "name": "rhwp",
                "version": RHWP_VERSION,
                "commit": RHWP_COMMIT,
            },
            "source": source_name,
            "usage": usage,
            "slides": {
                str(slide.slide_number): asdict(slide) for slide in slides
            },
        },
        warnings=warnings,
        analysis_seconds=0.0,
    )


def merge_semantic_and_visual(
    semantic: StructuredAnalysisResult,
    visual: StructuredAnalysisResult,
) -> StructuredAnalysisResult:
    visual_by_page = {slide.slide_number: slide for slide in visual.slides}
    semantic_pages = {slide.slide_number for slide in semantic.slides}
    unexpected = set(visual_by_page) - semantic_pages
    if unexpected:
        raise ProcessingFidelityError(
            f"Visual analysis returned unknown semantic pages: {sorted(unexpected)}."
        )

    merged_slides = tuple(
        _merge_slide(slide, visual_by_page.get(slide.slide_number))
        for slide in semantic.slides
    )
    visual_pages = sorted(visual_by_page)
    usage = {
        **visual.usage,
        "mode": "semantic-first",
        "semanticPages": len(merged_slides),
        "visualPages": visual_pages,
        "slideImages": len(visual_pages),
    }
    return StructuredAnalysisResult(
        source_name=semantic.source_name,
        slides=merged_slides,
        usage=usage,
        raw_result={
            **semantic.raw_result,
            "usage": usage,
            "visualPages": visual_pages,
            "slides": {
                str(slide.slide_number): asdict(slide) for slide in merged_slides
            },
            "visualAnalysis": visual.raw_result,
        },
        warnings=_analysis_warnings(merged_slides),
        analysis_seconds=visual.analysis_seconds,
    )


def _merge_slide(
    semantic: SlideContent,
    visual: SlideContent | None,
) -> SlideContent:
    if visual is None:
        return semantic
    return replace(
        semantic,
        summary=visual.summary or semantic.summary,
        facts=tuple(dict.fromkeys((*semantic.facts, *visual.facts))),
        sections=_merge_sections(semantic.sections, visual.sections),
        hierarchies=tuple(
            dict.fromkeys((*semantic.hierarchies, *visual.hierarchies))
        ),
        schedule=(
            visual.schedule if not visual.schedule.is_empty else semantic.schedule
        ),
        flows=tuple(dict.fromkeys((*semantic.flows, *visual.flows))),
        tables=_merge_tables(semantic.tables, visual.tables),
        charts=tuple(dict.fromkeys((*semantic.charts, *visual.charts))),
        relationships=tuple(
            dict.fromkeys((*semantic.relationships, *visual.relationships))
        ),
        images=tuple(dict.fromkeys((*semantic.images, *visual.images))),
        warnings=tuple(dict.fromkeys((*semantic.warnings, *visual.warnings))),
    )


def _merge_sections(
    semantic: tuple[ContentSection, ...],
    visual: tuple[ContentSection, ...],
) -> tuple[ContentSection, ...]:
    values = list(semantic)
    signatures = {_section_signature(section) for section in semantic}
    for section in visual:
        signature = _section_signature(section)
        if signature not in signatures:
            values.append(section)
            signatures.add(signature)
    return tuple(values)


def _merge_tables(
    semantic: tuple[ContentTable, ...],
    visual: tuple[ContentTable, ...],
) -> tuple[ContentTable, ...]:
    values = list(semantic)
    signatures = {_table_signature(table) for table in semantic}
    for table in visual:
        signature = _table_signature(table)
        if signature not in signatures:
            values.append(table)
            signatures.add(signature)
    return tuple(values)


def _section_signature(section: ContentSection) -> tuple[str, ...]:
    return tuple(
        value.strip().casefold()
        for value in (section.heading, *section.paragraphs, *section.bullets)
        if value.strip()
    )


def _table_signature(table: ContentTable) -> tuple[tuple[str, ...], ...]:
    return tuple(
        tuple(cell.strip().casefold() for cell in row)
        for row in (table.headers, *table.rows)
    )


def _analysis_warnings(
    slides: tuple[SlideContent, ...],
) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "slideNumber": slide.slide_number,
            "code": "semantic_warning",
            "message": warning,
        }
        for slide in slides
        for warning in slide.warnings
    )

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Any

from crewmeal.config import AppConfig
from crewmeal.search_enhancement.decryption import maybe_decrypt
from crewmeal.search_enhancement.formats import (
    ProcessingFidelityError,
    detect_handler,
)
from crewmeal.search_enhancement.html_renderer import (
    RenderedHtml,
    render_presentation_html,
)
from crewmeal.search_enhancement.models import StructuredAnalysisResult
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
    ) -> None:
        self._config = config
        self._analysis_service = analysis_service
        self._vision_model = vision_model
        # Snapshot of admin decryption toggles (``decryption.<id>.enabled``),
        # read when the worker starts. Empty/None means every provider is off.
        self._decryption_settings = decryption_settings

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

        source_bytes = maybe_decrypt(
            source_bytes,
            filename=source_name,
            settings=self._decryption_settings,
        )

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
        )
        return ProcessedPresentation(
            rendered=rendered,
            analysis=analysis,
            stage_timings={
                **prepared.stage_timings,
                "analysisSeconds": analysis.analysis_seconds,
            },
        )

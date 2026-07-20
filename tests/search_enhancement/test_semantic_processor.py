from pathlib import Path
from typing import Any

import pytest

from crewmeal.config import AppConfig
from crewmeal.models import RendererManifest, SourceManifest
from crewmeal.search_enhancement.formats.base import (
    PreparedDocument,
    ProcessingFidelityError,
)
from crewmeal.search_enhancement.models import (
    ContentFlow,
    ContentImage,
    ContentSection,
    ContentTable,
    SlideContent,
    SlideSchedule,
    StructuredAnalysisResult,
)
from crewmeal.search_enhancement import processor as processor_module
from crewmeal.search_enhancement.processor import PresentationProcessor


def _config() -> AppConfig:
    return AppConfig(
        endpoint=None,
        max_upload_bytes=1024,
        soffice_path=Path("soffice"),
    )


def _slide(
    number: int,
    *,
    text: str,
    tables: tuple[ContentTable, ...] = (),
    flows: tuple[ContentFlow, ...] = (),
    images: tuple[ContentImage, ...] = (),
) -> SlideContent:
    return SlideContent(
        slide_number=number,
        title=text,
        summary="",
        facts=(),
        sections=(
            ContentSection(heading="본문", paragraphs=(text,), bullets=()),
        ),
        hierarchies=(),
        schedule=SlideSchedule(time_axis=(), tasks=(), milestones=()),
        flows=flows,
        tables=tables,
        charts=(),
        relationships=(),
        images=images,
        warnings=(),
    )


def _prepared(
    slides: tuple[SlideContent, ...],
    *,
    page_images: dict[int, bytes],
) -> PreparedDocument:
    texts = {
        slide.slide_number: tuple(
            paragraph
            for section in slide.sections
            for paragraph in section.paragraphs
        )
        for slide in slides
    }
    source = SourceManifest(
        slide_count=len(slides),
        texts_by_slide=texts,
        links_by_slide={},
        alt_text_by_slide={},
        notes_by_slide={},
    )
    renderer = RendererManifest(
        page_count=len(slides),
        texts_by_page=texts,
        links_by_page={},
        page_images=page_images,
    )
    return PreparedDocument(
        source_manifest=source,
        renderer_manifest=renderer,
        semantic_slides=slides,
    )


class _Handler:
    format_id = "hwp"

    def __init__(self, prepared: PreparedDocument) -> None:
        self.prepared = prepared

    def validate(self, *_args: object, **_kwargs: object) -> None:
        return None

    def prepare(self, *_args: object, **_kwargs: object) -> PreparedDocument:
        return self.prepared


class _UnexpectedVision:
    def analyze(self, *_args: object, **_kwargs: object) -> StructuredAnalysisResult:
        raise AssertionError("Vision must be skipped for semantic-only pages")


class _TargetedVision:
    def __init__(self, result: StructuredAnalysisResult) -> None:
        self.result = result
        self.calls: list[tuple[dict[int, bytes], dict[str, Any]]] = []

    def analyze(
        self,
        page_images: dict[int, bytes],
        **kwargs: Any,
    ) -> StructuredAnalysisResult:
        self.calls.append((dict(page_images), kwargs))
        return self.result


def test_semantic_document_skips_vision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slide = _slide(1, text="분기 실적 보고")
    monkeypatch.setattr(
        processor_module,
        "detect_handler",
        lambda _name: _Handler(_prepared((slide,), page_images={})),
    )

    processed = PresentationProcessor(
        _config(),
        analysis_service=_UnexpectedVision(),
    ).process(b"source", source_name="report.hwp")

    assert processed.analysis.usage["mode"] == "semantic-first"
    assert processed.analysis.usage["slideImages"] == 0
    assert processed.analysis.usage["tokens"] == {}
    assert "분기 실적 보고" in processed.rendered.content


def test_semantic_only_corrections_raise_job_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slide = _slide(1, text="분기 실적 보고")
    monkeypatch.setattr(
        processor_module,
        "detect_handler",
        lambda _name: _Handler(_prepared((slide,), page_images={})),
    )

    with pytest.raises(ProcessingFidelityError, match="cannot be applied"):
        PresentationProcessor(
            _config(),
            analysis_service=_UnexpectedVision(),
        ).process(
            b"source",
            source_name="report.hwp",
            corrections=("표현을 수정하세요",),
        )


def test_semantic_document_merges_only_targeted_visual_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    semantic_table = ContentTable(
        title="표 1",
        headers=("구분", "내용"),
        rows=(("매출", "1억원"),),
        key_facts=(),
    )
    semantic_slides = (
        _slide(1, text="첫 페이지"),
        _slide(2, text="이미지 페이지", tables=(semantic_table,)),
    )
    monkeypatch.setattr(
        processor_module,
        "detect_handler",
        lambda _name: _Handler(
            _prepared(semantic_slides, page_images={2: b"png"})
        ),
    )
    visual_slide = _slide(
        2,
        text="이미지 페이지",
        flows=(ContentFlow(title="승인 흐름", lane="", steps=("신청", "승인")),),
        images=(
            ContentImage(
                description="승인 절차 다이어그램",
                role="프로세스",
                visible_text=("신청", "승인"),
            ),
        ),
    )
    vision_result = StructuredAnalysisResult(
        source_name="report.hwp",
        slides=(visual_slide,),
        usage={"slideImages": 1, "tokens": {"model-input": 10}},
        raw_result={"status": "Succeeded", "model": "fake"},
        warnings=(),
        analysis_seconds=0.5,
    )
    service = _TargetedVision(vision_result)

    processed = PresentationProcessor(
        _config(),
        analysis_service=service,
    ).process(b"source", source_name="report.hwp")

    assert len(service.calls) == 1
    images, kwargs = service.calls[0]
    assert images == {2: b"png"}
    assert kwargs["allow_partial_pages"] is True
    assert processed.analysis.usage["visualPages"] == [2]
    assert processed.analysis.slides[0] == semantic_slides[0]
    merged = processed.analysis.slides[1]
    assert merged.tables == (semantic_table,)
    assert merged.flows[0].steps == ("신청", "승인")
    assert merged.images[0].description == "승인 절차 다이어그램"

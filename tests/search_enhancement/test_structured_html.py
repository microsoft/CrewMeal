from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from types import SimpleNamespace
from typing import Any

import pytest

from crewmeal.config import AppConfig
from crewmeal.models import SourceManifest
from crewmeal.search_enhancement.html_renderer import (
    ALLOWED_TAGS,
    ContentTooLargeError,
    render_presentation_html,
)
from crewmeal.search_enhancement.structured_analysis import (
    StructuredSlideAnalysisError,
    StructuredSlideAnalysisService,
)


def _slide_payload(slide_number: int, *, title: str = "분기 실적") -> dict[str, Any]:
    return {
        "slideNumber": slide_number,
        "title": title,
        "summary": "Q2 매출은 Q1보다 증가했다.",
        "facts": [
            "사용자 교육 실시는 3월 넷째 주(W9)에 예정되어 있다.",
            "외화 펌뱅킹 프로세스에서 계좌간이체 다음 단계는 계좌간이체 결과처리이다.",
        ],
        "sections": [
            {
                "heading": "핵심 내용",
                "paragraphs": ["매출과 비용을 비교한다."],
                "bullets": ["매출 증가", "비용 감소"],
            }
        ],
        "hierarchies": [
            {
                "title": "추진 범위-내용-상세",
                "levelLabels": ["추진 범위", "추진 내용", "추진 상세 내용"],
                "rows": [
                    {"path": ["통신", "환경 설정", "사용자·거래은행 관리"], "note": "핵심"},
                    {"path": ["통신", "전문 처리", "송수신 로그"], "note": ""},
                ],
            }
        ],
        "schedule": {
            "timeAxis": ["3월 W1", "3월 W9"],
            "tasks": [
                {"taskPath": ["테스트/이관", "사용자 교육 실시"], "start": "3월 W9", "end": "3월 W9"}
            ],
            "milestones": [{"name": "Kick-Off", "when": "3월 W1"}],
        },
        "flows": [
            {
                "title": "지급이체 처리 흐름",
                "lane": "자금팀",
                "steps": ["계좌간이체", "계좌간이체 결과처리"],
            }
        ],
        "tables": [
            {
                "title": "분기별 매출",
                "headers": ["분기", "매출"],
                "rows": [["Q1", "100억"], ["Q2", "120억"]],
                "keyFacts": ["Q2 매출은 Q1보다 20% 높다."],
            }
        ],
        "charts": [
            {
                "title": "매출 추이",
                "dataPoints": [
                    {"series": "매출", "label": "Q1", "value": "100억"},
                    {"series": "매출", "label": "Q2", "value": "120억"},
                ],
                "insights": ["Q2가 최대값이다."],
            }
        ],
        "relationships": [
            {
                "source": "검색",
                "relation": "다음 단계",
                "target": "순위화",
                "description": "검색 후 순위화를 수행한다.",
            }
        ],
        "images": [
            {
                "description": "제품 화면",
                "role": "처리 결과 예시",
                "visibleText": ["완료"],
            }
        ],
        "warnings": [],
    }


class FakeResponse:
    def __init__(self, payload: str, slide_number: int) -> None:
        self.id = f"response-{slide_number}"
        self.choices = [
            SimpleNamespace(
                message=SimpleNamespace(content=payload, refusal=None),
                finish_reason="stop",
            )
        ]
        self.usage = SimpleNamespace(prompt_tokens=100, completion_tokens=50)

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        assert mode == "json"
        return {"id": self.id}


class FakeCompletions:
    def __init__(
        self,
        *,
        invalid_first: bool = False,
        invalid_always: bool = False,
    ) -> None:
        self.requests: list[dict[str, Any]] = []
        self.invalid_first = invalid_first
        self.invalid_always = invalid_always

    def create(self, **kwargs: Any) -> FakeResponse:
        self.requests.append(kwargs)
        text = kwargs["messages"][1]["content"][0]["text"]
        slide_number = int(re.search(r"Slide number: (\d+)", text).group(1))
        if self.invalid_always or (self.invalid_first and len(self.requests) == 1):
            return FakeResponse('{"slideNumber": 1}', slide_number)
        return FakeResponse(
            json.dumps(_slide_payload(slide_number), ensure_ascii=False),
            slide_number,
        )


def _config() -> AppConfig:
    return AppConfig(
        endpoint="https://example.cognitiveservices.azure.com",
        max_upload_bytes=1024,
        soffice_path=None,
        slide_image_max_workers=1,
    )


def _manifest(slides: int = 1) -> SourceManifest:
    return SourceManifest(
        slide_count=slides,
        texts_by_slide={
            number: (f"정확한 원문 {number}",) for number in range(1, slides + 1)
        },
        links_by_slide={number: () for number in range(1, slides + 1)},
        alt_text_by_slide={number: () for number in range(1, slides + 1)},
        notes_by_slide={number: () for number in range(1, slides + 1)},
    )


def test_structured_analysis_uses_strict_schema_and_retries_invalid_json() -> None:
    completions = FakeCompletions(invalid_first=True)
    with StructuredSlideAnalysisService(
        _config(),
        completions=completions,
        validation_attempts=2,
    ) as service:
        result = service.analyze(
            {1: b"png"},
            source_manifest=_manifest(),
            source_name="benchmark.pptx",
        )

    assert len(completions.requests) == 2
    assert result.slides[0].title == "분기 실적"
    assert result.usage["tokens"]["gpt-5.6-luna-input"] == 200
    assert result.usage["tokens"]["gpt-5.6-luna-output"] == 100
    assert result.raw_result["model"] == "gpt-5.6-luna"
    request = completions.requests[-1]
    assert request["response_format"]["type"] == "json_schema"
    assert request["response_format"]["json_schema"]["strict"] is True
    response_schema = request["response_format"]["json_schema"]["schema"]
    assert "minimum" not in json.dumps(response_schema)
    assert "minItems" not in json.dumps(response_schema)
    assert "정확한 원문 1" in request["messages"][1]["content"][0]["text"]
    assert request["messages"][1]["content"][1]["image_url"]["detail"] == "high"
    assert "Opaque hyphenated labels are searchable content" in (
        request["messages"][0]["content"]
    )


def test_structured_analysis_can_target_a_page_subset() -> None:
    completions = FakeCompletions()
    with StructuredSlideAnalysisService(
        _config(),
        completions=completions,
    ) as service:
        result = service.analyze(
            {2: b"png"},
            source_manifest=_manifest(slides=3),
            source_name="report.hwp",
            allow_partial_pages=True,
        )

    assert tuple(slide.slide_number for slide in result.slides) == (2,)
    assert len(completions.requests) == 1


def test_structured_analysis_rejects_a_page_subset_by_default() -> None:
    with StructuredSlideAnalysisService(
        _config(),
        completions=FakeCompletions(),
    ) as service:
        with pytest.raises(
            StructuredSlideAnalysisError,
            match="pages do not match",
        ):
            service.analyze(
                {2: b"png"},
                source_manifest=_manifest(slides=3),
                source_name="report.pptx",
            )


def test_structured_analysis_reports_usage_when_validation_fails() -> None:
    completions = FakeCompletions(invalid_always=True)
    with StructuredSlideAnalysisService(
        _config(),
        completions=completions,
        validation_attempts=2,
    ) as service:
        with pytest.raises(StructuredSlideAnalysisError) as raised:
            service.analyze(
                {1: b"png", 2: b"png"},
                source_manifest=_manifest(slides=2),
                source_name="benchmark.pptx",
            )

    assert raised.value.input_tokens == 400
    assert raised.value.output_tokens == 200


class TagCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: set[str] = set()
        self.attrs: list[tuple[str, list[tuple[str, str | None]]]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.tags.add(tag)
        self.attrs.append((tag, attrs))


def test_html_renderer_escapes_content_and_keeps_notes_separate() -> None:
    completions = FakeCompletions()
    with StructuredSlideAnalysisService(
        _config(),
        completions=completions,
    ) as service:
        analysis = service.analyze(
            {1: b"png"},
            source_manifest=_manifest(),
            source_name="분석 <script>.pptx",
        )

    rendered = render_presentation_html(
        source_name="분석 <script>.pptx",
        slides=analysis.slides,
        notes_by_slide={1: ("NOTE-SENTINEL", "<img onerror=alert(1)>")},
    )

    assert "<script>" not in rendered.content
    assert "&lt;script&gt;" in rendered.content
    assert "NOTE-SENTINEL" in rendered.content
    assert "&lt;img onerror=alert(1)&gt;" in rendered.content
    assert rendered.byte_count == len(rendered.content.encode("utf-8"))
    assert rendered.slide_titles == ("분기 실적",)
    assert "문서 요약" not in rendered.content  # header summary block removed

    assert "사용자 교육 실시는 3월 넷째 주(W9)에 예정되어 있다." in rendered.content
    assert "사용자·거래은행 관리" in rendered.content
    assert "계좌간이체 결과처리" in rendered.content
    assert "순서: 계좌간이체 → 계좌간이체 결과처리" in rendered.content
    assert "3월 W9" in rendered.content
    assert "<ol>" in rendered.content
    assert "<caption>작업 일정</caption>" in rendered.content

    parser = TagCollector()
    parser.feed(rendered.content)
    assert parser.tags <= ALLOWED_TAGS
    assert all(not attrs for _, attrs in parser.attrs)


def test_html_renderer_fails_instead_of_truncating_content() -> None:
    completions = FakeCompletions()
    with StructuredSlideAnalysisService(
        _config(),
        completions=completions,
    ) as service:
        analysis = service.analyze(
            {1: b"png"},
            source_manifest=_manifest(),
            source_name="benchmark.pptx",
        )

    with pytest.raises(ContentTooLargeError, match="CONTENT_TOO_LARGE"):
        render_presentation_html(
            source_name="benchmark.pptx",
            slides=analysis.slides,
            max_bytes=100,
        )

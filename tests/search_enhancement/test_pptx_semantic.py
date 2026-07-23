"""Unit tests for the OOXML text-slide semantic extractor."""

from __future__ import annotations

from _pptx_fixtures import (
    build_pptx,
    connector,
    graphic_frame,
    group,
    picture,
    placeholder,
    textbox,
)

from crewmeal.search_enhancement.pptx_semantic import extract_semantic_slides


def test_text_only_slide_is_extracted_without_vision() -> None:
    data = build_pptx(
        placeholder("title", "분기 실적 보고")
        + placeholder("body", "매출 1억원", "영업이익 2천만원")
    )

    extraction = extract_semantic_slides(data)

    assert extraction.visual_pages == frozenset()
    slide = extraction.slides[0]
    assert slide.slide_number == 1
    assert slide.title == "분기 실적 보고"
    body = next(section for section in slide.sections if section.heading == "본문")
    assert body.bullets == ("매출 1억원", "영업이익 2천만원")
    assert slide.warnings == ()


def test_subtitle_placeholder_becomes_section() -> None:
    data = build_pptx(
        placeholder("ctrTitle", "메인 타이틀")
        + placeholder("subTitle", "부제목입니다")
    )

    slide = extract_semantic_slides(data).slides[0]

    assert slide.title == "메인 타이틀"
    subtitle = next(
        section for section in slide.sections if section.heading == "부제"
    )
    assert subtitle.paragraphs == ("부제목입니다",)


def test_title_falls_back_to_first_body_line() -> None:
    data = build_pptx(placeholder("body", "첫 줄", "둘째 줄"))

    slide = extract_semantic_slides(data).slides[0]

    assert slide.title == "첫 줄"


def test_picture_slide_is_visual_with_title_only() -> None:
    data = build_pptx(placeholder("title", "그림 제목") + picture())

    extraction = extract_semantic_slides(data)

    assert extraction.visual_pages == frozenset({1})
    slide = extraction.slides[0]
    assert slide.title == "그림 제목"
    assert slide.sections == ()


def test_free_text_box_forces_visual() -> None:
    data = build_pptx(placeholder("title", "제목") + textbox("떠다니는 라벨"))

    extraction = extract_semantic_slides(data)

    assert extraction.visual_pages == frozenset({1})


def test_table_chart_connector_and_group_force_visual() -> None:
    data = build_pptx(
        placeholder("title", "표") + graphic_frame(),
        placeholder("title", "연결선") + connector(),
        placeholder("title", "그룹") + group(placeholder("body", "안쪽")),
    )

    extraction = extract_semantic_slides(data)

    assert extraction.visual_pages == frozenset({1, 2, 3})


def test_visual_slide_without_title_placeholder_has_empty_title() -> None:
    data = build_pptx(picture())

    slide = extract_semantic_slides(data).slides[0]

    assert slide.title == ""
    assert slide.sections == ()


def test_mixed_deck_reports_only_visual_pages() -> None:
    data = build_pptx(
        placeholder("title", "텍스트 슬라이드") + placeholder("body", "항목"),
        placeholder("title", "이미지 슬라이드") + picture(),
        placeholder("title", "또 다른 텍스트") + placeholder("body", "요점"),
    )

    extraction = extract_semantic_slides(data)

    assert len(extraction.slides) == 3
    assert extraction.visual_pages == frozenset({2})
    assert extraction.slides[0].sections  # text slide keeps content
    assert extraction.slides[1].sections == ()  # visual slide is minimal

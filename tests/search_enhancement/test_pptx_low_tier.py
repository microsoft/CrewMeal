from __future__ import annotations

import _low_tier_fixtures as fx

from crewmeal.search_enhancement.pptx_low_tier import extract_low_tier_slides


class _StubOcr:
    """Duck-typed stand-in for OcrEngine (no model download in unit tests)."""

    def __init__(self, lines: tuple[str, ...], *, available: bool = True) -> None:
        self._lines = lines
        self.available = available
        self.calls: list[bytes] = []

    def read_image(self, blob: bytes) -> tuple[str, ...]:
        self.calls.append(blob)
        return self._lines


def _all_text(slide) -> str:
    parts = [slide.title, slide.summary]
    for section in slide.sections:
        parts.extend(section.paragraphs)
        parts.extend(section.bullets)
    for table in slide.tables:
        parts.extend(table.headers)
        for row in table.rows:
            parts.extend(row)
    for image in slide.images:
        parts.extend(image.visible_text)
    return "\n".join(parts)


def test_explicit_title_from_placeholder():
    prs = fx.new_presentation()
    slide = fx.add_title_slide(prs)
    fx.set_title(slide, "명시적 제목")
    fx.add_textbox(slide, "본문 문장", top_in=3.0)

    (content,) = extract_low_tier_slides(fx.render(prs))
    assert content.title == "명시적 제목"


def test_heuristic_title_picks_topmost_textbox():
    prs = fx.new_presentation()
    slide = fx.add_blank_slide(prs)
    fx.add_textbox(slide, "본문입니다", top_in=3.0)
    fx.add_textbox(slide, "머리 제목", top_in=1.0)

    (content,) = extract_low_tier_slides(fx.render(prs))
    assert content.title == "머리 제목"
    bullets = content.sections[0].bullets
    assert "머리 제목" not in bullets
    assert "본문입니다" in bullets


def test_body_lines_are_reading_order_and_deduplicated():
    prs = fx.new_presentation()
    slide = fx.add_title_slide(prs)
    fx.set_title(slide, "cover")
    fx.add_textbox(slide, "gamma", top_in=4.0)
    fx.add_textbox(slide, "alpha", top_in=2.0)
    fx.add_textbox(slide, "beta", top_in=3.0)
    fx.add_textbox(slide, "alpha", top_in=5.0)

    (content,) = extract_low_tier_slides(fx.render(prs))
    assert content.sections[0].bullets == ("alpha", "beta", "gamma")


def test_table_becomes_content_table_grid():
    prs = fx.new_presentation()
    slide = fx.add_title_slide(prs)
    fx.set_title(slide, "표")
    fx.add_table(slide, [["H1", "H2"], ["a", "b"], ["c", "d"]])

    (content,) = extract_low_tier_slides(fx.render(prs))
    assert len(content.tables) == 1
    table = content.tables[0]
    assert table.headers == ("H1", "H2")
    assert table.rows == (("a", "b"), ("c", "d"))
    assert all(len(row) == len(table.headers) for row in table.rows)


def test_chart_data_points_are_extracted():
    prs = fx.new_presentation()
    slide = fx.add_title_slide(prs)
    fx.set_title(slide, "차트")
    fx.add_chart(slide, ["ICN", "LAX"], "Crew", [86, 72], title="탑승률")

    (content,) = extract_low_tier_slides(fx.render(prs))
    assert len(content.charts) == 1
    chart = content.charts[0]
    assert chart.title == "탑승률"
    points = {(p.label, p.value) for p in chart.data_points}
    assert points == {("ICN", "86"), ("LAX", "72")}
    assert all(p.series == "Crew" for p in chart.data_points)


def test_group_shape_text_is_collected():
    prs = fx.new_presentation()
    slide = fx.add_title_slide(prs)
    fx.set_title(slide, "그룹")
    fx.add_group_textbox(slide, "그룹 안 텍스트")

    (content,) = extract_low_tier_slides(fx.render(prs))
    assert "그룹 안 텍스트" in content.sections[0].bullets


def test_picture_is_routed_through_ocr():
    prs = fx.new_presentation()
    slide = fx.add_title_slide(prs)
    fx.set_title(slide, "이미지")
    fx.add_picture(slide)

    ocr = _StubOcr(("OCR-한글-777", "second line"))
    (content,) = extract_low_tier_slides(fx.render(prs), ocr=ocr)
    assert len(ocr.calls) == 1
    assert len(content.images) == 1
    image = content.images[0]
    assert image.role == "ocr"
    assert image.visible_text == ("OCR-한글-777", "second line")


def test_picture_without_ocr_engine_warns_and_skips():
    prs = fx.new_presentation()
    slide = fx.add_title_slide(prs)
    fx.set_title(slide, "이미지")
    fx.add_picture(slide)

    (content,) = extract_low_tier_slides(fx.render(prs), ocr=None)
    assert content.images == ()
    assert any("OCR 엔진을 사용할 수 없어" in w for w in content.warnings)


def test_unavailable_ocr_engine_warns_and_skips():
    prs = fx.new_presentation()
    slide = fx.add_title_slide(prs)
    fx.set_title(slide, "이미지")
    fx.add_picture(slide)

    ocr = _StubOcr((), available=False)
    (content,) = extract_low_tier_slides(fx.render(prs), ocr=ocr)
    assert ocr.calls == []
    assert content.images == ()
    assert any("OCR" in w for w in content.warnings)


def test_speaker_notes_are_never_extracted():
    prs = fx.new_presentation()
    slide = fx.add_title_slide(prs)
    fx.set_title(slide, "제목")
    fx.add_textbox(slide, "보이는 본문", top_in=3.0)
    fx.set_notes(slide, "SECRET-NOTE-XYZ")

    (content,) = extract_low_tier_slides(fx.render(prs))
    assert "SECRET-NOTE-XYZ" not in _all_text(content)
    assert "보이는 본문" in content.sections[0].bullets


def test_slide_count_and_numbering():
    prs = fx.new_presentation()
    for index in range(1, 4):
        slide = fx.add_title_slide(prs)
        fx.set_title(slide, f"슬라이드 {index}")

    slides = extract_low_tier_slides(fx.render(prs))
    assert [s.slide_number for s in slides] == [1, 2, 3]
    assert [s.title for s in slides] == ["슬라이드 1", "슬라이드 2", "슬라이드 3"]

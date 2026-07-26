"""Word (.docx / .docm) handler and OOXML normalization tests.

LibreOffice is stubbed out everywhere so the suite stays hermetic; the stubs
return the page texts a real conversion would have produced, which is exactly
what the block-to-page alignment consumes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from crewmeal.config import DEFAULT_MAX_UPLOAD_BYTES, AppConfig
from crewmeal.libreoffice import ConversionResult, LibreOfficeConversionError
from crewmeal.models import RendererManifest
from crewmeal.search_enhancement import docx_semantic
from crewmeal.search_enhancement.docx_semantic import (
    DocxSemanticError,
    align_blocks_to_pages,
    extract_docx,
    split_blocks_by_breaks,
)
from crewmeal.search_enhancement.formats import (
    detect_handler,
    enabled_extensions,
    is_format_enabled,
    supported_extensions,
)
from crewmeal.search_enhancement.formats import docx as docx_module
from crewmeal.search_enhancement.formats.base import (
    EncryptedDocumentError,
    InvalidDocumentError,
    ProcessingFidelityError,
)
from crewmeal.search_enhancement.formats.docx import DocxHandler
from crewmeal.search_enhancement.html_renderer import render_presentation_html
from crewmeal.search_enhancement.progress import NullProgressReporter

from tests.search_enhancement._docx_fixtures import (
    build_docx,
    cell,
    chart_anchor,
    merged_table,
    nested_table,
    package_without_document,
    page_break,
    paragraph,
    picture,
    row,
    side_label_table,
    simple_table,
    span_only_table,
    table,
)

_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_PNG = b"\x89PNG\r\n\x1a\nfixture"


def _config(tier: str = "vision") -> AppConfig:
    return AppConfig(
        endpoint=None,
        max_upload_bytes=DEFAULT_MAX_UPLOAD_BYTES,
        soffice_path=Path("soffice"),
        slide_image_render_dpi=96,
        pptx_analysis_tier=tier,
    )


def _stub_libreoffice(
    monkeypatch: pytest.MonkeyPatch,
    texts_by_page: dict[int, tuple[str, ...]],
    *,
    rendered_visual_pages: set[int] | None = None,
) -> None:
    """Replace the LibreOffice round-trip with a canned page rendering."""

    monkeypatch.setattr(
        docx_module,
        "convert_document_to_pdf",
        lambda source, outdir, **_kwargs: ConversionResult(
            pdf_path=Path(outdir) / "input.pdf",
            conversion_seconds=0.01,
            stdout="",
            stderr="",
        ),
    )
    monkeypatch.setattr(
        docx_module,
        "inspect_pdf",
        lambda _pdf_path, **_kwargs: RendererManifest(
            page_count=len(texts_by_page),
            texts_by_page=dict(texts_by_page),
            links_by_page={page: () for page in texts_by_page},
            page_images={page: _PNG for page in texts_by_page},
            render_dpi=96,
        ),
    )
    monkeypatch.setattr(
        docx_module,
        "_pages_with_rendered_visuals",
        lambda _pdf_path: set(rendered_visual_pages or ()),
    )


# --------------------------------------------------------------------------- #
# Registration and validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("filename", ["report.docx", "report.DOCX", "report.docm"])
def test_docx_handler_detected_by_extension(filename: str) -> None:
    assert detect_handler(filename).format_id == "docx"


def test_docx_is_supported_and_enabled_by_default() -> None:
    assert ".docx" in supported_extensions()
    assert ".docm" in supported_extensions()
    assert is_format_enabled("docx", {})
    assert ".docx" in enabled_extensions({})


def test_docx_validation_accepts_ooxml_package() -> None:
    handler = DocxHandler()
    data = build_docx(paragraph("본문"))
    handler.validate(data, filename="a.docx", max_bytes=DEFAULT_MAX_UPLOAD_BYTES)
    handler.validate(data, filename="a.docm", max_bytes=DEFAULT_MAX_UPLOAD_BYTES)


def test_docx_validation_rejects_bad_input() -> None:
    handler = DocxHandler()
    data = build_docx(paragraph("본문"))
    invalid = (
        (b"", "a.docx"),
        (b"not a package", "a.docx"),
        (data, "a.pptx"),
    )
    for payload, filename in invalid:
        with pytest.raises(InvalidDocumentError):
            handler.validate(
                payload, filename=filename, max_bytes=DEFAULT_MAX_UPLOAD_BYTES
            )


def test_docx_validation_enforces_size_limit() -> None:
    with pytest.raises(InvalidDocumentError, match="MB limit"):
        DocxHandler().validate(
            build_docx(paragraph("본문")), filename="a.docx", max_bytes=8
        )


def test_docx_validation_reports_password_protection() -> None:
    """Password-protected Office files are OLE2 wrappers, not ZIP packages."""

    with pytest.raises(EncryptedDocumentError, match="password-protected"):
        DocxHandler().validate(
            _OLE2_MAGIC + b"\x00" * 512,
            filename="secret.docx",
            max_bytes=DEFAULT_MAX_UPLOAD_BYTES,
        )


def test_docx_fingerprint_is_deterministic_and_prefixed() -> None:
    handler = DocxHandler()
    data = build_docx(paragraph("본문"))
    fingerprint = handler.fingerprint(data)
    assert fingerprint.startswith("docx-sha256:")
    assert fingerprint == handler.fingerprint(data)


# --------------------------------------------------------------------------- #
# OOXML extraction
# --------------------------------------------------------------------------- #


def test_extract_reads_blocks_parts_and_links() -> None:
    extraction = extract_docx(
        build_docx(
            paragraph("분기 실적 보고", style="Heading1")
            + paragraph("첫 문단")
            + paragraph("항목 하나", numbered=True)
            + simple_table(),
            header="머리말",
            footer="꼬리말",
            footnote="각주 내용",
            hyperlink="https://contoso.example/report",
            declared_pages=3,
        )
    )

    kinds = [block.kind for block in extraction.blocks]
    assert kinds == ["heading", "paragraph", "list_item", "table"]
    assert extraction.blocks[0].heading_level == 1
    assert extraction.blocks[2].is_list_item
    assert extraction.header_lines == ("머리말",)
    assert extraction.footer_lines == ("꼬리말",)
    assert extraction.footnote_lines == ("각주 내용",)
    assert extraction.links == ("https://contoso.example/report",)
    assert extraction.declared_page_count == 3


def test_extract_rejects_corrupt_or_empty_packages() -> None:
    with pytest.raises(DocxSemanticError, match="corrupt"):
        extract_docx(b"not a zip")
    with pytest.raises(DocxSemanticError, match="missing"):
        extract_docx(package_without_document())
    with pytest.raises(DocxSemanticError, match="no readable content"):
        extract_docx(build_docx(""))


def test_extract_collects_visual_anchors_and_alt_text() -> None:
    extraction = extract_docx(
        build_docx(paragraph("본문") + picture(description="조직도 이미지"))
    )
    visual = [block for block in extraction.blocks if block.is_visual]
    assert [block.visual_kinds for block in visual] == [("picture",)]
    assert visual[0].alt_texts == ("조직도 이미지",)


def test_extract_reads_chart_series_from_the_chart_part() -> None:
    extraction = extract_docx(build_docx(chart_anchor(), chart=True))
    charts = [block.chart for block in extraction.blocks if block.chart is not None]
    assert len(charts) == 1
    assert charts[0].title == "분기 매출"
    assert [(point.label, point.value) for point in charts[0].data_points] == [
        ("1분기", "100"),
        ("2분기", "150"),
    ]


# --------------------------------------------------------------------------- #
# Table normalization
# --------------------------------------------------------------------------- #


def test_simple_table_is_not_flagged_complex() -> None:
    block = extract_docx(build_docx(simple_table())).blocks[0]
    assert block.table is not None
    assert block.table.headers == ("구분", "내용")
    assert block.table.rows == (("매출", "1억원"),)
    assert block.complexity is not None
    assert not block.complexity.is_complex


def test_banded_header_is_joined_and_merges_are_duplicated() -> None:
    block = extract_docx(build_docx(merged_table())).blocks[0]
    assert block.table is not None
    assert block.table.headers == ("구분", "2024 / 상반기", "2024 / 하반기")
    assert block.table.rows == (("매출", "1억원", "1.2억원"),)
    assert block.complexity is not None
    assert block.complexity.multi_row_header
    assert block.complexity.vertical_merge
    assert block.complexity.is_complex


def test_vertical_merge_in_the_data_area_keeps_one_header_row() -> None:
    """A row-spanning side label must not be mistaken for a banded header."""

    block = extract_docx(build_docx(side_label_table())).blocks[0]
    assert block.table is not None
    assert block.table.headers == ("분류", "항목", "값")
    assert block.table.rows == (
        ("매출", "국내", "100"),
        ("매출", "해외", "50"),
    )
    assert block.complexity is not None
    assert not block.complexity.multi_row_header
    assert block.complexity.vertical_merge


def test_horizontal_merge_alone_is_lossless_enough_to_stay_simple() -> None:
    block = extract_docx(build_docx(span_only_table())).blocks[0]
    assert block.table is not None
    assert block.table.rows == (("비고", "비고"),)
    assert block.complexity is not None
    assert block.complexity.horizontal_merge
    assert not block.complexity.is_complex


def test_nested_table_is_split_into_its_own_content_table() -> None:
    block = extract_docx(build_docx(nested_table())).blocks[0]
    assert block.table is not None
    assert block.table.headers == ("구분", "내용")
    assert [table.title for table in block.nested_tables] == ["표 1-1 (중첩)"]
    assert block.nested_tables[0].headers == ("세부", "값")
    assert block.complexity is not None
    assert block.complexity.nested


def test_table_rows_are_padded_to_a_rectangle() -> None:
    block = extract_docx(
        build_docx(
            table(
                row(cell("구분"), cell("내용"), cell("비고")),
                row(cell("매출"), cell("1억원")),
                columns=3,
            )
        )
    ).blocks[0]
    assert block.table is not None
    assert block.table.rows == (("매출", "1억원", ""),)
    assert block.complexity is not None
    assert block.complexity.ragged_rows


def test_oversized_table_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docx_semantic, "MAX_TABLE_CELLS", 3)
    with pytest.raises(DocxSemanticError, match="안전 한도"):
        extract_docx(
            build_docx(
                table(
                    row(cell("가"), cell("나")),
                    row(cell("다"), cell("라")),
                    columns=2,
                )
            )
        )


# --------------------------------------------------------------------------- #
# Page alignment
# --------------------------------------------------------------------------- #


def test_align_blocks_to_pages_follows_the_rendered_text_order() -> None:
    extraction = extract_docx(
        build_docx(
            paragraph("첫 페이지 문단입니다")
            + page_break()
            + paragraph("둘째 페이지 문단입니다")
        )
    )
    page_by_block, warnings = align_blocks_to_pages(
        extraction.blocks,
        {1: ("첫 페이지 문단입니다",), 2: ("둘째 페이지 문단입니다",)},
    )
    assert page_by_block == {0: 1, 1: 2}
    assert warnings == ()


def test_align_blocks_reports_unmatched_blocks_without_failing() -> None:
    extraction = extract_docx(
        build_docx(paragraph("렌더링에 없는 필드 결과 문단"))
    )
    page_by_block, warnings = align_blocks_to_pages(
        extraction.blocks, {1: ("완전히 다른 본문",)}
    )
    assert page_by_block == {0: 1}
    assert warnings and "매칭하지 못해" in warnings[0]


def test_align_blocks_never_jumps_far_backwards() -> None:
    """A repeated short phrase must not drag later blocks onto an early page."""

    extraction = extract_docx(
        build_docx(
            paragraph("공통 안내 문구입니다")
            + page_break()
            + paragraph("두 번째 페이지 고유 본문")
            + paragraph("세 번째 페이지 고유 본문")
        )
    )
    page_by_block, _ = align_blocks_to_pages(
        extraction.blocks,
        {
            1: ("공통 안내 문구입니다",),
            2: ("두 번째 페이지 고유 본문",),
            3: ("세 번째 페이지 고유 본문",),
        },
    )
    assert page_by_block == {0: 1, 1: 2, 2: 3}


def test_align_blocks_requires_rendered_pages() -> None:
    with pytest.raises(DocxSemanticError, match="no pages"):
        align_blocks_to_pages((), {})


def test_align_blocks_locates_a_merged_table_by_its_rendered_cells() -> None:
    """Regression: a merged table must not be searched by its flattened text.

    Flattening duplicates spanned values and joins banded headers with " / ",
    neither of which a renderer draws, so matching on the output text always
    missed and pushed the table onto the previous page with a warning.
    """

    extraction = extract_docx(
        build_docx(paragraph("표 앞의 안내 문단입니다") + merged_table())
    )
    table_index = next(
        order
        for order, block in enumerate(extraction.blocks)
        if block.kind == "table"
    )
    page_by_block, warnings = align_blocks_to_pages(
        extraction.blocks,
        {
            1: ("표 앞의 안내 문단입니다",),
            # The renderer draws each cell once, in reading order.
            2: ("구분", "2024", "상반기", "하반기", "매출", "1억원", "1.2억원"),
        },
    )
    assert page_by_block[table_index] == 2
    assert warnings == ()


def test_align_blocks_falls_back_to_the_longest_cell_when_the_order_differs() -> (
    None
):
    """Nested tables and column-major extraction reorder a table's cell text.

    The precise full-sequence key then misses, so the longest single cell -- one
    literal, contiguous text run -- has to carry the match.
    """

    extraction = extract_docx(
        build_docx(
            paragraph("표 앞의 안내 문단입니다")
            + table(
                row(cell("구분"), cell("2024년 상반기 연결 기준 매출 실적")),
                row(cell("매출"), cell("1억원")),
                columns=2,
            )
        )
    )
    table_index = next(
        order
        for order, block in enumerate(extraction.blocks)
        if block.kind == "table"
    )
    page_by_block, warnings = align_blocks_to_pages(
        extraction.blocks,
        {
            1: ("표 앞의 안내 문단입니다",),
            # Column-major: the full reading-order key cannot be found here.
            2: ("구분", "매출", "2024년 상반기 연결 기준 매출 실적", "1억원"),
        },
    )
    assert page_by_block[table_index] == 2
    assert warnings == ()


def test_align_blocks_ignores_a_table_too_short_to_locate() -> None:
    """A table with no distinctive text inherits its page instead of warning."""

    extraction = extract_docx(
        build_docx(
            paragraph("표 앞의 안내 문단입니다")
            + table(row(cell("가"), cell("나")), columns=2)
        )
    )
    page_by_block, warnings = align_blocks_to_pages(
        extraction.blocks,
        {1: ("표 앞의 안내 문단입니다",), 2: ("가", "나")},
    )
    assert set(page_by_block.values()) == {1}
    assert warnings == ()


def test_split_blocks_by_breaks_uses_explicit_breaks_only() -> None:
    extraction = extract_docx(
        build_docx(paragraph("첫 페이지") + page_break() + paragraph("둘째 페이지"))
    )
    assert split_blocks_by_breaks(extraction.blocks) == {0: 1, 1: 2}


# --------------------------------------------------------------------------- #
# prepare()
# --------------------------------------------------------------------------- #


def test_prepare_skips_vision_for_a_text_only_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_libreoffice(monkeypatch, {1: ("분기 실적 보고", "구분 내용 매출 1억원")})

    prepared = DocxHandler().prepare(
        build_docx(
            paragraph("분기 실적 보고", style="Heading1")
            + simple_table(),
            header="머리말",
            footer="꼬리말",
            footnote="각주 내용",
        ),
        source_name="report.docx",
        config=_config(),
        reporter=NullProgressReporter(),
    )

    assert prepared.renderer_manifest.page_images == {}
    assert prepared.semantic_slides is not None
    slide = prepared.semantic_slides[0]
    assert slide.title == "분기 실적 보고"
    assert slide.tables[0].headers == ("구분", "내용")
    assert [section.heading for section in slide.sections] == [
        "머리말",
        "본문",
        "각주",
        "꼬리말",
    ]


def test_prepare_promotes_only_pages_with_visual_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_libreoffice(
        monkeypatch,
        {1: ("첫 페이지 본문입니다",), 2: ("둘째 페이지 본문입니다",)},
    )

    prepared = DocxHandler().prepare(
        build_docx(
            paragraph("첫 페이지 본문입니다")
            + page_break()
            + paragraph("둘째 페이지 본문입니다")
            + picture(description="조직도"),
        ),
        source_name="report.docx",
        config=_config(),
        reporter=NullProgressReporter(),
    )

    assert set(prepared.renderer_manifest.page_images) == {2}
    assert prepared.semantic_slides is not None
    assert not prepared.semantic_slides[0].warnings
    assert prepared.semantic_slides[1].warnings


def test_prepare_promotes_complex_table_pages_for_cross_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_libreoffice(
        monkeypatch,
        {1: ("구분 2024 상반기 하반기 매출 1억원 1.2억원",)},
    )

    prepared = DocxHandler().prepare(
        build_docx(merged_table()),
        source_name="report.docx",
        config=_config(),
        reporter=NullProgressReporter(),
    )

    assert set(prepared.renderer_manifest.page_images) == {1}
    assert prepared.semantic_slides is not None
    warnings = " ".join(prepared.semantic_slides[0].warnings)
    assert "세로 병합" in warnings
    assert "교차 확인" in warnings


def test_prepare_promotes_pages_libreoffice_drew_as_vectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SmartArt has no OOXML anchor we can map, so the rendering decides."""

    _stub_libreoffice(
        monkeypatch,
        {1: ("첫 페이지 본문입니다",), 2: ("둘째 페이지 본문입니다",)},
        rendered_visual_pages={2},
    )

    prepared = DocxHandler().prepare(
        build_docx(
            paragraph("첫 페이지 본문입니다")
            + page_break()
            + paragraph("둘째 페이지 본문입니다")
        ),
        source_name="report.docx",
        config=_config(),
        reporter=NullProgressReporter(),
    )

    assert set(prepared.renderer_manifest.page_images) == {2}


def test_prepare_warns_when_word_and_libreoffice_paginate_differently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_libreoffice(monkeypatch, {1: ("한 페이지 분량 본문입니다",)})

    prepared = DocxHandler().prepare(
        build_docx(paragraph("한 페이지 분량 본문입니다"), declared_pages=4),
        source_name="report.docx",
        config=_config(),
        reporter=NullProgressReporter(),
    )

    assert prepared.semantic_slides is not None
    warnings = " ".join(prepared.semantic_slides[0].warnings)
    assert "Word가 기록한 페이지 수(4)" in warnings


def test_prepare_attaches_page_furniture_once_for_multi_section_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_libreoffice(
        monkeypatch,
        {1: ("첫 페이지 본문입니다",), 2: ("둘째 페이지 본문입니다",)},
    )

    prepared = DocxHandler().prepare(
        build_docx(
            paragraph("첫 페이지 본문입니다")
            + page_break()
            + paragraph("둘째 페이지 본문입니다"),
            sections=2,
            header="머리말",
        ),
        source_name="report.docx",
        config=_config(),
        reporter=NullProgressReporter(),
    )

    assert prepared.semantic_slides is not None
    headings = [
        [section.heading for section in slide.sections]
        for slide in prepared.semantic_slides
    ]
    assert headings[0][0] == "머리말"
    assert "머리말" not in headings[1]
    assert any(
        "머리말·꼬리말을 첫 페이지에만" in warning
        for warning in prepared.semantic_slides[0].warnings
    )


def test_prepare_low_tier_never_calls_libreoffice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(*_args: object, **_kwargs: object) -> ConversionResult:
        raise AssertionError("the low tier must not run LibreOffice")

    monkeypatch.setattr(docx_module, "convert_document_to_pdf", unexpected)
    monkeypatch.setattr(docx_module, "inspect_pdf", unexpected)

    prepared = DocxHandler().prepare(
        build_docx(
            paragraph("첫 페이지 본문")
            + page_break()
            + paragraph("둘째 페이지 본문")
            + picture()
        ),
        source_name="report.docx",
        config=_config(tier="text_ocr"),
        reporter=NullProgressReporter(),
    )

    assert prepared.renderer_manifest.page_count == 2
    assert prepared.renderer_manifest.page_images == {}
    assert prepared.semantic_slides is not None
    assert len(prepared.semantic_slides) == 2
    assert prepared.stage_timings["conversionSeconds"] == 0.0


def test_prepare_maps_semantic_failure_to_invalid_document() -> None:
    with pytest.raises(InvalidDocumentError, match="could not be read"):
        DocxHandler().prepare(
            b"PK\x03\x04 not really a package",
            source_name="report.docx",
            config=_config(),
            reporter=NullProgressReporter(),
        )


def test_prepare_maps_conversion_failure_to_job_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed(*_args: object, **_kwargs: object) -> ConversionResult:
        raise LibreOfficeConversionError("soffice exited with code 1")

    monkeypatch.setattr(docx_module, "convert_document_to_pdf", failed)

    with pytest.raises(ProcessingFidelityError, match="could not lay out"):
        DocxHandler().prepare(
            build_docx(paragraph("본문")),
            source_name="report.docx",
            config=_config(),
            reporter=NullProgressReporter(),
        )


def test_prepare_rejects_an_empty_rendering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_libreoffice(monkeypatch, {})

    with pytest.raises(ProcessingFidelityError, match="no pages"):
        DocxHandler().prepare(
            build_docx(paragraph("본문 문단입니다")),
            source_name="report.docx",
            config=_config(),
            reporter=NullProgressReporter(),
        )


def test_prepared_pages_render_through_the_real_html_renderer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The published HTML is the actual output contract, so exercise it.

    `ContentTable` must be a strict rectangle and the renderer's allow-list
    rejects anything unexpected, so this catches normalization mistakes that a
    dataclass-level assertion would not.
    """

    _stub_libreoffice(
        monkeypatch,
        {1: ("구분 2024 상반기 하반기 매출 1억원 1.2억원", "분기 실적 보고")},
    )

    prepared = DocxHandler().prepare(
        build_docx(
            paragraph("분기 실적 보고", style="Heading1")
            + paragraph("첫째 항목", numbered=True)
            + merged_table()
            + nested_table(),
            header="사내 한정",
            footer="1 / 1",
            footnote="각주 내용",
        ),
        source_name="report.docx",
        config=_config(),
        reporter=NullProgressReporter(),
    )

    assert prepared.semantic_slides is not None
    rendered = render_presentation_html(
        source_name="report.docx",
        slides=prepared.semantic_slides,
        unit_label="페이지",
    )
    assert "<h2>페이지 1: 분기 실적 보고</h2>" in rendered.content
    assert "2024 / 상반기" in rendered.content
    assert "표 1-1 (중첩)" not in rendered.content or "세부" in rendered.content
    assert "각주 내용" in rendered.content

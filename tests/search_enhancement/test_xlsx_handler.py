"""Excel (.xlsx / .xlsm) handler, extraction and sheet-classification tests.

LibreOffice is stubbed out everywhere so the suite stays hermetic; the stubs
return the page texts a real Calc conversion would have produced -- including
its out-of-reading-order text, which is what page attribution has to survive.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from crewmeal.config import DEFAULT_MAX_UPLOAD_BYTES, AppConfig
from crewmeal.libreoffice import ConversionResult, LibreOfficeConversionError
from crewmeal.models import RendererManifest
from crewmeal.search_enhancement.formats import (
    detect_handler,
    enabled_extensions,
    format_status,
    is_format_enabled,
    supported_extensions,
)
from crewmeal.search_enhancement.formats import xlsx as xlsx_module
from crewmeal.search_enhancement.formats.base import (
    EncryptedDocumentError,
    InvalidDocumentError,
    ProcessingFidelityError,
)
from crewmeal.search_enhancement.formats.xlsx import XlsxHandler
from crewmeal.search_enhancement.html_renderer import render_presentation_html
from crewmeal.search_enhancement.progress import NullProgressReporter
from crewmeal.search_enhancement.xlsx_semantic import (
    XlsxSemanticError,
    attribute_pages_to_sheets,
    classify_sheet,
    data_sheet_summary,
    extract_workbook,
    hide_sheets,
    segment_sheet,
)

from tests.search_enhancement._xlsx_fixtures import (
    Sheet,
    build_xlsx,
    ledger_sheet,
    quote_sheet,
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
    capture: dict[str, bytes] | None = None,
) -> None:
    """Replace the LibreOffice round-trip with a canned Calc rendering."""

    def convert(source, outdir, **_kwargs):
        if capture is not None:
            capture["payload"] = Path(source).read_bytes()
        return ConversionResult(
            pdf_path=Path(outdir) / "input.pdf",
            conversion_seconds=0.01,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(xlsx_module, "convert_document_to_pdf", convert)
    monkeypatch.setattr(
        xlsx_module,
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
        xlsx_module,
        "_pages_with_rendered_visuals",
        lambda _pdf_path: set(rendered_visual_pages or ()),
    )


def _prepare(data: bytes, *, tier: str = "vision", name: str = "book.xlsx"):
    return XlsxHandler().prepare(
        data,
        source_name=name,
        config=_config(tier),
        reporter=NullProgressReporter(),
    )


# --------------------------------------------------------------------------- #
# Registration and validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "filename", ["budget.xlsx", "budget.XLSX", "budget.xlsm"]
)
def test_xlsx_handler_detected_by_extension(filename: str) -> None:
    assert detect_handler(filename).format_id == "xlsx"


def test_xlsx_is_supported_and_enabled_by_default() -> None:
    assert ".xlsx" in supported_extensions()
    assert ".xlsm" in supported_extensions()
    assert is_format_enabled("xlsx", {})
    assert ".xlsx" in enabled_extensions({})


def test_format_status_marks_xlsx_supported() -> None:
    rows = {row["format_id"]: row for row in format_status({})}
    assert rows["xlsx"]["supported"] is True


def test_validate_rejects_other_suffixes() -> None:
    with pytest.raises(InvalidDocumentError, match="Only .xlsx"):
        XlsxHandler().validate(
            b"PK\x03\x04", filename="book.csv", max_bytes=1024
        )


def test_validate_rejects_empty_payload() -> None:
    with pytest.raises(InvalidDocumentError, match="empty"):
        XlsxHandler().validate(b"", filename="book.xlsx", max_bytes=1024)


def test_validate_enforces_the_size_limit() -> None:
    with pytest.raises(InvalidDocumentError, match="MB limit"):
        XlsxHandler().validate(
            b"PK\x03\x04" + b"\x00" * 64,
            filename="book.xlsx",
            max_bytes=8,
        )


def test_validate_reports_encrypted_workbooks_distinctly() -> None:
    with pytest.raises(EncryptedDocumentError, match="password-protected"):
        XlsxHandler().validate(
            _OLE2_MAGIC + b"\x00" * 32, filename="book.xlsx", max_bytes=1024
        )


def test_validate_rejects_non_zip_payloads() -> None:
    with pytest.raises(InvalidDocumentError, match="not a valid Excel package"):
        XlsxHandler().validate(
            b"not a zip", filename="book.xlsx", max_bytes=1024
        )


def test_fingerprint_is_stable_and_content_addressed() -> None:
    handler = XlsxHandler()
    data = build_xlsx([quote_sheet()])
    assert handler.fingerprint(data) == handler.fingerprint(data)
    assert handler.fingerprint(data).startswith("xlsx-sha256:")


def test_prepare_rejects_a_package_without_a_workbook_part() -> None:
    broken = build_xlsx([quote_sheet()], drop_parts=["xl/workbook.xml"])
    with pytest.raises(InvalidDocumentError, match="could not be read"):
        _prepare(broken)


def test_extract_rejects_payloads_that_are_not_zips() -> None:
    with pytest.raises(XlsxSemanticError, match="not a valid Excel package"):
        extract_workbook(b"not a zip at all")


# --------------------------------------------------------------------------- #
# Cell extraction
# --------------------------------------------------------------------------- #


def test_inline_shared_and_typed_cells_are_all_read() -> None:
    sheet = Sheet(
        "혼합",
        [
            ["문자", "숫자", "정수"],
            ["가나다", 1.5, 42],
        ],
    )
    extraction = extract_workbook(build_xlsx([sheet]))
    assert extraction.sheets[0].grid == (
        ("문자", "숫자", "정수"),
        ("가나다", "1.5", "42"),
    )


def test_merged_values_are_duplicated_across_the_span() -> None:
    sheet = Sheet(
        "양식",
        [["견적서", None, None], ["품명", "수량", "금액"]],
        merges=["A1:C1"],
    )
    extracted = extract_workbook(build_xlsx([sheet])).sheets[0]
    assert extracted.grid[0] == ("견적서", "견적서", "견적서")
    # The renderer only draws the anchor once, so alignment must not look for
    # the duplicates.
    assert extracted.raw_grid[0] == ("견적서", "", "")


def test_sheets_beyond_the_row_cap_are_truncated_but_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "crewmeal.search_enhancement.xlsx_semantic.MAX_GRID_ROWS", 10
    )
    extracted = extract_workbook(build_xlsx([ledger_sheet(rows=40)])).sheets[0]
    assert extracted.row_count == 41
    assert len(extracted.grid) == 10
    assert extracted.truncated is True
    assert any("앞의" in warning for warning in extracted.warnings)


def test_formulas_without_a_cached_value_are_reported() -> None:
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/'
        'spreadsheetml/2006/main"><dimension ref="A1:B2"/><sheetData>'
        '<row r="1"><c r="A1" t="inlineStr"><is><t>합계</t></is></c>'
        '<c r="B1"><f>SUM(B2:B9)</f></c></row>'
        "</sheetData></worksheet>"
    )
    data = build_xlsx(
        [Sheet("계산", [["합계", None]])],
        extra_parts={"xl/worksheets/sheet1.xml": worksheet},
    )
    extracted = extract_workbook(data).sheets[0]
    assert any("계산된 값" in warning for warning in extracted.warnings)


def test_charts_and_images_are_read_from_the_drawing_chain() -> None:
    sheet = Sheet(
        "대시보드",
        [["구분", "매출"], ["1분기", 100]],
        images=1,
        alt_texts=("분기별 매출 추이",),
        charts=[
            {
                "title": "매출 추이",
                "series": [
                    {"name": "매출", "points": [("1분기", "100"), ("2분기", "200")]}
                ],
            }
        ],
    )
    extracted = extract_workbook(build_xlsx([sheet])).sheets[0]
    assert extracted.image_count == 1
    assert extracted.alt_texts == ("분기별 매출 추이",)
    assert extracted.charts[0].title == "매출 추이"
    assert [point.label for point in extracted.charts[0].data_points] == [
        "1분기",
        "2분기",
    ]


# --------------------------------------------------------------------------- #
# Number and date formats
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("serial", "style", "expected"),
    [
        (45_678, 14, "2025-01-21"),
        (1, 14, "1900-01-01"),
        (59, 14, "1900-02-28"),
        (61, 14, "1900-03-01"),
        (45_678.5, 22, "2025-01-21 12:00:00"),
        (0.25, 20, "06:00:00"),
    ],
)
def test_builtin_date_formats_become_readable_dates(
    serial: float, style: int, expected: str
) -> None:
    sheet = Sheet("일정", [["계약일"], [(serial, 0)]])
    data = build_xlsx([sheet], number_formats={0: style})
    assert extract_workbook(data).sheets[0].grid[1][0] == expected


def test_the_phantom_1900_leap_day_is_reported_literally() -> None:
    sheet = Sheet("일정", [["계약일"], [(60, 0)]])
    data = build_xlsx([sheet], number_formats={0: 14})
    assert extract_workbook(data).sheets[0].grid[1][0] == "1900-02-29"


def test_custom_korean_date_format_is_detected() -> None:
    sheet = Sheet("일정", [["계약일"], [(45_678, 0)]])
    data = build_xlsx([sheet], number_formats={0: 'yyyy"년" mm"월" dd"일"'})
    assert extract_workbook(data).sheets[0].grid[1][0] == "2025-01-21"


def test_the_1904_date_system_shifts_the_epoch() -> None:
    """The 1904 serial for the same day is 1,462 lower than the 1900 one."""

    sheet = Sheet("일정", [["계약일"], [(45_678 - 1_462, 0)]])
    data = build_xlsx([sheet], number_formats={0: 14}, date1904=True)
    assert extract_workbook(data).sheets[0].grid[1][0] == "2025-01-21"


def test_quoted_month_literal_does_not_make_a_number_a_date() -> None:
    # The literal contains "d" and "y", but only inside quotes.
    sheet = Sheet("금액", [["단가"], [(1500, 0)]])
    data = build_xlsx([sheet], number_formats={0: '#,##0"day"'})
    assert extract_workbook(data).sheets[0].grid[1][0] == "1500"


def test_percent_formats_are_rendered_as_percentages() -> None:
    sheet = Sheet("비율", [["달성률"], [(0.153, 0)]])
    data = build_xlsx([sheet], number_formats={0: "0.0%"})
    assert extract_workbook(data).sheets[0].grid[1][0] == "15.3%"


def test_unformatted_numbers_keep_their_integer_shape() -> None:
    sheet = Sheet("금액", [["단가"], [(1500.0, 0)]])
    data = build_xlsx([sheet], number_formats={0: 0})
    assert extract_workbook(data).sheets[0].grid[1][0] == "1500"


# --------------------------------------------------------------------------- #
# Sheet classification
# --------------------------------------------------------------------------- #


def test_a_korean_quotation_form_classifies_as_document() -> None:
    extracted = extract_workbook(build_xlsx([quote_sheet()])).sheets[0]
    classification = classify_sheet(extracted)
    assert classification.kind == "document"
    assert "인쇄 영역 지정됨" in classification.reasons


def test_a_long_filtered_ledger_classifies_as_data() -> None:
    extracted = extract_workbook(build_xlsx([ledger_sheet()])).sheets[0]
    classification = classify_sheet(extracted)
    assert classification.kind == "data"
    assert "구조적 표/필터/피벗 존재" in classification.reasons


def test_a_short_ledger_with_a_list_object_still_classifies_as_data() -> None:
    """Small is a document signal, but a declared table outweighs it."""

    sheet = Sheet(
        "품목",
        [["번호", "품명", "단가"]]
        + [[number, f"품목{number}", number * 100] for number in range(1, 12)],
        table_name="품목표",
        autofilter="A1:C12",
    )
    extracted = extract_workbook(build_xlsx([sheet])).sheets[0]
    assert extracted.list_object_names == ("품목표",)
    assert classify_sheet(extracted).kind == "data"


def test_an_empty_sheet_is_treated_as_data() -> None:
    extracted = extract_workbook(
        build_xlsx([quote_sheet(), Sheet("빈시트", [[None]])])
    ).sheets[1]
    classification = classify_sheet(extracted)
    assert classification.kind == "data"
    assert classification.reasons == ("빈 시트",)


def test_vertical_merges_push_a_borderline_sheet_toward_document() -> None:
    rows: list[list[object]] = [["구분", "항목", "금액"]]
    for number in range(1, 30):
        rows.append(["상반기" if number < 15 else "하반기", f"항목{number}", number])
    sheet = Sheet("정산", rows, merges=["A2:A15", "A16:A30"])
    extracted = extract_workbook(build_xlsx([sheet])).sheets[0]
    classification = classify_sheet(extracted)
    assert "세로 병합 존재" in classification.reasons
    assert classification.kind == "document"


# --------------------------------------------------------------------------- #
# Document-sheet segmentation
# --------------------------------------------------------------------------- #


def test_single_cell_rows_become_text_and_wide_rows_become_a_table() -> None:
    extracted = extract_workbook(
        build_xlsx(
            [
                Sheet(
                    "견적서",
                    [
                        ["견적서", None, None],
                        ["품명", "수량", "금액"],
                        ["A자재", 10, 50_000],
                        ["비고: 부가세 별도", None, None],
                    ],
                )
            ]
        )
    ).sheets[0]
    blocks = segment_sheet(extracted)
    assert [block.kind for block in blocks] == ["text", "table", "text"]
    assert blocks[0].lines == ("견적서",)
    assert blocks[1].table is not None
    assert blocks[1].table.headers == ("품명", "수량", "금액")
    assert blocks[1].table.rows == (("A자재", "10", "50000"),)
    assert blocks[2].lines == ("비고: 부가세 별도",)


def test_a_merged_title_bar_reads_as_one_text_line() -> None:
    """A1:C1 merged is one cell on the page, whatever the flat grid says."""

    extracted = extract_workbook(build_xlsx([quote_sheet()])).sheets[0]
    blocks = segment_sheet(extracted)
    assert blocks[0].kind == "text"
    assert blocks[0].lines == ("견적서",)
    assert blocks[1].kind == "table"
    assert blocks[1].table is not None
    assert blocks[1].table.headers == ("품명", "수량", "금액")


def test_a_metadata_band_is_not_mistaken_for_the_table_header() -> None:
    """Regression: a real Calc run published the customer as a column name.

    Korean forms stack a label/value band straight onto the line-item table
    with no blank row, so both land in one run of multi-cell rows.
    """

    sheet = Sheet(
        "견적서",
        [
            ["견적서", None, None, None],
            ["고객사", "한국전력공사", "견적일", "2025-01-21"],
            ["품명", "규격", "수량", "금액"],
            ["A자재", "10T", 10, 50_000],
            ["B자재", "20T", 5, 30_000],
        ],
        merges=["A1:D1"],
    )
    blocks = segment_sheet(extract_workbook(build_xlsx([sheet])).sheets[0])

    assert [block.kind for block in blocks] == ["text", "text", "table"]
    assert blocks[1].lines == ("고객사 한국전력공사 견적일 2025-01-21",)
    assert blocks[2].table is not None
    assert blocks[2].table.headers == ("품명", "규격", "수량", "금액")
    assert len(blocks[2].table.rows) == 2


def test_a_plain_table_keeps_its_first_row_as_the_header() -> None:
    """The preamble split must not fire when there is no band to peel."""

    sheet = Sheet(
        "단가표",
        [
            ["품명", "수량", "금액"],
            ["A자재", 10, 50_000],
            ["B자재", 5, 30_000],
            ["C자재", 2, 10_000],
        ],
    )
    blocks = segment_sheet(extract_workbook(build_xlsx([sheet])).sheets[0])

    assert [block.kind for block in blocks] == ["table"]
    assert blocks[0].table is not None
    assert blocks[0].table.headers == ("품명", "수량", "금액")
    assert len(blocks[0].table.rows) == 3


def test_a_banded_header_survives_the_preamble_split() -> None:
    """A merge opening the run means the first row really is a header."""

    sheet = Sheet(
        "실적표",
        [
            ["구분", "2024", None, "2025", None],
            [None, "상반기", "하반기", "상반기", "하반기"],
            ["매출", 100, 120, 130, 150],
        ],
        merges=["B1:C1", "D1:E1", "A1:A2"],
    )
    blocks = segment_sheet(extract_workbook(build_xlsx([sheet])).sheets[0])

    assert [block.kind for block in blocks] == ["table"]
    assert blocks[0].table is not None
    assert blocks[0].table.headers == (
        "구분",
        "2024 / 상반기",
        "2024 / 하반기",
        "2025 / 상반기",
        "2025 / 하반기",
    )


def test_a_merged_label_in_the_metadata_band_is_not_repeated() -> None:
    """The band is read from the raw grid, so a span is drawn once.

    B3:D3 is merged, so the flattened grid holds three copies of the address;
    the rendering draws it once, so repeating it would read wrong and strand
    the block during page alignment.
    """

    sheet = Sheet(
        "견적서",
        [
            ["견적서", None, None, None, None],
            ["고객사", "한국전력공사", "견적일", "2025-01-21", None],
            ["주소", "서울시 강남구", None, None, "비고"],
            ["품명", "규격", "수량", "금액", "단가"],
            ["A자재", "10T", 10, 50_000, 5_000],
        ],
        merges=["A1:E1", "B3:D3"],
    )
    blocks = segment_sheet(extract_workbook(build_xlsx([sheet])).sheets[0])

    assert [block.kind for block in blocks] == ["text", "text", "table"]
    assert blocks[1].lines == (
        "고객사 한국전력공사 견적일 2025-01-21",
        "주소 서울시 강남구 비고",
    )
    assert blocks[2].table is not None
    assert blocks[2].table.headers == ("품명", "규격", "수량", "금액", "단가")


def test_text_block_alignment_keys_are_built_per_cell() -> None:
    """Regression: Calc clips a cell overflowing into a filled neighbour.

    A real rendering of "고객사 | 한국전력공사 | 견적일" reads
    "고객사한국전력공견적일", so a key spanning the join never matches and the
    block falls back to the previous page with a warning.
    """

    sheet = Sheet(
        "견적서",
        [
            ["견적서", None, None, None],
            ["고객사", "한국전력공사", "견적일", "2025-01-21"],
            ["품명", "규격", "수량", "금액"],
            ["A자재", "10T", 10, 50_000],
            ["B자재", "20T", 5, 30_000],
        ],
        merges=["A1:D1"],
    )
    blocks = segment_sheet(extract_workbook(build_xlsx([sheet])).sheets[0])
    keys = blocks[1].alignment_keys

    assert "2025-01-21" in keys
    assert not any("한국전력공사견적일" in key for key in keys)


def test_a_clipped_cell_no_longer_strands_its_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: the clipped rendering must still align without warnings."""

    sheet = Sheet(
        "견적서",
        [
            ["견적서", None, None, None],
            ["고객사", "한국전력공사", "견적일", "2025-01-21"],
            ["품명", "규격", "수량", "금액"],
            ["A자재", "10T", 10, 50_000],
        ],
        merges=["A1:D1"],
        print_area="A1:D4",
    )
    # Verbatim from a real soffice run: clipped and out of reading order.
    _stub_libreoffice(
        monkeypatch,
        {
            1: (
                "A자재",
                "견적서",
                "고객사",
                "한국전력공견적일",
                "2025-01-21",
                "품명",
                "규격",
                "수량",
                "금액",
                "10T",
                "10",
                "50000",
            )
        },
    )
    prepared = _prepare(build_xlsx([sheet]))

    assert len(prepared.semantic_slides) == 1
    assert not prepared.semantic_slides[0].warnings


def test_a_banded_header_is_joined_with_a_separator() -> None:
    sheet = Sheet(
        "실적",
        [
            ["구분", "2024", None, "2025", None],
            [None, "상반기", "하반기", "상반기", "하반기"],
            ["매출", 100, 120, 130, 140],
            ["비용", 40, 50, 60, 70],
        ],
        merges=["B1:C1", "D1:E1"],
    )
    extracted = extract_workbook(build_xlsx([sheet])).sheets[0]
    block = segment_sheet(extracted)[0]
    assert block.table is not None
    assert block.table.headers == (
        "구분",
        "2024 / 상반기",
        "2024 / 하반기",
        "2025 / 상반기",
        "2025 / 하반기",
    )
    assert block.complexity is not None
    assert block.complexity.multi_row_header is True
    assert block.complexity.is_complex is True


def test_merged_tables_are_flattened_into_a_rectangle() -> None:
    sheet = Sheet(
        "정산",
        [
            ["구분", "항목", "금액"],
            ["상반기", "재료비", 100],
            [None, "인건비", 200],
        ],
        merges=["A2:A3"],
    )
    extracted = extract_workbook(build_xlsx([sheet])).sheets[0]
    block = segment_sheet(extracted)[0]
    assert block.table is not None
    assert block.table.rows == (
        ("상반기", "재료비", "100"),
        ("상반기", "인건비", "200"),
    )
    assert block.complexity is not None
    assert block.complexity.vertical_merge is True


def test_duplicate_headers_are_disambiguated() -> None:
    sheet = Sheet("표", [["금액", "금액", "비고"], [1, 2, "확인"]])
    extracted = extract_workbook(build_xlsx([sheet])).sheets[0]
    block = segment_sheet(extracted)[0]
    assert block.table is not None
    assert block.table.headers == ("금액", "금액 (2)", "비고")


def test_table_alignment_keys_come_from_the_unmerged_grid() -> None:
    """Flattened duplicates never appear in a rendering, so they must not be
    used as search keys -- the same defect that broke Word table alignment."""

    sheet = Sheet(
        "정산",
        [["구분", "항목", "금액"], ["상반기결산", "재료비", 100], [None, "인건비", 200]],
        merges=["A2:A3"],
    )
    extracted = extract_workbook(build_xlsx([sheet])).sheets[0]
    block = segment_sheet(extracted)[0]
    assert block.alignment_keys.count("상반기결산") == 1


# --------------------------------------------------------------------------- #
# Data-sheet summaries
# --------------------------------------------------------------------------- #


def test_a_data_sheet_summary_reports_schema_scale_and_samples() -> None:
    extracted = extract_workbook(build_xlsx([ledger_sheet(rows=500)])).sheets[0]
    sections, tables = data_sheet_summary(extracted, classify_sheet(extracted))

    overview = next(
        section for section in sections if section.heading == "시트 개요"
    )
    assert any("501행" in paragraph for paragraph in overview.paragraphs)
    assert any("자동 필터" in paragraph for paragraph in overview.paragraphs)

    columns = next(section for section in sections if section.heading == "컬럼")
    assert columns.bullets == ("번호 (숫자)", "지점 (텍스트)", "매출 (숫자)")

    assert tables[0].headers == ("번호", "지점", "매출")
    assert len(tables[0].rows) == 8
    assert tables[0].rows[0] == ("1", "지점1", "1000")


def test_a_summary_notes_that_the_sheet_was_hidden_in_the_source() -> None:
    sheet = ledger_sheet(rows=30)
    sheet.state = "hidden"
    extracted = extract_workbook(build_xlsx([quote_sheet(), sheet])).sheets[1]
    sections, _ = data_sheet_summary(extracted, classify_sheet(extracted))
    assert any(
        "숨겨진 시트" in paragraph for paragraph in sections[0].paragraphs
    )


def test_a_title_row_above_the_header_does_not_become_the_schema() -> None:
    rows: list[list[object]] = [["2025년 매출 원장", None, None], ["번호", "지점", "매출"]]
    rows.extend([[number, f"지점{number}", number * 100] for number in range(1, 20)])
    extracted = extract_workbook(
        build_xlsx([Sheet("원장", rows, autofilter="A2:C21")])
    ).sheets[0]
    _, tables = data_sheet_summary(extracted, classify_sheet(extracted))
    assert tables[0].headers == ("번호", "지점", "매출")


def test_wide_data_sheets_only_sample_the_leading_columns() -> None:
    headers = [f"컬럼{index}" for index in range(1, 31)]
    rows: list[list[object]] = [headers]
    rows.extend([[f"값{index}" for index in range(1, 31)] for _ in range(10)])
    extracted = extract_workbook(build_xlsx([Sheet("넓은표", rows)])).sheets[0]
    _, tables = data_sheet_summary(extracted, classify_sheet(extracted))
    assert len(tables[0].headers) == 16
    assert all(len(row) == 16 for row in tables[0].rows)
    assert "앞 16열" in tables[0].title


# --------------------------------------------------------------------------- #
# Sheet hiding and page attribution
# --------------------------------------------------------------------------- #


def test_hide_sheets_marks_only_the_named_sheets() -> None:
    data = build_xlsx([quote_sheet(), ledger_sheet(rows=30)])
    hidden = hide_sheets(data, ["매출원장"])
    extraction = extract_workbook(hidden)
    assert extraction.sheets[0].is_hidden is False
    assert extraction.sheets[1].is_hidden is True
    # Only the workbook part changes; the sheets keep their content.
    assert extraction.sheets[1].grid[0] == ("번호", "지점", "매출")


def test_hide_sheets_refuses_to_hide_every_sheet() -> None:
    data = build_xlsx([quote_sheet()])
    assert hide_sheets(data, ["견적서"]) == data


def test_hide_sheets_replaces_an_existing_state_attribute() -> None:
    sheet = ledger_sheet(rows=10)
    sheet.state = "veryHidden"
    data = build_xlsx([quote_sheet(), sheet])
    hidden = hide_sheets(data, ["매출원장"])
    assert extract_workbook(hidden).sheets[1].state == "hidden"


def test_pages_are_attributed_to_sheets_in_workbook_order() -> None:
    extraction = extract_workbook(
        build_xlsx(
            [
                Sheet("견적서", [["견적서"], ["품명", "수량"], ["갑자재", 10]]),
                Sheet("정산서", [["정산서"], ["항목", "금액"], ["을항목", 20]]),
            ]
        )
    )
    # Calc emits text out of cell reading order, so the fixture does too.
    texts = {
        1: ("품명", "수량", "10", "견적서", "갑자재"),
        2: ("항목", "금액", "20", "정산서", "을항목"),
    }
    assert attribute_pages_to_sheets(extraction.sheets, texts) == {1: 0, 2: 1}


def test_attribution_never_moves_backwards_through_the_workbook() -> None:
    extraction = extract_workbook(
        build_xlsx(
            [
                Sheet("첫시트", [["첫시트고유값"], ["항목", "값"], ["가나다라", 1]]),
                Sheet("둘째시트", [["둘째시트고유값"], ["항목", "값"], ["마바사아", 2]]),
            ]
        )
    )
    # Page 2 mentions the first sheet's title, but a later page can never
    # belong to an earlier sheet.
    texts = {
        1: ("첫시트고유값", "가나다라"),
        2: ("둘째시트고유값", "마바사아", "첫시트고유값"),
    }
    assert attribute_pages_to_sheets(extraction.sheets, texts) == {1: 0, 2: 1}


def test_every_sheet_keeps_at_least_one_page_when_text_is_useless() -> None:
    extraction = extract_workbook(
        build_xlsx(
            [
                Sheet("A", [["항목", "값"], ["가", 1]]),
                Sheet("B", [["항목", "값"], ["나", 2]]),
            ]
        )
    )
    texts = {1: ("알 수 없음",), 2: ("알 수 없음",)}
    assert attribute_pages_to_sheets(extraction.sheets, texts) == {1: 0, 2: 1}


# --------------------------------------------------------------------------- #
# prepare(): unit assembly
# --------------------------------------------------------------------------- #


def test_data_only_workbooks_skip_libreoffice_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("LibreOffice must not be called for data sheets")

    monkeypatch.setattr(xlsx_module, "convert_document_to_pdf", explode)
    prepared = _prepare(build_xlsx([ledger_sheet(rows=200)]))

    assert prepared.renderer_manifest.page_count == 1
    assert prepared.renderer_manifest.page_images == {}
    assert prepared.stage_timings["conversionSeconds"] == 0.0
    assert prepared.semantic_slides[0].title == "매출원장 (데이터 시트 요약)"


def test_data_sheets_are_hidden_before_conversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture: dict[str, bytes] = {}
    _stub_libreoffice(
        monkeypatch,
        {1: ("견적서", "품명", "수량", "금액", "A자재1", "10", "50000")},
        capture=capture,
    )
    _prepare(build_xlsx([quote_sheet(), ledger_sheet(rows=200)]))

    converted = extract_workbook(capture["payload"])
    assert converted.sheets[0].is_hidden is False
    assert converted.sheets[1].is_hidden is True


def test_mixed_workbooks_number_units_contiguously(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_libreoffice(
        monkeypatch,
        {1: ("견적서", "품명", "수량", "금액", "A자재1", "10", "50000")},
    )
    prepared = _prepare(
        build_xlsx(
            [quote_sheet(), ledger_sheet(rows=200), ledger_sheet(rows=150, name="구매원장")]
        )
    )

    assert [slide.slide_number for slide in prepared.semantic_slides] == [1, 2, 3]
    assert prepared.renderer_manifest.page_count == 3
    assert prepared.semantic_slides[1].title.startswith("매출원장")
    assert prepared.semantic_slides[2].title.startswith("구매원장")


def test_page_images_are_rekeyed_from_pdf_page_to_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A data sheet placed first shifts every rendered page's unit number."""

    _stub_libreoffice(
        monkeypatch,
        {1: ("견적서", "품명", "수량", "금액", "A자재1", "10", "50000")},
        rendered_visual_pages={1},
    )
    prepared = _prepare(
        build_xlsx([ledger_sheet(rows=200), quote_sheet()])
    )

    assert [slide.title for slide in prepared.semantic_slides][0].startswith(
        "매출원장"
    )
    # The quotation is PDF page 1 but unit 2.
    assert set(prepared.renderer_manifest.page_images) == {2}


def test_complex_tables_promote_their_unit_to_vision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sheet = Sheet(
        "정산서",
        [
            ["구분", "항목", "금액"],
            ["상반기", "재료비", 100],
            [None, "인건비", 200],
        ],
        merges=["A2:A3"],
        print_area=True,
    )
    _stub_libreoffice(
        monkeypatch,
        {1: ("구분", "항목", "금액", "상반기", "재료비", "인건비", "100", "200")},
    )
    prepared = _prepare(build_xlsx([sheet]))

    assert set(prepared.renderer_manifest.page_images) == {1}
    assert any(
        "세로 병합" in warning
        for warning in prepared.semantic_slides[0].warnings
    )


def test_a_text_only_document_sheet_costs_no_vision_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sheet = Sheet(
        "확인서",
        [["품명", "수량"], ["A자재", "열개"], ["B자재", "스무개"]],
        print_area=True,
    )
    _stub_libreoffice(
        monkeypatch, {1: ("품명", "수량", "A자재", "열개", "B자재", "스무개")}
    )
    prepared = _prepare(build_xlsx([sheet]))
    assert prepared.renderer_manifest.page_images == {}


def test_a_multi_page_document_sheet_splits_blocks_across_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows: list[list[object]] = [["앞표지제목", None], ["품명", "수량"]]
    rows.extend([[f"자재{number}", number] for number in range(1, 6)])
    rows.append(["뒷장주석내용", None])
    sheet = Sheet("명세서", rows, print_area=True)

    _stub_libreoffice(
        monkeypatch,
        {
            1: ("앞표지제목", "품명", "수량", "자재1", "자재2", "1", "2"),
            2: ("자재3", "자재4", "자재5", "뒷장주석내용"),
        },
    )
    prepared = _prepare(build_xlsx([sheet]))

    assert len(prepared.semantic_slides) == 2
    assert prepared.semantic_slides[0].title.startswith("명세서 (1/2)")
    assert "뒷표지" not in prepared.semantic_slides[0].title
    assert prepared.semantic_slides[1].sections[0].paragraphs == (
        "뒷장주석내용",
    )


def test_sheets_that_render_to_too_many_pages_are_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sheet = Sheet(
        "명세서", [["품명", "수량"], ["A자재", 1]], print_area=True
    )
    _stub_libreoffice(
        monkeypatch,
        {page: ("품명", "수량", "A자재") for page in range(1, 25)},
    )
    prepared = _prepare(build_xlsx([sheet]))
    assert any(
        "문서형으로 보기 어렵습니다" in warning
        for warning in prepared.semantic_slides[0].warnings
    )


def test_low_tier_summarizes_every_sheet_without_rendering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("the low tier must not call LibreOffice")

    monkeypatch.setattr(xlsx_module, "convert_document_to_pdf", explode)
    prepared = _prepare(
        build_xlsx([quote_sheet(), ledger_sheet(rows=50)]), tier="text_ocr"
    )

    assert len(prepared.semantic_slides) == 2
    assert prepared.renderer_manifest.page_images == {}
    assert prepared.stage_timings["lowTierExtractionSeconds"] > 0


def test_conversion_failures_surface_as_fidelity_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args, **_kwargs):
        raise LibreOfficeConversionError("calc filter missing")

    monkeypatch.setattr(xlsx_module, "convert_document_to_pdf", fail)
    with pytest.raises(ProcessingFidelityError, match="could not lay out"):
        _prepare(build_xlsx([quote_sheet()]))


def test_an_empty_rendering_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_libreoffice(monkeypatch, {})
    with pytest.raises(ProcessingFidelityError, match="no pages"):
        _prepare(build_xlsx([quote_sheet()]))


# --------------------------------------------------------------------------- #
# Output contract
# --------------------------------------------------------------------------- #


def test_prepared_units_render_through_the_real_html_renderer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The published artefact is HTML, so the units must survive it."""

    _stub_libreoffice(
        monkeypatch,
        {1: ("견적서", "품명", "수량", "금액", "A자재1", "10", "50000")},
    )
    prepared = _prepare(build_xlsx([quote_sheet(), ledger_sheet(rows=300)]))

    rendered = render_presentation_html(
        source_name="book.xlsx",
        slides=prepared.semantic_slides,
        unit_label="시트",
    )
    assert "견적서" in rendered.content
    assert "매출원장" in rendered.content
    assert "상위 8행 샘플" in rendered.content


def test_a_data_summary_stays_far_below_the_sharepoint_column_limit() -> None:
    """The whole point of summarizing: a huge ledger must still fit."""

    extracted = extract_workbook(build_xlsx([ledger_sheet(rows=3_000)])).sheets[0]
    sections, tables = data_sheet_summary(extracted, classify_sheet(extracted))
    total = sum(
        len(text)
        for section in sections
        for text in (*section.paragraphs, *section.bullets)
    ) + sum(
        len(cell) for table in tables for row in table.rows for cell in row
    )
    assert total < 4_000

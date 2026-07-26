"""Excel (.xlsx / .xlsm) workbook extraction and sheet classification.

Excel is not a document format that happens to have a grid; it is a grid that
Korean enterprises *also* use to draw documents. Those two uses need opposite
treatment, and mixing them in one file is the norm rather than the exception::

    견적서.xlsx
      ├─ "견적서"   ← a printed form: merged title bar, 20 rows, print area set
      └─ "품목마스터" ← a 40,000-row ledger with an AutoFilter

Transcribing the ledger is impossible (the SharePoint column ceiling is 63,999
characters) and pointless (SharePoint already indexes cell text natively).
Rendering it is worse: it becomes hundreds of PDF pages. So this module
classifies **each sheet** and the handler branches:

* **document sheets** are rendered and treated exactly like Word pages, with
  merged tables flattened and complex ones promoted to visual analysis;
* **data sheets** are never rendered and collapse to a single summary unit
  describing the schema plus a few sample rows.

Everything here is deterministic ``zipfile`` + ``defusedxml`` parsing -- no
model, no tokens, no new dependency.
"""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from defusedxml import ElementTree

from crewmeal.search_enhancement.models import (
    ContentChart,
    ContentSection,
    ContentTable,
)
from crewmeal.search_enhancement.ooxml import (
    NS as _BASE_NS,
    TableComplexity,
    chart_content,
    local_name as _local_name,
    normalize_space as _normalize_space,
    search_key as _search_key,
)

SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
SPREADSHEET_DRAWING_NS = (
    "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
)

NS = {**_BASE_NS, "s": SHEET_NS, "xdr": SPREADSHEET_DRAWING_NS}

WORKBOOK_PART = "xl/workbook.xml"
SHARED_STRINGS_PART = "xl/sharedStrings.xml"
STYLES_PART = "xl/styles.xml"

#: Rows materialized per sheet. Row *counts* stay exact beyond this -- the cap
#: only bounds memory, so a million-row ledger is still reported as such while
#: only its head is kept for schema inference and sampling.
MAX_GRID_ROWS = 5_000

#: Columns materialized per sheet. Excel allows 16,384; a real sheet that wide
#: is machine output, and the tail carries no describable meaning.
MAX_GRID_COLUMNS = 512

#: Upper bound on sheets read from one workbook.
MAX_SHEETS = 300

#: Rows of a data sheet reproduced verbatim in its summary unit.
SAMPLE_ROWS = 8

#: Columns shown in a data sheet's sample table. Wider tables stop being
#: readable and start eating the character budget the summary exists to save.
SAMPLE_COLUMNS = 16

#: Values scanned per column when guessing its type.
_TYPE_SAMPLE_LIMIT = 200

#: A document sheet is a page someone laid out by hand. Past this many rows it
#: is a list, whatever it looks like.
_DOCUMENT_ROW_CEILING = 60

#: Cell references are ``[$]COL[$]ROW`` -- the ``$`` absolute markers appear in
#: defined names such as print areas.
_CELL_REFERENCE = re.compile(r"^\$?([A-Z]{1,3})\$?([0-9]{1,7})$")

#: Excel built-in number format ids that mean "this number is a date or time".
#: 14-17 date, 18-21 time, 22 datetime, 45-47 elapsed time.
_BUILTIN_DATE_FORMATS = frozenset(range(14, 23)) | frozenset({45, 46, 47})
_BUILTIN_TIME_ONLY_FORMATS = frozenset(range(18, 22))
_BUILTIN_ELAPSED_FORMATS = frozenset({45, 46, 47})

#: Literal runs inside a number format code that must not be scanned for date
#: tokens: quoted text, backslash escapes, and ``[...]`` colour/locale sections.
_FORMAT_LITERALS = re.compile(r'"[^"]*"|\\.|\[[^\]]*\]')

SheetKind = Literal["document", "data"]


class XlsxSemanticError(ValueError):
    """Raised when an Excel package cannot be normalized safely."""


@dataclass(frozen=True, slots=True)
class MergeRange:
    """A rectangular merged region, 1-based and inclusive on both ends."""

    first_row: int
    first_column: int
    last_row: int
    last_column: int

    @property
    def spans_columns(self) -> bool:
        return self.last_column > self.first_column

    @property
    def spans_rows(self) -> bool:
        return self.last_row > self.first_row


@dataclass(frozen=True, slots=True)
class SheetExtraction:
    """One worksheet, normalized to a dense grid plus structural signals.

    ``grid`` has merged values propagated across the range they span, because
    :class:`~crewmeal.search_enhancement.models.ContentTable` is a strict
    rectangle. ``raw_grid`` keeps the original single-anchor placement, which is
    what a renderer actually draws -- aligning against ``grid`` would search the
    rendered page for duplicate text that is not there.
    """

    name: str
    index: int
    part: str
    state: str
    grid: tuple[tuple[str, ...], ...]
    raw_grid: tuple[tuple[str, ...], ...]
    merges: tuple[MergeRange, ...]
    row_count: int
    column_count: int
    has_print_area: bool = False
    has_autofilter: bool = False
    list_object_names: tuple[str, ...] = ()
    pivot_table_count: int = 0
    drawing_count: int = 0
    image_count: int = 0
    alt_texts: tuple[str, ...] = ()
    charts: tuple[ContentChart, ...] = ()
    defined_names: tuple[str, ...] = ()
    truncated: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not any(any(cell for cell in row) for row in self.grid)

    @property
    def is_hidden(self) -> bool:
        return self.state.casefold() in {"hidden", "veryhidden"}

    @property
    def has_visuals(self) -> bool:
        return bool(self.drawing_count or self.image_count or self.charts)


@dataclass(frozen=True, slots=True)
class SheetClassification:
    """The document/data verdict for one sheet, with its supporting signals."""

    kind: SheetKind
    document_score: int
    data_score: int
    reasons: tuple[str, ...]

    @property
    def is_document(self) -> bool:
        return self.kind == "document"


@dataclass(frozen=True, slots=True)
class SheetBlock:
    """A run of grid rows that reads as one unit: loose text or a table.

    Korean spreadsheet forms alternate between full-width title/note rows and
    tabular bodies, so a sheet is segmented by how many cells each row fills
    rather than by any structure Excel records.
    """

    kind: Literal["text", "table"]
    first_row: int
    last_row: int
    lines: tuple[str, ...] = ()
    table: ContentTable | None = None
    complexity: TableComplexity | None = None
    alignment_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkbookExtraction:
    """Everything read from one workbook package."""

    sheets: tuple[SheetExtraction, ...] = ()
    defined_names: tuple[str, ...] = ()
    date_system_1904: bool = False
    warnings: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def extract_workbook(data: bytes) -> WorkbookExtraction:
    """Read an .xlsx/.xlsm package into normalized per-sheet structure."""

    try:
        package = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise XlsxSemanticError("The file is not a valid Excel package.") from exc

    warnings: list[str] = []
    with package:
        names = set(package.namelist())
        if WORKBOOK_PART not in names:
            raise XlsxSemanticError(
                "The Excel package is missing xl/workbook.xml."
            )
        workbook_root = _read_xml(package, WORKBOOK_PART)
        relationships = _read_relationships(package, WORKBOOK_PART)
        shared_strings = _read_shared_strings(package, names)
        number_formats = _read_number_formats(package, names)
        date_system_1904 = _read_date_system(workbook_root)

        print_areas, global_names = _read_defined_names(workbook_root)

        sheets: list[SheetExtraction] = []
        entries = workbook_root.findall("s:sheets/s:sheet", NS)
        if len(entries) > MAX_SHEETS:
            warnings.append(
                f"시트가 {len(entries)}개여서 앞의 {MAX_SHEETS}개만 처리했습니다."
            )
            entries = entries[:MAX_SHEETS]

        for index, entry in enumerate(entries):
            name = entry.attrib.get("name") or f"시트 {index + 1}"
            relationship_id = entry.attrib.get(f"{{{NS['r']}}}id", "")
            relationship = relationships.get(relationship_id)
            if relationship is None or relationship["target_mode"] != "Internal":
                warnings.append(f"'{name}' 시트의 파트를 찾지 못해 건너뛰었습니다.")
                continue
            part = _resolve_part(WORKBOOK_PART, relationship["target"])
            if part not in names:
                warnings.append(f"'{name}' 시트의 파트를 찾지 못해 건너뛰었습니다.")
                continue
            sheets.append(
                _read_sheet(
                    package,
                    names=names,
                    part=part,
                    name=name,
                    index=index,
                    state=entry.attrib.get("state", "visible"),
                    shared_strings=shared_strings,
                    number_formats=number_formats,
                    date_system_1904=date_system_1904,
                    has_print_area=index in print_areas,
                    defined_names=tuple(
                        label
                        for label, owner in global_names
                        if owner is None or owner == index
                    ),
                )
            )

    if not sheets:
        raise XlsxSemanticError("The Excel workbook has no readable sheets.")

    return WorkbookExtraction(
        sheets=tuple(sheets),
        defined_names=tuple(label for label, _ in global_names),
        date_system_1904=date_system_1904,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _read_sheet(
    package: zipfile.ZipFile,
    *,
    names: set[str],
    part: str,
    name: str,
    index: int,
    state: str,
    shared_strings: Sequence[str],
    number_formats: Mapping[int, str],
    date_system_1904: bool,
    has_print_area: bool,
    defined_names: tuple[str, ...],
) -> SheetExtraction:
    root = _read_xml(package, part)
    warnings: list[str] = []

    cells, row_count, column_count, truncated, missing_cache = _read_cells(
        root,
        shared_strings=shared_strings,
        number_formats=number_formats,
        date_system_1904=date_system_1904,
    )
    if truncated:
        warnings.append(
            f"'{name}' 시트가 {row_count}행이어서 앞의 {MAX_GRID_ROWS}행만 읽었습니다."
        )
    if missing_cache:
        warnings.append(
            f"'{name}' 시트의 수식 {missing_cache}개에 계산된 값이 저장되어 있지 "
            "않아 해당 셀은 비어 있습니다."
        )

    merges = _read_merges(root)
    raw_grid = _dense_grid(cells, min(row_count, MAX_GRID_ROWS), column_count)
    grid = _propagate_merges(raw_grid, merges)

    sheet_relationships = _read_relationships(package, part)
    list_object_names, pivot_count = _read_sheet_parts(
        package, names=names, part=part, relationships=sheet_relationships
    )
    drawing_count, image_count, alt_texts, charts = _read_drawing(
        package, names=names, part=part, relationships=sheet_relationships
    )

    return SheetExtraction(
        name=name,
        index=index,
        part=part,
        state=state,
        grid=grid,
        raw_grid=raw_grid,
        merges=merges,
        row_count=row_count,
        column_count=column_count,
        has_print_area=has_print_area,
        has_autofilter=root.find("s:autoFilter", NS) is not None,
        list_object_names=list_object_names,
        pivot_table_count=pivot_count,
        drawing_count=drawing_count,
        image_count=image_count,
        alt_texts=alt_texts,
        charts=charts,
        defined_names=defined_names,
        truncated=truncated,
        warnings=tuple(warnings),
    )


def _read_cells(
    root: ElementTree.Element,
    *,
    shared_strings: Sequence[str],
    number_formats: Mapping[int, str],
    date_system_1904: bool,
) -> tuple[dict[tuple[int, int], str], int, int, bool, int]:
    cells: dict[tuple[int, int], str] = {}
    max_row = 0
    max_column = 0
    truncated = False
    missing_cache = 0

    sheet_data = root.find("s:sheetData", NS)
    if sheet_data is None:
        return cells, 0, 0, False, 0

    fallback_row = 0
    for row_element in sheet_data.findall("s:row", NS):
        fallback_row += 1
        row = _to_int(row_element.attrib.get("r"), fallback_row)
        fallback_row = row
        fallback_column = 0
        row_has_value = False
        for cell in row_element.findall("s:c", NS):
            fallback_column += 1
            reference = cell.attrib.get("r")
            if reference:
                parsed = _parse_reference(reference)
                if parsed is None:
                    continue
                _, column = parsed
            else:
                column = fallback_column
            fallback_column = column
            if column > MAX_GRID_COLUMNS:
                continue
            text, cached = _cell_text(
                cell,
                shared_strings=shared_strings,
                number_formats=number_formats,
                date_system_1904=date_system_1904,
            )
            if not cached:
                missing_cache += 1
            if not text:
                continue
            row_has_value = True
            max_column = max(max_column, column)
            if row <= MAX_GRID_ROWS:
                cells[(row, column)] = text
            else:
                truncated = True
        if row_has_value:
            max_row = max(max_row, row)

    return cells, max_row, max_column, truncated, missing_cache


def _cell_text(
    cell: ElementTree.Element,
    *,
    shared_strings: Sequence[str],
    number_formats: Mapping[int, str],
    date_system_1904: bool,
) -> tuple[str, bool]:
    """Return the cell's display text and whether a cached value existed."""

    cell_type = cell.attrib.get("t", "n")
    if cell_type == "inlineStr":
        inline = cell.find("s:is", NS)
        return (_rich_text(inline) if inline is not None else ""), True

    value_element = cell.find("s:v", NS)
    if value_element is None:
        # A formula with no cached <v> means the producer never calculated the
        # workbook. There is nothing to show and nothing to index.
        return "", cell.find("s:f", NS) is None
    value = (value_element.text or "").strip()
    if not value:
        return "", True

    if cell_type == "s":
        try:
            return shared_strings[int(value)], True
        except (ValueError, IndexError):
            return "", True
    if cell_type == "b":
        return ("TRUE" if value not in {"0", ""} else "FALSE"), True
    if cell_type in {"str", "e", "d"}:
        return _normalize_space(value), True

    return _format_number(
        value,
        format_code=number_formats.get(_to_int(cell.attrib.get("s"), 0), ""),
        date_system_1904=date_system_1904,
    ), True


def _rich_text(element: ElementTree.Element) -> str:
    parts = [
        node.text or ""
        for node in element.iter()
        if _local_name(node.tag) == "t"
    ]
    return _normalize_space("".join(parts))


# ---------------------------------------------------------------------------
# Number and date formatting
# ---------------------------------------------------------------------------


def _format_number(
    value: str, *, format_code: str, date_system_1904: bool
) -> str:
    """Render a numeric cell the way its number format makes it readable.

    Excel stores dates as serial numbers, so without this a 계약일 column
    indexes as ``45678`` and no one will ever find it by date.
    """

    try:
        number = float(value)
    except ValueError:
        return _normalize_space(value)

    kind = _format_kind(format_code)
    if kind == "elapsed":
        return _elapsed_text(number)
    if kind in {"date", "datetime", "time"}:
        rendered = _serial_to_text(
            number, kind=kind, date_system_1904=date_system_1904
        )
        if rendered:
            return rendered
    if kind == "percent":
        return f"{_plain_number(number * 100)}%"
    return _plain_number(number)


def _format_kind(format_code: str) -> str:
    if format_code.startswith("builtin:"):
        identifier = _to_int(format_code.split(":", 1)[1], 0)
        if identifier in _BUILTIN_ELAPSED_FORMATS:
            return "elapsed"
        if identifier in _BUILTIN_TIME_ONLY_FORMATS:
            return "time"
        if identifier == 22:
            return "datetime"
        if identifier in _BUILTIN_DATE_FORMATS:
            return "date"
        return "number"

    section = _FORMAT_LITERALS.sub("", format_code.split(";", 1)[0])
    if "[h" in format_code.casefold() or "[m" in format_code.casefold():
        return "elapsed"
    lowered = section.casefold()
    if "%" in lowered:
        return "percent"
    has_date = "y" in lowered or "d" in lowered
    has_time = "h" in lowered or "s" in lowered
    if has_date and has_time:
        return "datetime"
    if has_date:
        return "date"
    if has_time:
        return "time"
    return "number"


def _serial_to_text(
    serial: float, *, kind: str, date_system_1904: bool
) -> str:
    if serial < 0:
        return ""
    if date_system_1904:
        epoch = datetime(1904, 1, 1)
        offset = serial
    else:
        if serial == 60:
            # Excel keeps Lotus 1-2-3's phantom 1900-02-29. It is not a real
            # date, so it is reported literally rather than silently shifted.
            return "1900-02-29"
        # Serials at or above 61 are one day ahead of the true calendar because
        # of that phantom day; earlier serials are not.
        epoch = datetime(1899, 12, 30) if serial >= 61 else datetime(1899, 12, 31)
        offset = serial
    try:
        moment = epoch + timedelta(days=offset)
    except (OverflowError, ValueError):
        return ""
    if kind == "time":
        return moment.strftime("%H:%M:%S")
    if kind == "datetime":
        return moment.strftime("%Y-%m-%d %H:%M:%S")
    return moment.strftime("%Y-%m-%d")


def _elapsed_text(serial: float) -> str:
    total_seconds = round(serial * 86_400)
    hours, remainder = divmod(abs(total_seconds), 3_600)
    minutes, seconds = divmod(remainder, 60)
    sign = "-" if total_seconds < 0 else ""
    return f"{sign}{hours}:{minutes:02d}:{seconds:02d}"


def _plain_number(number: float) -> str:
    if number == int(number) and abs(number) < 1e15:
        return str(int(number))
    return f"{number:.10g}"


def _read_number_formats(
    package: zipfile.ZipFile, names: set[str]
) -> dict[int, str]:
    """Map each ``cellXfs`` style index to its number format code.

    Built-ins have no code in the file, so they are returned as
    ``"builtin:<id>"`` and resolved by id instead.
    """

    if STYLES_PART not in names:
        return {}
    try:
        root = _read_xml(package, STYLES_PART)
    except XlsxSemanticError:
        return {}

    custom = {
        _to_int(element.attrib.get("numFmtId"), -1): element.attrib.get(
            "formatCode", ""
        )
        for element in root.findall("s:numFmts/s:numFmt", NS)
    }
    formats: dict[int, str] = {}
    for style_index, element in enumerate(root.findall("s:cellXfs/s:xf", NS)):
        format_id = _to_int(element.attrib.get("numFmtId"), 0)
        formats[style_index] = custom.get(format_id) or f"builtin:{format_id}"
    return formats


def _read_date_system(workbook_root: ElementTree.Element) -> bool:
    properties = workbook_root.find("s:workbookPr", NS)
    if properties is None:
        return False
    value = properties.attrib.get("date1904") or properties.attrib.get(
        "dateCompatibility", ""
    )
    return value.casefold() in {"1", "true"}


# ---------------------------------------------------------------------------
# Grid assembly
# ---------------------------------------------------------------------------


def _dense_grid(
    cells: Mapping[tuple[int, int], str], row_count: int, column_count: int
) -> tuple[tuple[str, ...], ...]:
    rows = min(max(row_count, 0), MAX_GRID_ROWS)
    columns = min(max(column_count, 0), MAX_GRID_COLUMNS)
    if not rows or not columns:
        return ()
    return tuple(
        tuple(cells.get((row, column), "") for column in range(1, columns + 1))
        for row in range(1, rows + 1)
    )


def _propagate_merges(
    grid: tuple[tuple[str, ...], ...], merges: Sequence[MergeRange]
) -> tuple[tuple[str, ...], ...]:
    """Copy each merge anchor's value across the cells it visually covers.

    Excel stores a merged region's value only in its top-left cell. Leaving the
    rest blank would publish a table whose header row is mostly empty, so the
    value is duplicated -- the same flattening Word's ``gridSpan``/``vMerge``
    handling performs.
    """

    if not grid or not merges:
        return grid

    mutable = [list(row) for row in grid]
    rows = len(mutable)
    columns = len(mutable[0])
    for merge in merges:
        if merge.first_row > rows or merge.first_column > columns:
            continue
        anchor = mutable[merge.first_row - 1][merge.first_column - 1]
        if not anchor:
            continue
        for row in range(merge.first_row, min(merge.last_row, rows) + 1):
            for column in range(
                merge.first_column, min(merge.last_column, columns) + 1
            ):
                if not mutable[row - 1][column - 1]:
                    mutable[row - 1][column - 1] = anchor
    return tuple(tuple(row) for row in mutable)


def _read_merges(root: ElementTree.Element) -> tuple[MergeRange, ...]:
    merges: list[MergeRange] = []
    for element in root.findall("s:mergeCells/s:mergeCell", NS):
        parsed = _parse_range(element.attrib.get("ref", ""))
        if parsed is not None:
            merges.append(parsed)
    return tuple(merges)


def _parse_reference(reference: str) -> tuple[int, int] | None:
    match = _CELL_REFERENCE.match(reference.strip().upper())
    if match is None:
        return None
    letters, digits = match.groups()
    column = 0
    for character in letters:
        column = column * 26 + (ord(character) - ord("A") + 1)
    return int(digits), column


def _parse_range(reference: str) -> MergeRange | None:
    start, _, end = reference.partition(":")
    first = _parse_reference(start)
    if first is None:
        return None
    last = _parse_reference(end) if end else first
    if last is None:
        return None
    return MergeRange(
        first_row=min(first[0], last[0]),
        first_column=min(first[1], last[1]),
        last_row=max(first[0], last[0]),
        last_column=max(first[1], last[1]),
    )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

#: Classification weights. They are deliberately coarse: the goal is to be
#: obviously right on the two archetypes (a printed form, a ledger) and to fail
#: toward "data" in the middle, because misreading a ledger as a form is the
#: expensive mistake -- it renders hundreds of pages and calls Vision on them.
_W_PRINT_AREA = 3
_W_MERGE_DENSITY = 3
_W_VERTICAL_MERGE = 2
_W_SMALL_RANGE = 3
_W_TINY_RANGE = 2
_W_NO_LIST_STRUCTURE = 1

_W_LIST_STRUCTURE = 4
_W_LARGE_RANGE = 4
_W_MEDIUM_RANGE = 2
_W_UNIFORM_ROWS = 3
_W_HOMOGENEOUS_COLUMNS = 2

#: Merged regions per non-empty row above which the sheet is laid out, not
#: listed. Ledgers merge almost nothing; forms merge title bars and label cells.
_MERGE_DENSITY_THRESHOLD = 0.15

#: Rows below which a sheet is small enough to be a hand-built page.
_SMALL_RANGE_ROWS = 40
_TINY_RANGE_ROWS = 15

#: A run of rows this long is needed before "these rows all look alike" is
#: evidence of anything.
_MIN_UNIFORMITY_ROWS = 5

#: Share of body rows that must match the header's filled-column footprint.
_UNIFORM_ROW_SHARE = 0.85

#: Share of columns that must be type-homogeneous, and the share of a column's
#: values that must agree for the column itself to count as homogeneous.
_HOMOGENEOUS_COLUMN_SHARE = 0.6
_COLUMN_TYPE_AGREEMENT = 0.9

_NUMERIC_VALUE = re.compile(r"^-?[\d,]+(\.\d+)?%?$")
_ISO_DATE_VALUE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def classify_sheet(sheet: SheetExtraction) -> SheetClassification:
    """Decide whether a sheet is a laid-out document or a data table.

    Weighted signals rather than a single rule: real workbooks routinely
    contradict any one of them (a form with no print area, a ledger with a
    merged title). The verdict carries its reasons so the warning shown to the
    user explains itself.
    """

    if sheet.is_empty:
        return SheetClassification(
            kind="data", document_score=0, data_score=0, reasons=("빈 시트",)
        )

    document_score = 0
    data_score = 0
    reasons: list[str] = []

    if sheet.has_print_area:
        document_score += _W_PRINT_AREA
        reasons.append("인쇄 영역 지정됨")

    filled_rows = sum(1 for row in sheet.grid if any(row)) or 1
    if len(sheet.merges) / filled_rows >= _MERGE_DENSITY_THRESHOLD:
        document_score += _W_MERGE_DENSITY
        reasons.append("병합 셀 비율 높음")
    if any(merge.spans_rows for merge in sheet.merges):
        document_score += _W_VERTICAL_MERGE
        reasons.append("세로 병합 존재")

    if sheet.row_count <= _TINY_RANGE_ROWS:
        document_score += _W_SMALL_RANGE + _W_TINY_RANGE
        reasons.append(f"사용 범위 작음({sheet.row_count}행)")
    elif sheet.row_count <= _SMALL_RANGE_ROWS:
        document_score += _W_SMALL_RANGE
        reasons.append(f"사용 범위 작음({sheet.row_count}행)")
    elif sheet.row_count > _DOCUMENT_ROW_CEILING:
        data_score += _W_LARGE_RANGE
        reasons.append(f"행 수 많음({sheet.row_count}행)")
    else:
        data_score += _W_MEDIUM_RANGE
        reasons.append(f"행 수 중간({sheet.row_count}행)")

    has_structure = bool(
        sheet.list_object_names or sheet.has_autofilter or sheet.pivot_table_count
    )
    if has_structure:
        data_score += _W_LIST_STRUCTURE
        reasons.append("구조적 표/필터/피벗 존재")
    else:
        document_score += _W_NO_LIST_STRUCTURE

    if _has_uniform_body(sheet.grid):
        data_score += _W_UNIFORM_ROWS
        reasons.append("머리글 아래 행 채움이 균일함")
    if _has_homogeneous_columns(sheet.grid):
        data_score += _W_HOMOGENEOUS_COLUMNS
        reasons.append("컬럼별 타입이 동질적임")

    kind: SheetKind = "document" if document_score > data_score else "data"
    return SheetClassification(
        kind=kind,
        document_score=document_score,
        data_score=data_score,
        reasons=tuple(reasons),
    )


def _has_uniform_body(grid: Sequence[Sequence[str]]) -> bool:
    body = [row for row in grid[1:] if any(row)]
    if len(body) < _MIN_UNIFORMITY_ROWS or not grid:
        return False
    header_width = sum(1 for cell in grid[0] if cell)
    if header_width < 2:
        return False
    matching = sum(
        1 for row in body if abs(sum(1 for cell in row if cell) - header_width) <= 1
    )
    return matching / len(body) >= _UNIFORM_ROW_SHARE


def _has_homogeneous_columns(grid: Sequence[Sequence[str]]) -> bool:
    body = [row for row in grid[1:] if any(row)]
    if len(body) < _MIN_UNIFORMITY_ROWS:
        return False
    columns = len(grid[0])
    considered = 0
    homogeneous = 0
    for column in range(columns):
        values = [
            row[column]
            for row in body[:_TYPE_SAMPLE_LIMIT]
            if column < len(row) and row[column]
        ]
        if len(values) < _MIN_UNIFORMITY_ROWS:
            continue
        considered += 1
        kinds = [_value_kind(value) for value in values]
        dominant = max(set(kinds), key=kinds.count)
        if kinds.count(dominant) / len(kinds) >= _COLUMN_TYPE_AGREEMENT:
            homogeneous += 1
    if not considered:
        return False
    return homogeneous / considered >= _HOMOGENEOUS_COLUMN_SHARE


def _value_kind(value: str) -> str:
    if _ISO_DATE_VALUE.match(value):
        return "date"
    if _NUMERIC_VALUE.match(value):
        return "number"
    return "text"


_COLUMN_TYPE_LABELS = {"date": "날짜", "number": "숫자", "text": "텍스트"}


# ---------------------------------------------------------------------------
# Document-sheet segmentation
# ---------------------------------------------------------------------------

#: Shorter keys are not distinctive enough to locate a page: a lone number or a
#: two-character label matches almost anywhere in a rendered spreadsheet.
_MIN_ALIGNMENT_KEY = 4


def segment_sheet(sheet: SheetExtraction) -> tuple[SheetBlock, ...]:
    """Split a document sheet into text runs and tables.

    Excel records no such structure, so the split uses the only signal a laid
    out form actually has: rows that fill one cell are titles, notes and
    signatures; runs of rows that fill several are a table.

    The decision reads the *pre-merge* grid, because that is what a person
    sees. A title bar merged across A1:C1 is one cell on the page even though
    the flattened grid holds three copies of it -- counting the copies would
    turn every Korean form's title into a table header.
    """

    blocks: list[SheetBlock] = []
    pending_text: list[tuple[int, str, tuple[str, ...]]] = []
    pending_rows: list[tuple[int, Sequence[str]]] = []

    def flush_text() -> None:
        if not pending_text:
            return
        blocks.append(
            SheetBlock(
                kind="text",
                first_row=pending_text[0][0],
                last_row=pending_text[-1][0],
                lines=tuple(line for _, line, _ in pending_text),
                # Keys are per cell, never the joined line: Calc clips a cell
                # that overflows into a filled neighbour, so a rendering of
                # "고객사 | 한국전력공사 | 견적일" reads "고객사한국전력공견적일".
                # A key spanning the join can then never be found.
                alignment_keys=_alignment_keys(
                    cell for _, _, cells in pending_text for cell in cells
                ),
            )
        )
        pending_text.clear()

    def flush_table() -> None:
        if not pending_rows:
            return
        preamble, table_rows = _split_preamble(sheet, pending_rows)
        for offset, _ in preamble:
            # Raw, not propagated: a merged label is drawn once, so repeating
            # it per spanned column would both read wrong and break alignment.
            cells = tuple(cell for cell in sheet.raw_grid[offset - 1] if cell)
            pending_text.append((offset, " ".join(cells), cells))
        flush_text()
        if table_rows:
            block = _table_block(sheet, table_rows)
            if block is not None:
                blocks.append(block)
        pending_rows.clear()

    for offset, row in enumerate(sheet.grid, start=1):
        drawn = [cell for cell in sheet.raw_grid[offset - 1] if cell]
        if not drawn:
            flush_text()
            flush_table()
            continue
        if len(drawn) == 1:
            flush_table()
            pending_text.append((offset, drawn[0], (drawn[0],)))
            continue
        flush_text()
        pending_rows.append((offset, row))

    flush_text()
    flush_table()
    return tuple(blocks)


#: A form's metadata band ("고객사 | ... | 견적일 | ...") sits directly above the
#: line-item table with no blank row between them, so it lands in the same run.
#: Only a few such rows are plausible; more than this and the split is more
#: likely to be eating the table itself.
MAX_PREAMBLE_ROWS = 3


def _split_preamble(
    sheet: SheetExtraction, rows: Sequence[tuple[int, Sequence[str]]]
) -> tuple[
    tuple[tuple[int, Sequence[str]], ...], tuple[tuple[int, Sequence[str]], ...]
]:
    """Peel a form's metadata band off the top of a table run.

    Korean forms stack a label/value band ("고객사 | ... | 견적일 | ...")
    straight on top of the line-item table, and Excel records no boundary
    between them. Taking the first row as the header would publish the
    customer's name as a column heading and lose the real one.

    The header is the all-text row immediately above the first row that
    carries a number, which is where a line-item body starts. Anchoring on
    the data rather than searching for a "best" split matters: any score that
    rates the rows below a candidate gets better as the body shrinks, so it
    walks the split to the bottom of the table.
    """

    if len(rows) < 3:
        return (), tuple(rows)

    # A horizontal merge opening the run is the banded-header idiom, where the
    # first row genuinely is part of the header. Leave it to _table_block.
    first_row = rows[0][0]
    if any(
        merge.spans_columns and merge.first_row == first_row
        for merge in sheet.merges
    ):
        return (), tuple(rows)

    body_start = next(
        (
            index
            for index, (_, row) in enumerate(rows)
            if any(cell and _value_kind(cell) == "number" for cell in row)
        ),
        None,
    )
    if body_start is None:
        return (), tuple(rows)

    header = body_start - 1
    if not 1 <= header <= MAX_PREAMBLE_ROWS:
        return (), tuple(rows)
    if not _looks_like_header(rows[header][1]):
        return (), tuple(rows)
    return tuple(rows[:header]), tuple(rows[header:])


def _table_block(
    sheet: SheetExtraction, rows: Sequence[tuple[int, Sequence[str]]]
) -> SheetBlock | None:
    first_row = rows[0][0]
    last_row = rows[-1][0]
    width = max(
        (
            index + 1
            for _, row in rows
            for index, cell in enumerate(row)
            if cell
        ),
        default=0,
    )
    if width < 2:
        return None

    covered = [
        merge
        for merge in sheet.merges
        if merge.first_row <= last_row and merge.last_row >= first_row
    ]
    horizontal_merge = any(merge.spans_columns for merge in covered)
    vertical_merge = any(merge.spans_rows for merge in covered)

    # A banded header (two stacked header rows, the upper one merged across the
    # lower) is the Korean form idiom for grouped columns. ContentTable takes a
    # single header row, so the two are joined the same way Word's banded
    # headers are.
    header_rows = 1
    if (
        len(rows) >= 3
        and horizontal_merge
        and any(
            merge.spans_columns and merge.first_row == first_row
            for merge in covered
        )
        and _looks_like_header(rows[1][1][:width])
    ):
        header_rows = 2

    if header_rows == 2:
        headers = tuple(
            _join_header(rows[0][1][index], rows[1][1][index])
            for index in range(width)
        )
    else:
        headers = tuple(
            cell or f"열 {index + 1}"
            for index, cell in enumerate(rows[0][1][:width])
        )
    headers = _unique_headers(headers)

    body = tuple(
        tuple(row[index] if index < len(row) else "" for index in range(width))
        for _, row in rows[header_rows:]
    )

    title = f"{sheet.name} 표"
    table = ContentTable(
        title=title, headers=headers, rows=body, key_facts=()
    )
    complexity = TableComplexity(
        horizontal_merge=horizontal_merge,
        vertical_merge=vertical_merge,
        multi_row_header=header_rows > 1,
    )
    # Alignment must search for text the renderer actually drew, so it uses the
    # pre-merge grid: a value duplicated across a merged span appears once on
    # the page, not once per column.
    raw_rows = sheet.raw_grid[first_row - 1 : last_row]
    return SheetBlock(
        kind="table",
        first_row=first_row,
        last_row=last_row,
        table=table,
        complexity=complexity,
        alignment_keys=_alignment_keys(
            cell for row in raw_rows for cell in row if cell
        ),
    )


def _looks_like_header(cells: Sequence[str]) -> bool:
    filled = [cell for cell in cells if cell]
    if len(filled) < 2:
        return False
    textual = sum(1 for cell in filled if _value_kind(cell) == "text")
    return textual >= len(filled) / 2


def _join_header(upper: str, lower: str) -> str:
    if upper and lower and upper != lower:
        return f"{upper} / {lower}"
    return upper or lower


def _unique_headers(headers: Sequence[str]) -> tuple[str, ...]:
    seen: dict[str, int] = {}
    values: list[str] = []
    for index, header in enumerate(headers):
        label = header or f"열 {index + 1}"
        count = seen.get(label, 0) + 1
        seen[label] = count
        values.append(label if count == 1 else f"{label} ({count})")
    return tuple(values)


def _alignment_keys(values: Iterable[str]) -> tuple[str, ...]:
    keys: list[str] = []
    for value in values:
        key = _search_key(value)
        if len(key) >= _MIN_ALIGNMENT_KEY and key not in keys:
            keys.append(key)
        if len(keys) >= 40:
            break
    return tuple(keys)


# ---------------------------------------------------------------------------
# Data-sheet summarization
# ---------------------------------------------------------------------------


def data_sheet_summary(
    sheet: SheetExtraction, classification: SheetClassification
) -> tuple[tuple[ContentSection, ...], tuple[ContentTable, ...]]:
    """Describe a data sheet instead of transcribing it.

    SharePoint already indexes the cell text, and the 63,999-character column
    ceiling means a transcript would be truncated anyway. What is missing from
    native indexing is *what the sheet is*: its schema, its scale, and enough
    rows to recognise the shape of the data.
    """

    headers, body = _header_and_body(sheet)
    facts = [
        f"사용 범위: {sheet.row_count:,}행 × {sheet.column_count:,}열",
        f"시트 유형: 데이터형 ({', '.join(classification.reasons) or '신호 없음'})",
    ]
    if sheet.is_hidden:
        facts.append("원본에서 숨겨진 시트입니다.")
    if sheet.list_object_names:
        facts.append(f"구조적 표: {', '.join(sheet.list_object_names)}")
    if sheet.has_autofilter:
        facts.append("자동 필터가 설정되어 있습니다.")
    if sheet.pivot_table_count:
        facts.append(f"피벗 테이블 {sheet.pivot_table_count}개")
    if sheet.defined_names:
        facts.append(f"명명 범위: {', '.join(sheet.defined_names)}")
    if sheet.charts:
        facts.append(
            f"차트: {', '.join(chart.title for chart in sheet.charts)}"
        )
    if sheet.truncated:
        facts.append(
            f"미리보기는 앞의 {MAX_GRID_ROWS:,}행만 사용했습니다."
        )

    sections = [
        ContentSection(heading="시트 개요", paragraphs=tuple(facts), bullets=())
    ]
    if headers:
        sections.append(
            ContentSection(
                heading="컬럼",
                paragraphs=(),
                bullets=tuple(
                    f"{header} ({_COLUMN_TYPE_LABELS[kind]})"
                    for header, kind in zip(
                        headers, _column_kinds(headers, body), strict=True
                    )
                ),
            )
        )
    for alt_text in sheet.alt_texts:
        sections.append(
            ContentSection(
                heading="이미지 설명", paragraphs=(alt_text,), bullets=()
            )
        )

    tables: list[ContentTable] = []
    if headers and body:
        shown_columns = min(len(headers), SAMPLE_COLUMNS)
        suffix = (
            f" (앞 {shown_columns}열)" if shown_columns < len(headers) else ""
        )
        tables.append(
            ContentTable(
                title=f"{sheet.name} 상위 {min(len(body), SAMPLE_ROWS)}행 샘플{suffix}",
                headers=tuple(headers[:shown_columns]),
                rows=tuple(
                    tuple(row[:shown_columns]) for row in body[:SAMPLE_ROWS]
                ),
                key_facts=(),
            )
        )
    return tuple(sections), tuple(tables)


def _header_and_body(
    sheet: SheetExtraction,
) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """Find the header row and the rows below it.

    Ledgers exported from ERP systems often carry a title line or two above the
    real header, so the header is the first row that looks like one rather than
    row 1 unconditionally.
    """

    for offset, row in enumerate(sheet.grid):
        if _looks_like_header(row):
            width = max(
                (index + 1 for index, cell in enumerate(row) if cell), default=0
            )
            headers = _unique_headers(
                tuple(
                    cell or f"열 {index + 1}"
                    for index, cell in enumerate(row[:width])
                )
            )
            body = tuple(
                tuple(
                    later[index] if index < len(later) else ""
                    for index in range(width)
                )
                for later in sheet.grid[offset + 1 :]
                if any(later)
            )
            return headers, body
    return (), ()


def _column_kinds(
    headers: Sequence[str], body: Sequence[Sequence[str]]
) -> tuple[str, ...]:
    kinds: list[str] = []
    for column in range(len(headers)):
        values = [
            row[column]
            for row in body[:_TYPE_SAMPLE_LIMIT]
            if column < len(row) and row[column]
        ]
        if not values:
            kinds.append("text")
            continue
        observed = [_value_kind(value) for value in values]
        kinds.append(max(set(observed), key=observed.count))
    return tuple(kinds)


# ---------------------------------------------------------------------------
# Render support
# ---------------------------------------------------------------------------


def hide_sheets(data: bytes, names: Iterable[str]) -> bytes:
    """Return the package with the named sheets marked hidden.

    LibreOffice Calc omits hidden sheets from PDF export, so this is what keeps
    a 40,000-row ledger from becoming 400 rendered pages. Only
    ``xl/workbook.xml`` is rewritten; every other part is copied through
    untouched, and the last visible sheet is never hidden because a workbook
    with no visible sheet is invalid.
    """

    targets = {name for name in names}
    if not targets:
        return data

    try:
        source = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise XlsxSemanticError("The file is not a valid Excel package.") from exc

    with source:
        if WORKBOOK_PART not in set(source.namelist()):
            raise XlsxSemanticError(
                "The Excel package is missing xl/workbook.xml."
            )
        workbook = source.read(WORKBOOK_PART).decode("utf-8", errors="replace")
        rewritten, hidden = _mark_sheets_hidden(workbook, targets)
        if not hidden:
            return data

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as destination:
            for item in source.infolist():
                if item.filename == WORKBOOK_PART:
                    destination.writestr(item, rewritten.encode("utf-8"))
                else:
                    destination.writestr(item, source.read(item.filename))
    return buffer.getvalue()


_SHEET_ELEMENT = re.compile(r"<sheet\b[^>]*/?>", re.IGNORECASE)
_SHEET_NAME_ATTRIBUTE = re.compile(r'\bname\s*=\s*"([^"]*)"', re.IGNORECASE)
_SHEET_STATE_ATTRIBUTE = re.compile(r'\s+state\s*=\s*"[^"]*"', re.IGNORECASE)


def _mark_sheets_hidden(
    workbook_xml: str, targets: set[str]
) -> tuple[str, int]:
    hidden = 0
    visible_remaining = 0

    for element in _SHEET_ELEMENT.findall(workbook_xml):
        name_match = _SHEET_NAME_ATTRIBUTE.search(element)
        if name_match is None:
            continue
        if _unescape(name_match.group(1)) not in targets:
            visible_remaining += 1
    if not visible_remaining:
        # Hiding every sheet would produce an invalid workbook, so nothing is
        # hidden and the caller renders the whole thing.
        return workbook_xml, 0

    def replace(match: re.Match[str]) -> str:
        nonlocal hidden
        element = match.group(0)
        name_match = _SHEET_NAME_ATTRIBUTE.search(element)
        if name_match is None:
            return element
        if _unescape(name_match.group(1)) not in targets:
            return element
        hidden += 1
        stripped = _SHEET_STATE_ATTRIBUTE.sub("", element)
        closing = "/>" if stripped.endswith("/>") else ">"
        return f'{stripped[: -len(closing)]} state="hidden"{closing}'

    return _SHEET_ELEMENT.sub(replace, workbook_xml), hidden


def _unescape(value: str) -> str:
    return (
        value.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
    )


def attribute_pages_to_sheets(
    sheets: Sequence[SheetExtraction], page_texts: Mapping[int, Sequence[str]]
) -> dict[int, int]:
    """Map each rendered page to the sheet that produced it.

    Calc emits sheets in workbook order, so the assignment is monotonic; that
    constraint carries most of the weight and the cell-text overlap only has to
    find the boundaries. Matching is by value *presence*, never by order: Calc's
    PDF text stream does not follow cell reading order.
    """

    pages = sorted(page_texts)
    if not sheets or not pages:
        return {}

    keys = _distinctive_keys(sheets)
    page_keys = {
        page: _search_key(" ".join(page_texts[page])) for page in pages
    }

    assignment: dict[int, int] = {}
    current = 0
    last = len(sheets) - 1
    for position, page in enumerate(pages):
        remaining_pages = len(pages) - position - 1
        # Every later sheet still needs a page of its own.
        floor = max(current, last - remaining_pages)
        best = floor
        best_score = -1
        for index in range(floor, last + 1):
            score = sum(1 for key in keys[index] if key in page_keys[page])
            if score > best_score:
                best_score = score
                best = index
        current = best if best_score > 0 else floor
        assignment[page] = current
    return assignment


def _distinctive_keys(
    sheets: Sequence[SheetExtraction],
) -> tuple[frozenset[str], ...]:
    """Per-sheet cell keys that appear in exactly one sheet.

    Shared vocabulary ("합계", "비고") says nothing about which sheet a page
    came from, so it is dropped before scoring.
    """

    per_sheet = [
        {
            key
            for row in sheet.raw_grid
            for cell in row
            if cell and len(key := _search_key(cell)) >= 2
        }
        for sheet in sheets
    ]
    counts: dict[str, int] = {}
    for keys in per_sheet:
        for key in keys:
            counts[key] = counts.get(key, 0) + 1
    return tuple(
        frozenset(key for key in keys if counts[key] == 1) for keys in per_sheet
    )


def align_blocks_to_pages(
    blocks: Sequence[SheetBlock],
    page_texts: Mapping[int, Sequence[str]],
    pages: Sequence[int],
) -> tuple[dict[int, int], tuple[str, ...]]:
    """Assign each block of one sheet to one of that sheet's rendered pages.

    Same forward-only walk as the Word aligner, but matching is containment
    within a page rather than position in a concatenated stream, because Calc
    reorders text within a page.
    """

    if not pages:
        return {}, ()
    keys_by_page = {
        page: _search_key(" ".join(page_texts.get(page, ()))) for page in pages
    }

    assignment: dict[int, int] = {}
    unmatched = 0
    cursor = 0
    for order, block in enumerate(blocks):
        located = None
        for key in block.alignment_keys:
            for position in range(cursor, len(pages)):
                if key in keys_by_page[pages[position]]:
                    located = position
                    break
            if located is not None:
                break
        if located is None:
            if block.alignment_keys:
                unmatched += 1
        else:
            cursor = located
        assignment[order] = pages[cursor]

    warnings: list[str] = []
    if unmatched:
        warnings.append(
            f"{unmatched}개 블록을 렌더링된 페이지와 매칭하지 못해 직전 페이지에 "
            "배치했습니다."
        )
    return assignment, tuple(warnings)


# ---------------------------------------------------------------------------
# Package side parts
# ---------------------------------------------------------------------------


def _read_shared_strings(
    package: zipfile.ZipFile, names: set[str]
) -> tuple[str, ...]:
    if SHARED_STRINGS_PART not in names:
        return ()
    try:
        root = _read_xml(package, SHARED_STRINGS_PART)
    except XlsxSemanticError:
        return ()
    return tuple(_rich_text(item) for item in root.findall("s:si", NS))


def _read_defined_names(
    workbook_root: ElementTree.Element,
) -> tuple[set[int], tuple[tuple[str, int | None], ...]]:
    print_area_sheets: set[int] = set()
    labels: list[tuple[str, int | None]] = []
    for element in workbook_root.findall("s:definedNames/s:definedName", NS):
        name = element.attrib.get("name", "")
        local_sheet = element.attrib.get("localSheetId")
        owner = _to_int(local_sheet, -1) if local_sheet is not None else None
        if name == "_xlnm.Print_Area":
            if owner is not None and owner >= 0:
                print_area_sheets.add(owner)
            continue
        if name.startswith("_xlnm."):
            continue
        labels.append((name, owner if owner is None or owner >= 0 else None))
    return print_area_sheets, tuple(labels)


def _read_sheet_parts(
    package: zipfile.ZipFile,
    *,
    names: set[str],
    part: str,
    relationships: Mapping[str, Mapping[str, str]],
) -> tuple[tuple[str, ...], int]:
    list_objects: list[str] = []
    pivot_count = 0
    for relationship in relationships.values():
        if relationship["target_mode"] != "Internal":
            continue
        target_type = relationship["type"]
        resolved = _resolve_part(part, relationship["target"])
        if target_type.endswith("/pivotTable"):
            pivot_count += 1
            continue
        if not target_type.endswith("/table") or resolved not in names:
            continue
        try:
            table_root = _read_xml(package, resolved)
        except XlsxSemanticError:
            continue
        label = table_root.attrib.get("displayName") or table_root.attrib.get(
            "name", ""
        )
        if label:
            list_objects.append(label)
    return tuple(list_objects), pivot_count


def _read_drawing(
    package: zipfile.ZipFile,
    *,
    names: set[str],
    part: str,
    relationships: Mapping[str, Mapping[str, str]],
) -> tuple[int, int, tuple[str, ...], tuple[ContentChart, ...]]:
    drawing_part = next(
        (
            _resolve_part(part, relationship["target"])
            for relationship in relationships.values()
            if relationship["type"].endswith("/drawing")
            and relationship["target_mode"] == "Internal"
        ),
        None,
    )
    if drawing_part is None or drawing_part not in names:
        return 0, 0, (), ()

    try:
        drawing_root = _read_xml(package, drawing_part)
    except XlsxSemanticError:
        return 0, 0, (), ()

    anchors = [
        element
        for element in drawing_root
        if _local_name(element.tag).endswith("Anchor")
    ]
    alt_texts = tuple(
        dict.fromkeys(
            text
            for element in drawing_root.iter()
            if _local_name(element.tag) == "cNvPr"
            for text in (_normalize_space(element.attrib.get("descr", "")),)
            if text
        )
    )

    drawing_relationships = _read_relationships(package, drawing_part)
    image_count = 0
    charts: list[ContentChart] = []
    for relationship in drawing_relationships.values():
        if relationship["target_mode"] != "Internal":
            continue
        if relationship["type"].endswith("/image"):
            image_count += 1
            continue
        if not relationship["type"].endswith("/chart"):
            continue
        chart_part = _resolve_part(drawing_part, relationship["target"])
        if chart_part not in names:
            continue
        try:
            chart_root = _read_xml(package, chart_part)
        except XlsxSemanticError:
            continue
        chart = chart_content(chart_root)
        if chart is not None:
            charts.append(chart)
    return len(anchors), image_count, alt_texts, tuple(charts)


def _read_relationships(
    package: zipfile.ZipFile, source_part: str
) -> dict[str, dict[str, str]]:
    directory, filename = posixpath.split(source_part)
    relationships_part = posixpath.join(directory, "_rels", f"{filename}.rels")
    if relationships_part not in set(package.namelist()):
        return {}
    try:
        root = _read_xml(package, relationships_part)
    except XlsxSemanticError:
        return {}
    return {
        element.attrib["Id"]: {
            "type": element.attrib.get("Type", ""),
            "target": element.attrib.get("Target", ""),
            "target_mode": element.attrib.get("TargetMode", "Internal"),
        }
        for element in root.findall("pr:Relationship", NS)
        if "Id" in element.attrib
    }


def _read_xml(package: zipfile.ZipFile, part_name: str) -> ElementTree.Element:
    try:
        data = package.read(part_name)
    except KeyError as exc:
        raise XlsxSemanticError(
            f"The Excel package is missing {part_name}."
        ) from exc
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise XlsxSemanticError(f"{part_name} is not valid XML: {exc}") from exc


def _resolve_part(source_part: str, target: str) -> str:
    if target.startswith("/"):
        return posixpath.normpath(target).lstrip("/")
    return posixpath.normpath(
        posixpath.join(posixpath.dirname(source_part), target)
    )


def _to_int(value: str | None, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default

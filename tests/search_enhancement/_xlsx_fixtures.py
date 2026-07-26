"""In-memory Excel package builders for the .xlsx handler tests.

Binary fixtures are not committed: an .xlsx is a zip of XML, so the tests
assemble one. The packages produced here are *valid OPC* -- ``_rels/.rels`` and
per-part content-type overrides included -- because the Word work proved that
LibreOffice refuses to load anything less, and the same builders are used for
the real end-to-end conversion check.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterable, Mapping, Sequence

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_REL_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES_NS = (
    "http://schemas.openxmlformats.org/package/2006/content-types"
)
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
CHART_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
SPREADSHEET_DRAWING_NS = (
    "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
)

WORKSHEET_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
)
WORKBOOK_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
)

_XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'


class Sheet:
    """One worksheet to place in a generated workbook.

    ``rows`` holds display values; a value may be a ``(text, style_index)``
    pair to exercise number-format resolution, or ``None`` for an empty cell.
    """

    def __init__(
        self,
        name: str,
        rows: Sequence[Sequence[object]],
        *,
        state: str | None = None,
        merges: Sequence[str] = (),
        autofilter: str | None = None,
        table_name: str | None = None,
        print_area: bool = False,
        charts: Sequence[Mapping[str, object]] = (),
        images: int = 0,
        alt_texts: Sequence[str] = (),
    ) -> None:
        self.name = name
        self.rows = rows
        self.state = state
        self.merges = tuple(merges)
        self.autofilter = autofilter
        self.table_name = table_name
        self.print_area = print_area
        self.charts = tuple(charts)
        self.images = images
        self.alt_texts = tuple(alt_texts)

    @property
    def has_drawing(self) -> bool:
        return bool(self.charts or self.images or self.alt_texts)


def column_letter(index: int) -> str:
    """1-based column index to its spreadsheet letters (1 -> A, 27 -> AA)."""

    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def build_xlsx(
    sheets: Sequence[Sheet],
    *,
    number_formats: Mapping[int, str] | None = None,
    date1904: bool = False,
    defined_names: Sequence[tuple[str, str]] = (),
    extra_parts: Mapping[str, str] | None = None,
    drop_parts: Iterable[str] = (),
) -> bytes:
    parts: dict[str, str] = {}
    overrides: list[str] = [
        f'<Override PartName="/xl/workbook.xml" '
        f'ContentType="{WORKBOOK_CONTENT_TYPE}"/>'
    ]
    workbook_relationships: list[str] = []
    print_area_names: list[str] = []
    table_index = 0

    for position, sheet in enumerate(sheets, start=1):
        sheet_part = f"xl/worksheets/sheet{position}.xml"
        parts[sheet_part] = _worksheet_xml(sheet, position=position)
        overrides.append(
            f'<Override PartName="/{sheet_part}" '
            f'ContentType="{WORKSHEET_CONTENT_TYPE}"/>'
        )
        workbook_relationships.append(
            f'<Relationship Id="rId{position}" '
            f'Type="{OFFICE_REL_NS}/worksheet" '
            f'Target="worksheets/sheet{position}.xml"/>'
        )
        if sheet.print_area:
            last = _last_reference(sheet.rows)
            print_area_names.append(
                f'<definedName name="_xlnm.Print_Area" '
                f'localSheetId="{position - 1}">'
                f"{_escape(sheet.name)}!$A$1:${last}</definedName>"
            )

        sheet_relationships: list[str] = []
        if sheet.table_name is not None:
            table_index += 1
            table_part = f"xl/tables/table{table_index}.xml"
            parts[table_part] = _table_xml(sheet, table_index)
            sheet_relationships.append(
                f'<Relationship Id="rIdT{table_index}" '
                f'Type="{OFFICE_REL_NS}/table" '
                f'Target="../tables/table{table_index}.xml"/>'
            )
        if sheet.has_drawing:
            drawing_part = f"xl/drawings/drawing{position}.xml"
            parts[drawing_part] = _drawing_xml(sheet)
            sheet_relationships.append(
                f'<Relationship Id="rIdD{position}" '
                f'Type="{OFFICE_REL_NS}/drawing" '
                f'Target="../drawings/drawing{position}.xml"/>'
            )
            drawing_relationships: list[str] = []
            for chart_position, chart in enumerate(sheet.charts, start=1):
                chart_part = f"xl/charts/chart{position}_{chart_position}.xml"
                parts[chart_part] = _chart_xml(chart)
                drawing_relationships.append(
                    f'<Relationship Id="rIdC{chart_position}" '
                    f'Type="{OFFICE_REL_NS}/chart" '
                    f'Target="../charts/chart{position}_{chart_position}.xml"/>'
                )
            for image_position in range(1, sheet.images + 1):
                drawing_relationships.append(
                    f'<Relationship Id="rIdI{image_position}" '
                    f'Type="{OFFICE_REL_NS}/image" '
                    f'Target="../media/image{image_position}.png"/>'
                )
            parts[f"xl/drawings/_rels/drawing{position}.xml.rels"] = (
                _relationships_xml(drawing_relationships)
            )
        if sheet_relationships:
            parts[f"xl/worksheets/_rels/sheet{position}.xml.rels"] = (
                _relationships_xml(sheet_relationships)
            )

    defined = "".join(print_area_names) + "".join(
        f'<definedName name="{_escape(name)}">{_escape(target)}</definedName>'
        for name, target in defined_names
    )
    sheet_entries = "".join(
        f'<sheet name="{_escape(sheet.name)}" sheetId="{position}" '
        f'r:id="rId{position}"'
        + (f' state="{sheet.state}"' if sheet.state else "")
        + "/>"
        for position, sheet in enumerate(sheets, start=1)
    )
    parts["xl/workbook.xml"] = (
        f"{_XML_DECLARATION}"
        f'<workbook xmlns="{MAIN_NS}" xmlns:r="{OFFICE_REL_NS}">'
        + (f'<workbookPr date1904="1"/>' if date1904 else "")
        + f"<sheets>{sheet_entries}</sheets>"
        + (f"<definedNames>{defined}</definedNames>" if defined else "")
        + "</workbook>"
    )
    parts["xl/_rels/workbook.xml.rels"] = _relationships_xml(
        workbook_relationships
    )
    parts["_rels/.rels"] = _relationships_xml(
        [
            f'<Relationship Id="rIdWorkbook" '
            f'Type="{OFFICE_REL_NS}/officeDocument" Target="xl/workbook.xml"/>'
        ]
    )
    if number_formats:
        parts["xl/styles.xml"] = _styles_xml(number_formats)

    parts["[Content_Types].xml"] = (
        f"{_XML_DECLARATION}"
        f'<Types xmlns="{CONTENT_TYPES_NS}">'
        '<Default Extension="rels" ContentType="application/vnd.'
        'openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="png" ContentType="image/png"/>'
        f'{"".join(overrides)}</Types>'
    )
    parts.update(extra_parts or {})
    for name in drop_parts:
        parts.pop(name, None)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        # Readers expect the content-type map first; the Word fixtures proved
        # LibreOffice is unforgiving about package shape.
        content_types = parts.pop("[Content_Types].xml", None)
        if content_types is not None:
            package.writestr("[Content_Types].xml", content_types)
        for name, content in parts.items():
            package.writestr(name, content)
    return buffer.getvalue()


def _worksheet_xml(sheet: Sheet, *, position: int) -> str:
    rows: list[str] = []
    for row_index, values in enumerate(sheet.rows, start=1):
        cells: list[str] = []
        for column_index, value in enumerate(values, start=1):
            if value is None or value == "":
                continue
            reference = f"{column_letter(column_index)}{row_index}"
            cells.append(_cell_xml(reference, value))
        rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')

    merges = (
        f'<mergeCells count="{len(sheet.merges)}">'
        + "".join(f'<mergeCell ref="{ref}"/>' for ref in sheet.merges)
        + "</mergeCells>"
        if sheet.merges
        else ""
    )
    autofilter = (
        f'<autoFilter ref="{sheet.autofilter}"/>' if sheet.autofilter else ""
    )
    drawing = (
        f'<drawing r:id="rIdD{position}"/>' if sheet.has_drawing else ""
    )
    tables = (
        f'<tableParts count="1"><tablePart r:id="rIdT1"/></tableParts>'
        if sheet.table_name is not None
        else ""
    )
    return (
        f"{_XML_DECLARATION}"
        f'<worksheet xmlns="{MAIN_NS}" xmlns:r="{OFFICE_REL_NS}">'
        f'<dimension ref="A1:{_last_reference(sheet.rows)}"/>'
        f'<sheetData>{"".join(rows)}</sheetData>'
        f"{autofilter}{merges}{drawing}{tables}</worksheet>"
    )


def _cell_xml(reference: str, value: object) -> str:
    if isinstance(value, tuple):
        raw, style = value
        return f'<c r="{reference}" s="{style}"><v>{_escape(str(raw))}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{reference}"><v>{value}</v></c>'
    return (
        f'<c r="{reference}" t="inlineStr"><is><t>{_escape(str(value))}</t>'
        "</is></c>"
    )


def _styles_xml(number_formats: Mapping[int, str]) -> str:
    """Build ``cellXfs`` so style index N resolves to ``number_formats[N]``.

    A value is a numeric built-in id, or a format code string that is
    registered as a custom ``numFmt``.
    """

    custom: list[str] = []
    entries: list[str] = []
    next_custom_id = 164
    for index in range(max(number_formats) + 1):
        specification = number_formats.get(index, 0)
        if isinstance(specification, int):
            entries.append(f'<xf numFmtId="{specification}"/>')
            continue
        custom.append(
            f'<numFmt numFmtId="{next_custom_id}" '
            f'formatCode="{_escape(specification)}"/>'
        )
        entries.append(f'<xf numFmtId="{next_custom_id}"/>')
        next_custom_id += 1
    return (
        f"{_XML_DECLARATION}"
        f'<styleSheet xmlns="{MAIN_NS}">'
        + (
            f'<numFmts count="{len(custom)}">{"".join(custom)}</numFmts>'
            if custom
            else ""
        )
        + f'<cellXfs count="{len(entries)}">{"".join(entries)}</cellXfs>'
        "</styleSheet>"
    )


def _table_xml(sheet: Sheet, index: int) -> str:
    headers = [str(value) for value in sheet.rows[0]] if sheet.rows else []
    columns = "".join(
        f'<tableColumn id="{position}" name="{_escape(header)}"/>'
        for position, header in enumerate(headers, start=1)
    )
    return (
        f"{_XML_DECLARATION}"
        f'<table xmlns="{MAIN_NS}" id="{index}" '
        f'name="{_escape(sheet.table_name or "")}" '
        f'displayName="{_escape(sheet.table_name or "")}" '
        f'ref="A1:{_last_reference(sheet.rows)}">'
        f'<tableColumns count="{len(headers)}">{columns}</tableColumns>'
        "</table>"
    )


def _drawing_xml(sheet: Sheet) -> str:
    anchors: list[str] = []
    descriptions = list(sheet.alt_texts)
    total = max(len(sheet.charts) + sheet.images, len(descriptions))
    for position in range(total):
        description = (
            descriptions[position] if position < len(descriptions) else ""
        )
        anchors.append(
            "<xdr:twoCellAnchor>"
            "<xdr:pic><xdr:nvPicPr>"
            f'<xdr:cNvPr id="{position + 1}" name="개체 {position + 1}" '
            f'descr="{_escape(description)}"/>'
            "</xdr:nvPicPr></xdr:pic>"
            "</xdr:twoCellAnchor>"
        )
    return (
        f"{_XML_DECLARATION}"
        f'<xdr:wsDr xmlns:xdr="{SPREADSHEET_DRAWING_NS}" '
        f'xmlns:a="{DRAWING_NS}">{"".join(anchors)}</xdr:wsDr>'
    )


def _chart_xml(chart: Mapping[str, object]) -> str:
    title = str(chart.get("title", ""))
    series = chart.get("series", ())
    blocks: list[str] = []
    for entry in series:  # type: ignore[union-attr]
        name = str(entry.get("name", ""))
        points = entry.get("points", ())
        categories = "".join(
            f'<c:pt idx="{index}"><c:v>{_escape(str(label))}</c:v></c:pt>'
            for index, (label, _) in enumerate(points)  # type: ignore[misc]
        )
        values = "".join(
            f'<c:pt idx="{index}"><c:v>{_escape(str(value))}</c:v></c:pt>'
            for index, (_, value) in enumerate(points)  # type: ignore[misc]
        )
        blocks.append(
            "<c:ser>"
            f"<c:tx><c:strRef><c:v>{_escape(name)}</c:v></c:strRef></c:tx>"
            f"<c:cat><c:strRef><c:strCache>{categories}</c:strCache>"
            "</c:strRef></c:cat>"
            f"<c:val><c:numRef><c:numCache>{values}</c:numCache>"
            "</c:numRef></c:val>"
            "</c:ser>"
        )
    return (
        f"{_XML_DECLARATION}"
        f'<c:chartSpace xmlns:c="{CHART_NS}" xmlns:a="{DRAWING_NS}"><c:chart>'
        f"<c:title><c:tx><c:rich><a:p><a:r><a:t>{_escape(title)}</a:t>"
        "</a:r></a:p></c:rich></c:tx></c:title>"
        f'<c:plotArea>{"".join(blocks)}</c:plotArea>'
        "</c:chart></c:chartSpace>"
    )


def _relationships_xml(entries: Sequence[str]) -> str:
    return (
        f"{_XML_DECLARATION}"
        f'<Relationships xmlns="{PACKAGE_REL_NS}">{"".join(entries)}'
        "</Relationships>"
    )


def _last_reference(rows: Sequence[Sequence[object]]) -> str:
    if not rows:
        return "A1"
    width = max((len(row) for row in rows), default=1)
    return f"{column_letter(max(width, 1))}{len(rows)}"


def _escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def quote_sheet(rows: int = 3) -> Sheet:
    """A small Korean quotation form: merged title bar, print area, few rows."""

    body: list[list[object]] = [
        ["견적서", None, None],
        ["품명", "수량", "금액"],
    ]
    for number in range(1, rows + 1):
        body.append([f"A자재{number}", number * 10, number * 50_000])
    return Sheet(
        "견적서",
        body,
        merges=["A1:C1"],
        print_area=True,
    )


def ledger_sheet(rows: int = 120, name: str = "매출원장") -> Sheet:
    """A ledger: header row, uniform typed columns, AutoFilter, many rows."""

    body: list[list[object]] = [["번호", "지점", "매출"]]
    for number in range(1, rows + 1):
        body.append([number, f"지점{number}", number * 1_000])
    return Sheet(name, body, autofilter=f"A1:C{rows + 1}")

"""Normalize Word (.docx) OOXML into CrewMeal's semantic content contract.

Word has no native page concept: ``word/document.xml`` is one continuous block
stream and pagination is decided by the layout engine. This module therefore
extracts an *ordered block sequence* (headings, paragraphs, list items, tables,
visual anchors) plus the surrounding parts (headers/footers, foot/endnotes,
hyperlinks, charts), and leaves page assignment to
:func:`align_blocks_to_pages`, which matches the block stream against the text
LibreOffice produced when it laid the document out into PDF pages.

Tables get special attention. :class:`~crewmeal.search_enhancement.models.ContentTable`
is a strict rectangle (one header row + equal-width rows) and the HTML renderer
emits bare ``<td>`` with no ``colspan``/``rowspan``, so merged cells *must* be
flattened. Rather than silently losing structure, every flattening decision that
could distort a complex Korean-style table is recorded as a
:class:`TableComplexity` signal; the handler promotes those pages to visual
analysis so the Vision model reads the rendered table as a cross-check.
"""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field

from defusedxml import ElementTree

from crewmeal.search_enhancement.models import (
    ChartDataPoint,
    ContentChart,
    ContentTable,
)

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
RELATIONSHIPS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_REL_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
CHART_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
EXTENDED_PROPS_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
)

NS = {
    "w": WORD_NS,
    "r": OFFICE_REL_NS,
    "a": DRAWING_NS,
    "c": CHART_NS,
    "pr": RELATIONSHIPS_NS,
    "ep": EXTENDED_PROPS_NS,
}

DOCUMENT_PART = "word/document.xml"

#: Upper bound on a normalized table's cell count. Matches the rhwp limit so a
#: hostile or pathological document cannot allocate an unbounded grid.
MAX_TABLE_CELLS = 100_000

#: Shorter block texts are not distinctive enough to locate a page: a lone
#: number or a one-word label matches almost anywhere in the rendered stream.
_MIN_ALIGNMENT_KEY = 4

#: Drawing elements that carry no extractable text payload. Their presence marks
#: a page as needing visual analysis, mirroring the rhwp visual-page rule.
_VISUAL_LABELS = {
    "picture": "이미지",
    "chart": "차트",
    "diagram": "다이어그램(SmartArt)",
    "shape": "도형",
    "equation": "수식",
    "object": "포함 개체",
}


class DocxSemanticError(ValueError):
    """Raised when a Word package cannot be normalized safely."""


@dataclass(frozen=True, slots=True)
class TableComplexity:
    """Why a normalized table may not faithfully represent the original.

    Any non-empty signal set promotes the table's page to visual analysis: the
    flattened grid is still published (it is searchable text), but the Vision
    model also reads the rendered table so merged/nested structure is recovered.
    """

    nested: bool = False
    vertical_merge: bool = False
    horizontal_merge: bool = False
    multi_row_header: bool = False
    ragged_rows: bool = False

    @property
    def is_complex(self) -> bool:
        return (
            self.nested
            or self.vertical_merge
            or self.multi_row_header
            or self.ragged_rows
        )

    def reasons(self) -> tuple[str, ...]:
        values: list[str] = []
        if self.nested:
            values.append("중첩 표")
        if self.vertical_merge:
            values.append("세로 병합")
        if self.horizontal_merge:
            values.append("가로 병합")
        if self.multi_row_header:
            values.append("다단 머리글")
        if self.ragged_rows:
            values.append("행별 열 수 불일치")
        return tuple(values)


@dataclass(frozen=True, slots=True)
class DocxBlock:
    """One ordered unit of the document body.

    ``text`` is the normalized plain text used both for output and for aligning
    the block to a rendered PDF page. ``kind`` drives how the block becomes
    :class:`~crewmeal.search_enhancement.models.SlideContent` structure.

    ``alignment_text`` overrides ``text`` when locating the block in the
    rendered page stream. Only tables need it: flattening merges duplicates
    values and joins banded headers with ``" / "``, neither of which the
    renderer draws, so ``text`` would never be found.

    ``alignment_fallback`` is a shorter, less precise key tried when the primary
    one misses -- a table's longest single cell survives nested-table
    interleaving and column-major text extraction, which a full cell sequence
    does not.
    """

    kind: str
    text: str
    heading_level: int = 0
    is_list_item: bool = False
    table: ContentTable | None = None
    nested_tables: tuple[ContentTable, ...] = ()
    complexity: TableComplexity | None = None
    visual_kinds: tuple[str, ...] = ()
    chart: ContentChart | None = None
    alt_texts: tuple[str, ...] = ()
    starts_page: bool = False
    alignment_text: str = ""
    alignment_fallback: str = ""

    @property
    def is_visual(self) -> bool:
        return bool(self.visual_kinds)

    @property
    def alignment_candidates(self) -> tuple[str, ...]:
        """Keys to look for in the rendered stream, most precise first."""

        candidates = (self.alignment_text or self.text, self.alignment_fallback)
        return tuple(dict.fromkeys(value for value in candidates if value))


@dataclass(frozen=True, slots=True)
class DocxExtraction:
    """Everything read out of the Word package, before page assignment."""

    blocks: tuple[DocxBlock, ...]
    header_lines: tuple[str, ...]
    footer_lines: tuple[str, ...]
    footnote_lines: tuple[str, ...]
    links: tuple[str, ...]
    section_count: int
    declared_page_count: int | None
    warnings: tuple[str, ...] = ()


@dataclass(slots=True)
class _TableBuild:
    """Mutable accumulator used while normalizing one ``w:tbl``."""

    grid: list[list[str]] = field(default_factory=list)
    nested: list[ContentTable] = field(default_factory=list)
    #: Cell text in OOXML reading order, each spanned cell counted once. This is
    #: the order the renderer draws the table in, so it -- not the flattened
    #: grid -- is what can be found in the rendered page text.
    rendered_cells: list[str] = field(default_factory=list)
    vertical_merge: bool = False
    horizontal_merge: bool = False
    ragged_rows: bool = False
    #: Column index -> value carried down by ``w:vMerge`` continuation cells.
    carried: dict[int, str] = field(default_factory=dict)
    #: True when row 0 contains a horizontally merged cell -- the parent label of
    #: a banded header.
    row0_spans_columns: bool = False
    #: True when row 1 continues a vertical merge started in row 0 -- the side
    #: label of a banded header.
    row1_continues_row0: bool = False


def extract_docx(data: bytes) -> DocxExtraction:
    """Read a .docx package into an ordered block sequence plus part text."""

    try:
        package = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise DocxSemanticError("The Word ZIP package is corrupt.") from exc

    with package:
        names = set(package.namelist())
        if DOCUMENT_PART not in names:
            raise DocxSemanticError(
                f"The Word package is missing {DOCUMENT_PART}."
            )

        root = _read_xml(package, DOCUMENT_PART)
        relationships = _read_relationships(package, DOCUMENT_PART)
        body = root.find("w:body", NS)
        if body is None:
            raise DocxSemanticError("The Word document has no body.")

        warnings: list[str] = []
        blocks = tuple(
            _body_blocks(body, package=package, relationships=relationships)
        )
        header_lines = _part_lines(package, relationships, "/header")
        footer_lines = _part_lines(package, relationships, "/footer")
        footnote_lines = _notes_lines(package, relationships)
        links = tuple(
            dict.fromkeys(
                relationship["target"]
                for relationship in relationships.values()
                if relationship["type"].endswith("/hyperlink")
                and relationship["target_mode"] == "External"
            )
        )
        section_count = len(body.findall(".//w:sectPr", NS))
        declared_page_count = _declared_page_count(package, names)

    if not any(block.text for block in blocks) and not any(
        block.is_visual for block in blocks
    ):
        raise DocxSemanticError("The Word document contains no readable content.")

    return DocxExtraction(
        blocks=blocks,
        header_lines=header_lines,
        footer_lines=footer_lines,
        footnote_lines=footnote_lines,
        links=links,
        section_count=max(section_count, 1),
        declared_page_count=declared_page_count,
        warnings=tuple(warnings),
    )


def _body_blocks(
    body: ElementTree.Element,
    *,
    package: zipfile.ZipFile,
    relationships: Mapping[str, Mapping[str, str]],
) -> Iterator[DocxBlock]:
    table_index = 0
    pending_page_break = False

    for element in body:
        name = _local_name(element.tag)
        if name == "p":
            block = _paragraph_block(
                element,
                package=package,
                relationships=relationships,
                starts_page=pending_page_break,
            )
            pending_page_break = _has_page_break(element)
            if block is not None:
                yield block
            continue
        if name == "tbl":
            table_index += 1
            block = _table_block(
                element, index=table_index, starts_page=pending_page_break
            )
            pending_page_break = False
            if block is not None:
                yield block
            continue
        if name == "sectPr":
            pending_page_break = True


def _paragraph_block(
    paragraph: ElementTree.Element,
    *,
    package: zipfile.ZipFile,
    relationships: Mapping[str, Mapping[str, str]],
    starts_page: bool,
) -> DocxBlock | None:
    text = _paragraph_text(paragraph)
    visual_kinds = _visual_kinds(paragraph)
    alt_texts = _alt_texts(paragraph)
    chart = _paragraph_chart(paragraph, package=package, relationships=relationships)
    heading_level = _heading_level(paragraph)
    is_list_item = paragraph.find("w:pPr/w:numPr", NS) is not None

    if not text and not visual_kinds and not alt_texts:
        return None

    if visual_kinds:
        kind = "visual_anchor"
    elif heading_level:
        kind = "heading"
    elif is_list_item:
        kind = "list_item"
    else:
        kind = "paragraph"

    return DocxBlock(
        kind=kind,
        text=text,
        heading_level=heading_level,
        is_list_item=is_list_item,
        visual_kinds=visual_kinds,
        chart=chart,
        alt_texts=alt_texts,
        starts_page=starts_page,
    )


def _table_block(
    table: ElementTree.Element,
    *,
    index: int,
    starts_page: bool,
) -> DocxBlock | None:
    build = _TableBuild()
    column_count = len(table.findall("w:tblGrid/w:gridCol", NS))

    for row in table.findall("w:tr", NS):
        _append_table_row(build, row, index=index)

    grid = [row for row in build.grid if any(cell.strip() for cell in row)]
    if not grid:
        return None

    width = max(len(row) for row in grid)
    if column_count and any(len(row) != column_count for row in build.grid):
        build.ragged_rows = True
    if len({len(row) for row in build.grid}) > 1:
        build.ragged_rows = True
    grid = [row + [""] * (width - len(row)) for row in grid]

    if len(grid) * width > MAX_TABLE_CELLS:
        raise DocxSemanticError(
            f"표 {index}이(가) {MAX_TABLE_CELLS:,}셀 안전 한도를 초과했습니다."
        )

    # Word encodes a banded (two-level) header as a row-0 cell spanning columns
    # above the sub-labels, plus a row-0 side label spanning down into row 1.
    # Requiring *both* signals separates a real banded header from an ordinary
    # table whose first data column merges vertically. Deeper bands are left
    # alone and simply flagged complex so Vision cross-checks them.
    header_rows = 2 if build.row0_spans_columns and build.row1_continues_row0 else 1
    header_rows = min(header_rows, len(grid))
    multi_row_header = header_rows > 1
    if multi_row_header:
        headers = tuple(
            " / ".join(
                dict.fromkeys(
                    value
                    for value in (grid[row][column] for row in range(header_rows))
                    if value
                )
            )
            or f"열 {column + 1}"
            for column in range(width)
        )
        rows = tuple(tuple(row) for row in grid[header_rows:])
    elif len(grid) == 1:
        headers = tuple(f"열 {column + 1}" for column in range(width))
        rows = (tuple(grid[0]),)
    else:
        headers = tuple(
            value or f"열 {column + 1}" for column, value in enumerate(grid[0])
        )
        rows = tuple(tuple(row) for row in grid[1:])

    complexity = TableComplexity(
        nested=bool(build.nested),
        vertical_merge=build.vertical_merge,
        horizontal_merge=build.horizontal_merge,
        multi_row_header=multi_row_header,
        ragged_rows=build.ragged_rows,
    )
    content_table = ContentTable(
        title=f"표 {index}",
        headers=headers,
        rows=rows,
        key_facts=(),
    )
    nested_tables = tuple(
        ContentTable(
            title=f"표 {index}-{position} (중첩)",
            headers=nested.headers,
            rows=nested.rows,
            key_facts=(),
        )
        for position, nested in enumerate(build.nested, start=1)
    )
    text = " ".join(
        value
        for row in (headers, *rows)
        for value in row
        if value and not value.startswith("열 ")
    )
    return DocxBlock(
        kind="table",
        text=_normalize_space(text),
        alignment_text=_normalize_space(" ".join(build.rendered_cells)),
        alignment_fallback=max(build.rendered_cells, key=len, default=""),
        table=content_table,
        nested_tables=nested_tables,
        complexity=complexity,
        starts_page=starts_page,
    )


def _append_table_row(
    build: _TableBuild,
    row: ElementTree.Element,
    *,
    index: int,
) -> None:
    row_index = len(build.grid)
    values: list[str] = []
    column = 0
    has_cell = False

    for cell in row.findall("w:tc", NS):
        has_cell = True
        span = _grid_span(cell)
        if span > 1:
            build.horizontal_merge = True
            if row_index == 0:
                build.row0_spans_columns = True
        merge = cell.find("w:tcPr/w:vMerge", NS)
        continues = merge is not None and (
            merge.attrib.get(f"{{{WORD_NS}}}val", "continue") == "continue"
        )
        if merge is not None:
            build.vertical_merge = True
        if continues:
            value = build.carried.get(column, "")
            if row_index == 1 and column in build.carried:
                build.row1_continues_row0 = True
        else:
            value = _cell_text(cell)
            if value:
                build.rendered_cells.append(value)
            if merge is not None:
                build.carried[column] = value
        for offset in range(span):
            if not continues and merge is not None:
                build.carried[column + offset] = value
            values.append(value)
        column += span

        for nested in cell.findall("w:tbl", NS):
            nested_block = _table_block(nested, index=index, starts_page=False)
            if nested_block is not None and nested_block.table is not None:
                build.nested.append(nested_block.table)
                build.nested.extend(nested_block.nested_tables)

    if not has_cell:
        return
    build.grid.append(values)


def _grid_span(cell: ElementTree.Element) -> int:
    span_element = cell.find("w:tcPr/w:gridSpan", NS)
    if span_element is None:
        return 1
    try:
        span = int(span_element.attrib.get(f"{{{WORD_NS}}}val", "1"))
    except ValueError:
        return 1
    return max(1, min(span, 1024))


def _cell_text(cell: ElementTree.Element) -> str:
    values = [
        text
        for paragraph in cell.findall("w:p", NS)
        if (text := _paragraph_text(paragraph))
    ]
    return _normalize_space(" ".join(values))


def _paragraph_text(paragraph: ElementTree.Element) -> str:
    values: list[str] = []
    for element in paragraph.iter():
        name = _local_name(element.tag)
        if name == "t":
            values.append(element.text or "")
        elif name in {"tab"}:
            values.append(" ")
        elif name in {"br", "cr"}:
            values.append(" ")
    return _normalize_space("".join(values))


def _heading_level(paragraph: ElementTree.Element) -> int:
    style = paragraph.find("w:pPr/w:pStyle", NS)
    if style is None:
        return 0
    value = style.attrib.get(f"{{{WORD_NS}}}val", "")
    match = re.fullmatch(r"(?i)heading\s*([1-9])", value.replace("-", " ").strip())
    if match:
        return int(match.group(1))
    outline = paragraph.find("w:pPr/w:outlineLvl", NS)
    if outline is not None:
        try:
            return int(outline.attrib.get(f"{{{WORD_NS}}}val", "0")) + 1
        except ValueError:
            return 0
    return 0


def _has_page_break(paragraph: ElementTree.Element) -> bool:
    """True when the paragraph ends with an *author-authored* page break.

    ``w:lastRenderedPageBreak`` is deliberately ignored: it is a cache Word
    writes from its own layout pass, it is absent from documents produced by
    other tools, and it goes stale as soon as the content changes. Only explicit
    page breaks and section breaks are trustworthy.
    """

    for element in paragraph.iter():
        name = _local_name(element.tag)
        if name == "br" and element.attrib.get(f"{{{WORD_NS}}}type") == "page":
            return True
        if name == "sectPr":
            return True
    return False


def _visual_kinds(paragraph: ElementTree.Element) -> tuple[str, ...]:
    kinds: list[str] = []
    for element in paragraph.iter():
        name = _local_name(element.tag)
        if name == "pic":
            kinds.append("picture")
        elif name == "chart":
            kinds.append("chart")
        elif name in {"relIds", "dataModel"}:
            kinds.append("diagram")
        elif name in {"oMath", "oMathPara"}:
            kinds.append("equation")
        elif name in {"object", "OLEObject"}:
            kinds.append("object")
        elif name in {"wsp", "pict", "shape"}:
            kinds.append("shape")
    return tuple(dict.fromkeys(kinds))


def _alt_texts(paragraph: ElementTree.Element) -> tuple[str, ...]:
    values: list[str] = []
    for element in paragraph.iter():
        if _local_name(element.tag) != "docPr":
            continue
        for attribute in ("descr", "title"):
            value = element.attrib.get(attribute, "").strip()
            if value and value not in values:
                values.append(value)
    return tuple(values)


def _paragraph_chart(
    paragraph: ElementTree.Element,
    *,
    package: zipfile.ZipFile,
    relationships: Mapping[str, Mapping[str, str]],
) -> ContentChart | None:
    relationship_id = next(
        (
            element.attrib.get(f"{{{OFFICE_REL_NS}}}id")
            for element in paragraph.iter()
            if _local_name(element.tag) == "chart"
        ),
        None,
    )
    if not relationship_id:
        return None
    relationship = relationships.get(relationship_id)
    if relationship is None or relationship["target_mode"] != "Internal":
        return None
    part = _resolve_part(DOCUMENT_PART, relationship["target"])
    if part not in set(package.namelist()):
        return None
    try:
        chart_root = _read_xml(package, part)
    except DocxSemanticError:
        return None
    return _chart_content(chart_root)


def _chart_content(chart_root: ElementTree.Element) -> ContentChart | None:
    title = _normalize_space(
        " ".join(
            text
            for element in chart_root.findall(".//c:title//a:t", NS)
            if (text := (element.text or "").strip())
        )
    )
    data_points: list[ChartDataPoint] = []
    for series in chart_root.findall(".//c:ser", NS):
        series_name = _normalize_space(
            " ".join(
                text
                for element in series.findall("c:tx//c:v", NS)
                if (text := (element.text or "").strip())
            )
        )
        categories = [
            text
            for element in series.findall("c:cat//c:pt/c:v", NS)
            if (text := (element.text or "").strip())
        ]
        values = [
            text
            for element in series.findall("c:val//c:pt/c:v", NS)
            if (text := (element.text or "").strip())
        ]
        for position, value in enumerate(values):
            label = (
                categories[position]
                if position < len(categories)
                else f"항목 {position + 1}"
            )
            data_points.append(
                ChartDataPoint(series=series_name, label=label, value=value)
            )
    if not data_points:
        return None
    return ContentChart(
        title=title or "차트",
        data_points=tuple(data_points),
        insights=(),
    )


def _part_lines(
    package: zipfile.ZipFile,
    relationships: Mapping[str, Mapping[str, str]],
    type_suffix: str,
) -> tuple[str, ...]:
    names = set(package.namelist())
    values: list[str] = []
    for relationship in relationships.values():
        if not relationship["type"].endswith(type_suffix):
            continue
        part = _resolve_part(DOCUMENT_PART, relationship["target"])
        if part not in names:
            continue
        root = _read_xml(package, part)
        for paragraph in root.findall(".//w:p", NS):
            text = _paragraph_text(paragraph)
            if text and text not in values:
                values.append(text)
    return tuple(values)


def _notes_lines(
    package: zipfile.ZipFile,
    relationships: Mapping[str, Mapping[str, str]],
) -> tuple[str, ...]:
    names = set(package.namelist())
    values: list[str] = []
    for relationship in relationships.values():
        relationship_type = relationship["type"]
        if not (
            relationship_type.endswith("/footnotes")
            or relationship_type.endswith("/endnotes")
        ):
            continue
        part = _resolve_part(DOCUMENT_PART, relationship["target"])
        if part not in names:
            continue
        root = _read_xml(package, part)
        for note in root.findall(".//w:footnote", NS) + root.findall(
            ".//w:endnote", NS
        ):
            # Separator/continuation notes carry layout marks, not content.
            if note.attrib.get(f"{{{WORD_NS}}}type") in {
                "separator",
                "continuationSeparator",
                "continuationNotice",
            }:
                continue
            text = _normalize_space(
                " ".join(
                    value
                    for paragraph in note.findall(".//w:p", NS)
                    if (value := _paragraph_text(paragraph))
                )
            )
            if text and text not in values:
                values.append(text)
    return tuple(values)


def _declared_page_count(
    package: zipfile.ZipFile, names: set[str]
) -> int | None:
    """Word's cached page count from ``docProps/app.xml``, when present.

    Only used to warn about pagination drift: Word and LibreOffice legitimately
    paginate differently, so a mismatch must never fail the document.
    """

    if "docProps/app.xml" not in names:
        return None
    try:
        root = _read_xml(package, "docProps/app.xml")
    except DocxSemanticError:
        return None
    element = root.find("ep:Pages", NS)
    if element is None or not (element.text or "").strip():
        return None
    try:
        value = int(element.text.strip())
    except ValueError:
        return None
    return value if value > 0 else None


def align_blocks_to_pages(
    blocks: Sequence[DocxBlock],
    texts_by_page: Mapping[int, Sequence[str]],
) -> tuple[dict[int, int], tuple[str, ...]]:
    """Assign each block a 1-based page number using the rendered PDF text.

    The PDF page texts are concatenated into one normalized stream with recorded
    page boundaries; each block's normalized text is then searched forward from
    the previous match. This is a single left-to-right pass (two pointers), so
    cost is linear in the stream length rather than quadratic.

    The rendered stream is the only authority on pagination here -- explicit
    OOXML page breaks are already reflected in it, so they are not replayed on
    top (that would let a break disagree with the layout engine and drag every
    following block onto the wrong page).

    Returns ``(page_by_block_index, warnings)``. Blocks that cannot be located
    (field codes, generated TOCs, rotated text) inherit the previous block's page
    and are reported in ``warnings`` rather than failing the document.
    """

    page_numbers = sorted(texts_by_page)
    if not page_numbers:
        raise DocxSemanticError("The rendered document has no pages.")

    stream_parts: list[str] = []
    boundaries: list[tuple[int, int]] = []
    cursor = 0
    for page_number in page_numbers:
        page_text = _search_key(" ".join(texts_by_page[page_number]))
        stream_parts.append(page_text)
        cursor += len(page_text)
        boundaries.append((cursor, page_number))
    stream = "".join(stream_parts)

    page_by_block: dict[int, int] = {}
    unmatched = 0
    position = 0
    current_page = page_numbers[0]

    for order, block in enumerate(blocks):
        found = -1
        key = ""
        for candidate_text in block.alignment_candidates:
            key = _search_key(candidate_text)
            if len(key) < _MIN_ALIGNMENT_KEY:
                # Too short to identify a location: "표", "1.", a lone bullet
                # glyph would match almost anywhere and drag the pointer with it.
                continue
            found = _locate(
                stream,
                key,
                position=position,
                boundaries=boundaries,
                page_numbers=page_numbers,
                current_page=current_page,
            )
            if found >= 0:
                break
        if found < 0:
            if key and len(key) >= _MIN_ALIGNMENT_KEY:
                unmatched += 1
            page_by_block[order] = current_page
            continue
        current_page = _page_of(boundaries, found)
        position = found + len(key)
        page_by_block[order] = current_page

    warnings: list[str] = []
    if unmatched:
        warnings.append(
            f"{unmatched}개 블록을 렌더링된 페이지와 매칭하지 못해 직전 페이지에 "
            "배정했습니다."
        )
    return page_by_block, tuple(warnings)


def _locate(
    stream: str,
    key: str,
    *,
    position: int,
    boundaries: Sequence[tuple[int, int]],
    page_numbers: Sequence[int],
    current_page: int,
) -> int:
    """Find ``key`` at or after ``position``, allowing one page of backtrack."""

    found = stream.find(key, position)
    if found >= 0:
        return found
    # LibreOffice reorders floating content (text boxes, captions, anchored
    # images) relative to OOXML block order, so a block can legitimately render
    # slightly earlier. Allow one page of slack, but never accept a match that
    # jumps far back into the document.
    floor = _page_start(boundaries, _previous_page(current_page, page_numbers))
    candidate = stream.find(key, floor)
    return candidate if 0 <= candidate < position else -1


def _previous_page(current: int, page_numbers: Sequence[int]) -> int:
    try:
        index = page_numbers.index(current)
    except ValueError:
        return page_numbers[0]
    return page_numbers[max(index - 1, 0)]


def _page_start(boundaries: Sequence[tuple[int, int]], page: int) -> int:
    start = 0
    for end, page_number in boundaries:
        if page_number == page:
            return start
        start = end
    return 0


def _page_of(boundaries: Sequence[tuple[int, int]], position: int) -> int:
    for end, page_number in boundaries:
        if position < end:
            return page_number
    return boundaries[-1][1]


def split_blocks_by_breaks(
    blocks: Sequence[DocxBlock],
) -> dict[int, int]:
    """Assign pages using only explicit page/section breaks.

    Used by the no-Vision low tier, which never renders the document, so there
    is no PDF pagination to align against. A document without explicit breaks
    becomes a single page.
    """

    page_by_block: dict[int, int] = {}
    page = 1
    for order, block in enumerate(blocks):
        if block.starts_page and order > 0:
            page += 1
        page_by_block[order] = page
    return page_by_block


def visual_warnings(kinds: Mapping[str, int]) -> tuple[str, ...]:
    values: list[str] = []
    for kind, count in sorted(kinds.items()):
        label = _VISUAL_LABELS.get(kind, kind)
        values.append(
            f"{label} {count}개가 semantic payload 없이 선택적 시각 분석 대상으로 "
            "분류되었습니다."
        )
    return tuple(values)


def _read_relationships(
    package: zipfile.ZipFile, source_part: str
) -> dict[str, dict[str, str]]:
    directory, filename = posixpath.split(source_part)
    relationships_part = posixpath.join(directory, "_rels", f"{filename}.rels")
    if relationships_part not in set(package.namelist()):
        return {}

    root = _read_xml(package, relationships_part)
    relationships: dict[str, dict[str, str]] = {}
    for relationship in root.findall("pr:Relationship", NS):
        relationship_id = relationship.attrib.get("Id")
        target = relationship.attrib.get("Target")
        relationship_type = relationship.attrib.get("Type")
        if relationship_id and target and relationship_type:
            relationships[relationship_id] = {
                "target": target,
                "type": relationship_type,
                "target_mode": relationship.attrib.get("TargetMode", "Internal"),
            }
    return relationships


def _read_xml(package: zipfile.ZipFile, part_name: str) -> ElementTree.Element:
    try:
        data = package.read(part_name)
    except KeyError as exc:
        raise DocxSemanticError(
            f"The Word package is missing {part_name}."
        ) from exc
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise DocxSemanticError(
            f"The Word package contains invalid XML in {part_name}."
        ) from exc


def _resolve_part(source_part: str, target: str) -> str:
    if target.startswith("/"):
        return posixpath.normpath(target).lstrip("/")
    return posixpath.normpath(
        posixpath.join(posixpath.dirname(source_part), target)
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _search_key(value: str) -> str:
    """Collapse text to a comparison key that survives layout differences."""

    return re.sub(r"\s+", "", value).casefold()

"""Normalize rhwp RenderTree pages into CrewMeal's semantic content contract."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from crewmeal.search_enhancement.models import (
    ContentChart,
    ContentFlow,
    ContentHierarchy,
    ContentImage,
    ContentRelationship,
    ContentSection,
    ContentTable,
    SlideContent,
    SlideSchedule,
)

MAX_TABLE_CELLS = 100_000

_REGION_TYPES = frozenset({"Header", "Body", "FootnoteArea", "Footer"})
_SEMANTIC_NODE_TYPES = frozenset(
    {
        "Page",
        "PageBg",
        "Header",
        "Body",
        "Column",
        "Footer",
        "FootnoteArea",
        "TextLine",
        "TextRun",
        "Table",
        "Cell",
        "TextBox",
        "FnMarker",
        "Line",
    }
)
_VISUAL_LABELS = {
    "Image": "이미지",
    "Equation": "수식",
    "Placeholder": "자리표시자",
    "Rect": "사각형 도형",
    "Ellipse": "타원 도형",
}


class RhwpSemanticError(ValueError):
    """Raised when a RenderTree cannot be normalized safely."""


@dataclass(frozen=True, slots=True)
class RhwpSemanticExtraction:
    slides: tuple[SlideContent, ...]
    texts_by_page: Mapping[int, tuple[str, ...]]
    element_counts_by_page: Mapping[int, dict[str, int]]
    visual_pages: frozenset[int]


def extract_semantic_content(
    pages: Mapping[int, Mapping[str, Any]],
    *,
    engine_warnings: Sequence[str] = (),
) -> RhwpSemanticExtraction:
    if not pages:
        raise RhwpSemanticError("rhwp returned no RenderTree pages.")

    page_numbers = tuple(sorted(pages))
    expected = tuple(range(1, len(page_numbers) + 1))
    if page_numbers != expected:
        raise RhwpSemanticError(
            f"RenderTree pages must be contiguous: expected {expected}, got {page_numbers}."
        )

    slides: list[SlideContent] = []
    texts_by_page: dict[int, tuple[str, ...]] = {}
    counts_by_page: dict[int, dict[str, int]] = {}
    visual_pages: set[int] = set()

    for page_number in page_numbers:
        root = pages[page_number]
        counts = Counter(_node_types(root))
        counts_by_page[page_number] = dict(sorted(counts.items()))
        texts_by_page[page_number] = _text_lines(root)

        visual_counts = {
            node_type: count
            for node_type, count in counts.items()
            if node_type not in _SEMANTIC_NODE_TYPES
        }
        if visual_counts:
            visual_pages.add(page_number)

        regions = {
            node_type: _first_child(root, node_type) for node_type in _REGION_TYPES
        }
        header_lines = _text_lines(regions["Header"], skip_types={"Table"})
        body_lines = _text_lines(
            regions["Body"],
            skip_types={"Table", "FootnoteArea", "Header", "Footer"},
        )
        footnote_lines = _text_lines(
            regions["FootnoteArea"],
            skip_types={"Table"},
        )
        footer_lines = _text_lines(regions["Footer"], skip_types={"Table"})
        floating_lines = _floating_text_lines(root)

        sections = tuple(
            section
            for heading, lines in (
                ("머리말", header_lines),
                ("본문", body_lines),
                ("글상자 및 도형 텍스트", floating_lines),
                ("각주", footnote_lines),
                ("꼬리말", footer_lines),
            )
            if (section := _section(heading, lines)) is not None
        )
        tables = tuple(
            table
            for index, node in enumerate(_nodes_of_type(root, "Table"), start=1)
            if (table := _table(node, index=index)) is not None
        )
        warnings = _visual_warnings(visual_counts)
        if page_number == 1:
            warnings = (*warnings, *tuple(engine_warnings))

        title_candidates = (*body_lines, *floating_lines, *header_lines)
        title = next(
            (line for line in title_candidates if line.strip()),
            f"페이지 {page_number}",
        )
        slides.append(
            SlideContent(
                slide_number=page_number,
                title=title,
                summary="",
                facts=(),
                sections=sections,
                hierarchies=(),
                schedule=SlideSchedule(time_axis=(), tasks=(), milestones=()),
                flows=(),
                tables=tables,
                charts=(),
                relationships=(),
                images=(),
                warnings=warnings,
            )
        )

    return RhwpSemanticExtraction(
        slides=tuple(slides),
        texts_by_page=texts_by_page,
        element_counts_by_page=counts_by_page,
        visual_pages=frozenset(visual_pages),
    )


def _section(heading: str, lines: tuple[str, ...]) -> ContentSection | None:
    cleaned = tuple(line for line in lines if line.strip())
    if not cleaned:
        return None
    return ContentSection(heading=heading, paragraphs=cleaned, bullets=())


def _table(
    node: Mapping[str, Any],
    *,
    index: int,
) -> ContentTable | None:
    cells = tuple(
        child
        for child in _children(node)
        if child.get("type") == "Cell"
    )
    declared_rows = _nonnegative_int(node.get("rows"), field="Table.rows")
    declared_columns = _nonnegative_int(node.get("cols"), field="Table.cols")
    max_row = max(
        (_nonnegative_int(cell.get("row"), field="Cell.row") for cell in cells),
        default=-1,
    )
    max_column = max(
        (_nonnegative_int(cell.get("col"), field="Cell.col") for cell in cells),
        default=-1,
    )
    row_count = max(declared_rows, max_row + 1)
    column_count = max(declared_columns, max_column + 1)
    if row_count == 0 or column_count == 0:
        return None
    if row_count * column_count > MAX_TABLE_CELLS:
        raise RhwpSemanticError(
            f"Table {index} exceeds the {MAX_TABLE_CELLS:,} cell safety limit."
        )

    grid = [["" for _ in range(column_count)] for _ in range(row_count)]
    for cell in cells:
        row = _nonnegative_int(cell.get("row"), field="Cell.row")
        column = _nonnegative_int(cell.get("col"), field="Cell.col")
        if row >= row_count or column >= column_count:
            raise RhwpSemanticError(
                f"Table {index} cell ({row}, {column}) is outside its dimensions."
            )
        value = " ".join(_text_lines(cell, skip_types={"Table"})).strip()
        if grid[row][column] and value:
            grid[row][column] = f"{grid[row][column]} {value}"
        elif value:
            grid[row][column] = value

    if row_count == 1:
        headers = tuple(f"열 {column + 1}" for column in range(column_count))
        rows = (tuple(grid[0]),)
    else:
        headers = tuple(
            value or f"열 {column + 1}"
            for column, value in enumerate(grid[0])
        )
        rows = tuple(tuple(row) for row in grid[1:])

    return ContentTable(
        title=f"표 {index}",
        headers=headers,
        rows=rows,
        key_facts=(),
    )


def _visual_warnings(counts: Mapping[str, int]) -> tuple[str, ...]:
    warnings: list[str] = []
    for node_type, count in sorted(counts.items()):
        label = _VISUAL_LABELS.get(node_type, node_type)
        warnings.append(
            f"{label} {count}개가 semantic payload 없이 선택적 시각 분석 대상으로 "
            "분류되었습니다."
        )
    return tuple(warnings)


def _floating_text_lines(root: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for child in _children(root):
        if child.get("type") in _REGION_TYPES or child.get("type") == "PageBg":
            continue
        values.extend(_text_lines(child, skip_types={"Table"}))
    return tuple(values)


def _text_lines(
    node: Mapping[str, Any] | None,
    *,
    skip_types: set[str] | None = None,
) -> tuple[str, ...]:
    if node is None:
        return ()
    skipped = skip_types or set()
    if node.get("type") in skipped:
        return ()
    if node.get("type") == "TextLine":
        text = "".join(
            str(child.get("text", ""))
            for child in _children(node)
            if child.get("type") == "TextRun"
        ).strip()
        return (text,) if text else ()
    values: list[str] = []
    for child in _children(node):
        values.extend(_text_lines(child, skip_types=skipped))
    return tuple(values)


def _nodes_of_type(
    node: Mapping[str, Any],
    node_type: str,
) -> tuple[Mapping[str, Any], ...]:
    values: list[Mapping[str, Any]] = []
    if node.get("type") == node_type:
        values.append(node)
    for child in _children(node):
        values.extend(_nodes_of_type(child, node_type))
    return tuple(values)


def _node_types(node: Mapping[str, Any]) -> tuple[str, ...]:
    node_type = node.get("type")
    values = [str(node_type)] if isinstance(node_type, str) else []
    for child in _children(node):
        values.extend(_node_types(child))
    return tuple(values)


def _first_child(
    node: Mapping[str, Any],
    node_type: str,
) -> Mapping[str, Any] | None:
    return next(
        (child for child in _children(node) if child.get("type") == node_type),
        None,
    )


def _children(node: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    value = node.get("children", ())
    if not isinstance(value, list):
        return ()
    return tuple(child for child in value if isinstance(child, dict))


def _nonnegative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RhwpSemanticError(f"{field} must be a non-negative integer.")
    return value

"""Shared OOXML helpers used by more than one format handler.

Word and Excel packages are both OPC zips built from the same DrawingML
vocabulary, so chart extraction, table-complexity reporting and text
normalization are identical between them. They live here so the two handlers
cannot drift apart -- a table flagged "complex" must mean the same thing (and
produce the same Korean warning) whether it came from ``w:tbl`` or from a
worksheet's merge ranges.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from xml.etree import ElementTree

from crewmeal.search_enhancement.models import ChartDataPoint, ContentChart

RELATIONSHIPS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_REL_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
CHART_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"

#: Namespace prefixes shared by every OOXML part. Format modules extend this
#: with their own vocabulary (``w:`` for Word, ``s:`` for the spreadsheet).
NS = {
    "r": OFFICE_REL_NS,
    "a": DRAWING_NS,
    "c": CHART_NS,
    "pr": RELATIONSHIPS_NS,
}


@dataclass(frozen=True, slots=True)
class TableComplexity:
    """Which structural risks a table carried before it was flattened.

    ``ContentTable`` is a strict rectangle and the HTML allow-list emits no
    attributes, so ``colspan``/``rowspan`` cannot survive to the output. Every
    merge therefore has to be flattened, and this record says how lossy that
    was so the page can be cross-checked visually when it matters.
    """

    nested: bool = False
    vertical_merge: bool = False
    horizontal_merge: bool = False
    multi_row_header: bool = False
    ragged_rows: bool = False

    @property
    def is_complex(self) -> bool:
        """Whether flattening may have lost meaning a reader would rely on.

        A purely horizontal merge is deliberately excluded: duplicating one
        value across the columns it spanned reads the same as the original, so
        it does not justify the cost of a Vision call.
        """

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


def chart_content(chart_root: ElementTree.Element) -> ContentChart | None:
    """Read a DrawingML chart part into structured series data.

    Word and Excel store charts in byte-identical ``c:chartSpace`` parts, so
    this reads either. Returns ``None`` when the chart has no cached values --
    charts whose data lives only in a formula reference carry nothing
    extractable.
    """

    title = normalize_space(
        " ".join(
            text
            for element in chart_root.findall(".//c:title//a:t", NS)
            if (text := (element.text or "").strip())
        )
    )
    data_points: list[ChartDataPoint] = []
    for series in chart_root.findall(".//c:ser", NS):
        series_name = normalize_space(
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


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def search_key(value: str) -> str:
    """Collapse text to a comparison key that survives layout differences."""

    return re.sub(r"\s+", "", value).casefold()

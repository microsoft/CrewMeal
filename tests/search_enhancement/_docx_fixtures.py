"""Word (.docx) fixture builders.

Fixtures are assembled in-memory with ``zipfile`` so the repository stays free
of binary test files and each test can express exactly the OOXML shape it needs
(merged cells, nested tables, drawings, headers/footers, charts).
"""

from __future__ import annotations

import io
import zipfile

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
WP = 'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
PIC = 'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"'
C = 'xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"'

_RELS_ROOT = (
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
    'relationships">{items}</Relationships>'
)
_REL_BASE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_WML = "application/vnd.openxmlformats-officedocument.wordprocessingml"


def _override(part_name: str, content_type: str) -> str:
    return f'<Override PartName="{part_name}" ContentType="{content_type}"/>'


def paragraph(text: str, *, style: str | None = None, numbered: bool = False) -> str:
    properties = ""
    if style or numbered:
        parts = []
        if style:
            parts.append(f'<w:pStyle w:val="{style}"/>')
        if numbered:
            parts.append('<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>')
        properties = f"<w:pPr>{''.join(parts)}</w:pPr>"
    return f"<w:p>{properties}<w:r><w:t>{text}</w:t></w:r></w:p>"


def page_break() -> str:
    return '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'


def picture(*, description: str = "") -> str:
    descr = f' descr="{description}"' if description else ""
    return (
        "<w:p><w:r><w:drawing>"
        f"<wp:inline><wp:docPr id=\"1\" name=\"Picture 1\"{descr}/>"
        "<a:graphic><a:graphicData><pic:pic><pic:blipFill/></pic:pic>"
        "</a:graphicData></a:graphic></wp:inline>"
        "</w:drawing></w:r></w:p>"
    )


def chart_anchor(relationship_id: str = "rId9") -> str:
    return (
        "<w:p><w:r><w:drawing><wp:inline>"
        '<wp:docPr id="2" name="Chart 1"/>'
        "<a:graphic><a:graphicData>"
        f'<c:chart r:id="{relationship_id}"/>'
        "</a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"
    )


def cell(text: str, *, span: int = 1, v_merge: str | None = None, nested: str = "") -> str:
    properties = []
    if span > 1:
        properties.append(f'<w:gridSpan w:val="{span}"/>')
    if v_merge is not None:
        properties.append(f'<w:vMerge w:val="{v_merge}"/>')
    prefix = f"<w:tcPr>{''.join(properties)}</w:tcPr>" if properties else ""
    body = f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" if text else "<w:p/>"
    return f"<w:tc>{prefix}{body}{nested}</w:tc>"


def row(*cells: str) -> str:
    return f"<w:tr>{''.join(cells)}</w:tr>"


def table(*rows: str, columns: int) -> str:
    grid = "".join('<w:gridCol w:w="1000"/>' for _ in range(columns))
    return f"<w:tbl><w:tblGrid>{grid}</w:tblGrid>{''.join(rows)}</w:tbl>"


def simple_table() -> str:
    return table(
        row(cell("구분"), cell("내용")),
        row(cell("매출"), cell("1억원")),
        columns=2,
    )


def merged_table() -> str:
    """Banded header: a column-spanning parent label over vertical sub-labels.

    ::

        | 구분 |        2024        |
        |  ^   | 상반기  |  하반기  |
        | 매출 | 1억원   | 1.2억원  |
    """

    return table(
        row(cell("구분", v_merge="restart"), cell("2024", span=2)),
        row(cell("", v_merge="continue"), cell("상반기"), cell("하반기")),
        row(cell("매출"), cell("1억원"), cell("1.2억원")),
        columns=3,
    )


def side_label_table() -> str:
    """A vertical merge in the *data* area, not a banded header.

    The row-0 cells span no columns, so this must keep a single header row.
    """

    return table(
        row(cell("분류"), cell("항목"), cell("값")),
        row(cell("매출", v_merge="restart"), cell("국내"), cell("100")),
        row(cell("", v_merge="continue"), cell("해외"), cell("50")),
        columns=3,
    )


def span_only_table() -> str:
    """Horizontal merge with no vertical merge: flattening is lossless enough."""

    return table(
        row(cell("구분"), cell("내용")),
        row(cell("비고", span=2)),
        columns=2,
    )


def nested_table() -> str:
    inner = table(
        row(cell("세부"), cell("값")),
        row(cell("국내"), cell("100")),
        columns=2,
    )
    return table(
        row(cell("구분"), cell("내용", nested=inner)),
        row(cell("매출"), cell("1억원")),
        columns=2,
    )


def package_without_document() -> bytes:
    """A well-formed ZIP that is missing ``word/document.xml``."""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("docProps/app.xml", "<Properties/>")
    return buffer.getvalue()


def document_xml(body: str, *, sections: int = 1) -> str:
    section_breaks = "".join(
        '<w:p><w:pPr><w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr></w:pPr></w:p>'
        for _ in range(max(sections - 1, 0))
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"<w:document {W} {R} {WP} {A} {PIC} {C}><w:body>"
        f"{body}{section_breaks}"
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr>'
        "</w:body></w:document>"
    )


def chart_xml() -> str:
    return (
        f"<c:chartSpace {C} {A}><c:chart>"
        "<c:title><c:tx><c:rich><a:p><a:r><a:t>분기 매출</a:t></a:r></a:p>"
        "</c:rich></c:tx></c:title>"
        "<c:plotArea><c:barChart><c:ser>"
        "<c:tx><c:strRef><c:strCache><c:pt idx=\"0\"><c:v>매출</c:v></c:pt>"
        "</c:strCache></c:strRef></c:tx>"
        "<c:cat><c:strRef><c:strCache>"
        '<c:pt idx="0"><c:v>1분기</c:v></c:pt>'
        '<c:pt idx="1"><c:v>2분기</c:v></c:pt>'
        "</c:strCache></c:strRef></c:cat>"
        "<c:val><c:numRef><c:numCache>"
        '<c:pt idx="0"><c:v>100</c:v></c:pt>'
        '<c:pt idx="1"><c:v>150</c:v></c:pt>'
        "</c:numCache></c:numRef></c:val>"
        "</c:ser></c:barChart></c:plotArea></c:chart></c:chartSpace>"
    )


def build_docx(
    body: str,
    *,
    sections: int = 1,
    header: str | None = None,
    footer: str | None = None,
    footnote: str | None = None,
    hyperlink: str | None = None,
    chart: bool = False,
    declared_pages: int | None = None,
    extra_parts: dict[str, str] | None = None,
) -> bytes:
    relationships: list[str] = []
    overrides: list[str] = [
        _override("/word/document.xml", f"{_WML}.document.main+xml")
    ]
    parts: dict[str, str] = {
        "_rels/.rels": _RELS_ROOT.format(
            items=(
                f'<Relationship Id="rIdRoot" Type="{_REL_BASE}/officeDocument" '
                'Target="word/document.xml"/>'
            )
        ),
        "word/document.xml": document_xml(body, sections=sections),
    }

    if header is not None:
        parts["word/header1.xml"] = (
            f"<w:hdr {W}>{paragraph(header)}</w:hdr>"
        )
        relationships.append(
            f'<Relationship Id="rId1" Type="{_REL_BASE}/header" '
            'Target="header1.xml"/>'
        )
        overrides.append(_override("/word/header1.xml", f"{_WML}.header+xml"))
    if footer is not None:
        parts["word/footer1.xml"] = (
            f"<w:ftr {W}>{paragraph(footer)}</w:ftr>"
        )
        relationships.append(
            f'<Relationship Id="rId2" Type="{_REL_BASE}/footer" '
            'Target="footer1.xml"/>'
        )
        overrides.append(_override("/word/footer1.xml", f"{_WML}.footer+xml"))
    if footnote is not None:
        parts["word/footnotes.xml"] = (
            f"<w:footnotes {W}>"
            '<w:footnote w:type="separator" w:id="-1"><w:p/></w:footnote>'
            f'<w:footnote w:id="1">{paragraph(footnote)}</w:footnote>'
            "</w:footnotes>"
        )
        relationships.append(
            f'<Relationship Id="rId3" Type="{_REL_BASE}/footnotes" '
            'Target="footnotes.xml"/>'
        )
        overrides.append(
            _override("/word/footnotes.xml", f"{_WML}.footnotes+xml")
        )
    if hyperlink is not None:
        relationships.append(
            f'<Relationship Id="rId4" Type="{_REL_BASE}/hyperlink" '
            f'Target="{hyperlink}" TargetMode="External"/>'
        )
    if chart:
        parts["word/charts/chart1.xml"] = chart_xml()
        relationships.append(
            f'<Relationship Id="rId9" Type="{_REL_BASE}/chart" '
            'Target="charts/chart1.xml"/>'
        )
        overrides.append(
            _override(
                "/word/charts/chart1.xml",
                "application/vnd.openxmlformats-officedocument.drawingml.chart+xml",
            )
        )
    if declared_pages is not None:
        parts["docProps/app.xml"] = (
            '<Properties xmlns="http://schemas.openxmlformats.org/'
            'officeDocument/2006/extended-properties">'
            f"<Pages>{declared_pages}</Pages></Properties>"
        )
        overrides.append(
            _override(
                "/docProps/app.xml",
                "application/vnd.openxmlformats-officedocument."
                "extended-properties+xml",
            )
        )

    if relationships:
        parts["word/_rels/document.xml.rels"] = _RELS_ROOT.format(
            items="".join(relationships)
        )
    parts["[Content_Types].xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
        'content-types">'
        '<Default Extension="rels" ContentType="application/vnd.'
        'openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'{"".join(overrides)}'
        "</Types>"
    )
    parts.update(extra_parts or {})

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        # OPC readers expect the content-type map first in the archive.
        package.writestr("[Content_Types].xml", parts.pop("[Content_Types].xml"))
        for name, content in parts.items():
            package.writestr(name, content)
    return buffer.getvalue()

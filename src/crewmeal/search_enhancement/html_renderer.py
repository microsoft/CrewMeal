from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser
from typing import Mapping

from crewmeal.search_enhancement.models import (
    ContentFlow,
    ContentHierarchy,
    ColumnRenderedContent,
    RenderedHtml,
    SlideContent,
    SlideSchedule,
)


CONTENT_HTML_LIMIT_BYTES = 3_000_000
SHAREPOINT_COLUMN_LIMIT_CHARACTERS = 63_999
SHAREPOINT_MARKDOWN_OMISSION_NOTICE = (
    "## 콘텐츠 생략\n\n"
    "SharePoint 컬럼 용량 제한으로 이후 콘텐츠가 생략되었습니다."
)
ALLOWED_TAGS = frozenset(
    {
        "article",
        "header",
        "section",
        "h1",
        "h2",
        "h3",
        "p",
        "ul",
        "ol",
        "li",
        "table",
        "caption",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "strong",
        "em",
    }
)


class ContentTooLargeError(ValueError):
    """Raised when rendered content exceeds the connector content budget."""


class UnsafeHtmlError(ValueError):
    """Raised when generated HTML violates the renderer allowlist."""


class ColumnContentTooLargeError(ValueError):
    """Raised when even the fixed column wrapper cannot fit the field budget."""


class _AllowlistParser(HTMLParser):
    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag not in ALLOWED_TAGS:
            raise UnsafeHtmlError(f"HTML tag is not allowed: {tag}")
        if attrs:
            raise UnsafeHtmlError(f"HTML attributes are not allowed on: {tag}")

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag not in ALLOWED_TAGS:
            raise UnsafeHtmlError(f"HTML tag is not allowed: {tag}")


@dataclass(slots=True)
class _ArticleParts:
    header: str = ""
    sections: list[str] | None = None

    def __post_init__(self) -> None:
        if self.sections is None:
            self.sections = []


class _ArticlePartsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts = _ArticleParts()
        self._article_depth = 0
        self._capture_kind: str | None = None
        self._capture_depth = 0
        self._buffer: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if attrs:
            raise UnsafeHtmlError(f"HTML attributes are not allowed on: {tag}")
        if tag == "article" and self._article_depth == 0:
            self._article_depth = 1
            return
        if self._article_depth == 0:
            raise UnsafeHtmlError("Column HTML must have one article root.")
        if self._capture_kind is None and self._article_depth == 1:
            if tag not in {"header", "section"}:
                raise UnsafeHtmlError(
                    f"Unexpected top-level article element: {tag}"
                )
            self._capture_kind = tag
            self._capture_depth = 1
            self._buffer = [f"<{tag}>"]
        elif self._capture_kind is not None:
            self._capture_depth += 1
            self._buffer.append(f"<{tag}>")
        self._article_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "article" and self._article_depth == 1:
            if self._capture_kind is not None:
                raise UnsafeHtmlError("Article child was not closed.")
            self._article_depth = 0
            return
        if self._capture_kind is None:
            raise UnsafeHtmlError(f"Unexpected closing tag: {tag}")
        self._buffer.append(f"</{tag}>")
        self._capture_depth -= 1
        self._article_depth -= 1
        if self._capture_depth == 0:
            value = "".join(self._buffer)
            if self._capture_kind == "header":
                if self.parts.header:
                    raise UnsafeHtmlError("Article must contain only one header.")
                self.parts.header = value
            else:
                assert self.parts.sections is not None
                self.parts.sections.append(value)
            self._capture_kind = None
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._capture_kind is not None:
            self._buffer.append(data)
        elif data.strip():
            raise UnsafeHtmlError("Text is not allowed directly under article.")

    def handle_entityref(self, name: str) -> None:
        if self._capture_kind is not None:
            self._buffer.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._capture_kind is not None:
            self._buffer.append(f"&#{name};")


class _SectionFragmentsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.fragments: list[str] = []
        self._outer_seen = False
        self._depth = 0
        self._buffer: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if attrs:
            raise UnsafeHtmlError(f"HTML attributes are not allowed on: {tag}")
        if not self._outer_seen:
            if tag != "section":
                raise UnsafeHtmlError("Expected a section root.")
            self._outer_seen = True
            return
        if self._depth == 0:
            self._buffer = []
        self._buffer.append(f"<{tag}>")
        self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "section" and self._depth == 0:
            return
        if self._depth <= 0:
            raise UnsafeHtmlError(f"Unexpected closing tag: {tag}")
        self._buffer.append(f"</{tag}>")
        self._depth -= 1
        if self._depth == 0:
            self.fragments.append("".join(self._buffer))
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._depth:
            self._buffer.append(data)
        elif data.strip():
            raise UnsafeHtmlError("Text is not allowed directly under section.")

    def handle_entityref(self, name: str) -> None:
        if self._depth:
            self._buffer.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._depth:
            self._buffer.append(f"&#{name};")


@dataclass(slots=True)
class _HtmlNode:
    tag: str
    children: list[_HtmlNode | str] = field(default_factory=list)


class _HtmlTreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _HtmlNode("root")
        self._stack = [self.root]

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag not in ALLOWED_TAGS:
            raise UnsafeHtmlError(f"HTML tag is not allowed: {tag}")
        if attrs:
            raise UnsafeHtmlError(f"HTML attributes are not allowed on: {tag}")
        node = _HtmlNode(tag)
        self._stack[-1].children.append(node)
        self._stack.append(node)

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if len(self._stack) == 1 or self._stack[-1].tag != tag:
            raise UnsafeHtmlError(f"Unexpected closing tag: {tag}")
        self._stack.pop()

    def handle_data(self, data: str) -> None:
        self._stack[-1].children.append(data)

    def validate_closed(self) -> None:
        if len(self._stack) != 1:
            raise UnsafeHtmlError(
                f"HTML element was not closed: {self._stack[-1].tag}"
            )


def render_presentation_html(
    *,
    source_name: str,
    slides: tuple[SlideContent, ...],
    notes_by_slide: Mapping[int, tuple[str, ...]] | None = None,
    max_bytes: int = CONTENT_HTML_LIMIT_BYTES,
    unit_label: str = "슬라이드",
) -> RenderedHtml:
    if not slides:
        raise ValueError("At least one slide is required.")
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive.")
    if not unit_label.strip():
        raise ValueError("unit_label must not be empty.")

    ordered = tuple(sorted(slides, key=lambda slide: slide.slide_number))
    numbers = tuple(slide.slide_number for slide in ordered)
    if len(set(numbers)) != len(numbers):
        raise ValueError("Slide numbers must be unique.")

    notes = notes_by_slide or {}
    parts = [
        "<article>",
        "<header>",
        f"<h1>{_text(source_name)}</h1>",
        "</header>",
    ]
    for slide in ordered:
        parts.extend(
            _render_slide(
                slide,
                notes.get(slide.slide_number, ()),
                unit_label=unit_label,
            )
        )
    parts.append("</article>")
    content = "".join(parts)

    parser = _AllowlistParser(convert_charrefs=True)
    parser.feed(content)
    parser.close()

    content_bytes = content.encode("utf-8")
    if len(content_bytes) > max_bytes:
        raise ContentTooLargeError(
            f"CONTENT_TOO_LARGE: HTML is {len(content_bytes):,} bytes; "
            f"limit is {max_bytes:,} bytes."
        )

    titles = tuple(
        slide.title or f"{unit_label} {slide.slide_number}" for slide in ordered
    )
    return RenderedHtml(
        content=content,
        byte_count=len(content_bytes),
        sha256=hashlib.sha256(content_bytes).hexdigest(),
        slide_titles=titles,
        keywords=_keywords(ordered),
    )


def render_sharepoint_column_markdown(
    rendered: RenderedHtml,
    *,
    max_characters: int = SHAREPOINT_COLUMN_LIMIT_CHARACTERS,
) -> ColumnRenderedContent:
    if max_characters <= 0:
        raise ValueError("max_characters must be positive.")

    article = _ArticlePartsParser()
    article.feed(rendered.content)
    article.close()
    sections = article.parts.sections or []
    if not article.parts.header or not sections:
        raise UnsafeHtmlError(
            "Column content requires one header and at least one section."
        )

    header = _html_to_markdown(article.parts.header)
    markdown_sections = tuple(_html_to_markdown(section) for section in sections)
    full_content = _join_markdown_units((header, *markdown_sections))
    original_characters = sharepoint_character_count(full_content)
    if original_characters <= max_characters:
        return _column_result(
            full_content,
            original_characters=original_characters,
            truncated=False,
            omitted_units=0,
        )

    fixed_content = _join_markdown_units(
        (header, SHAREPOINT_MARKDOWN_OMISSION_NOTICE)
    )
    if sharepoint_character_count(fixed_content) > max_characters:
        raise ColumnContentTooLargeError(
            "COLUMN_CONTENT_TOO_LARGE: document header and omission notice "
            f"exceed the {max_characters:,}-character limit."
        )

    accepted: list[str] = []
    omitted = 0
    for index, section in enumerate(sections):
        markdown_section = markdown_sections[index]
        candidate = _join_markdown_units(
            (
                header,
                *accepted,
                markdown_section,
                SHAREPOINT_MARKDOWN_OMISSION_NOTICE,
            )
        )
        if sharepoint_character_count(candidate) <= max_characters:
            accepted.append(markdown_section)
            continue

        fragments = _section_fragments(section)
        partial: list[str] = []
        for fragment in fragments:
            markdown_fragment = _html_to_markdown(fragment)
            candidate = _join_markdown_units(
                (
                    header,
                    *accepted,
                    *partial,
                    markdown_fragment,
                    SHAREPOINT_MARKDOWN_OMISSION_NOTICE,
                )
            )
            if sharepoint_character_count(candidate) > max_characters:
                break
            partial.append(markdown_fragment)
        accepted.extend(partial)
        omitted = len(sections) - index
        break

    content = _join_markdown_units(
        (header, *accepted, SHAREPOINT_MARKDOWN_OMISSION_NOTICE)
    )
    count = sharepoint_character_count(content)
    if count > max_characters:
        raise AssertionError("Column renderer exceeded its character budget.")
    return _column_result(
        content,
        original_characters=original_characters,
        truncated=True,
        omitted_units=omitted or 1,
    )


def sharepoint_character_count(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _section_fragments(section: str) -> tuple[str, ...]:
    parser = _SectionFragmentsParser()
    parser.feed(section)
    parser.close()
    return tuple(parser.fragments)


def _validate_html(content: str) -> None:
    parser = _AllowlistParser(convert_charrefs=True)
    parser.feed(content)
    parser.close()


def _html_to_markdown(content: str) -> str:
    parser = _HtmlTreeParser()
    parser.feed(content)
    parser.close()
    parser.validate_closed()
    return _render_markdown_blocks(parser.root.children)


def _render_markdown_blocks(nodes: list[_HtmlNode | str]) -> str:
    blocks: list[str] = []
    for node in nodes:
        if isinstance(node, str):
            text = _normalize_inline(_escape_markdown(node))
            if text:
                blocks.append(text)
            continue
        block = _render_markdown_block(node)
        if block:
            blocks.append(block)
    return _join_markdown_units(blocks)


def _render_markdown_block(node: _HtmlNode) -> str:
    if node.tag in {"article", "header", "section", "thead", "tbody"}:
        return _render_markdown_blocks(node.children)
    if node.tag in {"h1", "h2", "h3"}:
        level = int(node.tag[1])
        return f"{'#' * level} {_render_markdown_inline(node.children)}"
    if node.tag == "p":
        return _render_markdown_inline(node.children)
    if node.tag in {"ul", "ol"}:
        lines: list[str] = []
        number = 1
        for child in node.children:
            if not isinstance(child, _HtmlNode) or child.tag != "li":
                continue
            marker = "- " if node.tag == "ul" else f"{number}. "
            lines.append(marker + _render_markdown_inline(child.children))
            number += 1
        return "\n".join(lines)
    if node.tag == "table":
        return _render_markdown_table(node)
    if node.tag in {"li", "caption", "tr", "th", "td", "strong", "em"}:
        return _render_markdown_inline(node.children)
    raise UnsafeHtmlError(f"Cannot render HTML tag as Markdown: {node.tag}")


def _render_markdown_inline(nodes: list[_HtmlNode | str]) -> str:
    parts: list[str] = []
    for node in nodes:
        if isinstance(node, str):
            parts.append(_escape_markdown(node))
        elif node.tag == "strong":
            parts.append(f"**{_render_markdown_inline(node.children)}**")
        elif node.tag == "em":
            parts.append(f"*{_render_markdown_inline(node.children)}*")
        else:
            parts.append(_render_markdown_inline(node.children))
    return _normalize_inline("".join(parts))


def _render_markdown_table(table: _HtmlNode) -> str:
    caption = next(
        (
            _plain_text(child)
            for child in table.children
            if isinstance(child, _HtmlNode) and child.tag == "caption"
        ),
        "",
    )
    rows: list[tuple[bool, list[str]]] = []
    for row in _descendants(table, "tr"):
        cells = [
            _table_cell_text(child)
            for child in row.children
            if isinstance(child, _HtmlNode) and child.tag in {"th", "td"}
        ]
        if cells:
            rows.append(
                (
                    any(
                        isinstance(child, _HtmlNode) and child.tag == "th"
                        for child in row.children
                    ),
                    cells,
                )
            )
    if not rows:
        return f"**표: {_escape_markdown(caption)}**" if caption else ""

    width = max(len(cells) for _, cells in rows)
    header_index = next(
        (index for index, (is_header, _) in enumerate(rows) if is_header),
        -1,
    )
    if header_index >= 0:
        headers = rows[header_index][1]
        body = [cells for index, (_, cells) in enumerate(rows) if index != header_index]
    else:
        headers = [f"열 {index + 1}" for index in range(width)]
        body = [cells for _, cells in rows]
    headers = headers + [""] * (width - len(headers))

    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    for cells in body:
        padded = cells + [""] * (width - len(cells))
        lines.append("| " + " | ".join(padded) + " |")
    table_markdown = "\n".join(lines)
    if caption:
        return f"**표: {_escape_markdown(caption)}**\n\n{table_markdown}"
    return table_markdown


def _descendants(node: _HtmlNode, tag: str) -> tuple[_HtmlNode, ...]:
    matches: list[_HtmlNode] = []
    for child in node.children:
        if not isinstance(child, _HtmlNode):
            continue
        if child.tag == tag:
            matches.append(child)
        matches.extend(_descendants(child, tag))
    return tuple(matches)


def _plain_text(node: _HtmlNode) -> str:
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        else:
            parts.append(_plain_text(child))
    return _normalize_inline("".join(parts))


def _table_cell_text(node: _HtmlNode) -> str:
    value = _plain_text(node)
    return value.replace("\\", "\\\\").replace("|", "\\|")


def _escape_markdown(value: str) -> str:
    return re.sub(r"([\\`*_{}\[\]<>#|])", r"\\\1", value)


def _normalize_inline(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _join_markdown_units(units: tuple[str, ...] | list[str]) -> str:
    return "\n\n".join(unit.strip() for unit in units if unit.strip())


def _column_result(
    content: str,
    *,
    original_characters: int,
    truncated: bool,
    omitted_units: int,
) -> ColumnRenderedContent:
    content_bytes = content.encode("utf-8")
    return ColumnRenderedContent(
        content=content,
        byte_count=len(content_bytes),
        character_count=sharepoint_character_count(content),
        original_character_count=original_characters,
        sha256=hashlib.sha256(content_bytes).hexdigest(),
        truncated=truncated,
        omitted_units=omitted_units,
    )


def _render_slide(
    slide: SlideContent,
    notes: tuple[str, ...],
    *,
    unit_label: str,
) -> list[str]:
    title = slide.title or "제목 없음"
    parts = [
        "<section>",
        f"<h2>{_text(unit_label)} {slide.slide_number}: {_text(title)}</h2>",
    ]
    if slide.summary:
        parts.append(f"<p><strong>요약:</strong> {_text(slide.summary)}</p>")

    if slide.facts:
        parts.append("<h3>핵심 사실</h3>")
        parts.append("<ul>")
        parts.extend(f"<li>{_text(fact)}</li>" for fact in slide.facts)
        parts.append("</ul>")

    for section in slide.sections:
        if section.heading:
            parts.append(f"<h3>{_text(section.heading)}</h3>")
        parts.extend(f"<p>{_text(paragraph)}</p>" for paragraph in section.paragraphs)
        if section.bullets:
            parts.append("<ul>")
            parts.extend(f"<li>{_text(item)}</li>" for item in section.bullets)
            parts.append("</ul>")

    for hierarchy in slide.hierarchies:
        parts.extend(_render_hierarchy(hierarchy))

    parts.extend(_render_schedule(slide.schedule))

    for flow in slide.flows:
        parts.extend(_render_flow(flow))

    for table in slide.tables:
        parts.extend(["<table>", f"<caption>{_text(table.title or '표')}</caption>"])
        parts.extend(["<thead>", "<tr>"])
        parts.extend(f"<th>{_text(header)}</th>" for header in table.headers)
        parts.extend(["</tr>", "</thead>", "<tbody>"])
        for row in table.rows:
            parts.append("<tr>")
            parts.extend(f"<td>{_text(cell)}</td>" for cell in row)
            parts.append("</tr>")
        parts.extend(["</tbody>", "</table>"])
        if table.key_facts:
            parts.extend(["<h3>표의 핵심 사실</h3>", "<ul>"])
            parts.extend(f"<li>{_text(fact)}</li>" for fact in table.key_facts)
            parts.append("</ul>")

    for chart in slide.charts:
        parts.append(f"<h3>차트: {_text(chart.title or '제목 없음')}</h3>")
        for point in chart.data_points:
            prefix = f"{point.series} - " if point.series else ""
            parts.append(
                f"<p>{_text(prefix + point.label)}: "
                f"<strong>{_text(point.value)}</strong></p>"
            )
        if chart.insights:
            parts.append("<ul>")
            parts.extend(f"<li>{_text(insight)}</li>" for insight in chart.insights)
            parts.append("</ul>")

    if slide.relationships:
        parts.append("<h3>도형 및 프로세스 관계</h3>")
        for relationship in slide.relationships:
            sentence = (
                f"{relationship.source} → {relationship.relation} → "
                f"{relationship.target}."
            )
            if relationship.description:
                sentence = f"{sentence} {relationship.description}"
            parts.append(f"<p>{_text(sentence)}</p>")

    if slide.images:
        parts.append("<h3>이미지 의미</h3>")
        for image in slide.images:
            description = image.description
            if image.role:
                description = f"{description} 역할: {image.role}."
            parts.append(f"<p>{_text(description)}</p>")
            if image.visible_text:
                parts.append("<ul>")
                parts.extend(f"<li>{_text(value)}</li>" for value in image.visible_text)
                parts.append("</ul>")

    cleaned_notes = tuple(note.strip() for note in notes if note.strip())
    if cleaned_notes:
        parts.extend(["<h3>발표자 노트</h3>", "<ul>"])
        parts.extend(f"<li>{_text(note)}</li>" for note in cleaned_notes)
        parts.append("</ul>")

    if slide.warnings:
        parts.extend(["<h3>판독 경고</h3>", "<ul>"])
        parts.extend(f"<li>{_text(warning)}</li>" for warning in slide.warnings)
        parts.append("</ul>")

    parts.append("</section>")
    return parts


def _render_hierarchy(hierarchy: ContentHierarchy) -> list[str]:
    if not hierarchy.rows:
        return []
    column_count = max(
        [len(hierarchy.level_labels)] + [len(row.path) for row in hierarchy.rows]
    )
    if column_count == 0:
        return []
    has_note = any(row.note for row in hierarchy.rows)
    title = hierarchy.title or "계층 구조"
    parts = [
        f"<h3>계층 구조: {_text(title)}</h3>",
        "<table>",
        f"<caption>{_text(title)}</caption>",
        "<thead>",
        "<tr>",
    ]
    labels = list(hierarchy.level_labels)
    for index in range(column_count):
        header = labels[index] if index < len(labels) and labels[index] else f"수준 {index + 1}"
        parts.append(f"<th>{_text(header)}</th>")
    if has_note:
        parts.append("<th>비고</th>")
    parts.extend(["</tr>", "</thead>", "<tbody>"])
    for row in hierarchy.rows:
        cells = list(row.path) + [""] * (column_count - len(row.path))
        parts.append("<tr>")
        parts.extend(f"<td>{_text(cell)}</td>" for cell in cells[:column_count])
        if has_note:
            parts.append(f"<td>{_text(row.note)}</td>")
        parts.append("</tr>")
    parts.extend(["</tbody>", "</table>"])
    return parts


def _render_schedule(schedule: SlideSchedule) -> list[str]:
    if schedule.is_empty:
        return []
    parts: list[str] = ["<h3>일정</h3>"]
    if schedule.time_axis:
        axis = f"{schedule.time_axis[0]} ~ {schedule.time_axis[-1]}"
        parts.append(f"<p><strong>기간 축:</strong> {_text(axis)}</p>")
    if schedule.tasks:
        parts.extend(
            [
                "<table>",
                "<caption>작업 일정</caption>",
                "<thead>",
                "<tr>",
                "<th>작업</th>",
                "<th>시작</th>",
                "<th>종료</th>",
                "</tr>",
                "</thead>",
                "<tbody>",
            ]
        )
        for task in schedule.tasks:
            name = " &gt; ".join(_text(cell) for cell in task.task_path if cell.strip())
            parts.append("<tr>")
            parts.append(f"<td>{name}</td>")
            parts.append(f"<td>{_text(task.start)}</td>")
            parts.append(f"<td>{_text(task.end)}</td>")
            parts.append("</tr>")
        parts.extend(["</tbody>", "</table>"])
    if schedule.milestones:
        parts.append("<h3>마일스톤</h3>")
        parts.append("<ul>")
        for milestone in schedule.milestones:
            text = milestone.name
            if milestone.when:
                text = f"{milestone.name}: {milestone.when}"
            parts.append(f"<li>{_text(text)}</li>")
        parts.append("</ul>")
    return parts


def _render_flow(flow: ContentFlow) -> list[str]:
    if not flow.steps:
        return []
    heading = flow.title or "프로세스 흐름"
    if flow.lane:
        heading = f"{heading} ({flow.lane})"
    parts = [f"<h3>흐름: {_text(heading)}</h3>", "<ol>"]
    parts.extend(f"<li>{_text(step)}</li>" for step in flow.steps)
    parts.append("</ol>")
    if len(flow.steps) > 1:
        sequence = " → ".join(flow.steps)
        parts.append(f"<p>{_text('순서: ' + sequence)}</p>")
    return parts


def _keywords(slides: tuple[SlideContent, ...]) -> tuple[str, ...]:
    candidates: list[str] = []
    for slide in slides:
        candidates.extend([slide.title, *(section.heading for section in slide.sections)])
        candidates.extend(fact for table in slide.tables for fact in table.key_facts)
        candidates.extend(insight for chart in slide.charts for insight in chart.insights)
        for hierarchy in slide.hierarchies:
            candidates.extend(hierarchy.level_labels)
            candidates.extend(row.path[-1] for row in hierarchy.rows if row.path)
        candidates.extend(
            " > ".join(task.task_path) for task in slide.schedule.tasks if task.task_path
        )
        candidates.extend(milestone.name for milestone in slide.schedule.milestones)
        for flow in slide.flows:
            candidates.extend(flow.steps)
    return tuple(dict.fromkeys(value.strip() for value in candidates if value.strip()))


def _text(value: object) -> str:
    return escape(str(value), quote=True)

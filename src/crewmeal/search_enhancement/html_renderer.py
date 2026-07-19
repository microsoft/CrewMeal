from __future__ import annotations

import hashlib
from html import escape
from html.parser import HTMLParser
from typing import Mapping

from crewmeal.search_enhancement.models import (
    ContentFlow,
    ContentHierarchy,
    RenderedHtml,
    SlideContent,
    SlideSchedule,
)


CONTENT_HTML_LIMIT_BYTES = 3_000_000
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


def render_presentation_html(
    *,
    source_name: str,
    slides: tuple[SlideContent, ...],
    notes_by_slide: Mapping[int, tuple[str, ...]] | None = None,
    max_bytes: int = CONTENT_HTML_LIMIT_BYTES,
) -> RenderedHtml:
    if not slides:
        raise ValueError("At least one slide is required.")
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive.")

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
        parts.extend(_render_slide(slide, notes.get(slide.slide_number, ())))
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
        slide.title or f"슬라이드 {slide.slide_number}" for slide in ordered
    )
    return RenderedHtml(
        content=content,
        byte_count=len(content_bytes),
        sha256=hashlib.sha256(content_bytes).hexdigest(),
        slide_titles=titles,
        keywords=_keywords(ordered),
    )


def _render_slide(slide: SlideContent, notes: tuple[str, ...]) -> list[str]:
    title = slide.title or "제목 없음"
    parts = [
        "<section>",
        f"<h2>슬라이드 {slide.slide_number}: {_text(title)}</h2>",
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

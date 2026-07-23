"""Extract search-ready semantic content from PowerPoint slides without Vision.

This is the PPTX counterpart to :mod:`crewmeal.search_enhancement.rhwp_semantic`.
It reads the OOXML slide shape tree and, for slides that are *unambiguously*
linear text (only layout placeholders, with no pictures, charts, tables,
connectors, or grouped shapes), produces a faithful :class:`SlideContent`
directly from the source markup. Such slides carry no spatial meaning that the
Vision model is needed to recover, so the shared pipeline can skip rendering and
analyzing them.

Every other slide is classified as *visual*: it keeps only a minimal semantic
placeholder (its title, when a title placeholder exists) and is reported in
``visual_pages`` so the pipeline still renders it and runs targeted Vision, whose
richer output is merged back in. The gate is deliberately conservative -- any
free-floating text box or non-text placeholder makes a slide visual -- because a
handful of text shapes arranged in space (a 2x2 matrix, a left/right comparison)
can encode meaning that flat text extraction would destroy.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass

from crewmeal.search_enhancement.models import (
    ContentSection,
    SlideContent,
    SlideSchedule,
)
from crewmeal.source import (
    DRAWING_NS,
    PRESENTATION_NS,
    InvalidPresentationError,
    _local_name,
    _ordered_slide_parts,
    _read_xml,
)

# Placeholder ``type`` values whose shapes carry only linear text and are safe
# to linearize. An empty string is the OOXML default (a generic content
# placeholder with no explicit ``type`` attribute).
_TEXT_PLACEHOLDER_TYPES = frozenset(
    {"", "title", "ctrTitle", "subTitle", "body", "obj"}
)
_TITLE_PLACEHOLDER_TYPES = frozenset({"title", "ctrTitle"})
# Shape-tree elements that mean the slide has non-text visual content. A group
# shape (``grpSp``) is treated as visual because its members may be arranged to
# convey spatial meaning.
_VISUAL_SHAPE_TAGS = frozenset({"pic", "graphicFrame", "cxnSp", "grpSp"})

_SP_TAG = f"{{{PRESENTATION_NS}}}sp"
_TX_BODY_TAG = f"{{{PRESENTATION_NS}}}txBody"
_PH_PATH = (
    f"{{{PRESENTATION_NS}}}nvSpPr"
    f"/{{{PRESENTATION_NS}}}nvPr"
    f"/{{{PRESENTATION_NS}}}ph"
)
_SPTREE_PATH = f"{{{PRESENTATION_NS}}}cSld/{{{PRESENTATION_NS}}}spTree"
_A_P_TAG = f"{{{DRAWING_NS}}}p"
_A_T_TAG = f"{{{DRAWING_NS}}}t"


@dataclass(frozen=True, slots=True)
class PptxSemanticExtraction:
    """Per-slide semantic content plus the pages that still need Vision."""

    slides: tuple[SlideContent, ...]
    visual_pages: frozenset[int]


def extract_semantic_slides(data: bytes) -> PptxSemanticExtraction:
    """Build semantic slides from a validated PPTX package.

    ``slides`` always covers every page. Text-only pages carry full content;
    visual pages carry a minimal placeholder and appear in ``visual_pages``.
    """

    try:
        package = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise InvalidPresentationError(
            "The PPTX ZIP package is corrupt."
        ) from exc

    slides: list[SlideContent] = []
    visual_pages: set[int] = set()
    with package:
        slide_parts = _ordered_slide_parts(package)
        for slide_number, slide_part in enumerate(slide_parts, start=1):
            root = _read_xml(package, slide_part)
            shape_tree = root.find(_SPTREE_PATH)
            if shape_tree is None or _slide_is_visual(shape_tree):
                visual_pages.add(slide_number)
                slides.append(
                    _visual_slide(slide_number, _title_text(shape_tree))
                )
            else:
                slides.append(_text_slide(slide_number, shape_tree))

    return PptxSemanticExtraction(
        slides=tuple(slides),
        visual_pages=frozenset(visual_pages),
    )


def _slide_is_visual(shape_tree) -> bool:
    """Return ``True`` unless every shape is a pure-text placeholder."""

    for element in shape_tree.iter():
        name = _local_name(element.tag)
        if name in _VISUAL_SHAPE_TAGS:
            return True
        if name == "sp" and not _is_text_placeholder(element):
            return True
    return False


def _is_text_placeholder(shape) -> bool:
    placeholder = shape.find(_PH_PATH)
    if placeholder is None:
        return False
    return placeholder.attrib.get("type", "") in _TEXT_PLACEHOLDER_TYPES


def _text_slide(slide_number: int, shape_tree) -> SlideContent:
    title_lines, subtitle_lines, body_lines = _collect_text(shape_tree)
    title = " ".join(title_lines).strip()
    if not title:
        fallback = next(
            (line for line in (*subtitle_lines, *body_lines) if line.strip()),
            "",
        )
        title = fallback or f"슬라이드 {slide_number}"

    sections: list[ContentSection] = []
    if subtitle_lines:
        sections.append(
            ContentSection(heading="부제", paragraphs=subtitle_lines, bullets=())
        )
    if body_lines:
        sections.append(
            ContentSection(heading="본문", paragraphs=(), bullets=body_lines)
        )
    return _slide_content(slide_number, title, tuple(sections))


def _visual_slide(slide_number: int, title: str) -> SlideContent:
    # An empty title lets the merge step fall back to the Vision-derived title.
    return _slide_content(slide_number, title, ())


def _slide_content(
    slide_number: int,
    title: str,
    sections: tuple[ContentSection, ...],
) -> SlideContent:
    return SlideContent(
        slide_number=slide_number,
        title=title,
        summary="",
        facts=(),
        sections=sections,
        hierarchies=(),
        schedule=SlideSchedule(time_axis=(), tasks=(), milestones=()),
        flows=(),
        tables=(),
        charts=(),
        relationships=(),
        images=(),
        warnings=(),
    )


def _title_text(shape_tree) -> str:
    if shape_tree is None:
        return ""
    title_lines, _subtitle, _body = _collect_text(shape_tree)
    return " ".join(title_lines).strip()


def _collect_text(
    shape_tree,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    title_lines: list[str] = []
    subtitle_lines: list[str] = []
    body_lines: list[str] = []
    for shape in shape_tree:
        if shape.tag != _SP_TAG:
            continue
        lines = _shape_paragraphs(shape)
        if not lines:
            continue
        placeholder = shape.find(_PH_PATH)
        ph_type = (
            placeholder.attrib.get("type", "") if placeholder is not None else ""
        )
        if ph_type in _TITLE_PLACEHOLDER_TYPES:
            title_lines.extend(lines)
        elif ph_type == "subTitle":
            subtitle_lines.extend(lines)
        else:
            body_lines.extend(lines)
    return tuple(title_lines), tuple(subtitle_lines), tuple(body_lines)


def _shape_paragraphs(shape) -> tuple[str, ...]:
    text_body = shape.find(_TX_BODY_TAG)
    if text_body is None:
        return ()
    lines: list[str] = []
    for paragraph in text_body.findall(_A_P_TAG):
        text = "".join(
            run.text or "" for run in paragraph.iter(_A_T_TAG)
        ).strip()
        if text:
            lines.append(text)
    return tuple(lines)

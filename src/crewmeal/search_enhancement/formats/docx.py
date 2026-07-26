"""Word (.docx / .docm) document handler.

Word documents are semantic-first like HWP, but Word has no native page model
and no rhwp-equivalent renderer. So the pipeline splits responsibilities:

* **Content** comes from OOXML (:mod:`crewmeal.search_enhancement.docx_semantic`).
  Body text, headings, lists, tables, charts, headers/footers and footnotes are
  extracted deterministically -- no model, no tokens.
* **Pagination and pixels** come from LibreOffice. The document is converted with
  the Writer PDF filter purely as a *rendering* step: the PDF supplies page
  boundaries (used to assign each OOXML block to a page) and PNGs for the pages
  that actually need visual analysis.

Only pages with visual payload are sent to the Vision model: pages holding
images/shapes/diagrams/equations, pages LibreOffice drew with raster or vector
content, and pages whose tables were too complex to flatten faithfully (merged
or nested cells). A text-only document therefore costs zero Vision calls, while
a page with a merged Korean-style table gets a visual cross-check and the
results are merged by the shared semantic-first pipeline.
"""

from __future__ import annotations

import hashlib
import tempfile
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path

import fitz

from crewmeal.config import AppConfig
from crewmeal.libreoffice import (
    LibreOfficeConversionError,
    convert_document_to_pdf,
    inspect_pdf,
)
from crewmeal.models import RendererManifest, SourceManifest
from crewmeal.search_enhancement.docx_semantic import (
    DocxBlock,
    DocxExtraction,
    DocxSemanticError,
    align_blocks_to_pages,
    extract_docx,
    split_blocks_by_breaks,
    visual_warnings,
)
from crewmeal.search_enhancement.formats.base import (
    EncryptedDocumentError,
    InvalidDocumentError,
    PreparedDocument,
    ProcessingFidelityError,
)
from crewmeal.search_enhancement.models import (
    ContentChart,
    ContentSection,
    ContentTable,
    SlideContent,
    SlideSchedule,
)
from crewmeal.search_enhancement.progress import ProgressReporter, Stage

DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
DOCM_CONTENT_TYPE = "application/vnd.ms-word.document.macroEnabled.12"

_ZIP_MAGIC = b"PK\x03\x04"
#: Password-protected Office files are OLE2 compound documents wrapping the
#: encrypted OOXML package, not ZIPs.
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

#: Vector drawings below this count are page furniture (rules, table borders,
#: underlines) rather than real diagrams, so they must not force a Vision call.
_MIN_MEANINGFUL_DRAWINGS = 12


class DocxHandler:
    """Detect, validate, and prepare Word documents."""

    format_id = "docx"
    display_name = "Word"
    extensions = frozenset({".docx", ".docm"})
    content_types = frozenset({DOCX_CONTENT_TYPE, DOCM_CONTENT_TYPE})
    supported = True

    def validate(self, data: bytes, *, filename: str, max_bytes: int) -> None:
        if Path(filename).suffix.lower() not in self.extensions:
            raise InvalidDocumentError(
                "Only .docx and .docm files are supported by the Word handler."
            )
        if not data:
            raise InvalidDocumentError("The uploaded Word document is empty.")
        if len(data) > max_bytes:
            raise InvalidDocumentError(
                f"The Word document exceeds the {max_bytes // (1024 * 1024)} MB limit."
            )
        if data.startswith(_OLE2_MAGIC):
            raise EncryptedDocumentError(
                "The Word document appears to be password-protected. Enable a "
                "decryption provider to process it."
            )
        if not data.startswith(_ZIP_MAGIC):
            raise InvalidDocumentError("The file is not a valid Word package.")

    def fingerprint(self, data: bytes) -> str:
        return f"docx-sha256:{hashlib.sha256(data).hexdigest()}"

    def prepare(
        self,
        data: bytes,
        *,
        source_name: str,
        config: AppConfig,
        reporter: ProgressReporter,
    ) -> PreparedDocument:
        source_started = time.perf_counter()
        reporter.stage(Stage.CONVERTING, message="OOXML 본문·표 추출")
        try:
            extraction = extract_docx(data)
        except DocxSemanticError as exc:
            raise InvalidDocumentError(
                f"The Word document could not be read: {exc}"
            ) from exc
        semantic_seconds = time.perf_counter() - source_started

        if config.low_tier_enabled:
            return self._prepare_low_tier(
                extraction,
                semantic_seconds=semantic_seconds,
                config=config,
                reporter=reporter,
            )
        return self._prepare_semantic_first(
            data,
            extraction=extraction,
            semantic_seconds=semantic_seconds,
            source_name=source_name,
            config=config,
            reporter=reporter,
        )

    def _prepare_low_tier(
        self,
        extraction: DocxExtraction,
        *,
        semantic_seconds: float,
        config: AppConfig,
        reporter: ProgressReporter,
    ) -> PreparedDocument:
        """No-Vision tier: OOXML only, paginated by explicit breaks.

        LibreOffice never runs here, so there is no PDF pagination to align
        against; pages come from explicit page/section breaks and a document
        without any becomes a single page. Nothing downstream compares this
        count to a renderer, so the approximation is safe.
        """

        reporter.stage(
            Stage.RENDERING,
            message="저품질 티어: Vision 렌더링 건너뜀",
            detail={"total": 0},
        )
        page_by_block = split_blocks_by_breaks(extraction.blocks)
        slides, texts_by_page, _ = self._build_pages(
            extraction,
            page_by_block=page_by_block,
            page_count=max(page_by_block.values(), default=1),
            alignment_warnings=(),
        )
        page_count = len(slides)
        renderer_manifest = RendererManifest(
            page_count=page_count,
            texts_by_page=dict(texts_by_page),
            links_by_page={page: () for page in range(1, page_count + 1)},
            page_images={},
            render_dpi=config.slide_image_render_dpi,
        )
        return PreparedDocument(
            source_manifest=self._source_manifest(
                extraction, texts_by_page, page_count
            ),
            renderer_manifest=renderer_manifest,
            geometry_by_page={},
            stage_timings={
                "sourceInspectionSeconds": semantic_seconds,
                "conversionSeconds": 0.0,
                "lowTierExtractionSeconds": semantic_seconds,
                "renderingSeconds": 0.0,
            },
            semantic_slides=slides,
        )

    def _prepare_semantic_first(
        self,
        data: bytes,
        *,
        extraction: DocxExtraction,
        semantic_seconds: float,
        source_name: str,
        config: AppConfig,
        reporter: ProgressReporter,
    ) -> PreparedDocument:
        suffix = Path(source_name).suffix.lower() or ".docx"
        with tempfile.TemporaryDirectory(prefix="crewmeal-docx-") as workspace:
            workspace_path = Path(workspace)
            docx_path = workspace_path / f"input{suffix}"
            docx_path.write_bytes(data)

            reporter.stage(Stage.CONVERTING, message="LibreOffice DOCX→PDF")
            try:
                conversion = convert_document_to_pdf(
                    docx_path,
                    workspace_path,
                    soffice_path=config.require_soffice(),
                    pdf_filter="pdf:writer_pdf_Export",
                )
            except LibreOfficeConversionError as exc:
                raise ProcessingFidelityError(
                    f"LibreOffice could not lay out the Word document: {exc}"
                ) from exc

            rendering_started = time.perf_counter()
            reporter.stage(Stage.RENDERING, message="페이지 렌더링")
            rendered = inspect_pdf(
                conversion.pdf_path,
                render_dpi=config.slide_image_render_dpi,
            )
            rendered_visual_pages = _pages_with_rendered_visuals(
                conversion.pdf_path
            )
            rendering_seconds = time.perf_counter() - rendering_started

        if rendered.page_count <= 0:
            raise ProcessingFidelityError(
                "LibreOffice produced a Word rendering with no pages."
            )

        page_by_block, alignment_warnings = align_blocks_to_pages(
            extraction.blocks, rendered.texts_by_page
        )
        warnings = list(alignment_warnings)
        if (
            extraction.declared_page_count is not None
            and extraction.declared_page_count != rendered.page_count
        ):
            # Word and LibreOffice legitimately paginate differently (fonts,
            # hyphenation, field results), so this is informational only.
            warnings.append(
                f"Word가 기록한 페이지 수({extraction.declared_page_count})와 "
                f"렌더링 페이지 수({rendered.page_count})가 다릅니다. 페이지 구분은 "
                "렌더링 기준입니다."
            )

        slides, texts_by_page, visual_pages = self._build_pages(
            extraction,
            page_by_block=page_by_block,
            page_count=rendered.page_count,
            alignment_warnings=tuple(warnings),
        )
        visual_pages = visual_pages | (
            rendered_visual_pages & set(range(1, rendered.page_count + 1))
        )

        if visual_pages:
            page_images = {
                page: image
                for page, image in rendered.page_images.items()
                if page in visual_pages
            }
        else:
            reporter.stage(
                Stage.RENDERING,
                message="semantic coverage complete; Vision skipped",
                detail={"total": 0},
            )
            page_images = {}
        renderer_manifest = replace(
            rendered,
            texts_by_page=dict(texts_by_page),
            page_images=page_images,
        )

        return PreparedDocument(
            source_manifest=self._source_manifest(
                extraction, texts_by_page, rendered.page_count
            ),
            renderer_manifest=renderer_manifest,
            geometry_by_page={},
            stage_timings={
                "sourceInspectionSeconds": semantic_seconds,
                "conversionSeconds": conversion.conversion_seconds,
                "semanticExtractionSeconds": semantic_seconds,
                "renderingSeconds": rendering_seconds,
            },
            semantic_slides=slides,
        )

    def _build_pages(
        self,
        extraction: DocxExtraction,
        *,
        page_by_block: dict[int, int],
        page_count: int,
        alignment_warnings: tuple[str, ...],
    ) -> tuple[tuple[SlideContent, ...], dict[int, tuple[str, ...]], set[int]]:
        page_count = max(page_count, max(page_by_block.values(), default=1), 1)
        blocks_by_page: dict[int, list[DocxBlock]] = {
            page: [] for page in range(1, page_count + 1)
        }
        for order, block in enumerate(extraction.blocks):
            page = min(max(page_by_block.get(order, 1), 1), page_count)
            blocks_by_page[page].append(block)

        # Headers and footers repeat on every rendered page. Attaching them to
        # each page keeps single-section documents faithful; multi-section
        # documents can have per-section variants we cannot resolve without
        # layout, so those are attached to page 1 with a warning instead.
        repeat_page_furniture = extraction.section_count <= 1
        furniture_warning = (
            ()
            if repeat_page_furniture
            else (
                "구역이 여러 개여서 머리말·꼬리말을 첫 페이지에만 배치했습니다.",
            )
        )

        slides: list[SlideContent] = []
        texts_by_page: dict[int, tuple[str, ...]] = {}
        visual_pages: set[int] = set()

        for page in range(1, page_count + 1):
            page_blocks = blocks_by_page[page]
            include_furniture = repeat_page_furniture or page == 1
            header_lines = extraction.header_lines if include_furniture else ()
            footer_lines = extraction.footer_lines if include_furniture else ()
            # Footnotes render at the bottom of the page that references them,
            # but OOXML stores them in a separate part without page anchors, so
            # they are attached to page 1 where they remain discoverable.
            footnote_lines = extraction.footnote_lines if page == 1 else ()

            body_lines: list[str] = []
            bullet_lines: list[str] = []
            tables: list[ContentTable] = []
            charts: list[ContentChart] = []
            visual_counts: Counter[str] = Counter()
            table_warnings: list[str] = []
            heading = ""

            for block in page_blocks:
                if block.kind == "heading" and not heading:
                    heading = block.text
                if block.kind == "table" and block.table is not None:
                    tables.append(block.table)
                    tables.extend(block.nested_tables)
                    if block.complexity is not None and block.complexity.is_complex:
                        visual_pages.add(page)
                        table_warnings.append(
                            f"{block.table.title}은(는) "
                            f"{', '.join(block.complexity.reasons())} 때문에 "
                            "직사각형으로 평탄화했으며 시각 분석으로 교차 확인합니다."
                        )
                    continue
                if block.is_visual:
                    visual_pages.add(page)
                    visual_counts.update(block.visual_kinds)
                if block.chart is not None:
                    charts.append(block.chart)
                if block.text:
                    if block.is_list_item:
                        bullet_lines.append(block.text)
                    else:
                        body_lines.append(block.text)
                for alt_text in block.alt_texts:
                    body_lines.append(f"[이미지 설명] {alt_text}")

            sections: list[ContentSection] = []
            if header_lines:
                sections.append(
                    ContentSection(
                        heading="머리말", paragraphs=header_lines, bullets=()
                    )
                )
            if body_lines or bullet_lines:
                sections.append(
                    ContentSection(
                        heading="본문",
                        paragraphs=tuple(body_lines),
                        bullets=tuple(bullet_lines),
                    )
                )
            if footnote_lines:
                sections.append(
                    ContentSection(
                        heading="각주", paragraphs=footnote_lines, bullets=()
                    )
                )
            if footer_lines:
                sections.append(
                    ContentSection(
                        heading="꼬리말", paragraphs=footer_lines, bullets=()
                    )
                )

            title = heading or next(
                (line for line in body_lines if line.strip()),
                f"페이지 {page}",
            )
            warnings = (
                *visual_warnings(dict(visual_counts)),
                *table_warnings,
                *(alignment_warnings if page == 1 else ()),
                *(furniture_warning if page == 1 else ()),
                *(extraction.warnings if page == 1 else ()),
            )
            texts_by_page[page] = (
                *header_lines,
                *body_lines,
                *bullet_lines,
                *footnote_lines,
                *footer_lines,
            )
            slides.append(
                SlideContent(
                    slide_number=page,
                    title=title,
                    summary="",
                    facts=(),
                    sections=tuple(sections),
                    hierarchies=(),
                    schedule=SlideSchedule(time_axis=(), tasks=(), milestones=()),
                    flows=(),
                    tables=tuple(tables),
                    charts=tuple(charts),
                    relationships=(),
                    images=(),
                    warnings=tuple(dict.fromkeys(warnings)),
                )
            )

        return tuple(slides), texts_by_page, visual_pages

    def _source_manifest(
        self,
        extraction: DocxExtraction,
        texts_by_page: dict[int, tuple[str, ...]],
        page_count: int,
    ) -> SourceManifest:
        alt_texts = tuple(
            dict.fromkeys(
                alt_text
                for block in extraction.blocks
                for alt_text in block.alt_texts
            )
        )
        return SourceManifest(
            slide_count=page_count,
            texts_by_slide=dict(texts_by_page),
            links_by_slide={
                page: (extraction.links if page == 1 else ())
                for page in range(1, page_count + 1)
            },
            alt_text_by_slide={
                page: (alt_texts if page == 1 else ())
                for page in range(1, page_count + 1)
            },
            notes_by_slide={},
        )


def _pages_with_rendered_visuals(pdf_path: Path) -> set[int]:
    """Pages whose rendering contains raster images or substantial vectors.

    LibreOffice flattens SmartArt, grouped shapes and some equations into vector
    drawings with no OOXML anchor we can map back, so the rendered PDF is the
    only reliable signal for them. Trivial drawing counts are ignored so table
    borders and paragraph rules do not force needless Vision calls.
    """

    pages: set[int] = set()
    try:
        document = fitz.open(pdf_path)
    except (fitz.FileDataError, RuntimeError):
        return pages
    with document:
        for page_index, page in enumerate(document, start=1):
            try:
                if page.get_images():
                    pages.add(page_index)
                    continue
                if len(page.get_drawings()) >= _MIN_MEANINGFUL_DRAWINGS:
                    pages.add(page_index)
            except (RuntimeError, ValueError):
                continue
    return pages

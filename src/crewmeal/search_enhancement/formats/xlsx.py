"""Excel (.xlsx / .xlsm) workbook handler.

A workbook is not one kind of thing. Korean enterprise files routinely hold a
printed form and a 40,000-row ledger side by side, and the two need opposite
treatment, so this handler classifies every sheet and branches:

* **Document sheets** go through the same path as Word: LibreOffice renders
  them, merged tables are flattened, and pages whose tables were too complex to
  flatten faithfully (or that carry images and charts) are promoted to visual
  analysis.
* **Data sheets** are never rendered. Transcribing them is impossible against
  the 63,999-character SharePoint column ceiling and pointless because
  SharePoint already indexes cell text natively, so each collapses to a single
  summary unit: schema, scale, column types, and a few sample rows.

Data sheets are hidden in a temporary copy of the package before conversion,
because LibreOffice Calc omits hidden sheets from PDF export. That keeps the
conversion to a single ``soffice`` call and stops a ledger from becoming
hundreds of rendered pages.
"""

from __future__ import annotations

import hashlib
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from crewmeal.config import AppConfig
from crewmeal.libreoffice import (
    LibreOfficeConversionError,
    convert_document_to_pdf,
    inspect_pdf,
    pages_with_rendered_visuals as _pages_with_rendered_visuals,
)
from crewmeal.models import RendererManifest, SourceManifest
from crewmeal.search_enhancement.formats.base import (
    EncryptedDocumentError,
    InvalidDocumentError,
    PreparedDocument,
    ProcessingFidelityError,
)
from crewmeal.search_enhancement.models import (
    ContentSection,
    ContentTable,
    SlideContent,
    SlideSchedule,
)
from crewmeal.search_enhancement.progress import ProgressReporter, Stage
from crewmeal.search_enhancement.xlsx_semantic import (
    SheetBlock,
    SheetClassification,
    SheetExtraction,
    WorkbookExtraction,
    XlsxSemanticError,
    align_blocks_to_pages,
    attribute_pages_to_sheets,
    classify_sheet,
    data_sheet_summary,
    extract_workbook,
    hide_sheets,
    segment_sheet,
)

XLSX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
XLSM_CONTENT_TYPE = "application/vnd.ms-excel.sheet.macroEnabled.12"

_ZIP_MAGIC = b"PK\x03\x04"
#: Password-protected Office files are OLE2 compound documents wrapping the
#: encrypted OOXML package, not ZIPs.
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

#: Rendered pages a single document sheet may occupy before it is treated as a
#: data sheet instead. A form does not span twenty pages; a sheet that does had
#: a paper or scaling setting that makes rendering meaningless.
MAX_DOCUMENT_SHEET_PAGES = 20


class XlsxHandler:
    """Detect, validate, and prepare Excel workbooks."""

    format_id = "xlsx"
    display_name = "Excel"
    extensions = frozenset({".xlsx", ".xlsm"})
    content_types = frozenset({XLSX_CONTENT_TYPE, XLSM_CONTENT_TYPE})
    content_type_by_extension = {
        ".xlsx": XLSX_CONTENT_TYPE,
        ".xlsm": XLSM_CONTENT_TYPE,
    }
    supported = True
    pipeline = "시트별 문서형/데이터형 자동 판별"
    summary = (
        "시트마다 성격을 판별해 분기합니다. 견적서·정산서 같은 문서형 시트만 "
        "렌더링해 분석하고, 대장·원장 같은 데이터형 시트는 렌더링 없이 "
        "열 스키마와 대표 행만 요약합니다(비전 호출 없음). "
        "SharePoint가 이미 셀 텍스트를 인덱싱하므로 전사가 아니라 요약이 목표입니다."
    )

    def validate(self, data: bytes, *, filename: str, max_bytes: int) -> None:
        if Path(filename).suffix.lower() not in self.extensions:
            raise InvalidDocumentError(
                "Only .xlsx and .xlsm files are supported by the Excel handler."
            )
        if not data:
            raise InvalidDocumentError("The uploaded Excel workbook is empty.")
        if len(data) > max_bytes:
            raise InvalidDocumentError(
                f"The Excel workbook exceeds the {max_bytes // (1024 * 1024)} MB limit."
            )
        if data.startswith(_OLE2_MAGIC):
            raise EncryptedDocumentError(
                "The Excel workbook appears to be password-protected. Enable a "
                "decryption provider to process it."
            )
        if not data.startswith(_ZIP_MAGIC):
            raise InvalidDocumentError("The file is not a valid Excel package.")

    def fingerprint(self, data: bytes) -> str:
        return f"xlsx-sha256:{hashlib.sha256(data).hexdigest()}"

    def prepare(
        self,
        data: bytes,
        *,
        source_name: str,
        config: AppConfig,
        reporter: ProgressReporter,
    ) -> PreparedDocument:
        source_started = time.perf_counter()
        reporter.stage(Stage.CONVERTING, message="OOXML 시트·표 추출")
        try:
            extraction = extract_workbook(data)
        except XlsxSemanticError as exc:
            raise InvalidDocumentError(
                f"The Excel workbook could not be read: {exc}"
            ) from exc
        semantic_seconds = time.perf_counter() - source_started

        classifications = {
            sheet.name: classify_sheet(sheet) for sheet in extraction.sheets
        }
        document_sheets = [
            sheet
            for sheet in extraction.sheets
            if classifications[sheet.name].is_document and not sheet.is_hidden
        ]

        if config.low_tier_enabled or not document_sheets:
            return self._prepare_without_rendering(
                extraction,
                classifications=classifications,
                semantic_seconds=semantic_seconds,
                config=config,
                reporter=reporter,
                low_tier=config.low_tier_enabled,
            )
        return self._prepare_semantic_first(
            data,
            extraction=extraction,
            classifications=classifications,
            document_sheets=document_sheets,
            semantic_seconds=semantic_seconds,
            source_name=source_name,
            config=config,
            reporter=reporter,
        )

    def _prepare_without_rendering(
        self,
        extraction: WorkbookExtraction,
        *,
        classifications: dict[str, SheetClassification],
        semantic_seconds: float,
        config: AppConfig,
        reporter: ProgressReporter,
        low_tier: bool,
    ) -> PreparedDocument:
        """One summary unit per sheet, with no LibreOffice and no Vision.

        This is both the low-quality tier and the common case of a workbook
        that is pure data: there is nothing a rendering would add that the
        schema and a sample of rows do not already say.
        """

        reporter.stage(
            Stage.RENDERING,
            message=(
                "저품질 티어: Vision 렌더링 건너뜀"
                if low_tier
                else "데이터 시트만 있어 렌더링 없이 요약합니다"
            ),
            detail={"total": 0},
        )
        slides: list[SlideContent] = []
        texts_by_page: dict[int, tuple[str, ...]] = {}
        for position, sheet in enumerate(extraction.sheets, start=1):
            slide, texts = self._summary_slide(
                sheet,
                classification=classifications[sheet.name],
                unit=position,
                extra_warnings=(
                    extraction.warnings if position == 1 else ()
                ),
            )
            slides.append(slide)
            texts_by_page[position] = texts

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
                "semanticExtractionSeconds": semantic_seconds,
                **({"lowTierExtractionSeconds": semantic_seconds} if low_tier else {}),
                "renderingSeconds": 0.0,
            },
            semantic_slides=tuple(slides),
        )

    def _prepare_semantic_first(
        self,
        data: bytes,
        *,
        extraction: WorkbookExtraction,
        classifications: dict[str, SheetClassification],
        document_sheets: list[SheetExtraction],
        semantic_seconds: float,
        source_name: str,
        config: AppConfig,
        reporter: ProgressReporter,
    ) -> PreparedDocument:
        suffix = Path(source_name).suffix.lower() or ".xlsx"
        rendered_names = {sheet.name for sheet in document_sheets}
        hidden_names = [
            sheet.name
            for sheet in extraction.sheets
            if sheet.name not in rendered_names
        ]
        payload = hide_sheets(data, hidden_names) if hidden_names else data

        with tempfile.TemporaryDirectory(prefix="crewmeal-xlsx-") as workspace:
            workspace_path = Path(workspace)
            workbook_path = workspace_path / f"input{suffix}"
            workbook_path.write_bytes(payload)

            reporter.stage(Stage.CONVERTING, message="LibreOffice XLSX→PDF")
            try:
                conversion = convert_document_to_pdf(
                    workbook_path,
                    workspace_path,
                    soffice_path=config.require_soffice(),
                    pdf_filter="pdf:calc_pdf_Export",
                )
            except LibreOfficeConversionError as exc:
                raise ProcessingFidelityError(
                    f"LibreOffice could not lay out the Excel workbook: {exc}"
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
                "LibreOffice produced an Excel rendering with no pages."
            )

        pages_by_sheet, warnings = self._pages_by_sheet(
            document_sheets, rendered.texts_by_page
        )
        slides, texts_by_page, visual_units, page_by_unit = self._build_units(
            extraction,
            classifications=classifications,
            pages_by_sheet=pages_by_sheet,
            rendered_texts=rendered.texts_by_page,
            workbook_warnings=(*extraction.warnings, *warnings),
        )

        # Vision reports results by unit number, so the rendered page images
        # have to be re-keyed from PDF page to unit before they are published.
        unit_by_page = {page: unit for unit, page in page_by_unit.items()}
        visual_units = visual_units | {
            unit_by_page[page]
            for page in rendered_visual_pages
            if page in unit_by_page
        }
        page_images = {
            unit_by_page[page]: image
            for page, image in rendered.page_images.items()
            if page in unit_by_page and unit_by_page[page] in visual_units
        }
        if not page_images:
            reporter.stage(
                Stage.RENDERING,
                message="semantic coverage complete; Vision skipped",
                detail={"total": 0},
            )

        renderer_manifest = replace(
            rendered,
            page_count=len(slides),
            texts_by_page=dict(texts_by_page),
            links_by_page={unit: () for unit in range(1, len(slides) + 1)},
            page_images=page_images,
        )
        return PreparedDocument(
            source_manifest=self._source_manifest(
                extraction, texts_by_page, len(slides)
            ),
            renderer_manifest=renderer_manifest,
            geometry_by_page={},
            stage_timings={
                "sourceInspectionSeconds": semantic_seconds,
                "conversionSeconds": conversion.conversion_seconds,
                "semanticExtractionSeconds": semantic_seconds,
                "renderingSeconds": rendering_seconds,
            },
            semantic_slides=tuple(slides),
        )

    def _pages_by_sheet(
        self,
        document_sheets: list[SheetExtraction],
        texts_by_page: dict[int, tuple[str, ...]],
    ) -> tuple[dict[str, tuple[int, ...]], tuple[str, ...]]:
        assignment = attribute_pages_to_sheets(document_sheets, texts_by_page)
        pages_by_sheet: dict[str, list[int]] = {
            sheet.name: [] for sheet in document_sheets
        }
        for page in sorted(assignment):
            pages_by_sheet[document_sheets[assignment[page]].name].append(page)

        warnings: list[str] = []
        for sheet in document_sheets:
            pages = pages_by_sheet[sheet.name]
            if not pages:
                warnings.append(
                    f"'{sheet.name}' 시트를 렌더링 페이지와 연결하지 못했습니다."
                )
            elif len(pages) > MAX_DOCUMENT_SHEET_PAGES:
                warnings.append(
                    f"'{sheet.name}' 시트가 {len(pages)}페이지로 렌더링되어 "
                    "문서형으로 보기 어렵습니다. 용지·배율 설정을 확인하세요."
                )
        return {
            name: tuple(pages) for name, pages in pages_by_sheet.items()
        }, tuple(warnings)

    def _build_units(
        self,
        extraction: WorkbookExtraction,
        *,
        classifications: dict[str, SheetClassification],
        pages_by_sheet: dict[str, tuple[int, ...]],
        rendered_texts: dict[int, tuple[str, ...]],
        workbook_warnings: tuple[str, ...],
    ) -> tuple[
        tuple[SlideContent, ...],
        dict[int, tuple[str, ...]],
        set[int],
        dict[int, int],
    ]:
        """Lay sheets out as a contiguous unit sequence in workbook order.

        ``processor.merge_semantic_and_visual`` requires every page Vision
        reports to exist as a semantic unit, so a document sheet contributes
        exactly as many units as it produced rendered pages and a data sheet
        contributes exactly one. Because the two interleave, the unit number is
        not the PDF page number, and the mapping between them is returned so
        page images can be re-keyed.
        """

        slides: list[SlideContent] = []
        texts_by_page: dict[int, tuple[str, ...]] = {}
        visual_units: set[int] = set()
        page_by_unit: dict[int, int] = {}
        unit = 0
        pending_warnings = list(workbook_warnings)

        for sheet in extraction.sheets:
            classification = classifications[sheet.name]
            pages = pages_by_sheet.get(sheet.name, ())
            if not classification.is_document or not pages:
                unit += 1
                slide, texts = self._summary_slide(
                    sheet,
                    classification=classification,
                    unit=unit,
                    extra_warnings=tuple(pending_warnings),
                )
                pending_warnings.clear()
                slides.append(slide)
                texts_by_page[unit] = texts
                continue

            blocks = segment_sheet(sheet)
            assignment, alignment_warnings = align_blocks_to_pages(
                blocks, rendered_texts, pages
            )
            pending_warnings.extend(alignment_warnings)
            for offset, page in enumerate(pages):
                unit += 1
                page_by_unit[unit] = page
                slide, texts, needs_vision = self._document_slide(
                    sheet,
                    blocks=[
                        block
                        for order, block in enumerate(blocks)
                        if assignment.get(order) == page
                    ],
                    unit=unit,
                    page_position=offset,
                    page_total=len(pages),
                    extra_warnings=tuple(pending_warnings),
                )
                pending_warnings.clear()
                if needs_vision:
                    visual_units.add(unit)
                slides.append(slide)
                texts_by_page[unit] = texts

        if pending_warnings and slides:
            slides[0] = replace(
                slides[0],
                warnings=tuple(
                    dict.fromkeys((*slides[0].warnings, *pending_warnings))
                ),
            )
        return tuple(slides), texts_by_page, visual_units, page_by_unit

    def _document_slide(
        self,
        sheet: SheetExtraction,
        *,
        blocks: list[SheetBlock],
        unit: int,
        page_position: int,
        page_total: int,
        extra_warnings: tuple[str, ...],
    ) -> tuple[SlideContent, tuple[str, ...], bool]:
        lines: list[str] = []
        tables: list[ContentTable] = []
        warnings: list[str] = list(extra_warnings)
        needs_vision = False

        for block in blocks:
            if block.kind == "text":
                lines.extend(block.lines)
                continue
            if block.table is None:
                continue
            tables.append(block.table)
            if block.complexity is not None and block.complexity.is_complex:
                needs_vision = True
                warnings.append(
                    f"{block.table.title}은(는) "
                    f"{', '.join(block.complexity.reasons())} 때문에 "
                    "직사각형으로 평탄화했으며 시각 분석으로 교차 확인합니다."
                )

        charts = list(sheet.charts) if page_position == 0 else []
        if page_position == 0 and sheet.has_visuals:
            needs_vision = True
        for alt_text in sheet.alt_texts if page_position == 0 else ():
            lines.append(f"[이미지 설명] {alt_text}")
        if page_position == 0:
            warnings.extend(sheet.warnings)

        sections: list[ContentSection] = []
        if lines:
            sections.append(
                ContentSection(
                    heading="본문", paragraphs=tuple(lines), bullets=()
                )
            )

        suffix = f" ({page_position + 1}/{page_total})" if page_total > 1 else ""
        # A form whose title bar repeats the sheet name is the common case, so
        # the two are not stapled together.
        headline = lines[0] if lines and lines[0] != sheet.name else ""
        title = (
            f"{sheet.name}{suffix}"
            if not headline
            else f"{sheet.name}{suffix} · {headline}"
        )
        return (
            SlideContent(
                slide_number=unit,
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
            ),
            tuple(lines),
            needs_vision,
        )

    def _summary_slide(
        self,
        sheet: SheetExtraction,
        *,
        classification: SheetClassification,
        unit: int,
        extra_warnings: tuple[str, ...],
    ) -> tuple[SlideContent, tuple[str, ...]]:
        sections, tables = data_sheet_summary(sheet, classification)
        warnings = (*sheet.warnings, *extra_warnings)
        texts = tuple(
            paragraph
            for section in sections
            for paragraph in (*section.paragraphs, *section.bullets)
        )
        return (
            SlideContent(
                slide_number=unit,
                title=f"{sheet.name} (데이터 시트 요약)",
                summary="",
                facts=(),
                sections=sections,
                hierarchies=(),
                schedule=SlideSchedule(time_axis=(), tasks=(), milestones=()),
                flows=(),
                tables=tables,
                charts=sheet.charts,
                relationships=(),
                images=(),
                warnings=tuple(dict.fromkeys(warnings)),
            ),
            texts,
        )

    def _source_manifest(
        self,
        extraction: WorkbookExtraction,
        texts_by_page: dict[int, tuple[str, ...]],
        page_count: int,
    ) -> SourceManifest:
        alt_texts = tuple(
            dict.fromkeys(
                alt_text
                for sheet in extraction.sheets
                for alt_text in sheet.alt_texts
            )
        )
        return SourceManifest(
            slide_count=page_count,
            texts_by_slide=dict(texts_by_page),
            links_by_slide={page: () for page in range(1, page_count + 1)},
            alt_text_by_slide={
                page: (alt_texts if page == 1 else ())
                for page in range(1, page_count + 1)
            },
            notes_by_slide={},
        )

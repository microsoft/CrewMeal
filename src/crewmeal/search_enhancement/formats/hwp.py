"""Semantic-first Hancom Office (.hwp / .hwpx) document handler.

The pinned rhwp engine emits page RenderTree JSON with text, tables, headers,
footers, notes, and visual-object boundaries. CrewMeal normalizes that evidence
directly and renders PNG only for pages containing objects without semantic
payload, such as images or equations.
"""

from __future__ import annotations

import hashlib
import tempfile
import time
from pathlib import Path

from crewmeal.config import AppConfig
from crewmeal.models import RendererManifest, SourceManifest
from crewmeal.rhwp import (
    RhwpEncryptedError,
    RhwpError,
    RhwpInvalidFileError,
    export_png_pages,
    extract_render_trees,
)
from crewmeal.search_enhancement.formats.base import (
    EncryptedDocumentError,
    InvalidDocumentError,
    PreparedDocument,
    ProcessingFidelityError,
)
from crewmeal.search_enhancement.progress import ProgressReporter, Stage
from crewmeal.search_enhancement.rhwp_semantic import (
    RhwpSemanticError,
    extract_semantic_content,
)

HWP_CONTENT_TYPE = "application/x-hwp"
HWPX_CONTENT_TYPE = "application/vnd.hancom.hwpx"

_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MAGIC = b"PK\x03\x04"
_HWP3_MAGIC = b"HWP Document File"


class HwpHandler:
    """Detect, validate, and prepare HWP 5.x and HWPX documents with rhwp."""

    format_id = "hwp"
    display_name = "한글(HWP/HWPX)"
    extensions = frozenset({".hwp", ".hwpx"})
    content_types = frozenset(
        {
            HWP_CONTENT_TYPE,
            HWPX_CONTENT_TYPE,
            "application/haansofthwp",
            "application/vnd.hancom.hwp",
            "application/hwp+zip",
        }
    )
    supported = True

    def validate(self, data: bytes, *, filename: str, max_bytes: int) -> None:
        suffix = Path(filename).suffix.lower()
        if suffix not in self.extensions:
            raise InvalidDocumentError(
                "Only .hwp and .hwpx files are supported by the HWP handler."
            )
        if not data:
            raise InvalidDocumentError("The uploaded HWP document is empty.")
        if len(data) > max_bytes:
            raise InvalidDocumentError(
                f"The HWP document exceeds the {max_bytes // (1024 * 1024)} MB limit."
            )
        if suffix == ".hwpx":
            if not data.startswith(_ZIP_MAGIC):
                raise InvalidDocumentError("The file is not a valid HWPX package.")
            return
        if data.startswith(_HWP3_MAGIC):
            raise InvalidDocumentError(
                "Legacy HWP 3.x documents are not supported; HWP 5.x is required."
            )
        if not data.startswith(_OLE2_MAGIC):
            raise InvalidDocumentError(
                "The file is not a recognized HWP 5.x document."
            )

    def fingerprint(self, data: bytes) -> str:
        return f"hwp-sha256:{hashlib.sha256(data).hexdigest()}"

    def prepare(
        self,
        data: bytes,
        *,
        source_name: str,
        config: AppConfig,
        reporter: ProgressReporter,
    ) -> PreparedDocument:
        source_started = time.perf_counter()
        suffix = Path(source_name).suffix.lower() or ".hwp"
        with tempfile.TemporaryDirectory(prefix="crewmeal-hwp-") as workspace:
            workspace_path = Path(workspace)
            hwp_path = workspace_path / f"input{suffix}"
            hwp_path.write_bytes(data)
            source_seconds = time.perf_counter() - source_started

            reporter.stage(Stage.CONVERTING, message="rhwp RenderTree semantic 추출")
            try:
                render_tree = extract_render_trees(
                    hwp_path,
                    workspace_path / "render-tree",
                    rhwp_path=config.require_rhwp(),
                    timeout_seconds=config.rhwp_timeout_seconds,
                )
                semantic = extract_semantic_content(
                    render_tree.pages,
                    engine_warnings=render_tree.warnings,
                )
                if semantic.visual_pages:
                    reporter.stage(
                        Stage.RENDERING,
                        message="rhwp visual-only 페이지 렌더링",
                        detail={"total": len(semantic.visual_pages)},
                    )
                    png = export_png_pages(
                        hwp_path,
                        workspace_path / "png",
                        tuple(semantic.visual_pages),
                        rhwp_path=config.require_rhwp(),
                        dpi=config.slide_image_render_dpi,
                        timeout_seconds=config.rhwp_timeout_seconds,
                    )
                else:
                    reporter.stage(
                        Stage.RENDERING,
                        message="semantic coverage complete; PNG skipped",
                        detail={"total": 0},
                    )
                    png = None
            except RhwpEncryptedError as exc:
                raise EncryptedDocumentError(
                    "The HWP document is encrypted and rhwp cannot decrypt it."
                ) from exc
            except (RhwpInvalidFileError, RhwpSemanticError) as exc:
                raise InvalidDocumentError(
                    f"rhwp rejected the HWP/HWPX document: {exc}"
                ) from exc
            except RhwpError as exc:
                raise ProcessingFidelityError(
                    f"rhwp processing failed: {exc}"
                ) from exc

        page_images = dict(png.page_images) if png is not None else {}
        rendering_seconds = png.elapsed_seconds if png is not None else 0.0
        page_count = len(semantic.slides)
        renderer_manifest = RendererManifest(
            page_count=page_count,
            texts_by_page=dict(semantic.texts_by_page),
            links_by_page={page: () for page in range(1, page_count + 1)},
            page_images=page_images,
            render_dpi=config.slide_image_render_dpi,
        )
        source_manifest = SourceManifest(
            slide_count=page_count,
            texts_by_slide=dict(semantic.texts_by_page),
            links_by_slide={page: () for page in range(1, page_count + 1)},
            alt_text_by_slide={},
            notes_by_slide={},
            element_counts_by_slide=dict(semantic.element_counts_by_page),
        )
        return PreparedDocument(
            source_manifest=source_manifest,
            renderer_manifest=renderer_manifest,
            geometry_by_page={},
            stage_timings={
                "sourceInspectionSeconds": source_seconds,
                "conversionSeconds": 0.0,
                "semanticExtractionSeconds": render_tree.elapsed_seconds,
                "renderingSeconds": rendering_seconds,
            },
            semantic_slides=semantic.slides,
        )

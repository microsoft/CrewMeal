"""Hancom Office (.hwp / .hwpx) document handler.

HWP is the dominant document format in Korean public-sector and enterprise
SharePoint libraries. HWP documents are not page-based on their own, so we lean
on LibreOffice's HWP import filter to convert to PDF, then reuse the shared
PyMuPDF renderer and the common vision-analysis pipeline (the same path PDF and
PPTX take once a PDF exists).

Two container shapes are supported:

* ``.hwp``  — HWP 5.x binary (an OLE2 compound file) or the legacy HWP 3.x
  ``HWP Document File`` signature.
* ``.hwpx`` — the OOXML-style ZIP package (``PK\\x03\\x04``).
"""

from __future__ import annotations

import hashlib
import tempfile
import time
from pathlib import Path

from crewmeal.config import AppConfig
from crewmeal.libreoffice import convert_hwp_to_pdf, inspect_pdf
from crewmeal.models import SourceManifest
from crewmeal.search_enhancement.formats.base import (
    InvalidDocumentError,
    PreparedDocument,
)
from crewmeal.search_enhancement.progress import ProgressReporter, Stage

HWP_CONTENT_TYPE = "application/x-hwp"
HWPX_CONTENT_TYPE = "application/vnd.hancom.hwpx"

# OLE2 compound-document magic used by HWP 5.x binary files.
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
# ZIP magic used by the HWPX package.
_ZIP_MAGIC = b"PK\x03\x04"
# Legacy HWP 3.x signature.
_HWP3_MAGIC = b"HWP Document File"


class HwpHandler:
    """Detect, validate, and prepare Hancom HWP/HWPX documents."""

    format_id = "hwp"
    display_name = "한글(HWP)"
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
        else:  # .hwp
            if not (
                data.startswith(_OLE2_MAGIC) or data.startswith(_HWP3_MAGIC)
            ):
                raise InvalidDocumentError(
                    "The file is not a recognized HWP document."
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

            reporter.stage(Stage.CONVERTING, message="LibreOffice HWP→PDF")
            conversion = convert_hwp_to_pdf(
                hwp_path,
                workspace_path,
                soffice_path=config.require_soffice(),
            )

            rendering_started = time.perf_counter()
            reporter.stage(Stage.RENDERING)
            renderer_manifest = inspect_pdf(
                conversion.pdf_path,
                render_dpi=config.slide_image_render_dpi,
            )
            rendering_seconds = time.perf_counter() - rendering_started

        source_manifest = SourceManifest(
            slide_count=renderer_manifest.page_count,
            texts_by_slide=dict(renderer_manifest.texts_by_page),
            links_by_slide=dict(renderer_manifest.links_by_page),
            alt_text_by_slide={},
            notes_by_slide={},
        )

        return PreparedDocument(
            source_manifest=source_manifest,
            renderer_manifest=renderer_manifest,
            geometry_by_page={},
            stage_timings={
                "sourceInspectionSeconds": source_seconds,
                "conversionSeconds": conversion.conversion_seconds,
                "renderingSeconds": rendering_seconds,
            },
        )

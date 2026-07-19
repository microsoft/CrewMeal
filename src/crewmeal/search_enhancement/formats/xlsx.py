"""Skeleton handler for Excel workbooks (.xlsx).

Registered so the format is discoverable (detection, admin settings UI) but not
yet implemented: extraction/rendering will land in a later phase. Until then the
handler advertises ``supported = False`` so the registry keeps it out of the
active ingest allow-list, and :meth:`prepare` fails loudly if reached.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from crewmeal.config import AppConfig
from crewmeal.search_enhancement.formats.base import (
    InvalidDocumentError,
    PreparedDocument,
)
from crewmeal.search_enhancement.progress import ProgressReporter

XLSX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)

_ZIP_MAGIC = b"PK\x03\x04"


class XlsxHandler:
    """Detect and validate Excel workbooks; extraction is not yet implemented."""

    format_id = "xlsx"
    display_name = "Excel"
    extensions = frozenset({".xlsx"})
    content_types = frozenset({XLSX_CONTENT_TYPE})
    supported = False

    def validate(self, data: bytes, *, filename: str, max_bytes: int) -> None:
        if Path(filename).suffix.lower() != ".xlsx":
            raise InvalidDocumentError(
                "Only .xlsx files are supported by the Excel handler."
            )
        if not data:
            raise InvalidDocumentError("The uploaded Excel workbook is empty.")
        if len(data) > max_bytes:
            raise InvalidDocumentError(
                f"The Excel workbook exceeds the {max_bytes // (1024 * 1024)} MB limit."
            )
        if not data.startswith(_ZIP_MAGIC):
            raise InvalidDocumentError("The file is not a valid .xlsx package.")

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
        raise NotImplementedError(
            "Excel (.xlsx) enrichment is not implemented yet. Enable it once the "
            "XlsxHandler pipeline lands."
        )

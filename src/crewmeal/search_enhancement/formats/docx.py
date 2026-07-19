"""Skeleton handler for Word documents (.docx).

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

DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

_ZIP_MAGIC = b"PK\x03\x04"


class DocxHandler:
    """Detect and validate Word documents; extraction is not yet implemented."""

    format_id = "docx"
    display_name = "Word"
    extensions = frozenset({".docx"})
    content_types = frozenset({DOCX_CONTENT_TYPE})
    supported = False

    def validate(self, data: bytes, *, filename: str, max_bytes: int) -> None:
        if Path(filename).suffix.lower() != ".docx":
            raise InvalidDocumentError(
                "Only .docx files are supported by the Word handler."
            )
        if not data:
            raise InvalidDocumentError("The uploaded Word document is empty.")
        if len(data) > max_bytes:
            raise InvalidDocumentError(
                f"The Word document exceeds the {max_bytes // (1024 * 1024)} MB limit."
            )
        if not data.startswith(_ZIP_MAGIC):
            raise InvalidDocumentError("The file is not a valid .docx package.")

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
        raise NotImplementedError(
            "Word (.docx) enrichment is not implemented yet. Enable it once the "
            "DocxHandler pipeline lands."
        )

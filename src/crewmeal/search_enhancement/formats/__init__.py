"""Pluggable document-format handlers for the enrichment pipeline.

Importing this package registers every built-in handler. Callers use the
registry helpers (``detect_handler``, ``supported_extensions``,
``content_fingerprint``) to route documents by format instead of assuming
PowerPoint.
"""

from __future__ import annotations

from crewmeal.search_enhancement.formats.base import (
    DocumentHandler,
    EncryptedDocumentError,
    InvalidDocumentError,
    PreparedDocument,
    ProcessingFidelityError,
    UnsupportedFormatError,
)
from crewmeal.search_enhancement.formats.docx import DocxHandler
from crewmeal.search_enhancement.formats.hwp import HwpHandler
from crewmeal.search_enhancement.formats.pdf import PdfHandler
from crewmeal.search_enhancement.formats.pptx import PptxHandler
from crewmeal.search_enhancement.formats.xlsx import XlsxHandler
from crewmeal.search_enhancement.formats.registry import (
    active_handlers,
    all_handlers,
    content_fingerprint,
    detect_handler,
    enabled_content_types,
    enabled_extensions,
    enabled_handlers,
    format_setting_key,
    format_status,
    is_format_enabled,
    register,
    supported_content_types,
    supported_extensions,
)

register(PptxHandler())
register(PdfHandler())
register(HwpHandler())
register(DocxHandler())
register(XlsxHandler())

__all__ = [
    "DocumentHandler",
    "EncryptedDocumentError",
    "InvalidDocumentError",
    "PreparedDocument",
    "ProcessingFidelityError",
    "UnsupportedFormatError",
    "DocxHandler",
    "HwpHandler",
    "PdfHandler",
    "PptxHandler",
    "XlsxHandler",
    "active_handlers",
    "all_handlers",
    "content_fingerprint",
    "detect_handler",
    "enabled_content_types",
    "enabled_extensions",
    "enabled_handlers",
    "format_setting_key",
    "format_status",
    "is_format_enabled",
    "register",
    "supported_content_types",
    "supported_extensions",
]

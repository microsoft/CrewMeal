"""Excel is still a registered-but-unimplemented skeleton.

The registry must keep advertising the format (so detection reports "not yet
implemented" instead of "unknown format") while keeping it out of every set that
drives ingestion, uploads, and the admin toggles.
"""

import pytest

from crewmeal.config import DEFAULT_MAX_UPLOAD_BYTES, AppConfig
from crewmeal.search_enhancement.formats import (
    UnsupportedFormatError,
    all_handlers,
    detect_handler,
    enabled_extensions,
    format_setting_key,
    format_status,
    is_format_enabled,
    supported_extensions,
)
from crewmeal.search_enhancement.formats.xlsx import XlsxHandler
from crewmeal.search_enhancement.progress import NullProgressReporter

_ZIP_MAGIC = b"PK\x03\x04"


def _config() -> AppConfig:
    return AppConfig(
        endpoint=None,
        max_upload_bytes=DEFAULT_MAX_UPLOAD_BYTES,
        soffice_path=None,
        slide_image_render_dpi=96,
    )


def test_skeleton_format_is_registered_but_unsupported():
    handlers = {handler.format_id: handler for handler in all_handlers()}
    assert "xlsx" in handlers
    assert handlers["xlsx"].supported is False


def test_skeleton_format_not_in_active_or_enabled_sets():
    assert ".xlsx" not in supported_extensions()
    assert ".xlsx" not in enabled_extensions({})


def test_detect_handler_reports_not_yet_implemented():
    with pytest.raises(UnsupportedFormatError, match="not yet"):
        detect_handler("budget.xlsx")


def test_skeleton_format_can_never_be_enabled():
    forced_on = {format_setting_key("xlsx"): True}
    assert is_format_enabled("xlsx", forced_on) is False


def test_format_status_marks_skeleton_unsupported():
    rows = {row["format_id"]: row for row in format_status({})}
    assert rows["xlsx"]["supported"] is False
    assert rows["xlsx"]["enabled"] is False


def test_skeleton_prepare_raises_not_implemented():
    handler = XlsxHandler()
    with pytest.raises(NotImplementedError):
        handler.prepare(
            _ZIP_MAGIC + b"\x00" * 32,
            source_name="x.xlsx",
            config=_config(),
            reporter=NullProgressReporter(),
        )

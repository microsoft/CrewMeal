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
from crewmeal.search_enhancement.formats.docx import DocxHandler
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


@pytest.mark.parametrize("format_id", ["docx", "xlsx"])
def test_skeleton_formats_are_registered_but_unsupported(format_id):
    handlers = {handler.format_id: handler for handler in all_handlers()}
    assert format_id in handlers
    assert handlers[format_id].supported is False


@pytest.mark.parametrize("filename", ["memo.docx", "budget.xlsx"])
def test_skeleton_formats_not_in_active_or_enabled_sets(filename):
    handler_ext = "." + filename.rsplit(".", 1)[1]
    assert handler_ext not in supported_extensions()
    assert handler_ext not in enabled_extensions({})


@pytest.mark.parametrize("filename", ["memo.docx", "budget.xlsx"])
def test_detect_handler_reports_not_yet_implemented(filename):
    with pytest.raises(UnsupportedFormatError, match="not yet"):
        detect_handler(filename)


@pytest.mark.parametrize("format_id", ["docx", "xlsx"])
def test_skeleton_formats_can_never_be_enabled(format_id):
    forced_on = {format_setting_key(format_id): True}
    assert is_format_enabled(format_id, forced_on) is False


def test_format_status_marks_skeletons_unsupported():
    rows = {row["format_id"]: row for row in format_status({})}
    for format_id in ("docx", "xlsx"):
        assert rows[format_id]["supported"] is False
        assert rows[format_id]["enabled"] is False


@pytest.mark.parametrize("handler_cls", [DocxHandler, XlsxHandler])
def test_skeleton_prepare_raises_not_implemented(handler_cls):
    handler = handler_cls()
    with pytest.raises(NotImplementedError):
        handler.prepare(
            _ZIP_MAGIC + b"\x00" * 32,
            source_name=f"x.{handler.format_id}",
            config=_config(),
            reporter=NullProgressReporter(),
        )

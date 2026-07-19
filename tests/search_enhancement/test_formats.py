import io
import zipfile

import pytest

from crewmeal.search_enhancement.formats import (
    UnsupportedFormatError,
    content_fingerprint,
    detect_handler,
    enabled_content_types,
    enabled_extensions,
    format_setting_key,
    format_status,
    is_format_enabled,
    register,
    supported_content_types,
    supported_extensions,
)
from crewmeal.search_enhancement.formats import registry
from crewmeal.search_enhancement.formats.pptx import PPTX_CONTENT_TYPE, PptxHandler
from crewmeal.source import pptx_content_fingerprint


def _pptx_bytes(marker: str = "x") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", "<Types/>")
        package.writestr("ppt/presentation.xml", "<p:presentation/>")
        package.writestr("ppt/_rels/presentation.xml.rels", "<Relationships/>")
        package.writestr("ppt/slides/slide1.xml", f"<slide>{marker}</slide>")
    return output.getvalue()


@pytest.fixture
def restore_registry():
    saved = list(registry.all_handlers())
    try:
        yield
    finally:
        registry._HANDLERS[:] = saved


def test_pptx_handler_is_registered_and_active():
    handlers = {handler.format_id: handler for handler in registry.all_handlers()}
    assert "pptx" in handlers
    assert handlers["pptx"].supported is True
    assert ".pptx" in supported_extensions()
    assert PPTX_CONTENT_TYPE in supported_content_types()


def test_detect_handler_by_extension():
    handler = detect_handler("quarterly-review.pptx")
    assert handler.format_id == "pptx"


def test_detect_handler_rejects_unknown_extension():
    with pytest.raises(UnsupportedFormatError):
        detect_handler("notes.txt")


def test_content_fingerprint_matches_legacy_pptx_value():
    data = _pptx_bytes("fingerprint")
    assert content_fingerprint(data, filename="deck.pptx") == pptx_content_fingerprint(
        data
    )


def test_register_is_idempotent_by_format_id():
    before = len(registry.all_handlers())
    register(PptxHandler())
    assert len(registry.all_handlers()) == before


def test_registered_but_inactive_format_reports_clearly(restore_registry):
    class _DraftHandler:
        format_id = "draft"
        display_name = "Draft Format"
        extensions = frozenset({".draft"})
        content_types = frozenset()
        supported = False

        def validate(self, data, *, filename, max_bytes):  # pragma: no cover
            raise NotImplementedError

        def fingerprint(self, data):  # pragma: no cover
            raise NotImplementedError

        def prepare(self, data, *, source_name, config, reporter):  # pragma: no cover
            raise NotImplementedError

    register(_DraftHandler())
    assert ".draft" not in supported_extensions()
    with pytest.raises(UnsupportedFormatError, match="not yet"):
        detect_handler("plan.draft")


def test_implemented_formats_enabled_by_default_without_settings():
    assert is_format_enabled("pptx", {}) is True
    assert is_format_enabled("pdf", {}) is True
    assert ".pptx" in enabled_extensions({})
    assert ".pdf" in enabled_extensions({})


def test_admin_can_disable_an_implemented_format():
    settings = {format_setting_key("pdf"): False}
    assert is_format_enabled("pdf", settings) is False
    assert ".pdf" not in enabled_extensions(settings)
    # Disabling one format must not affect the others.
    assert is_format_enabled("pptx", settings) is True
    assert ".pptx" in enabled_extensions(settings)


def test_enabled_setting_accepts_string_values():
    assert is_format_enabled("pdf", {format_setting_key("pdf"): "false"}) is False
    assert is_format_enabled("pdf", {format_setting_key("pdf"): "on"}) is True


def test_enabled_content_types_follows_toggle():
    assert enabled_content_types({}) == supported_content_types()
    disabled = {format_setting_key("pdf"): False}
    assert enabled_content_types(disabled) < supported_content_types()


def test_skeleton_format_can_never_be_enabled(restore_registry):
    class _DraftHandler:
        format_id = "draft"
        display_name = "Draft Format"
        extensions = frozenset({".draft"})
        content_types = frozenset()
        supported = False

        def validate(self, data, *, filename, max_bytes):  # pragma: no cover
            raise NotImplementedError

        def fingerprint(self, data):  # pragma: no cover
            raise NotImplementedError

        def prepare(self, data, *, source_name, config, reporter):  # pragma: no cover
            raise NotImplementedError

    register(_DraftHandler())
    # Even if an operator forces the setting on, an unimplemented format stays off.
    assert is_format_enabled("draft", {format_setting_key("draft"): True}) is False
    assert ".draft" not in enabled_extensions({format_setting_key("draft"): True})


def test_format_status_reports_supported_and_enabled_flags():
    rows = {row["format_id"]: row for row in format_status({})}
    assert rows["pdf"]["supported"] is True
    assert rows["pdf"]["enabled"] is True

    disabled_rows = {
        row["format_id"]: row
        for row in format_status({format_setting_key("pdf"): False})
    }
    assert disabled_rows["pdf"]["supported"] is True
    assert disabled_rows["pdf"]["enabled"] is False

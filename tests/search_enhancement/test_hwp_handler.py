import fitz
import pytest

from pathlib import Path

from crewmeal.config import DEFAULT_MAX_UPLOAD_BYTES, AppConfig
from crewmeal.libreoffice import ConversionResult
from crewmeal.search_enhancement.formats import detect_handler
from crewmeal.search_enhancement.formats import hwp as hwp_module
from crewmeal.search_enhancement.formats.base import InvalidDocumentError
from crewmeal.search_enhancement.formats.hwp import HwpHandler
from crewmeal.search_enhancement.progress import NullProgressReporter

_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MAGIC = b"PK\x03\x04"


def _config() -> AppConfig:
    return AppConfig(
        endpoint=None,
        max_upload_bytes=DEFAULT_MAX_UPLOAD_BYTES,
        soffice_path=Path("soffice"),
        slide_image_render_dpi=96,
    )


def _fake_hwp(magic: bytes = _OLE2_MAGIC) -> bytes:
    return magic + b"\x00" * 512


def test_hwp_handler_detected_by_extension():
    assert detect_handler("report.hwp").format_id == "hwp"
    assert detect_handler("report.hwpx").format_id == "hwp"


def test_hwp_validation_accepts_known_signatures():
    handler = HwpHandler()
    handler.validate(
        _fake_hwp(_OLE2_MAGIC), filename="a.hwp", max_bytes=DEFAULT_MAX_UPLOAD_BYTES
    )
    handler.validate(
        _fake_hwp(_ZIP_MAGIC), filename="a.hwpx", max_bytes=DEFAULT_MAX_UPLOAD_BYTES
    )


def test_hwp_validation_rejects_bad_input():
    handler = HwpHandler()
    with pytest.raises(InvalidDocumentError):
        handler.validate(b"", filename="a.hwp", max_bytes=DEFAULT_MAX_UPLOAD_BYTES)
    with pytest.raises(InvalidDocumentError):
        handler.validate(
            b"not hwp", filename="a.hwp", max_bytes=DEFAULT_MAX_UPLOAD_BYTES
        )
    with pytest.raises(InvalidDocumentError):
        handler.validate(
            _fake_hwp(_OLE2_MAGIC),
            filename="a.docx",
            max_bytes=DEFAULT_MAX_UPLOAD_BYTES,
        )
    # .hwpx must be a ZIP package, not an OLE2 binary.
    with pytest.raises(InvalidDocumentError):
        handler.validate(
            _fake_hwp(_OLE2_MAGIC),
            filename="a.hwpx",
            max_bytes=DEFAULT_MAX_UPLOAD_BYTES,
        )


def test_hwp_fingerprint_is_deterministic_and_prefixed():
    handler = HwpHandler()
    data = _fake_hwp()
    fingerprint = handler.fingerprint(data)
    assert fingerprint.startswith("hwp-sha256:")
    assert fingerprint == handler.fingerprint(data)


def test_hwp_prepare_converts_then_renders(monkeypatch, tmp_path):
    """prepare() should convert via LibreOffice then reuse the PDF renderer.

    The LibreOffice call is stubbed to emit a real PDF so the shared renderer /
    manifest-building logic is exercised without a soffice binary or HWP filter.
    """

    def _fake_convert(source_path, output_dir, *, soffice_path, timeout_seconds=180):
        document = fitz.open()
        for text in ("한글 문서 1쪽", "한글 문서 2쪽"):
            page = document.new_page()
            page.insert_text((72, 72), text)
        pdf_path = output_dir / f"{source_path.stem}.pdf"
        document.save(pdf_path)
        document.close()
        return ConversionResult(
            pdf_path=pdf_path,
            conversion_seconds=0.01,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(hwp_module, "convert_hwp_to_pdf", _fake_convert)

    handler = HwpHandler()
    prepared = handler.prepare(
        _fake_hwp(),
        source_name="deck.hwp",
        config=_config(),
        reporter=NullProgressReporter(),
    )

    assert prepared.source_manifest.slide_count == 2
    assert prepared.renderer_manifest.page_count == 2
    assert set(prepared.renderer_manifest.page_images) == {1, 2}
    assert prepared.geometry_by_page == {}

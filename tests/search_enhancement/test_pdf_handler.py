import fitz
import pytest

from crewmeal.config import DEFAULT_MAX_UPLOAD_BYTES, AppConfig
from crewmeal.search_enhancement.formats import detect_handler
from crewmeal.search_enhancement.formats.base import (
    EncryptedDocumentError,
    InvalidDocumentError,
)
from crewmeal.search_enhancement.formats.pdf import PdfHandler
from crewmeal.search_enhancement.progress import NullProgressReporter


def _pdf_bytes(pages: list[str]) -> bytes:
    document = fitz.open()
    for text in pages:
        page = document.new_page()
        page.insert_text((72, 72), text)
    data = document.tobytes()
    document.close()
    return data


def _encrypted_pdf_bytes() -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "secret")
    data = document.tobytes(
        encryption=fitz.PDF_ENCRYPT_AES_256,
        owner_pw="owner",
        user_pw="user",
    )
    document.close()
    return data


def _config() -> AppConfig:
    return AppConfig(
        endpoint=None,
        max_upload_bytes=DEFAULT_MAX_UPLOAD_BYTES,
        soffice_path=None,
        slide_image_render_dpi=96,
    )


def test_pdf_handler_is_detected_by_extension():
    handler = detect_handler("report.pdf")
    assert handler.format_id == "pdf"


def test_pdf_validation_rejects_bad_input():
    handler = PdfHandler()
    with pytest.raises(InvalidDocumentError):
        handler.validate(b"", filename="x.pdf", max_bytes=DEFAULT_MAX_UPLOAD_BYTES)
    with pytest.raises(InvalidDocumentError):
        handler.validate(b"not a pdf", filename="x.pdf", max_bytes=DEFAULT_MAX_UPLOAD_BYTES)
    with pytest.raises(InvalidDocumentError):
        handler.validate(
            _pdf_bytes(["a"]), filename="x.docx", max_bytes=DEFAULT_MAX_UPLOAD_BYTES
        )


def test_pdf_fingerprint_is_deterministic_and_prefixed():
    handler = PdfHandler()
    data = _pdf_bytes(["hello"])
    fingerprint = handler.fingerprint(data)
    assert fingerprint.startswith("pdf-sha256:")
    assert fingerprint == handler.fingerprint(data)


def test_pdf_prepare_renders_pages_and_extracts_text():
    handler = PdfHandler()
    data = _pdf_bytes(["CrewMeal page one", "CrewMeal page two"])
    prepared = handler.prepare(
        data,
        source_name="deck.pdf",
        config=_config(),
        reporter=NullProgressReporter(),
    )
    assert prepared.source_manifest.slide_count == 2
    assert prepared.renderer_manifest.page_count == 2
    assert set(prepared.renderer_manifest.page_images) == {1, 2}
    joined = " ".join(prepared.source_manifest.texts_by_slide[1])
    assert "CrewMeal page one" in joined
    assert prepared.geometry_by_page == {}


def test_pdf_prepare_rejects_encrypted_document():
    handler = PdfHandler()
    with pytest.raises(EncryptedDocumentError):
        handler.prepare(
            _encrypted_pdf_bytes(),
            source_name="secret.pdf",
            config=_config(),
            reporter=NullProgressReporter(),
        )

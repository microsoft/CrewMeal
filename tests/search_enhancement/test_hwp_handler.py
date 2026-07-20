from pathlib import Path

import pytest

from crewmeal.config import DEFAULT_MAX_UPLOAD_BYTES, AppConfig
from crewmeal.rhwp import (
    RhwpEncryptedError,
    RhwpError,
    RhwpPngResult,
    RhwpRenderTreeResult,
)
from crewmeal.search_enhancement.formats import detect_handler
from crewmeal.search_enhancement.formats import hwp as hwp_module
from crewmeal.search_enhancement.formats.base import (
    EncryptedDocumentError,
    InvalidDocumentError,
    ProcessingFidelityError,
)
from crewmeal.search_enhancement.formats.hwp import HwpHandler
from crewmeal.search_enhancement.progress import NullProgressReporter

_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MAGIC = b"PK\x03\x04"
_HWP3_MAGIC = b"HWP Document File"
_PNG = b"\x89PNG\r\n\x1a\nfixture"


def _config() -> AppConfig:
    return AppConfig(
        endpoint=None,
        max_upload_bytes=DEFAULT_MAX_UPLOAD_BYTES,
        soffice_path=Path("soffice"),
        slide_image_render_dpi=96,
        rhwp_path=Path("rhwp"),
    )


def _fake_hwp(magic: bytes = _OLE2_MAGIC) -> bytes:
    return magic + b"\x00" * 512


def _text_line(text: str) -> dict[str, object]:
    return {
        "type": "TextLine",
        "children": [{"type": "TextRun", "text": text}],
    }


def _semantic_page() -> dict[str, object]:
    return {
        "type": "Page",
        "children": [
            {"type": "Header", "children": [_text_line("머리말")]},
            {
                "type": "Body",
                "children": [
                    {
                        "type": "Column",
                        "children": [
                            _text_line("분기 실적 보고"),
                            {
                                "type": "Table",
                                "rows": 2,
                                "cols": 2,
                                "children": [
                                    {
                                        "type": "Cell",
                                        "row": 0,
                                        "col": 0,
                                        "children": [_text_line("구분")],
                                    },
                                    {
                                        "type": "Cell",
                                        "row": 0,
                                        "col": 1,
                                        "children": [_text_line("내용")],
                                    },
                                    {
                                        "type": "Cell",
                                        "row": 1,
                                        "col": 0,
                                        "children": [_text_line("매출")],
                                    },
                                    {
                                        "type": "Cell",
                                        "row": 1,
                                        "col": 1,
                                        "children": [_text_line("1억원")],
                                    },
                                ],
                            },
                        ],
                    }
                ],
            },
            {"type": "FootnoteArea", "children": [_text_line("각주 내용")]},
            {"type": "Footer", "children": [_text_line("꼬리말")]},
        ],
    }


def test_hwp_handler_detected_by_extension() -> None:
    assert detect_handler("report.hwp").format_id == "hwp"
    assert detect_handler("report.hwpx").format_id == "hwp"


def test_hwp_validation_accepts_known_signatures() -> None:
    handler = HwpHandler()
    handler.validate(
        _fake_hwp(_OLE2_MAGIC), filename="a.hwp", max_bytes=DEFAULT_MAX_UPLOAD_BYTES
    )
    handler.validate(
        _fake_hwp(_ZIP_MAGIC), filename="a.hwpx", max_bytes=DEFAULT_MAX_UPLOAD_BYTES
    )


def test_hwp_validation_rejects_bad_or_legacy_input() -> None:
    handler = HwpHandler()
    invalid = (
        (b"", "a.hwp"),
        (b"not hwp", "a.hwp"),
        (_fake_hwp(_OLE2_MAGIC), "a.docx"),
        (_fake_hwp(_OLE2_MAGIC), "a.hwpx"),
        (_fake_hwp(_HWP3_MAGIC), "a.hwp"),
    )
    for data, filename in invalid:
        with pytest.raises(InvalidDocumentError):
            handler.validate(
                data,
                filename=filename,
                max_bytes=DEFAULT_MAX_UPLOAD_BYTES,
            )


def test_hwp_fingerprint_is_deterministic_and_prefixed() -> None:
    handler = HwpHandler()
    data = _fake_hwp()
    fingerprint = handler.fingerprint(data)
    assert fingerprint.startswith("hwp-sha256:")
    assert fingerprint == handler.fingerprint(data)


def test_hwp_prepare_uses_semantic_render_tree_without_png(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        hwp_module,
        "extract_render_trees",
        lambda *_args, **_kwargs: RhwpRenderTreeResult(
            pages={1: _semantic_page()},
            warnings=(),
            elapsed_seconds=0.02,
        ),
    )

    def unexpected_png(*_args: object, **_kwargs: object) -> RhwpPngResult:
        raise AssertionError("semantic-only pages must not be rendered to PNG")

    monkeypatch.setattr(hwp_module, "export_png_pages", unexpected_png)

    prepared = HwpHandler().prepare(
        _fake_hwp(),
        source_name="report.hwp",
        config=_config(),
        reporter=NullProgressReporter(),
    )

    assert prepared.source_manifest.slide_count == 1
    assert prepared.renderer_manifest.page_count == 1
    assert prepared.renderer_manifest.page_images == {}
    assert prepared.semantic_slides is not None
    slide = prepared.semantic_slides[0]
    assert slide.title == "분기 실적 보고"
    assert slide.tables[0].headers == ("구분", "내용")
    assert slide.tables[0].rows == (("매출", "1억원"),)
    assert {section.heading for section in slide.sections} == {
        "머리말",
        "본문",
        "각주",
        "꼬리말",
    }


def test_hwp_prepare_renders_only_visual_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_page = _semantic_page()
    first_page["children"].append({"type": "Image"})
    second_page = _semantic_page()
    monkeypatch.setattr(
        hwp_module,
        "extract_render_trees",
        lambda *_args, **_kwargs: RhwpRenderTreeResult(
            pages={1: first_page, 2: second_page},
            warnings=(),
            elapsed_seconds=0.02,
        ),
    )
    requested: list[tuple[int, ...]] = []

    def fake_png(
        _source_path: Path,
        _output_dir: Path,
        page_numbers: tuple[int, ...],
        **_kwargs: object,
    ) -> RhwpPngResult:
        requested.append(page_numbers)
        return RhwpPngResult(
            page_images={1: _PNG},
            warnings=(),
            elapsed_seconds=0.03,
        )

    monkeypatch.setattr(hwp_module, "export_png_pages", fake_png)

    prepared = HwpHandler().prepare(
        _fake_hwp(),
        source_name="report.hwp",
        config=_config(),
        reporter=NullProgressReporter(),
    )

    assert requested == [(1,)]
    assert prepared.renderer_manifest.page_images == {1: _PNG}
    assert prepared.semantic_slides is not None
    assert prepared.semantic_slides[0].warnings
    assert not prepared.semantic_slides[1].warnings


def test_hwp_prepare_maps_encrypted_rhwp_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def encrypted(*_args: object, **_kwargs: object) -> RhwpRenderTreeResult:
        raise RhwpEncryptedError("암호화된 문서는 지원하지 않습니다")

    monkeypatch.setattr(hwp_module, "extract_render_trees", encrypted)

    with pytest.raises(EncryptedDocumentError, match="encrypted"):
        HwpHandler().prepare(
            _fake_hwp(),
            source_name="secret.hwp",
            config=_config(),
            reporter=NullProgressReporter(),
        )


def test_hwp_prepare_maps_engine_failure_to_job_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed(*_args: object, **_kwargs: object) -> RhwpRenderTreeResult:
        raise RhwpError("incomplete RenderTree pages")

    monkeypatch.setattr(hwp_module, "extract_render_trees", failed)

    with pytest.raises(ProcessingFidelityError, match="rhwp processing failed"):
        HwpHandler().prepare(
            _fake_hwp(),
            source_name="report.hwp",
            config=_config(),
            reporter=NullProgressReporter(),
        )

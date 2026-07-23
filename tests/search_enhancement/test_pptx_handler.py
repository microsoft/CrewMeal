"""Tests for PptxHandler's semantic-first prepare branch (flagged prototype)."""

from __future__ import annotations

from pathlib import Path

import pytest

from _pptx_fixtures import build_pptx, picture, placeholder

from crewmeal.config import DEFAULT_MAX_UPLOAD_BYTES, AppConfig
from crewmeal.libreoffice import ConversionResult
from crewmeal.models import RendererManifest
from crewmeal.search_enhancement.formats import pptx as pptx_module
from crewmeal.search_enhancement.formats.pptx import PptxHandler
from crewmeal.search_enhancement.progress import NullProgressReporter

_PNG = b"\x89PNG\r\n\x1a\n"


def _config(*, semantic: bool) -> AppConfig:
    return AppConfig(
        endpoint=None,
        max_upload_bytes=DEFAULT_MAX_UPLOAD_BYTES,
        soffice_path=Path("soffice"),
        slide_image_render_dpi=96,
        pptx_semantic_text_slides=semantic,
    )


def _all_pages_manifest(page_count: int) -> RendererManifest:
    return RendererManifest(
        page_count=page_count,
        texts_by_page={n: () for n in range(1, page_count + 1)},
        links_by_page={n: () for n in range(1, page_count + 1)},
        page_images={n: _PNG for n in range(1, page_count + 1)},
        render_dpi=96,
    )


def test_all_text_slides_skip_conversion_and_vision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_convert(*_args: object, **_kwargs: object) -> ConversionResult:
        raise AssertionError("text-only decks must not be converted to PDF")

    monkeypatch.setattr(pptx_module, "convert_pptx_to_pdf", unexpected_convert)

    data = build_pptx(
        placeholder("title", "슬라이드 1") + placeholder("body", "요점 A"),
        placeholder("title", "슬라이드 2") + placeholder("body", "요점 B"),
    )

    prepared = PptxHandler().prepare(
        data,
        source_name="deck.pptx",
        config=_config(semantic=True),
        reporter=NullProgressReporter(),
    )

    assert prepared.semantic_slides is not None
    assert len(prepared.semantic_slides) == 2
    assert prepared.renderer_manifest.page_count == 2
    assert prepared.renderer_manifest.page_images == {}
    assert prepared.geometry_by_page == {}
    assert prepared.stage_timings["conversionSeconds"] == 0.0


def test_mixed_deck_renders_only_visual_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_convert(
        _source: Path, output_dir: Path, **_kwargs: object
    ) -> ConversionResult:
        pdf_path = output_dir / "input.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        return ConversionResult(
            pdf_path=pdf_path,
            conversion_seconds=0.01,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(pptx_module, "convert_pptx_to_pdf", fake_convert)
    monkeypatch.setattr(
        pptx_module,
        "inspect_pdf",
        lambda *_args, **_kwargs: _all_pages_manifest(3),
    )
    monkeypatch.setattr(
        pptx_module,
        "geometry_facts_by_slide",
        lambda _data: {2: "geometry for the visual slide"},
    )

    data = build_pptx(
        placeholder("title", "텍스트") + placeholder("body", "항목"),
        placeholder("title", "이미지") + picture(),
        placeholder("title", "텍스트 2") + placeholder("body", "요점"),
    )

    prepared = PptxHandler().prepare(
        data,
        source_name="deck.pptx",
        config=_config(semantic=True),
        reporter=NullProgressReporter(),
    )

    assert prepared.semantic_slides is not None
    assert prepared.renderer_manifest.page_count == 3
    assert set(prepared.renderer_manifest.page_images) == {2}
    assert set(prepared.geometry_by_page) == {2}
    assert prepared.stage_timings["conversionSeconds"] == 0.01


def test_flag_off_keeps_visual_first_all_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_convert(
        _source: Path, output_dir: Path, **_kwargs: object
    ) -> ConversionResult:
        pdf_path = output_dir / "input.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        return ConversionResult(
            pdf_path=pdf_path,
            conversion_seconds=0.01,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(pptx_module, "convert_pptx_to_pdf", fake_convert)
    monkeypatch.setattr(
        pptx_module,
        "inspect_pdf",
        lambda *_args, **_kwargs: _all_pages_manifest(2),
    )
    monkeypatch.setattr(
        pptx_module,
        "geometry_facts_by_slide",
        lambda _data: {},
    )

    data = build_pptx(
        placeholder("title", "슬라이드 1") + placeholder("body", "요점 A"),
        placeholder("title", "슬라이드 2") + placeholder("body", "요점 B"),
    )

    prepared = PptxHandler().prepare(
        data,
        source_name="deck.pptx",
        config=_config(semantic=False),
        reporter=NullProgressReporter(),
    )

    assert prepared.semantic_slides is None
    assert set(prepared.renderer_manifest.page_images) == {1, 2}

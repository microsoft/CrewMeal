from __future__ import annotations

import _low_tier_fixtures as fx

from crewmeal.config import (
    ANALYSIS_TIER_TEXT_OCR,
    ANALYSIS_TIER_VISION,
    AppConfig,
)
from crewmeal.search_enhancement.analysis_tier import AnalysisTierSettings
from crewmeal.search_enhancement.processor import PresentationProcessor


def _deck() -> bytes:
    prs = fx.new_presentation()
    slide = fx.add_title_slide(prs)
    fx.set_title(slide, "저품질 라우팅")
    fx.add_textbox(slide, "본문 텍스트", top_in=3.0)
    fx.add_table(slide, [["구분", "값"], ["매출", "1억"]])
    return fx.render(prs)


def _config(**overrides) -> AppConfig:
    base = dict(endpoint=None, max_upload_bytes=8_000_000, soffice_path=None)
    base.update(overrides)
    return AppConfig(**base)


def test_low_tier_processes_without_soffice_or_vision():
    config = _config(
        pptx_analysis_tier=ANALYSIS_TIER_TEXT_OCR, pptx_ocr_enabled=False
    )
    processed = PresentationProcessor(config).process(
        _deck(), source_name="deck.pptx"
    )

    usage = processed.analysis.usage
    assert usage["mode"] == "semantic-first"
    assert usage["slideImages"] == 0
    assert usage["tokens"] == {}

    content = processed.rendered.content
    assert "저품질 라우팅" in content
    assert "본문 텍스트" in content
    assert "매출" in content

    # No LibreOffice conversion / Vision render happened.
    assert processed.stage_timings.get("renderingSeconds") == 0.0
    assert processed.stage_timings.get("conversionSeconds") == 0.0
    assert "lowTierExtractionSeconds" in processed.stage_timings


def test_admin_tier_overlay_activates_low_tier_over_vision_default():
    # Base config defaults to vision; the admin overlay flips it to low.
    tier = AnalysisTierSettings(tier=ANALYSIS_TIER_TEXT_OCR, ocr_enabled=False)
    processed = PresentationProcessor(_config(), analysis_tier=tier).process(
        _deck(), source_name="deck.pptx"
    )
    assert processed.analysis.usage["mode"] == "semantic-first"
    assert processed.analysis.usage["tokens"] == {}


def test_low_tier_drops_vision_tuning_corrections_instead_of_raising():
    config = _config(
        pptx_analysis_tier=ANALYSIS_TIER_TEXT_OCR, pptx_ocr_enabled=False
    )
    # Corrections only apply to Vision; the low tier must ignore them rather
    # than fail the document on the "corrections but no visual pages" guard.
    processed = PresentationProcessor(config).process(
        _deck(), source_name="deck.pptx", corrections=("표현을 수정하세요",)
    )
    assert processed.analysis.usage["mode"] == "semantic-first"


def test_vision_tier_does_not_enable_low_path():
    tier = AnalysisTierSettings(tier=ANALYSIS_TIER_VISION, ocr_enabled=True)
    processor = PresentationProcessor(_config(), analysis_tier=tier)
    assert processor._effective_config().low_tier_enabled is False

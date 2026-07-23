from pathlib import Path

from crewmeal.config import (
    ANALYSIS_TIER_TEXT_OCR,
    ANALYSIS_TIER_VISION,
    DEFAULT_MAX_UPLOAD_BYTES,
    AppConfig,
)
from crewmeal.search_enhancement.analysis_tier import (
    ANALYSIS_OCR_KEY,
    ANALYSIS_TIER_KEY,
    analysis_tier_status,
    resolve_analysis_tier,
)


def _config(**overrides) -> AppConfig:
    base = dict(
        endpoint="https://cu.example.com",
        max_upload_bytes=DEFAULT_MAX_UPLOAD_BYTES,
        soffice_path=None,
    )
    base.update(overrides)
    return AppConfig(**base)


def test_defaults_reproduce_config_without_settings():
    resolved = resolve_analysis_tier(_config())
    assert resolved.tier == ANALYSIS_TIER_VISION
    assert resolved.ocr_enabled is True
    assert resolved.is_low_tier is False


def test_admin_override_selects_low_tier_via_korean_alias():
    resolved = resolve_analysis_tier(
        _config(), {ANALYSIS_TIER_KEY: "저품질", ANALYSIS_OCR_KEY: False}
    )
    assert resolved.tier == ANALYSIS_TIER_TEXT_OCR
    assert resolved.is_low_tier is True
    assert resolved.ocr_enabled is False


def test_unknown_tier_falls_back_to_config_default_without_raising():
    resolved = resolve_analysis_tier(
        _config(pptx_analysis_tier=ANALYSIS_TIER_TEXT_OCR),
        {ANALYSIS_TIER_KEY: "nonsense"},
    )
    assert resolved.tier == ANALYSIS_TIER_TEXT_OCR


def test_ocr_toggle_coerces_common_string_values():
    for raw, expected in (
        ("1", True),
        ("true", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("", False),
    ):
        resolved = resolve_analysis_tier(_config(), {ANALYSIS_OCR_KEY: raw})
        assert resolved.ocr_enabled is expected, raw


def test_missing_ocr_setting_uses_config_default():
    assert resolve_analysis_tier(
        _config(pptx_ocr_enabled=False)
    ).ocr_enabled is False


def test_status_reports_missing_korean_model():
    status = analysis_tier_status(_config())
    assert status["tier"] == ANALYSIS_TIER_VISION
    assert status["ocr_model_configured"] is False
    assert status["ocr_model_path"] == ""
    values = {tier["value"] for tier in status["tiers"]}
    assert values == {ANALYSIS_TIER_VISION, ANALYSIS_TIER_TEXT_OCR}


def test_status_reports_configured_korean_model(tmp_path: Path):
    model = tmp_path / "rec.onnx"
    model.write_bytes(b"onnx")
    status = analysis_tier_status(
        _config(pptx_ocr_rec_model_path=model),
        {ANALYSIS_TIER_KEY: "text_ocr"},
    )
    assert status["is_low_tier"] is True
    assert status["ocr_model_configured"] is True
    assert status["ocr_model_path"] == str(model)

from pathlib import Path

import pytest

from crewmeal.config import AppConfig, ConfigurationError


def test_environment_config_normalizes_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "CONTENTUNDERSTANDING_ENDPOINT",
        "https://example.services.ai.azure.com/",
    )
    monkeypatch.setenv(
        "SLIDE_IMAGE_DEPLOYMENT",
        "deploy-test",
    )
    monkeypatch.setenv("SOFFICE_PATH", str(Path(__file__)))
    monkeypatch.setenv("RHWP_PATH", str(Path(__file__)))

    config = AppConfig.from_environment()

    assert config.endpoint == "https://example.services.ai.azure.com"
    assert config.slide_image_deployment == "deploy-test"
    assert config.soffice_path == Path(__file__).resolve()
    assert config.rhwp_path == Path(__file__).resolve()
    assert config.openai_base_url() == (
        "https://example.services.ai.azure.com/openai/v1/"
    )


def test_require_endpoint_reports_missing_configuration() -> None:
    config = AppConfig(
        endpoint=None,
        max_upload_bytes=1,
        soffice_path=None,
    )

    with pytest.raises(ConfigurationError, match="CONTENTUNDERSTANDING_ENDPOINT"):
        config.require_endpoint()


def test_require_rhwp_reports_missing_configuration() -> None:
    config = AppConfig(
        endpoint=None,
        max_upload_bytes=1,
        soffice_path=None,
    )

    with pytest.raises(ConfigurationError, match="RHWP_PATH"):
        config.require_rhwp()


def test_slide_image_defaults_use_luna() -> None:
    config = AppConfig(
        endpoint="https://example.services.ai.azure.com",
        max_upload_bytes=1,
        soffice_path=None,
    )

    assert config.slide_image_model == "gpt-5.6-luna"
    assert config.slide_image_deployment == "gpt-5-6-luna-test"


def test_slide_image_integer_settings_are_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLIDE_IMAGE_MAX_WORKERS", "0")

    with pytest.raises(ConfigurationError, match="SLIDE_IMAGE_MAX_WORKERS"):
        AppConfig.from_environment()


def test_slide_image_model_is_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLIDE_IMAGE_MODEL", "unsupported")

    with pytest.raises(ConfigurationError, match="SLIDE_IMAGE_MODEL"):
        AppConfig.from_environment()


def test_luna_slide_image_model_is_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLIDE_IMAGE_MODEL", "gpt-5.6-luna")

    assert AppConfig.from_environment().slide_image_model == "gpt-5.6-luna"


def test_analysis_tier_environment_accepts_korean_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPTX_ANALYSIS_TIER", "저품질")

    config = AppConfig.from_environment()

    assert config.pptx_analysis_tier == "text_ocr"
    assert config.low_tier_enabled is True


def test_analysis_tier_environment_rejects_unknown_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPTX_ANALYSIS_TIER", "medium")

    with pytest.raises(ConfigurationError, match="PPTX_ANALYSIS_TIER"):
        AppConfig.from_environment()


def test_ocr_enabled_environment_is_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPTX_OCR_ENABLED", "0")

    assert AppConfig.from_environment().pptx_ocr_enabled is False


def test_resolve_ocr_model_paths_from_model_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "rec.onnx").write_bytes(b"onnx")
    (tmp_path / "dict.txt").write_text("keys", encoding="utf-8")
    monkeypatch.delenv("PPTX_OCR_REC_MODEL", raising=False)
    monkeypatch.delenv("PPTX_OCR_REC_KEYS", raising=False)
    monkeypatch.setenv("PPTX_OCR_MODEL_DIR", str(tmp_path))

    config = AppConfig.from_environment()

    assert config.pptx_ocr_rec_model_path == tmp_path / "rec.onnx"
    assert config.pptx_ocr_rec_keys_path == tmp_path / "dict.txt"


def test_resolve_ocr_model_paths_absent_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("PPTX_OCR_REC_MODEL", "PPTX_OCR_REC_KEYS", "PPTX_OCR_MODEL_DIR"):
        monkeypatch.delenv(name, raising=False)

    config = AppConfig.from_environment()

    assert config.pptx_ocr_rec_model_path is None
    assert config.pptx_ocr_rec_keys_path is None

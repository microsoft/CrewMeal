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

    config = AppConfig.from_environment()

    assert config.endpoint == "https://example.services.ai.azure.com"
    assert config.slide_image_deployment == "deploy-test"
    assert config.soffice_path == Path(__file__).resolve()
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

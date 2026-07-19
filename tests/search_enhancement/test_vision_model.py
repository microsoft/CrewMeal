from crewmeal.config import DEFAULT_MAX_UPLOAD_BYTES, AppConfig
from crewmeal.search_enhancement.vision_model import (
    PROVIDER_AZURE_OPENAI,
    VISION_BASE_URL_KEY,
    VISION_DEPLOYMENT_KEY,
    VISION_MODEL_KEY,
    VISION_PROVIDER_KEY,
    VISION_REASONING_EFFORT_KEY,
    resolve_vision_model,
    vision_model_fields,
)


def _config() -> AppConfig:
    return AppConfig(
        endpoint="https://cu.example.com",
        max_upload_bytes=DEFAULT_MAX_UPLOAD_BYTES,
        soffice_path=None,
        slide_image_deployment="env-deploy",
        slide_image_model="env-model",
    )


def test_resolve_defaults_reproduce_config_without_settings():
    resolved = resolve_vision_model(_config())
    assert resolved.provider == PROVIDER_AZURE_OPENAI
    assert resolved.model == "env-model"
    assert resolved.deployment == "env-deploy"
    assert resolved.base_url is None
    assert resolved.reasoning_effort == "high"
    assert resolved.is_supported is True


def test_admin_overrides_take_precedence():
    settings = {
        VISION_PROVIDER_KEY: "openai_compatible",
        VISION_MODEL_KEY: "qwen2-vl",
        VISION_DEPLOYMENT_KEY: "qwen2-vl-7b",
        VISION_BASE_URL_KEY: "https://models.internal/v1/",
        VISION_REASONING_EFFORT_KEY: "low",
    }
    resolved = resolve_vision_model(_config(), settings)
    assert resolved.provider == "openai_compatible"
    assert resolved.model == "qwen2-vl"
    assert resolved.deployment == "qwen2-vl-7b"
    assert resolved.base_url == "https://models.internal/v1/"
    assert resolved.reasoning_effort == "low"
    # openai_compatible is advertised but not implemented yet.
    assert resolved.is_supported is False


def test_blank_overrides_fall_back_to_defaults():
    settings = {
        VISION_MODEL_KEY: "   ",
        VISION_DEPLOYMENT_KEY: "",
        VISION_BASE_URL_KEY: "",
    }
    resolved = resolve_vision_model(_config(), settings)
    assert resolved.model == "env-model"
    assert resolved.deployment == "env-deploy"
    assert resolved.base_url is None


def test_invalid_reasoning_effort_is_coerced():
    resolved = resolve_vision_model(
        _config(), {VISION_REASONING_EFFORT_KEY: "ludicrous"}
    )
    assert resolved.reasoning_effort == "high"


def test_vision_model_fields_expose_typed_controls():
    fields = {f["key"]: f for f in vision_model_fields(_config(), {})}
    assert fields[VISION_PROVIDER_KEY]["type"] == "select"
    assert fields[VISION_REASONING_EFFORT_KEY]["type"] == "select"
    # Text fields carry the env default as placeholder, not as a forced value.
    assert fields[VISION_DEPLOYMENT_KEY]["placeholder"] == "env-deploy"
    assert fields[VISION_DEPLOYMENT_KEY]["value"] == ""

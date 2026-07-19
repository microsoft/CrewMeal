"""Swappable vision-model configuration for image analysis.

The enrichment pipeline sends each rendered page to a vision LLM. That model is
the dominant cost driver (a 30-page deck can cost more than the rest of the
pipeline combined), so operators need to swap it — for a customer-hosted model,
a cheaper OpenAI-compatible endpoint, etc. — from the admin portal without a
redeploy.

This module is the *seam* for that swap. It resolves an effective
:class:`VisionModelSettings` by layering admin overrides (persisted settings)
on top of environment/``AppConfig`` defaults, and exposes the field metadata the
admin settings page renders. Only the Azure OpenAI provider is wired today; the
other providers are advertised as "roadmap" so the UI can present the choice
before the concrete integration lands.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from crewmeal.config import AppConfig

VISION_PROVIDER_KEY = "vision.provider"
VISION_MODEL_KEY = "vision.model"
VISION_DEPLOYMENT_KEY = "vision.deployment"
VISION_BASE_URL_KEY = "vision.base_url"
VISION_REASONING_EFFORT_KEY = "vision.reasoning_effort"

VISION_SETTING_KEYS = (
    VISION_PROVIDER_KEY,
    VISION_MODEL_KEY,
    VISION_DEPLOYMENT_KEY,
    VISION_BASE_URL_KEY,
    VISION_REASONING_EFFORT_KEY,
)

# Provider identifiers. Only ``azure_openai`` is implemented; the rest are
# placeholders that let the admin UI advertise the cost-optimization roadmap
# (customer-hosted or third-party OpenAI-compatible models) without pretending
# they work yet.
PROVIDER_AZURE_OPENAI = "azure_openai"
PROVIDER_OPENAI_COMPATIBLE = "openai_compatible"

SUPPORTED_PROVIDERS = frozenset({PROVIDER_AZURE_OPENAI})
KNOWN_PROVIDERS = (
    (PROVIDER_AZURE_OPENAI, "Azure OpenAI (기본)"),
    (PROVIDER_OPENAI_COMPATIBLE, "OpenAI 호환 엔드포인트 (구현 예정)"),
)

REASONING_EFFORTS = ("low", "medium", "high")
_DEFAULT_REASONING_EFFORT = "high"


@dataclass(frozen=True, slots=True)
class VisionModelSettings:
    """Effective vision-model configuration for one analysis run."""

    provider: str
    model: str
    deployment: str
    base_url: str | None
    reasoning_effort: str

    @property
    def is_supported(self) -> bool:
        """Whether the resolved provider is actually implemented."""

        return self.provider in SUPPORTED_PROVIDERS


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def resolve_vision_model(
    config: AppConfig, settings: Mapping[str, Any] | None = None
) -> VisionModelSettings:
    """Layer admin overrides on top of env/config defaults.

    Passing ``None``/empty ``settings`` reproduces the pre-abstraction behavior
    exactly (Azure OpenAI, ``config`` deployment/model, ``high`` reasoning,
    endpoint-derived base URL), which keeps existing callers and tests stable.
    """

    values = settings or {}
    provider = _clean(values.get(VISION_PROVIDER_KEY)) or PROVIDER_AZURE_OPENAI
    model = _clean(values.get(VISION_MODEL_KEY)) or config.slide_image_model
    deployment = (
        _clean(values.get(VISION_DEPLOYMENT_KEY)) or config.slide_image_deployment
    )
    base_url = _clean(values.get(VISION_BASE_URL_KEY))
    reasoning = (
        _clean(values.get(VISION_REASONING_EFFORT_KEY)) or _DEFAULT_REASONING_EFFORT
    )
    if reasoning not in REASONING_EFFORTS:
        reasoning = _DEFAULT_REASONING_EFFORT
    return VisionModelSettings(
        provider=provider,
        model=model,
        deployment=deployment,
        base_url=base_url,
        reasoning_effort=reasoning,
    )


def vision_model_fields(
    config: AppConfig, settings: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Field metadata for the admin settings page (typed controls)."""

    values = settings or {}
    effective = resolve_vision_model(config, values)
    return [
        {
            "key": VISION_PROVIDER_KEY,
            "label": "제공자",
            "type": "select",
            "value": effective.provider,
            "options": [
                {"value": value, "label": label} for value, label in KNOWN_PROVIDERS
            ],
            "help": "이미지 분석에 사용할 모델 제공자. 현재 Azure OpenAI만 동작합니다.",
        },
        {
            "key": VISION_MODEL_KEY,
            "label": "모델 라벨",
            "type": "text",
            "value": _clean(values.get(VISION_MODEL_KEY)) or "",
            "placeholder": config.slide_image_model,
            "help": "비용/사용량 집계에 쓰이는 모델 이름. 비우면 환경 기본값을 사용합니다.",
        },
        {
            "key": VISION_DEPLOYMENT_KEY,
            "label": "배포/모델 ID",
            "type": "text",
            "value": _clean(values.get(VISION_DEPLOYMENT_KEY)) or "",
            "placeholder": config.slide_image_deployment,
            "help": "실제 API 호출에 전달되는 배포 이름 또는 모델 ID.",
        },
        {
            "key": VISION_BASE_URL_KEY,
            "label": "Base URL",
            "type": "text",
            "value": _clean(values.get(VISION_BASE_URL_KEY)) or "",
            "placeholder": "환경 엔드포인트에서 자동 유도",
            "help": "OpenAI 호환 엔드포인트를 직접 지정할 때 사용. 비우면 배포 엔드포인트를 사용합니다.",
        },
        {
            "key": VISION_REASONING_EFFORT_KEY,
            "label": "Reasoning effort",
            "type": "select",
            "value": effective.reasoning_effort,
            "options": [{"value": item, "label": item} for item in REASONING_EFFORTS],
            "help": "추론 강도. 낮출수록 비용/지연이 줄지만 정확도가 떨어질 수 있습니다.",
        },
    ]

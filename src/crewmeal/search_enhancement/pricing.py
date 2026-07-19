"""Token-cost estimation for the search-enhancement pipeline.

The worker records per-job GPT usage on ``jobs.usage_json`` as
``{"tokens": {"<model>-input": N, "<model>-output": M}, ...}``. This module turns
that usage into an *estimated* cost in USD and KRW for display on the status page
and admin dashboard.

The numbers are estimates: they cover only GPT token spend (not embeddings,
Container Apps compute, or Azure overhead), and the per-token rates for a future
model are not authoritative. Every rate is therefore overridable via environment
variables so the estimate can be corrected without a code change:

* ``CREWMEAL_PRICE_MODEL_LABEL``       – Active model whose rate can be overridden
* ``CREWMEAL_PRICE_INPUT_USD_PER_M``  – Active-model USD per 1M input tokens
* ``CREWMEAL_PRICE_OUTPUT_USD_PER_M`` – Active-model USD per 1M output tokens
* ``CREWMEAL_USD_TO_KRW``             – USD -> KRW conversion rate

Usage remains model-prefixed, so historical and fallback runs are priced with
their own known rates instead of being relabeled as the current production model.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Mapping

# Defaults reflect Azure OpenAI GPT-5.6 Luna GlobalStandard pay-as-you-go pricing and
# the USD->KRW rate observed in mid-2026. Override via environment when they move.
DEFAULT_MODEL_LABEL = "gpt-5.6-luna"
DEFAULT_INPUT_USD_PER_MILLION = 1.00
DEFAULT_OUTPUT_USD_PER_MILLION = 6.00
DEFAULT_USD_TO_KRW = 1480.0
DEFAULT_MODEL_RATES: Mapping[str, tuple[float, float]] = {
    "gpt-5.2": (1.75, 14.00),
    "gpt-5.6-luna": (
        DEFAULT_INPUT_USD_PER_MILLION,
        DEFAULT_OUTPUT_USD_PER_MILLION,
    ),
}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _env_text(name: str, default: str) -> str:
    raw = os.getenv(name)
    return raw.strip() if raw and raw.strip() else default


@dataclass(frozen=True, slots=True)
class PricingConfig:
    """Per-token rates and the currency conversion used for cost estimates."""

    model_label: str = DEFAULT_MODEL_LABEL
    input_usd_per_million: float = DEFAULT_INPUT_USD_PER_MILLION
    output_usd_per_million: float = DEFAULT_OUTPUT_USD_PER_MILLION
    usd_to_krw: float = DEFAULT_USD_TO_KRW
    model_rates: Mapping[str, tuple[float, float]] = field(
        default_factory=lambda: dict(DEFAULT_MODEL_RATES)
    )

    @classmethod
    def from_environment(cls) -> "PricingConfig":
        return cls(
            model_label=_env_text("CREWMEAL_PRICE_MODEL_LABEL", DEFAULT_MODEL_LABEL),
            input_usd_per_million=_env_float(
                "CREWMEAL_PRICE_INPUT_USD_PER_M", DEFAULT_INPUT_USD_PER_MILLION
            ),
            output_usd_per_million=_env_float(
                "CREWMEAL_PRICE_OUTPUT_USD_PER_M", DEFAULT_OUTPUT_USD_PER_MILLION
            ),
            usd_to_krw=_env_float("CREWMEAL_USD_TO_KRW", DEFAULT_USD_TO_KRW),
        )

    @property
    def assumptions_text(self) -> str:
        return (
            f"입력 ${self.input_usd_per_million:g}/1M · "
            f"출력 ${self.output_usd_per_million:g}/1M · "
            f"환율 ₩{self.usd_to_krw:,.0f}/$"
        )

    def rates_for(self, model: str) -> tuple[float, float]:
        if model == self.model_label:
            return self.input_usd_per_million, self.output_usd_per_million
        return self.model_rates.get(
            model,
            (self.input_usd_per_million, self.output_usd_per_million),
        )


@dataclass(frozen=True, slots=True)
class ModelCost:
    """Tokens, rates, and estimated USD for one recorded model."""

    model: str
    input_tokens: int
    output_tokens: int
    input_usd_per_million: float
    output_usd_per_million: float
    usd: float


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """An estimated token cost, pre-formatted for template display."""

    input_tokens: int
    output_tokens: int
    usd: float
    krw: float
    config: PricingConfig
    model_costs: tuple[ModelCost, ...] = ()

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def has_usage(self) -> bool:
        return self.total_tokens > 0

    @property
    def krw_display(self) -> str:
        return f"{round(self.krw):,}"

    @property
    def usd_display(self) -> str:
        return f"{self.usd:,.2f}"

    @property
    def tokens_display(self) -> str:
        return f"{self.total_tokens:,}"

    @property
    def model_label(self) -> str:
        models = [item.model for item in self.model_costs]
        return " + ".join(models) if models else self.config.model_label

    @property
    def assumptions_text(self) -> str:
        if not self.model_costs:
            return self.config.assumptions_text
        rates = " / ".join(
            (
                f"{item.model}: 입력 ${item.input_usd_per_million:g}/1M · "
                f"출력 ${item.output_usd_per_million:g}/1M"
            )
            for item in self.model_costs
        )
        return f"{rates} · 환율 ₩{self.config.usd_to_krw:,.0f}/$"


def _usage_tokens_by_model(
    usages: Iterable[Mapping[str, Any] | None],
) -> dict[str, tuple[int, int]]:
    totals: dict[str, list[int]] = {}
    for usage in usages:
        if not isinstance(usage, Mapping):
            continue
        tokens = usage.get("tokens")
        if not isinstance(tokens, Mapping):
            continue
        for key, value in tokens.items():
            if not isinstance(key, str):
                continue
            if key.endswith("-input"):
                model = key[: -len("-input")]
                bucket = 0
            elif key.endswith("-output"):
                model = key[: -len("-output")]
                bucket = 1
            else:
                continue
            if not model:
                continue
            try:
                amount = int(value)
            except (TypeError, ValueError):
                continue
            totals.setdefault(model, [0, 0])[bucket] += amount
    return {model: (values[0], values[1]) for model, values in totals.items()}


def sum_usage_tokens(usages: Iterable[Mapping[str, Any] | None]) -> tuple[int, int]:
    """Sum input/output tokens across any number of ``usage`` dicts.

    Token keys are model-suffixed (for example, ``"gpt-5.6-luna-input"``);
    we bucket purely by the ``-input`` / ``-output`` suffix so the total is
    model-agnostic (in practice a single model is used).
    """

    by_model = _usage_tokens_by_model(usages)
    return (
        sum(tokens[0] for tokens in by_model.values()),
        sum(tokens[1] for tokens in by_model.values()),
    )


def estimate_cost(
    usages: Iterable[Mapping[str, Any] | None],
    config: PricingConfig | None = None,
) -> CostEstimate:
    """Estimate the USD/KRW cost of the given token usage records."""

    config = config or PricingConfig.from_environment()
    by_model = _usage_tokens_by_model(usages)
    model_costs: list[ModelCost] = []
    for model, (input_tokens, output_tokens) in sorted(by_model.items()):
        input_rate, output_rate = config.rates_for(model)
        model_usd = (
            input_tokens / 1_000_000 * input_rate
            + output_tokens / 1_000_000 * output_rate
        )
        model_costs.append(
            ModelCost(
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                input_usd_per_million=input_rate,
                output_usd_per_million=output_rate,
                usd=model_usd,
            )
        )
    input_tokens = sum(item.input_tokens for item in model_costs)
    output_tokens = sum(item.output_tokens for item in model_costs)
    usd = sum(item.usd for item in model_costs)
    krw = usd * config.usd_to_krw
    return CostEstimate(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        usd=usd,
        krw=krw,
        config=config,
        model_costs=tuple(model_costs),
    )

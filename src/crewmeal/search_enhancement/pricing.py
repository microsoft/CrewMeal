"""Token-cost estimation for the search-enhancement pipeline.

The worker records per-job GPT usage on ``jobs.usage_json`` as
``{"tokens": {"<model>-input": N, "<model>-output": M}, ...}``. This module turns
that usage into an *estimated* cost in USD and KRW for display on the status page
and admin dashboard.

The numbers are estimates: they cover only GPT token spend (not embeddings,
Container Apps compute, or Azure overhead), and the per-token rates for a future
model are not authoritative. Every rate is therefore overridable via environment
variables so the estimate can be corrected without a code change:

* ``CREWMEAL_PRICE_INPUT_USD_PER_M``  – USD per 1M input tokens
* ``CREWMEAL_PRICE_OUTPUT_USD_PER_M`` – USD per 1M output tokens
* ``CREWMEAL_USD_TO_KRW``             – USD -> KRW conversion rate
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Mapping

# Defaults reflect Azure OpenAI gpt-5.2 GlobalStandard pay-as-you-go pricing and
# the USD->KRW rate observed in mid-2026. Override via environment when they move.
DEFAULT_INPUT_USD_PER_MILLION = 1.75
DEFAULT_OUTPUT_USD_PER_MILLION = 14.00
DEFAULT_USD_TO_KRW = 1480.0


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class PricingConfig:
    """Per-token rates and the currency conversion used for cost estimates."""

    input_usd_per_million: float = DEFAULT_INPUT_USD_PER_MILLION
    output_usd_per_million: float = DEFAULT_OUTPUT_USD_PER_MILLION
    usd_to_krw: float = DEFAULT_USD_TO_KRW

    @classmethod
    def from_environment(cls) -> "PricingConfig":
        return cls(
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


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """An estimated token cost, pre-formatted for template display."""

    input_tokens: int
    output_tokens: int
    usd: float
    krw: float
    config: PricingConfig

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
    def assumptions_text(self) -> str:
        return self.config.assumptions_text


def sum_usage_tokens(usages: Iterable[Mapping[str, Any] | None]) -> tuple[int, int]:
    """Sum input/output tokens across any number of ``usage`` dicts.

    Token keys are model-suffixed (``"gpt-5.2-input"`` / ``"gpt-5.2-output"``);
    we bucket purely by the ``-input`` / ``-output`` suffix so the total is
    model-agnostic (in practice a single model is used).
    """

    input_tokens = 0
    output_tokens = 0
    for usage in usages:
        if not isinstance(usage, Mapping):
            continue
        tokens = usage.get("tokens")
        if not isinstance(tokens, Mapping):
            continue
        for key, value in tokens.items():
            try:
                amount = int(value)
            except (TypeError, ValueError):
                continue
            if key.endswith("-input"):
                input_tokens += amount
            elif key.endswith("-output"):
                output_tokens += amount
    return input_tokens, output_tokens


def estimate_cost(
    usages: Iterable[Mapping[str, Any] | None],
    config: PricingConfig | None = None,
) -> CostEstimate:
    """Estimate the USD/KRW cost of the given token usage records."""

    config = config or PricingConfig.from_environment()
    input_tokens, output_tokens = sum_usage_tokens(usages)
    usd = (
        input_tokens / 1_000_000 * config.input_usd_per_million
        + output_tokens / 1_000_000 * config.output_usd_per_million
    )
    krw = usd * config.usd_to_krw
    return CostEstimate(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        usd=usd,
        krw=krw,
        config=config,
    )

from __future__ import annotations

import pytest

from crewmeal.search_enhancement.pricing import (
    PricingConfig,
    estimate_cost,
    sum_usage_tokens,
)


def test_sum_usage_tokens_buckets_by_suffix() -> None:
    usages = [
        {"tokens": {"gpt-5.2-input": 100, "gpt-5.2-output": 40}},
        {"tokens": {"gpt-5.2-input": 200, "gpt-5.2-output": 60}},
        {"slideImages": 3},  # no token block
        None,  # tolerated
    ]
    assert sum_usage_tokens(usages) == (300, 100)


def test_estimate_cost_uses_config_rates() -> None:
    config = PricingConfig(
        model_label="m",
        input_usd_per_million=2.0, output_usd_per_million=10.0, usd_to_krw=1000.0
    )
    cost = estimate_cost(
        [{"tokens": {"m-input": 1_000_000, "m-output": 1_000_000}}], config
    )
    assert cost.input_tokens == 1_000_000
    assert cost.output_tokens == 1_000_000
    assert cost.usd == pytest.approx(12.0)
    assert cost.krw == pytest.approx(12_000.0)
    assert cost.has_usage
    assert cost.krw_display == "12,000"
    assert cost.tokens_display == "2,000,000"
    assert cost.model_label == "m"


def test_estimate_cost_prices_mixed_models_separately() -> None:
    cost = estimate_cost(
        [
            {
                "tokens": {
                    "gpt-5.2-input": 1_000_000,
                    "gpt-5.2-output": 1_000_000,
                    "gpt-5.6-luna-input": 1_000_000,
                    "gpt-5.6-luna-output": 1_000_000,
                }
            }
        ]
    )

    assert cost.input_tokens == 2_000_000
    assert cost.output_tokens == 2_000_000
    assert cost.usd == pytest.approx(22.75)
    assert cost.model_label == "gpt-5.2 + gpt-5.6-luna"
    assert "gpt-5.2: 입력 $1.75/1M · 출력 $14/1M" in cost.assumptions_text
    assert "gpt-5.6-luna: 입력 $1/1M · 출력 $6/1M" in cost.assumptions_text


def test_estimate_cost_empty_has_no_usage() -> None:
    cost = estimate_cost([])
    assert not cost.has_usage
    assert cost.total_tokens == 0
    assert cost.krw_display == "0"


def test_pricing_config_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREWMEAL_PRICE_MODEL_LABEL", "custom-model")
    monkeypatch.setenv("CREWMEAL_PRICE_INPUT_USD_PER_M", "3")
    monkeypatch.setenv("CREWMEAL_PRICE_OUTPUT_USD_PER_M", "30")
    monkeypatch.setenv("CREWMEAL_USD_TO_KRW", "1500")
    config = PricingConfig.from_environment()
    assert config.model_label == "custom-model"
    assert config.input_usd_per_million == 3.0
    assert config.output_usd_per_million == 30.0
    assert config.usd_to_krw == 1500.0


def test_pricing_config_ignores_bad_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREWMEAL_USD_TO_KRW", "not-a-number")
    config = PricingConfig.from_environment()
    assert config.usd_to_krw == 1480.0  # falls back to default


def test_pricing_defaults_use_luna_rates() -> None:
    config = PricingConfig()

    assert config.model_label == "gpt-5.6-luna"
    assert config.input_usd_per_million == 1.0
    assert config.output_usd_per_million == 6.0

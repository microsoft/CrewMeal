"""Admin-selectable analysis quality tier for document enrichment.

Each rendered page normally goes to the Vision LLM, which dominates pipeline
cost (a large deck can cost more in image tokens than everything else combined).
For text- and screenshot-heavy decks that spend is often unnecessary: the
literal content already lives in native text boxes, tables, and charts, and the
text baked into screenshots can be recovered with local OCR. This module is the
*seam* that lets an operator trade quality for cost from the admin portal.

Two tiers:

* ``vision`` (고품질) -- the existing render-every-slide + Vision-LLM path. Best
  for diagram/architecture decks where meaning lives in pixels and layout.
* ``text_ocr`` (저품질) -- no LibreOffice, no Vision, zero LLM tokens. Extract
  text/tables/charts from OOXML and run Korean-capable OCR on embedded raster
  images (CPU only). Recovers literal content but not interpretive summaries,
  flow narration, or chart insights.

Like :mod:`crewmeal.search_enhancement.vision_model`, an effective
:class:`AnalysisTierSettings` is resolved by layering admin overrides (persisted
settings) on top of environment/``AppConfig`` defaults, and the admin settings
page renders the field metadata this module exposes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from crewmeal.config import (
    ANALYSIS_TIER_TEXT_OCR,
    ANALYSIS_TIER_VISION,
    AppConfig,
    normalize_analysis_tier,
)

ANALYSIS_TIER_KEY = "analysis.tier"
ANALYSIS_OCR_KEY = "analysis.ocr.enabled"

ANALYSIS_SETTING_KEYS = (
    ANALYSIS_TIER_KEY,
    ANALYSIS_OCR_KEY,
)

# Human labels for the admin tier picker.
KNOWN_TIERS = (
    (ANALYSIS_TIER_VISION, "고품질 · Vision AI (기본)"),
    (ANALYSIS_TIER_TEXT_OCR, "저품질 · 텍스트 추출 + OCR (LLM 토큰 0)"),
)


@dataclass(frozen=True, slots=True)
class AnalysisTierSettings:
    """Effective analysis-tier configuration for one worker run."""

    tier: str
    ocr_enabled: bool

    @property
    def is_low_tier(self) -> bool:
        """Whether the no-Vision text+OCR path is active."""

        return self.tier == ANALYSIS_TIER_TEXT_OCR


_TRUE_TOKENS = frozenset({"1", "true", "on", "yes", "y", "t"})
_FALSE_TOKENS = frozenset({"0", "false", "off", "no", "n", "f", ""})


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().casefold()
    if text in _TRUE_TOKENS:
        return True
    if text in _FALSE_TOKENS:
        return False
    return default


def resolve_analysis_tier(
    config: AppConfig, settings: Mapping[str, Any] | None = None
) -> AnalysisTierSettings:
    """Layer admin overrides on top of env/config defaults.

    Passing ``None``/empty ``settings`` reproduces the environment default
    exactly (``config.pptx_analysis_tier`` / ``config.pptx_ocr_enabled``), which
    keeps existing callers and tests stable. An unrecognized stored tier falls
    back to the environment default rather than raising, so a malformed setting
    never takes the whole worker down.
    """

    values = settings or {}
    tier = (
        normalize_analysis_tier(values.get(ANALYSIS_TIER_KEY))
        or config.pptx_analysis_tier
    )
    ocr_enabled = _coerce_bool(
        values.get(ANALYSIS_OCR_KEY), default=config.pptx_ocr_enabled
    )
    return AnalysisTierSettings(tier=tier, ocr_enabled=ocr_enabled)


def analysis_tier_status(
    config: AppConfig, settings: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Settings-page context for the tier picker (select + OCR toggle + status).

    Also reports whether a Korean-capable OCR recognition model is configured,
    because the low tier's OCR is only useful for Korean text when one is wired
    (the bundled RapidOCR default reads Chinese/English only).
    """

    effective = resolve_analysis_tier(config, settings)
    ocr_model_configured = config.pptx_ocr_rec_model_path is not None
    return {
        "tier": effective.tier,
        "is_low_tier": effective.is_low_tier,
        "ocr_enabled": effective.ocr_enabled,
        "ocr_model_configured": ocr_model_configured,
        "ocr_model_path": (
            str(config.pptx_ocr_rec_model_path) if ocr_model_configured else ""
        ),
        "tiers": [
            {"value": value, "label": label} for value, label in KNOWN_TIERS
        ],
    }

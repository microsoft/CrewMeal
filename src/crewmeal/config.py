from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_SLIDE_IMAGE_DEPLOYMENT = "gpt-5-6-luna-test"
DEFAULT_MAX_UPLOAD_BYTES = 200 * 1024 * 1024
DEFAULT_SLIDE_IMAGE_RENDER_DPI = 144
DEFAULT_SLIDE_IMAGE_MAX_WORKERS = 2
DEFAULT_SLIDE_IMAGE_MAX_COMPLETION_TOKENS = 40_000
DEFAULT_SLIDE_IMAGE_REQUEST_TIMEOUT = 900
DEFAULT_SLIDE_IMAGE_MODEL = "gpt-5.6-luna"
DEFAULT_RHWP_TIMEOUT_SECONDS = 300
SUPPORTED_SLIDE_IMAGE_MODELS = frozenset(
    {"gpt-5-mini", "gpt-5.2", "gpt-5.6-luna"}
)

# Analysis quality tiers. ``vision`` (high) renders every slide and sends it to
# the Vision LLM -- the richest but most expensive path. ``text_ocr`` (low) skips
# LibreOffice rendering and Vision entirely, extracting slide text/tables/charts
# straight from OOXML and reading embedded raster images with a local OCR engine.
# Operators pick the tier from the admin portal (or ``PPTX_ANALYSIS_TIER``).
ANALYSIS_TIER_VISION = "vision"
ANALYSIS_TIER_TEXT_OCR = "text_ocr"
SUPPORTED_ANALYSIS_TIERS = frozenset(
    {ANALYSIS_TIER_VISION, ANALYSIS_TIER_TEXT_OCR}
)
DEFAULT_ANALYSIS_TIER = ANALYSIS_TIER_VISION


class ConfigurationError(RuntimeError):
    """Raised when a required local or Azure setting is unavailable."""


def resolve_soffice_path() -> Path | None:
    configured = os.getenv("SOFFICE_PATH")
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())

    command = shutil.which("soffice")
    if command:
        candidates.append(Path(command))

    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        candidates.append(
            Path(local_app_data)
            / "Programs"
            / "LibreOfficePortable"
            / "program"
            / "soffice.exe"
        )

    candidates.extend(
        [
            Path(r"C:\Program Files\LibreOffice\program\soffice.exe"),
            Path(r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"),
        ]
    )

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def resolve_rhwp_path() -> Path | None:
    configured = os.getenv("RHWP_PATH")
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())

    command = shutil.which("rhwp")
    if command:
        candidates.append(Path(command))

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def resolve_ocr_model_paths() -> tuple[Path | None, Path | None]:
    """Locate the low-tier OCR recognition model + character dictionary.

    Low-tier analysis reads embedded raster images with a local OCR engine. The
    engine bundles a Chinese/English recognition model that cannot read Korean,
    so a Korean recognition model must be supplied for Korean decks. Paths are
    taken from ``PPTX_OCR_REC_MODEL`` / ``PPTX_OCR_REC_KEYS``; when a single
    ``PPTX_OCR_MODEL_DIR`` is given instead, ``rec.onnx`` and ``dict.txt`` inside
    it are used. Returns ``(None, None)`` when nothing is configured, in which
    case the engine falls back to its bundled default model.
    """

    rec = os.getenv("PPTX_OCR_REC_MODEL")
    keys = os.getenv("PPTX_OCR_REC_KEYS")
    model_dir = os.getenv("PPTX_OCR_MODEL_DIR")
    rec_path = Path(rec).expanduser() if rec else None
    keys_path = Path(keys).expanduser() if keys else None
    if model_dir:
        base = Path(model_dir).expanduser()
        rec_path = rec_path or base / "rec.onnx"
        keys_path = keys_path or base / "dict.txt"
    return (
        rec_path if rec_path and rec_path.is_file() else None,
        keys_path if keys_path and keys_path.is_file() else None,
    )


@dataclass(frozen=True, slots=True)
class AppConfig:
    endpoint: str | None
    max_upload_bytes: int
    soffice_path: Path | None
    slide_image_deployment: str = DEFAULT_SLIDE_IMAGE_DEPLOYMENT
    slide_image_render_dpi: int = DEFAULT_SLIDE_IMAGE_RENDER_DPI
    slide_image_max_workers: int = DEFAULT_SLIDE_IMAGE_MAX_WORKERS
    slide_image_max_completion_tokens: int = (
        DEFAULT_SLIDE_IMAGE_MAX_COMPLETION_TOKENS
    )
    slide_image_request_timeout: int = DEFAULT_SLIDE_IMAGE_REQUEST_TIMEOUT
    slide_image_model: str = DEFAULT_SLIDE_IMAGE_MODEL
    rhwp_path: Path | None = None
    rhwp_timeout_seconds: int = DEFAULT_RHWP_TIMEOUT_SECONDS
    # Prototype: when enabled, PowerPoint slides that are unambiguously linear
    # text (only layout placeholders, no pictures/charts/tables/connectors/
    # groups) are extracted straight from OOXML and skip the Vision model. Only
    # the remaining visual slides are rendered and analyzed. Off keeps the
    # byte-for-byte legacy behavior where every slide goes through Vision.
    pptx_semantic_text_slides: bool = False
    # Analysis quality tier. ``vision`` (default/high) renders every slide and
    # runs the Vision LLM. ``text_ocr`` (low) skips LibreOffice + Vision entirely
    # and extracts text/tables/charts from OOXML plus OCR on embedded images --
    # zero LLM tokens, CPU only. Admins pick this per deployment.
    pptx_analysis_tier: str = DEFAULT_ANALYSIS_TIER
    # Whether the low tier OCRs embedded raster images. Off = text extraction
    # only (still zero tokens, no OCR CPU cost).
    pptx_ocr_enabled: bool = True
    # Korean OCR recognition model + character dictionary for the low tier. When
    # unset the OCR engine uses its bundled Chinese/English model, which cannot
    # read Korean.
    pptx_ocr_rec_model_path: Path | None = None
    pptx_ocr_rec_keys_path: Path | None = None

    @property
    def low_tier_enabled(self) -> bool:
        return self.pptx_analysis_tier == ANALYSIS_TIER_TEXT_OCR

    @classmethod
    def from_environment(cls) -> "AppConfig":
        endpoint = os.getenv("CONTENTUNDERSTANDING_ENDPOINT")
        if endpoint:
            endpoint = endpoint.rstrip("/")

        ocr_rec_model, ocr_rec_keys = resolve_ocr_model_paths()
        return cls(
            endpoint=endpoint,
            max_upload_bytes=DEFAULT_MAX_UPLOAD_BYTES,
            soffice_path=resolve_soffice_path(),
            slide_image_deployment=os.getenv(
                "SLIDE_IMAGE_DEPLOYMENT",
                DEFAULT_SLIDE_IMAGE_DEPLOYMENT,
            ),
            slide_image_render_dpi=_positive_int_environment(
                "SLIDE_IMAGE_RENDER_DPI",
                DEFAULT_SLIDE_IMAGE_RENDER_DPI,
            ),
            slide_image_max_workers=_positive_int_environment(
                "SLIDE_IMAGE_MAX_WORKERS",
                DEFAULT_SLIDE_IMAGE_MAX_WORKERS,
            ),
            slide_image_max_completion_tokens=_positive_int_environment(
                "SLIDE_IMAGE_MAX_COMPLETION_TOKENS",
                DEFAULT_SLIDE_IMAGE_MAX_COMPLETION_TOKENS,
            ),
            slide_image_request_timeout=_positive_int_environment(
                "SLIDE_IMAGE_REQUEST_TIMEOUT",
                DEFAULT_SLIDE_IMAGE_REQUEST_TIMEOUT,
            ),
            slide_image_model=_slide_image_model_environment(),
            rhwp_path=resolve_rhwp_path(),
            rhwp_timeout_seconds=_positive_int_environment(
                "RHWP_TIMEOUT_SECONDS",
                DEFAULT_RHWP_TIMEOUT_SECONDS,
            ),
            pptx_semantic_text_slides=_bool_environment(
                "PPTX_SEMANTIC_TEXT_SLIDES",
                default=False,
            ),
            pptx_analysis_tier=_analysis_tier_environment(),
            pptx_ocr_enabled=_bool_environment(
                "PPTX_OCR_ENABLED",
                default=True,
            ),
            pptx_ocr_rec_model_path=ocr_rec_model,
            pptx_ocr_rec_keys_path=ocr_rec_keys,
        )

    def require_endpoint(self) -> str:
        if not self.endpoint:
            raise ConfigurationError(
                "CONTENTUNDERSTANDING_ENDPOINT is not configured. "
                "Run azd provision and load the azd environment first."
            )
        return self.endpoint

    def require_soffice(self) -> Path:
        if not self.soffice_path:
            raise ConfigurationError(
                "LibreOffice was not found. Set SOFFICE_PATH to soffice.exe."
            )
        return self.soffice_path

    def require_rhwp(self) -> Path:
        if not self.rhwp_path:
            raise ConfigurationError(
                "rhwp was not found. Set RHWP_PATH to the pinned rhwp executable."
            )
        return self.rhwp_path

    def openai_base_url(self) -> str:
        return f"{self.require_endpoint()}/openai/v1/"


def _positive_int_environment(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a positive integer.") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be a positive integer.")
    return value


def _bool_environment(name: str, *, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ConfigurationError(
        f"{name} must be a boolean (true/false, 1/0, yes/no, on/off)."
    )


def _slide_image_model_environment() -> str:
    value = os.getenv("SLIDE_IMAGE_MODEL", DEFAULT_SLIDE_IMAGE_MODEL).strip()
    if value not in SUPPORTED_SLIDE_IMAGE_MODELS:
        supported = ", ".join(sorted(SUPPORTED_SLIDE_IMAGE_MODELS))
        raise ConfigurationError(
            f"SLIDE_IMAGE_MODEL must be one of: {supported}."
        )
    return value


def normalize_analysis_tier(value: Any) -> str | None:
    """Return a canonical tier for ``value`` or ``None`` when unrecognized."""

    if value is None:
        return None
    text = str(value).strip().casefold().replace("-", "_")
    aliases = {
        "vision": ANALYSIS_TIER_VISION,
        "high": ANALYSIS_TIER_VISION,
        "고품질": ANALYSIS_TIER_VISION,
        "text_ocr": ANALYSIS_TIER_TEXT_OCR,
        "text": ANALYSIS_TIER_TEXT_OCR,
        "ocr": ANALYSIS_TIER_TEXT_OCR,
        "low": ANALYSIS_TIER_TEXT_OCR,
        "저품질": ANALYSIS_TIER_TEXT_OCR,
    }
    return aliases.get(text)


def _analysis_tier_environment() -> str:
    raw = os.getenv("PPTX_ANALYSIS_TIER")
    if raw is None or not raw.strip():
        return DEFAULT_ANALYSIS_TIER
    tier = normalize_analysis_tier(raw)
    if tier is None:
        supported = ", ".join(sorted(SUPPORTED_ANALYSIS_TIERS))
        raise ConfigurationError(
            f"PPTX_ANALYSIS_TIER must be one of: {supported}."
        )
    return tier

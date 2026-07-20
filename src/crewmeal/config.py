from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


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

    @classmethod
    def from_environment(cls) -> "AppConfig":
        endpoint = os.getenv("CONTENTUNDERSTANDING_ENDPOINT")
        if endpoint:
            endpoint = endpoint.rstrip("/")

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


def _slide_image_model_environment() -> str:
    value = os.getenv("SLIDE_IMAGE_MODEL", DEFAULT_SLIDE_IMAGE_MODEL).strip()
    if value not in SUPPORTED_SLIDE_IMAGE_MODELS:
        supported = ", ".join(sorted(SUPPORTED_SLIDE_IMAGE_MODELS))
        raise ConfigurationError(
            f"SLIDE_IMAGE_MODEL must be one of: {supported}."
        )
    return value

"""Pluggable decryption pipeline (structure only).

Some tenants store rights-protected documents in SharePoint — Microsoft Purview
Information Protection (MIP) labels, or a third-party encryption gateway. Those
payloads must be decrypted *before* a format handler can read them.

This module is the seam for that step. It defines a :class:`DecryptionProvider`
protocol and a small registry, plus :func:`maybe_decrypt`, which the processor
calls before format detection. Providers are **off by default** and gated by an
admin toggle (``decryption.<id>.enabled``). Only *enabled* providers get to
inspect a payload, so turning everything off is a guaranteed no-op.

:class:`MipDecryptionProvider` is implemented: it detects MIP/IRM markers and
delegates the actual decryption to a MIP File SDK CLI through the
:mod:`~crewmeal.search_enhancement.mip_sdk` runner seam. When MIP is enabled but
no SDK runner is configured, it fails loudly with
:class:`DecryptionUnavailableError` instead of silently mis-processing a
document. :class:`GenericDecryptionProvider` is still a placeholder and raises
:class:`DecryptionNotImplementedError`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from crewmeal.search_enhancement.mip_sdk import (
    MipSdkExecutionError,
    MipSdkRunner,
    MipSdkUnavailableError,
)

DECRYPTION_SETTING_PREFIX = "decryption."
DECRYPTION_SETTING_SUFFIX = ".enabled"


class DecryptionError(RuntimeError):
    """Base error for the decryption pipeline."""


class DecryptionNotImplementedError(DecryptionError):
    """Raised when an enabled provider matched a payload it cannot decrypt yet."""


class DecryptionUnavailableError(DecryptionError):
    """Raised when an enabled provider matched a payload but has no backend configured."""


class DecryptionFailedError(DecryptionError):
    """Raised when a provider's backend ran but failed to decrypt the payload."""


@runtime_checkable
class DecryptionProvider(Protocol):
    provider_id: str
    display_name: str
    implemented: bool

    def detect(
        self, data: bytes, *, filename: str, content_type: str | None
    ) -> bool: ...

    def decrypt(self, data: bytes, *, filename: str) -> bytes: ...


def decryption_setting_key(provider_id: str) -> str:
    return f"{DECRYPTION_SETTING_PREFIX}{provider_id}{DECRYPTION_SETTING_SUFFIX}"


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "on", "yes", "y"}
    return False


class MipDecryptionProvider:
    """Microsoft Purview Information Protection (MIP) decryption via the File SDK.

    Detection stays local (marker sniffing); the actual decryption is delegated
    to a MIP SDK CLI through an injected :class:`~crewmeal.search_enhancement.mip_sdk.MipSdkRunner`.
    When enabled without a configured runner, :meth:`decrypt` raises
    :class:`DecryptionUnavailableError` so the failure is explicit.
    """

    provider_id = "mip"
    display_name = "Microsoft Purview (MIP) 암호화"
    implemented = True

    # Markers that appear in MIP/IRM-protected wrappers. Safe to match on: they
    # do not occur in ordinary Office/PDF payloads.
    _MARKERS = (b"MicrosoftIRMServices", b"MSIP_Label", b"\x09DRMContent")

    def __init__(self, runner: MipSdkRunner | None = None) -> None:
        self._runner = runner

    def detect(
        self, data: bytes, *, filename: str, content_type: str | None
    ) -> bool:
        head = data[:4096]
        return any(marker in head for marker in self._MARKERS)

    def decrypt(self, data: bytes, *, filename: str) -> bytes:
        if self._runner is None:
            raise DecryptionUnavailableError(
                "MIP decryption is enabled but no SDK runner is configured. Set "
                "CREWMEAL_MIP_SDK_CLI (production: the Microsoft MIP File SDK CLI; "
                "local/CI: 'python -m crewmeal.search_enhancement.mip_tool') and "
                "provide M365 credentials whose service principal is an Azure RMS "
                "super user."
            )
        try:
            return self._runner.run(data, filename=filename)
        except MipSdkUnavailableError as exc:
            raise DecryptionUnavailableError(str(exc)) from exc
        except MipSdkExecutionError as exc:
            raise DecryptionFailedError(
                f"MIP decryption failed for {filename!r}: {exc}"
            ) from exc


class GenericDecryptionProvider:
    """Third-party / generic encryption gateway — not implemented yet.

    Detection is intentionally inert (returns ``False``) because a generic
    gateway's envelope is deployment-specific; concrete detection ships with the
    real integration. Enabling this provider is therefore a safe no-op until then.
    """

    provider_id = "generic"
    display_name = "기타 암호화 솔루션"
    implemented = False

    def detect(
        self, data: bytes, *, filename: str, content_type: str | None
    ) -> bool:
        return False

    def decrypt(self, data: bytes, *, filename: str) -> bytes:
        raise DecryptionNotImplementedError(
            "Generic decryption is enabled but not implemented yet. Disable the "
            "provider or install the decryption integration."
        )


_PROVIDERS: list[DecryptionProvider] = [
    MipDecryptionProvider(),
    GenericDecryptionProvider(),
]


def all_providers() -> tuple[DecryptionProvider, ...]:
    return tuple(_PROVIDERS)


def is_decryption_enabled(
    provider_id: str, settings: Mapping[str, Any]
) -> bool:
    return _coerce_bool(settings.get(decryption_setting_key(provider_id)))


def enabled_providers(
    settings: Mapping[str, Any],
) -> tuple[DecryptionProvider, ...]:
    return tuple(
        provider
        for provider in _PROVIDERS
        if is_decryption_enabled(provider.provider_id, settings)
    )


def decryption_status(
    settings: Mapping[str, Any],
    *,
    configured: Mapping[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """Per-provider rows for the admin settings UI.

    ``implemented`` reports whether the integration code exists; ``configured``
    reports whether a runnable backend (e.g. a MIP SDK CLI) is wired up in the
    current environment. A provider can be implemented but not configured.
    """

    configured = configured or {}
    return [
        {
            "provider_id": provider.provider_id,
            "display_name": provider.display_name,
            "implemented": provider.implemented,
            "configured": bool(configured.get(provider.provider_id, False)),
            "enabled": is_decryption_enabled(provider.provider_id, settings),
        }
        for provider in _PROVIDERS
    ]


def maybe_decrypt(
    data: bytes,
    *,
    filename: str,
    content_type: str | None = None,
    settings: Mapping[str, Any] | None = None,
    mip_runner: MipSdkRunner | None = None,
) -> bytes:
    """Run enabled decryption providers before format handling.

    Returns the (possibly decrypted) bytes. Providers that are disabled never
    inspect the payload, so this is a no-op unless an admin has explicitly
    enabled a provider *and* that provider recognizes the payload.

    ``mip_runner`` is the runtime backend for the MIP provider (built by the
    processor from the environment). When it is ``None`` and MIP matches a
    payload, decryption fails loudly with :class:`DecryptionUnavailableError`.
    """

    for provider in enabled_providers(settings or {}):
        if provider.detect(data, filename=filename, content_type=content_type):
            provider = _bind_runner(provider, mip_runner=mip_runner)
            return provider.decrypt(data, filename=filename)
    return data


def _bind_runner(
    provider: DecryptionProvider, *, mip_runner: MipSdkRunner | None
) -> DecryptionProvider:
    """Return a provider bound to the runtime runner where applicable.

    The module-level registry holds runner-less singletons for detection and
    status. When an actual decryption is about to run, the MIP provider is
    rebound to the injected runner so the same seam works in tests, CI (via the
    reference CLI), and production (via the Microsoft MIP SDK CLI).
    """

    if provider.provider_id == "mip" and mip_runner is not None:
        return MipDecryptionProvider(runner=mip_runner)
    return provider

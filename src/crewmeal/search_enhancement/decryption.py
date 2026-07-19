"""Pluggable decryption pipeline (structure only).

Some tenants store rights-protected documents in SharePoint — Microsoft Purview
Information Protection (MIP) labels, or a third-party encryption gateway. Those
payloads must be decrypted *before* a format handler can read them.

This module is the seam for that step. It defines a :class:`DecryptionProvider`
protocol and a small registry, plus :func:`maybe_decrypt`, which the processor
calls before format detection. Providers are **off by default** and gated by an
admin toggle (``decryption.<id>.enabled``). Only *enabled* providers get to
inspect a payload, so turning everything off is a guaranteed no-op.

No provider is implemented yet: :class:`MipDecryptionProvider` and
:class:`GenericDecryptionProvider` advertise themselves for the admin UI, detect
their marker, and then fail loudly with :class:`DecryptionNotImplementedError`
so an operator who enables a provider before it is built gets a clear error
instead of a silently mis-processed document.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

DECRYPTION_SETTING_PREFIX = "decryption."
DECRYPTION_SETTING_SUFFIX = ".enabled"


class DecryptionError(RuntimeError):
    """Base error for the decryption pipeline."""


class DecryptionNotImplementedError(DecryptionError):
    """Raised when an enabled provider matched a payload it cannot decrypt yet."""


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
    """Microsoft Purview Information Protection (MIP) — not implemented yet."""

    provider_id = "mip"
    display_name = "Microsoft Purview (MIP) 암호화"
    implemented = False

    # Markers that appear in MIP/IRM-protected wrappers. Safe to match on: they
    # do not occur in ordinary Office/PDF payloads.
    _MARKERS = (b"MicrosoftIRMServices", b"MSIP_Label", b"\x09DRMContent")

    def detect(
        self, data: bytes, *, filename: str, content_type: str | None
    ) -> bool:
        head = data[:4096]
        return any(marker in head for marker in self._MARKERS)

    def decrypt(self, data: bytes, *, filename: str) -> bytes:
        raise DecryptionNotImplementedError(
            "MIP decryption is enabled but not implemented yet. Disable the MIP "
            "provider or install the MIP decryption integration."
        )


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


def decryption_status(settings: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Per-provider rows for the admin settings UI."""

    return [
        {
            "provider_id": provider.provider_id,
            "display_name": provider.display_name,
            "implemented": provider.implemented,
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
) -> bytes:
    """Run enabled decryption providers before format handling.

    Returns the (possibly decrypted) bytes. Providers that are disabled never
    inspect the payload, so this is a no-op unless an admin has explicitly
    enabled a provider *and* that provider recognizes the payload.
    """

    for provider in enabled_providers(settings or {}):
        if provider.detect(data, filename=filename, content_type=content_type):
            return provider.decrypt(data, filename=filename)
    return data

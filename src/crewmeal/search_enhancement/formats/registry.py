"""Registry and detection for pluggable document-format handlers.

Handlers register themselves here; the ingest pipeline (SharePoint worker,
admin tryout, CLI) then routes documents by extension/content-type instead of
hard-coding PowerPoint. ``supported=False`` handlers are still registered so a
document is *detected* and reported with a clear message rather than being
silently ignored.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from crewmeal.search_enhancement.formats.base import (
    DocumentHandler,
    UnsupportedFormatError,
)

_HANDLERS: list[DocumentHandler] = []

FORMAT_SETTING_PREFIX = "format."
FORMAT_SETTING_SUFFIX = ".enabled"


def register(handler: DocumentHandler) -> None:
    """Register ``handler``, replacing any existing one with the same id.

    Idempotent replacement keeps module reloads (and repeated imports in tests)
    from raising on duplicate registration.
    """

    for index, existing in enumerate(_HANDLERS):
        if existing.format_id == handler.format_id:
            _HANDLERS[index] = handler
            return
    _HANDLERS.append(handler)


def all_handlers() -> tuple[DocumentHandler, ...]:
    return tuple(_HANDLERS)


def active_handlers() -> tuple[DocumentHandler, ...]:
    """Handlers that are wired end-to-end (``supported=True``)."""

    return tuple(handler for handler in _HANDLERS if handler.supported)


def supported_extensions(*, active_only: bool = True) -> frozenset[str]:
    handlers = active_handlers() if active_only else all_handlers()
    extensions: set[str] = set()
    for handler in handlers:
        extensions.update(handler.extensions)
    return frozenset(extensions)


def supported_content_types(*, active_only: bool = True) -> frozenset[str]:
    handlers = active_handlers() if active_only else all_handlers()
    content_types: set[str] = set()
    for handler in handlers:
        content_types.update(handler.content_types)
    return frozenset(content_types)


def _suffix(filename: str) -> str:
    return Path(filename).suffix.lower()


def detect_handler(
    filename: str,
    data: bytes | None = None,
    *,
    active_only: bool = True,
) -> DocumentHandler:
    """Return the handler for ``filename`` or raise ``UnsupportedFormatError``.

    Detection is by file extension. When ``active_only`` is set and a *registered
    but inactive* handler matches, the error names the format so callers can tell
    "recognized but not yet implemented" apart from "unknown type".
    """

    suffix = _suffix(filename)
    handlers = active_handlers() if active_only else all_handlers()
    for handler in handlers:
        if suffix in handler.extensions:
            return handler

    if active_only:
        for handler in all_handlers():
            if suffix in handler.extensions and not handler.supported:
                raise UnsupportedFormatError(
                    f"{handler.display_name} documents are recognized but not yet "
                    f"supported ({suffix})."
                )

    raise UnsupportedFormatError(
        f"No handler is registered for '{filename}'. "
        f"Supported types: {sorted(supported_extensions(active_only=active_only))}."
    )


def content_fingerprint(
    data: bytes,
    *,
    filename: str,
    active_only: bool = True,
) -> str:
    """Content hash used to detect source changes, dispatched by format.

    For PowerPoint this returns the exact same ``pptx-sha256:`` value as the
    legacy helper, so stored fingerprints stay comparable without migration.
    """

    handler = detect_handler(filename, data, active_only=active_only)
    return handler.fingerprint(data)


# --------------------------------------------------------------------------- #
# Admin-controlled per-format enablement
#
# ``supported`` (on the handler) means "implemented end-to-end". Enablement is a
# separate, admin-controlled gate persisted in the settings store under
# ``format.<id>.enabled``. A format is offered for *ingest* only when it is both
# implemented and enabled. Implemented formats default to ON when no setting
# exists, preserving historical behavior; skeleton formats can never be enabled.
# --------------------------------------------------------------------------- #
def format_setting_key(format_id: str) -> str:
    return f"{FORMAT_SETTING_PREFIX}{format_id}{FORMAT_SETTING_SUFFIX}"


def _handler_by_id(format_id: str) -> DocumentHandler | None:
    for handler in _HANDLERS:
        if handler.format_id == format_id:
            return handler
    return None


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "on", "yes", "y"}
    return False


def is_format_enabled(format_id: str, settings: Mapping[str, Any]) -> bool:
    handler = _handler_by_id(format_id)
    if handler is None or not handler.supported:
        return False
    value = settings.get(format_setting_key(format_id))
    if value is None:
        return True
    return _coerce_bool(value)


def enabled_handlers(settings: Mapping[str, Any]) -> tuple[DocumentHandler, ...]:
    return tuple(
        handler
        for handler in _HANDLERS
        if is_format_enabled(handler.format_id, settings)
    )


def enabled_extensions(settings: Mapping[str, Any]) -> frozenset[str]:
    extensions: set[str] = set()
    for handler in enabled_handlers(settings):
        extensions.update(handler.extensions)
    return frozenset(extensions)


def enabled_content_types(settings: Mapping[str, Any]) -> frozenset[str]:
    content_types: set[str] = set()
    for handler in enabled_handlers(settings):
        content_types.update(handler.content_types)
    return frozenset(content_types)


def format_status(settings: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Per-format rows for the admin settings UI."""

    rows: list[dict[str, Any]] = []
    for handler in _HANDLERS:
        rows.append(
            {
                "format_id": handler.format_id,
                "display_name": handler.display_name,
                "extensions": sorted(handler.extensions),
                "supported": handler.supported,
                "enabled": is_format_enabled(handler.format_id, settings),
            }
        )
    return rows

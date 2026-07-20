"""Core types for pluggable document-format handlers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from crewmeal.config import AppConfig
from crewmeal.models import RendererManifest, SourceManifest
from crewmeal.search_enhancement.models import SlideContent
from crewmeal.search_enhancement.progress import ProgressReporter


class UnsupportedFormatError(ValueError):
    """Raised when no active handler can process a document."""


class InvalidDocumentError(ValueError):
    """Raised when a document fails a handler's format validation."""


class EncryptedDocumentError(InvalidDocumentError):
    """Raised when a document is encrypted and no decryption is available."""


class ProcessingFidelityError(RuntimeError):
    """Raised when rendering changes the source document's page count."""


@dataclass(frozen=True, slots=True)
class PreparedDocument:
    """Format-neutral inputs the shared analysis pipeline consumes.

    ``source_manifest`` carries per-page text evidence (and speaker notes for
    PowerPoint); ``renderer_manifest`` carries rendered page images. In
    semantic-first mode, ``semantic_slides`` contains every page while
    ``renderer_manifest.page_images`` contains only pages that need targeted
    visual analysis.
    ``geometry_by_page`` holds optional deterministic layout facts (currently
    only PowerPoint Gantt/diagram coordinates); other formats leave it empty.
    """

    source_manifest: SourceManifest
    renderer_manifest: RendererManifest
    geometry_by_page: Mapping[int, str] = field(default_factory=dict)
    stage_timings: dict[str, float] = field(default_factory=dict)
    semantic_slides: tuple[SlideContent, ...] | None = None


@runtime_checkable
class DocumentHandler(Protocol):
    """Validates, fingerprints, and prepares one document format.

    ``supported`` is ``False`` for formats that are registered (so they are
    detected and reported clearly) but not yet implemented end-to-end.
    """

    format_id: str
    display_name: str
    extensions: frozenset[str]
    content_types: frozenset[str]
    supported: bool

    def validate(self, data: bytes, *, filename: str, max_bytes: int) -> None: ...

    def fingerprint(self, data: bytes) -> str: ...

    def prepare(
        self,
        data: bytes,
        *,
        source_name: str,
        config: AppConfig,
        reporter: ProgressReporter,
    ) -> PreparedDocument: ...

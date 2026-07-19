from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SourceManifest:
    slide_count: int
    texts_by_slide: dict[int, tuple[str, ...]]
    links_by_slide: dict[int, tuple[str, ...]]
    alt_text_by_slide: dict[int, tuple[str, ...]]
    notes_by_slide: dict[int, tuple[str, ...]]
    element_counts_by_slide: dict[int, dict[str, int]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RendererManifest:
    page_count: int
    texts_by_page: dict[int, tuple[str, ...]]
    links_by_page: dict[int, tuple[str, ...]]
    page_images: dict[int, bytes] = field(repr=False)
    render_dpi: int = 144

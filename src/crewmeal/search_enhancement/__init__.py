"""SharePoint opt-in PowerPoint search enhancement."""

from crewmeal.search_enhancement.html_renderer import (
    CONTENT_HTML_LIMIT_BYTES,
    ContentTooLargeError,
    RenderedHtml,
    render_presentation_html,
)
from crewmeal.search_enhancement.models import (
    SlideContent,
    StructuredAnalysisResult,
)

__all__ = [
    "CONTENT_HTML_LIMIT_BYTES",
    "ContentTooLargeError",
    "RenderedHtml",
    "SlideContent",
    "StructuredAnalysisResult",
    "render_presentation_html",
]

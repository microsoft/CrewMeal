from pathlib import Path

import pytest

from crewmeal.config import AppConfig
from crewmeal.libreoffice import convert_pptx_to_pdf, inspect_pdf


BENCHMARK_DIR = Path(__file__).parents[1] / "benchmark"


def test_libreoffice_renders_benchmark(tmp_path: Path) -> None:
    soffice_path = AppConfig.from_environment().soffice_path
    if soffice_path is None:
        pytest.skip("LibreOffice is not installed.")

    result = convert_pptx_to_pdf(
        BENCHMARK_DIR / "complex-benchmark.pptx",
        tmp_path,
        soffice_path=soffice_path,
    )
    manifest = inspect_pdf(result.pdf_path, render_dpi=72)

    assert result.conversion_seconds > 0
    assert manifest.page_count == 6
    assert manifest.render_dpi == 72
    assert "TABLE-CELL-LAX-9330" in "\n".join(manifest.texts_by_page[2])
    assert "SPEAKER-NOTE-SENTINEL-906" not in "\n".join(
        text
        for page_texts in manifest.texts_by_page.values()
        for text in page_texts
    )
    assert (
        "https://learn.microsoft.com/azure/ai-services/content-understanding/overview"
        in manifest.links_by_page[5]
    )
    assert all(image.startswith(b"\x89PNG") for image in manifest.page_images.values())

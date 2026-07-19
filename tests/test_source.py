import io
import json
import zipfile
from pathlib import Path

from crewmeal.source import build_source_manifest, pptx_content_fingerprint


BENCHMARK_DIR = Path(__file__).parents[1] / "benchmark"


def test_source_manifest_reads_benchmark_metadata() -> None:
    expected = json.loads(
        (BENCHMARK_DIR / "benchmark_manifest.json").read_text(encoding="utf-8")
    )

    manifest = build_source_manifest(BENCHMARK_DIR / "complex-benchmark.pptx")

    assert manifest.slide_count == expected["document"]["slideCount"] == 6
    assert "READING-A" in manifest.texts_by_slide[1]
    assert "TABLE-REGION" in manifest.texts_by_slide[2]
    assert manifest.element_counts_by_slide[2]["tables"] == 1
    assert manifest.element_counts_by_slide[3]["charts"] == 1
    assert any(
        "CHART-SENTINEL" in value for value in manifest.texts_by_slide[3]
    )
    assert manifest.element_counts_by_slide[5]["pictures"] == 1
    assert any("ALT-SENTINEL" in value for value in manifest.alt_text_by_slide[5])
    assert expected["slides"][4]["links"][0] in manifest.links_by_slide[5]
    assert any(
        "SPEAKER-NOTE-SENTINEL-906" in value
        for value in manifest.notes_by_slide[6]
    )


def test_pptx_content_fingerprint_ignores_package_metadata() -> None:
    source = (BENCHMARK_DIR / "complex-benchmark.pptx").read_bytes()

    def rewrite(part_name: str, suffix: bytes) -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(source)) as existing, zipfile.ZipFile(
            output, "w", compression=zipfile.ZIP_DEFLATED
        ) as updated:
            for info in existing.infolist():
                data = existing.read(info.filename)
                if info.filename == part_name:
                    data += suffix
                updated.writestr(info, data)
        return output.getvalue()

    metadata_update = rewrite("docProps/custom.xml", b" ")
    slide_update = rewrite("ppt/slides/slide1.xml", b" ")

    assert pptx_content_fingerprint(metadata_update) == pptx_content_fingerprint(
        source
    )
    assert pptx_content_fingerprint(slide_update) != pptx_content_fingerprint(
        source
    )

import json
from pathlib import Path

import fitz
import pytest

from crewmeal.search_enhancement.rhwp_render_validation import (
    DockerRhwpRunner,
    RhwpValidationError,
    _backend_status,
    _output_path,
    _pixmap_nonwhite_ratio,
    inspect_pdf,
    inspect_svg_directory,
    load_archive_documents,
    render_html_report,
    write_html_report,
)


def test_pixmap_nonwhite_ratio_distinguishes_content():
    document = fitz.open()
    blank_page = document.new_page(width=200, height=200)
    blank = blank_page.get_pixmap(alpha=False)

    content_page = document.new_page(width=200, height=200)
    content_page.insert_text((20, 100), "CrewMeal rhwp")
    content = content_page.get_pixmap(alpha=False)

    assert _pixmap_nonwhite_ratio(blank) == 0
    assert _pixmap_nonwhite_ratio(content) > 0


def test_svg_inspection_counts_pages_and_blank_output(tmp_path):
    (tmp_path / "page-1.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="20">'
        '<text x="1" y="5">hello</text></svg>',
        encoding="utf-8",
    )
    (tmp_path / "page-2.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="20"/>',
        encoding="utf-8",
    )

    result = inspect_svg_directory(tmp_path)

    assert result["valid"] is True
    assert result["page_count"] == 2
    assert result["blank_pages"] == 1


def test_pdf_inspection_reads_pages_text_and_pixels(tmp_path):
    path = tmp_path / "sample.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "rhwp PDF")
    document.save(path)
    document.close()

    result = inspect_pdf(path)

    assert result["valid"] is True
    assert result["page_count"] == 1
    assert result["text_characters"] == 7
    assert result["blank_pages"] == 0


def test_archive_loader_deduplicates_and_uses_content_classification(tmp_path):
    hwp_path = tmp_path / "unique" / "hwpx" / "one.hwpx"
    hwpx_path = tmp_path / "unique" / "hwp" / "two.hwp"
    hwp_path.parent.mkdir(parents=True)
    hwpx_path.parent.mkdir(parents=True)
    hwp_path.write_bytes(b"hwp")
    hwpx_path.write_bytes(b"hwpx")
    records = [
        {
            "repository": "owner/repo",
            "path": "one.hwpx",
            "bytes": 3,
            "sha256": "a" * 64,
            "classification": "extension-mismatch-hwp5",
            "unique_relative_path": "unique/hwpx/one.hwpx",
        },
        {
            "repository": "owner/repo",
            "path": "duplicate.hwpx",
            "bytes": 3,
            "sha256": "a" * 64,
            "classification": "extension-mismatch-hwp5",
            "unique_relative_path": "unique/hwpx/one.hwpx",
        },
        {
            "repository": "owner/repo",
            "path": "two.hwp",
            "bytes": 4,
            "sha256": "b" * 64,
            "classification": "extension-mismatch-hwpx",
            "unique_relative_path": "unique/hwp/two.hwp",
        },
        {
            "repository": "owner/repo",
            "path": "legacy.hwp",
            "bytes": 4,
            "sha256": "c" * 64,
            "classification": "hwp-legacy",
            "unique_relative_path": "unique/hwp/legacy.hwp",
        },
    ]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"records": records}), encoding="utf-8")

    documents = load_archive_documents(manifest)

    assert len(documents) == 2
    assert {document.format for document in documents} == {"hwp", "hwpx"}
    assert next(document for document in documents if document.sha256[0] == "a").format == "hwp"


@pytest.mark.parametrize(
    ("sha256", "relative_path", "message"),
    [
        ("not-a-hash", "unique/one.hwp", "64 hexadecimal"),
        ("a" * 64, "../outside.hwp", "must stay within"),
    ],
)
def test_archive_loader_rejects_unsafe_manifest_paths(
    tmp_path,
    sha256,
    relative_path,
    message,
):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "repository": "owner/repo",
                        "path": "one.hwp",
                        "bytes": 3,
                        "sha256": sha256,
                        "classification": "hwp5-ole",
                        "unique_relative_path": relative_path,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RhwpValidationError, match=message):
        load_archive_documents(manifest)


def test_output_path_rejects_traversal_component(tmp_path):
    with pytest.raises(RhwpValidationError, match="unsafe component"):
        _output_path(tmp_path, "controlled", "../outside")


def test_backend_status_requires_command_and_valid_output():
    assert _backend_status({"status": "success"}, {"valid": True}) == "success"
    assert (
        _backend_status({"status": "success"}, {"valid": False})
        == "invalid-output"
    )
    assert _backend_status({"status": "timeout"}, {"valid": False}) == "timeout"


def test_docker_runner_rejects_paths_outside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    output = tmp_path / "output"
    workspace.mkdir()
    runner = DockerRhwpRunner(
        image="rhwp:test",
        workspace_root=workspace,
        output_root=output,
    )

    assert runner.workspace_path(workspace / "sample.hwp") == "/workspace/sample.hwp"
    with pytest.raises(RhwpValidationError, match="outside"):
        runner.workspace_path(tmp_path / "elsewhere.hwp")


def test_html_report_is_self_contained_and_escapes_failures(tmp_path):
    payload = {
        "generated_at": "2026-01-01T00:00:00+00:00",
        "rhwp": {"version": "0.7.19", "commit": "abc"},
        "controlled": [
            {
                "document_id": "sample",
                "format": "hwp",
                "status": "success",
                "page_counts_agree": True,
                "backends": {
                    backend: {
                        "valid": True,
                        "duration_seconds": 0.1,
                        "page_count": 1,
                        "blank_pages": 0,
                        "warnings": [],
                    }
                    for backend in ("svg", "pdf", "png")
                },
            }
        ],
        "sweep": [
            {
                "source": "owner/<sample>",
                "classification": "hwpx-zip",
                "status": "failed",
                "visual_only": False,
                "text": {"stderr_tail": "<missing part>"},
                "png": {},
            }
        ],
    }

    report = render_html_report(payload)

    assert "rhwp 도입 검증 보고서" in report
    assert "--cp-bg: #f7f4ef;" in report
    assert 'new URLSearchParams(window.location.search).get("scoutTheme")' in report
    assert "owner/&lt;sample&gt;" in report
    assert "&lt;missing part&gt;" in report
    path = write_html_report(tmp_path, payload)
    assert path.read_text(encoding="utf-8") == report

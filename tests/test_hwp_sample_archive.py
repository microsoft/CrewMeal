import json
import zipfile

from crewmeal.hwp_sample_archive import (
    HWP5_MAGIC,
    SOURCES,
    _report_html,
    _summary,
    build_unique_view,
    classify_document,
)


def test_sources_pin_all_investigated_parser_repositories():
    repositories = {source.repository for source in SOURCES}

    assert repositories == {
        "chrisryugj/kordoc",
        "edwardkim/rhwp",
        "KimDaehyeon6873/hwp-hwpx-parser",
        "mete0r/pyhwp",
        "sxa-lab/openhanji",
        "airmang/python-hwpx",
    }
    assert all(len(source.commit) == 40 for source in SOURCES)


def test_classify_document_recognizes_hwp5_and_hwpx(tmp_path):
    hwp = tmp_path / "sample.hwp"
    hwp.write_bytes(HWP5_MAGIC + b"\0" * 32)
    hwpx = tmp_path / "sample.hwpx"
    with zipfile.ZipFile(hwpx, "w") as archive:
        archive.writestr("mimetype", "application/hwp+zip")
        archive.writestr("Contents/section0.xml", "<section/>")

    assert classify_document(hwp) == ("hwp5-ole", True, None)
    assert classify_document(hwpx) == ("hwpx-zip", True, None)


def test_unique_view_groups_duplicate_occurrences(tmp_path):
    first = tmp_path / "by-repository" / "one" / "a.hwp"
    second = tmp_path / "by-repository" / "two" / "b.hwp"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    content = HWP5_MAGIC + b"same"
    first.write_bytes(content)
    second.write_bytes(content)
    sha256 = __import__("hashlib").sha256(content).hexdigest()
    records = [
        {
            "repository": "one/repo",
            "path": "a.hwp",
            "format": "hwp",
            "sha256": sha256,
            "relative_path": first.relative_to(tmp_path).as_posix(),
        },
        {
            "repository": "two/repo",
            "path": "b.hwp",
            "format": "hwp",
            "sha256": sha256,
            "relative_path": second.relative_to(tmp_path).as_posix(),
        },
    ]

    build_unique_view(records, tmp_path)

    assert records[0]["duplicate_count"] == 2
    assert records[0]["unique_relative_path"] == records[1]["unique_relative_path"]
    assert (tmp_path / records[0]["unique_relative_path"]).read_bytes() == content


def test_summary_separates_occurrence_and_unique_bytes():
    records = [
        {
            "repository": "one/repo",
            "format": "hwp",
            "classification": "hwp5-ole",
            "sha256": "a" * 64,
            "bytes": 100,
            "valid": True,
            "git_lfs_oid": None,
        },
        {
            "repository": "one/repo",
            "format": "hwp",
            "classification": "hwp5-ole",
            "sha256": "a" * 64,
            "bytes": 100,
            "valid": True,
            "git_lfs_oid": None,
        },
    ]

    summary = _summary(records, [SOURCES[0]])

    assert summary["occurrences"] == 2
    assert summary["unique_binaries"] == 1
    assert summary["occurrence_bytes"] == 200
    assert summary["unique_bytes"] == 100


def test_html_report_is_self_contained_and_uses_clawpilot_theme():
    payload = {
        "sources": [],
        "summary": {
            "occurrences": 0,
            "unique_binaries": 0,
            "duplicate_occurrences": 0,
            "occurrence_bytes": 0,
            "unique_bytes": 0,
            "formats": {},
        },
        "records": [],
    }

    report = _report_html(payload)

    assert 'new URLSearchParams(window.location.search).get("scoutTheme")' in report
    assert "--cp-bg: #f7f4ef;" in report
    assert 'font-family: "Segoe UI", Aptos, Calibri' in report
    assert '<script id="inventory-data" type="application/json">' in report
    json_text = report.split(
        '<script id="inventory-data" type="application/json">', 1
    )[1].split("</script>", 1)[0]
    assert json.loads(json_text)["records"] == []

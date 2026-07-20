import json
from pathlib import Path

import pytest

from crewmeal.search_enhancement.hwp_parser_benchmark import (
    DEFAULT_MANIFEST_PATH,
    AdapterOutputError,
    CorpusDocument,
    CorpusManifestError,
    add_agreement_metrics,
    aggregate_records,
    load_corpus_manifest,
    render_html_report,
    render_markdown_report,
    score_expectations,
    validate_adapter_output,
    write_benchmark_reports,
)


def _document(**expectations):
    return CorpusDocument(
        id="sample",
        format="hwp",
        filename="sample.hwp",
        source={
            "repository": "owner/repo",
            "commit": "a" * 40,
            "path": "sample.hwp",
            "url": "https://example.test/sample.hwp",
        },
        expected_bytes=8,
        sha256="b" * 64,
        license="MIT",
        provenance="Test",
        tags=("fixture",),
        expectations=expectations,
    )


def _output(text="alpha beta", **overrides):
    value = {
        "schema_version": 1,
        "parser": "parser",
        "version": "1.0.0",
        "text": text,
        "markdown": "",
        "tables": [],
        "images_count": 0,
        "pages_count": 1,
        "footnotes_count": 0,
        "endnotes_count": 0,
        "links_count": 0,
        "metadata": {},
        "warnings": [],
    }
    value.update(overrides)
    return value


def test_default_manifest_has_ten_unique_files_per_format():
    documents = load_corpus_manifest()

    assert len(documents) == 20
    assert sum(document.format == "hwp" for document in documents) == 10
    assert sum(document.format == "hwpx" for document in documents) == 10
    assert len({document.sha256 for document in documents}) == 20
    assert all(document.source["commit"] in document.source["url"] for document in documents)


def test_manifest_rejects_duplicate_binary(tmp_path):
    payload = json.loads(DEFAULT_MANIFEST_PATH.read_text(encoding="utf-8"))
    payload["documents"][1]["sha256"] = payload["documents"][0]["sha256"]
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(CorpusManifestError, match="duplicate binary"):
        load_corpus_manifest(path)


def test_adapter_contract_rejects_negative_counts():
    output = _output(images_count=-1)

    with pytest.raises(AdapterOutputError, match="images_count"):
        validate_adapter_output(output)


def test_expectation_score_checks_text_and_structure():
    document = _document(
        sentinels=["회의 결과", "동의"],
        minTables=1,
        minImages=2,
        minPages=3,
    )
    output = _output(
        "회의   결과: 전원 동의",
        tables=[{"rows": 1, "columns": 1, "cells": [["value"]]}],
        images_count=1,
        pages_count=3,
    )

    score = score_expectations(document, output)

    assert score["passed"] == 4
    assert score["total"] == 5
    assert next(
        check for check in score["checks"] if check["name"] == "minImages"
    )["passed"] is False


def test_agreement_is_labeled_separately_from_expectations():
    records = []
    outputs = {}
    for engine, text in (
        ("crewmeal", "alpha beta"),
        ("kordoc", "alpha beta gamma"),
        ("rhwp", "alpha beta delta"),
    ):
        records.append(
            {
                "engine": engine,
                "document_id": "sample",
                "format": "hwp",
                "status": "success",
                "duration_seconds": 1.0,
                "version": "1",
                "metrics": {
                    "characters": len(text),
                    "expectations": {"passed": 1, "total": 1},
                },
            }
        )
        outputs[(engine, "sample")] = _output(text)

    add_agreement_metrics(records, outputs)

    assert records[0]["metrics"]["consensus_f1"] == 1.0
    assert records[1]["metrics"]["consensus_precision"] < 1.0
    assert records[2]["metrics"]["pairwise_jaccard_median"] > 0


def test_aggregate_counts_failures_against_expected_evidence():
    records = [
        {
            "engine": "pyhwp",
            "document_id": "one",
            "format": "hwp",
            "status": "success",
            "duration_seconds": 2.0,
            "version": "0.1b15",
            "metrics": {
                "characters": 10,
                "expectations": {"passed": 2, "total": 3},
                "consensus_f1": 0.75,
            },
        },
        {
            "engine": "pyhwp",
            "document_id": "two",
            "format": "hwp",
            "status": "error",
            "duration_seconds": 0.2,
            "version": None,
            "metrics": {"expectations": {"passed": 0, "total": 2}},
        },
    ]

    aggregate = aggregate_records(records, ["pyhwp"])[0]

    assert aggregate["success_rate"] == 50.0
    assert aggregate["expectation_recall"] == 40.0
    assert aggregate["median_seconds"] == 2.0


def test_report_states_agreement_is_not_accuracy():
    summary = {
        "generated_at": "2026-01-01T00:00:00+00:00",
        "aggregates": [
            {
                "engine": "crewmeal",
                "format": "hwp",
                "successful": 1,
                "attempted": 1,
                "success_rate": 100.0,
                "expectation_recall": 50.0,
                "median_agreement_f1": 0.8,
                "median_seconds": 1.0,
                "p95_seconds": 1.0,
                "empty_text": 0,
            }
        ],
        "availability": {
            "crewmeal": {"available": True, "detail": "LibreOffice"}
        },
        "runs": [],
    }

    report = render_markdown_report(summary, [_document(sentinels=["alpha"])])

    assert "it is not an accuracy score" in report
    assert "Third-party document binaries" in report


def test_html_report_is_self_contained_and_escapes_environment_details(tmp_path):
    aggregates = []
    runs = []
    for file_format, recall in (("hwp", 84.09), ("hwpx", 93.18)):
        aggregates.append(
            {
                "engine": "kordoc",
                "format": file_format,
                "version": "4.2.3",
                "successful": 1,
                "attempted": 1,
                "success_rate": 100.0,
                "expectation_recall": recall,
                "median_agreement_f1": 1.0,
                "median_seconds": 0.3,
                "p95_seconds": 0.4,
                "empty_text": 0,
            }
        )
        runs.append(
            {
                "engine": "kordoc",
                "document_id": f"sample-{file_format}",
                "format": file_format,
                "status": "success",
            }
        )
    summary = {
        "generated_at": "2026-01-01T00:00:00+00:00",
        "aggregates": aggregates,
        "availability": {
            "kordoc": {"available": True, "detail": "<local executable>"}
        },
        "runs": runs,
    }

    report = render_html_report(summary, [_document(sentinels=["alpha"])])

    assert "HWP/HWPX 파서 벤치마크" in report
    assert '--cp-bg: #f7f4ef;' in report
    assert 'new URLSearchParams(window.location.search).get("scoutTheme")' in report
    assert "정확도 점수가 아닙니다" in report
    assert "&lt;local executable&gt;" in report
    assert "__RESULT_ROWS__" not in report

    markdown_path, html_path = write_benchmark_reports(
        summary, [_document(sentinels=["alpha"])], tmp_path
    )
    assert markdown_path.is_file()
    assert html_path.read_text(encoding="utf-8") == report

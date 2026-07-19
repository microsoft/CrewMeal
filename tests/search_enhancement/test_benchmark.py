import argparse

import pytest

from crewmeal.search_enhancement.benchmark import (
    _model_specs,
    _parse_price,
    score_benchmark,
)


def _manifest():
    return {
        "slides": [
            {
                "slideNumber": 1,
                "title": "Process",
                "sentinels": ["VISIBLE"],
                "readingOrder": ["First", "Second"],
                "processOrder": ["Collect", "Approve"],
                "notes": ["SECRET-NOTE"],
            },
            {
                "slideNumber": 2,
                "title": "Table",
                "sentinels": ["TABLE"],
                "table": {
                    "rowCount": 1,
                    "columnCount": 2,
                    "expectedValues": ["ICN", "12,480"],
                },
                "chart": {
                    "categories": ["Q1"],
                    "series": {
                        "Crew meals": [86],
                        "Special meals": [14],
                    },
                    "concepts": ["Q1 has the highest Crew meals share"],
                },
                "altText": ["SECRET-ALT"],
            },
        ]
    }


def _slides():
    return [
        {
            "slide_number": 1,
            "title": "Process",
            "summary": "VISIBLE. First, then Second.",
            "flows": [{"steps": ["Collect", "Approve"]}],
        },
        {
            "slide_number": 2,
            "title": "Table",
            "facts": ["TABLE"],
            "tables": [{"headers": ["Airport", "Meals"], "rows": [["ICN", "12,480"]]}],
            "charts": [
                {
                    "data_points": [
                        {"series": "Crew meals", "label": "Q1", "value": "86"},
                        {"series": "Special meals", "label": "Q1", "value": "14"},
                    ],
                    "insights": ["Q1 is the highest."],
                }
            ],
        },
    ]


def test_score_benchmark_accepts_complete_structured_output():
    score = score_benchmark(_manifest(), _slides())

    assert score.passed == score.total
    assert score.percent == 100.0
    assert score.to_dict()["byCategory"]["core"]["percent"] == 100.0


def test_score_benchmark_detects_wrong_order_and_hidden_content_leak():
    slides = _slides()
    slides[0]["flows"] = [{"steps": ["Approve", "Collect"]}]
    slides[0]["facts"] = ["SECRET-NOTE"]

    score = score_benchmark(_manifest(), slides)
    failures = {check.name for check in score.checks if not check.passed}

    assert "process_order" in failures
    assert "speaker_notes_excluded" in failures


def test_score_benchmark_rejects_prefix_only_matches():
    manifest = {
        "slides": [
            {
                "slideNumber": 1,
                "title": "Identifiers",
                "sentinels": ["DUPLICATE-ALPHA", "DUPLICATE-ALPHA-2", "9"],
            }
        ]
    }
    slides = [
        {
            "slide_number": 1,
            "title": "Identifiers",
            "facts": ["DUPLICATE-ALPHA-2", "91"],
        }
    ]

    score = score_benchmark(manifest, slides)

    assert next(check for check in score.checks if check.name == "sentinels").passed is False


def test_score_benchmark_allows_korean_particle_after_identifier():
    manifest = {
        "slides": [
            {
                "slideNumber": 1,
                "title": "Identifier",
                "sentinels": ["TABLE-CELL-LAX-9330"],
            }
        ]
    }
    slides = [
        {
            "slide_number": 1,
            "title": "Identifier",
            "facts": ["TABLE-CELL-LAX-9330이 표시되어 있다."],
        }
    ]

    score = score_benchmark(manifest, slides)

    assert score.passed == score.total


def test_chart_checks_read_structured_data_and_insights_only():
    slides = _slides()
    slides[1]["summary"] = "Q1 Meals 86 highest"
    slides[1]["charts"] = [
        {
            "data_points": [
                {"series": "Crew meals", "label": "Q1", "value": "8"},
                {"series": "Special meals", "label": "Q1", "value": "14"},
            ],
            "insights": ["Q1 is the lowest."],
        }
    ]

    score = score_benchmark(_manifest(), slides)
    failures = {check.name for check in score.checks if not check.passed}

    assert "chart_data" in failures
    assert "chart_insights" in failures


def test_chart_insight_requires_the_expected_series():
    slides = _slides()
    slides[1]["charts"][0]["insights"] = [
        "Q1 has the highest Special meals share."
    ]

    score = score_benchmark(_manifest(), slides)

    assert next(
        check for check in score.checks if check.name == "chart_insights"
    ).passed is False


def test_score_benchmark_rejects_unscored_manifest_keys():
    manifest = _manifest()
    manifest["slides"][0]["links"] = ["https://example.com"]

    with pytest.raises(ValueError, match="Unsupported benchmark slide"):
        score_benchmark(manifest, _slides())


def test_score_benchmark_rejects_malformed_expectations():
    manifest = _manifest()
    manifest["slides"][0]["readingOrder"] = "First, Second"

    with pytest.raises(ValueError, match="readingOrder must be an array"):
        score_benchmark(manifest, _slides())


@pytest.mark.parametrize("value", ["model=nan,1", "model=1,inf"])
def test_parse_price_rejects_non_finite_values(value):
    with pytest.raises(argparse.ArgumentTypeError, match="finite"):
        _parse_price(value)


def test_model_specs_rejects_unknown_price_labels():
    with pytest.raises(ValueError, match="no matching"):
        _model_specs([("luna", "luna-deployment")], [("typo", (1.0, 6.0))])

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from crewmeal.config import AppConfig
from crewmeal.search_enhancement.formats import detect_handler
from crewmeal.search_enhancement.progress import NullProgressReporter
from crewmeal.search_enhancement.structured_analysis import (
    StructuredSlideAnalysisError,
    StructuredSlideAnalysisService,
)
from crewmeal.search_enhancement.vision_model import (
    PROVIDER_AZURE_OPENAI,
    VisionModelSettings,
)


@dataclass(frozen=True, slots=True)
class BenchmarkCheck:
    slide_number: int
    name: str
    category: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class BenchmarkScore:
    checks: tuple[BenchmarkCheck, ...]

    @property
    def passed(self) -> int:
        return sum(check.passed for check in self.checks)

    @property
    def total(self) -> int:
        return len(self.checks)

    @property
    def percent(self) -> float:
        if not self.checks:
            return 0.0
        return round(self.passed / self.total * 100, 2)

    def to_dict(self) -> dict[str, Any]:
        categories: dict[str, dict[str, float | int]] = {}
        for category in sorted({check.category for check in self.checks}):
            category_checks = [
                check for check in self.checks if check.category == category
            ]
            passed = sum(check.passed for check in category_checks)
            total = len(category_checks)
            categories[category] = {
                "passed": passed,
                "total": total,
                "percent": round(passed / total * 100, 2) if total else 0.0,
            }
        return {
            "passed": self.passed,
            "total": self.total,
            "percent": self.percent,
            "byCategory": categories,
            "checks": [asdict(check) for check in self.checks],
        }


@dataclass(frozen=True, slots=True)
class ModelSpec:
    label: str
    deployment: str
    input_usd_per_million: float | None = None
    output_usd_per_million: float | None = None


_SLIDE_EXPECTATION_KEYS = frozenset(
    {
        "slideNumber",
        "title",
        "sentinels",
        "readingOrder",
        "table",
        "chart",
        "processOrder",
        "imageOcr",
        "mixedText",
        "altText",
        "notes",
    }
)
_TABLE_EXPECTATION_KEYS = frozenset(
    {"expectedValues", "rowCount", "columnCount"}
)
_CHART_EXPECTATION_KEYS = frozenset(
    {"categories", "series", "concepts"}
)
_ARRAY_EXPECTATION_KEYS = (
    "sentinels",
    "readingOrder",
    "processOrder",
    "imageOcr",
    "mixedText",
    "altText",
    "notes",
)


def score_benchmark(
    manifest: Mapping[str, Any],
    slides: Sequence[Mapping[str, Any]],
) -> BenchmarkScore:
    _validate_manifest(manifest)
    slides_by_number = {
        _slide_number(slide): slide
        for slide in slides
    }
    checks: list[BenchmarkCheck] = []

    for expected in manifest.get("slides", []):
        slide_number = int(expected["slideNumber"])
        slide = slides_by_number.get(slide_number)
        checks.append(
            BenchmarkCheck(
                slide_number,
                "slide_present",
                "core",
                slide is not None,
                "slide returned" if slide is not None else "slide missing",
            )
        )
        slide = slide or {}
        text = "\n".join(_all_text(slide))

        _append_term_check(
            checks,
            slide_number,
            "title",
            str(slide.get("title", "")),
            [str(expected["title"])],
            category="core",
        )
        _append_term_check(
            checks,
            slide_number,
            "sentinels",
            text,
            [str(item) for item in expected.get("sentinels", [])],
            category="literal_recall",
        )

        reading_order = [str(item) for item in expected.get("readingOrder", [])]
        if reading_order:
            passed = _contains_in_order(text, reading_order)
            checks.append(
                BenchmarkCheck(
                    slide_number,
                    "reading_order",
                    "core",
                    passed,
                    "correct order" if passed else "expected order not preserved",
                )
            )

        table = expected.get("table")
        if isinstance(table, Mapping):
            tables = [
                item
                for item in slide.get("tables", [])
                if isinstance(item, Mapping)
            ]
            structure_passed, structure_detail = _table_structure_matches(
                tables, table
            )
            checks.append(
                BenchmarkCheck(
                    slide_number,
                    "table_structure",
                    "core",
                    structure_passed,
                    structure_detail,
                )
            )
            _append_term_check(
                checks,
                slide_number,
                "table_values",
                "\n".join(_all_text(tables)),
                [str(item) for item in table.get("expectedValues", [])],
                category="core",
            )

        chart = expected.get("chart")
        if isinstance(chart, Mapping):
            charts = [
                item
                for item in slide.get("charts", [])
                if isinstance(item, Mapping)
            ]
            checks.append(
                BenchmarkCheck(
                    slide_number,
                    "chart_structure",
                    "core",
                    bool(charts),
                    "chart emitted" if charts else "no chart emitted",
                )
            )
            data_passed, data_detail = _chart_data_matches(charts, chart)
            checks.append(
                BenchmarkCheck(
                    slide_number,
                    "chart_data",
                    "core",
                    data_passed,
                    data_detail,
                )
            )
            concepts = [str(item) for item in chart.get("concepts", [])]
            if concepts:
                insights_passed, insights_detail = _chart_insights_match(
                    charts,
                    chart,
                )
                checks.append(
                    BenchmarkCheck(
                        slide_number,
                        "chart_insights",
                        "core",
                        insights_passed,
                        insights_detail,
                    )
                )

        process_order = [str(item) for item in expected.get("processOrder", [])]
        if process_order:
            passed = any(
                _contains_in_order(
                    "\n".join(_all_text(flow.get("steps", []))),
                    process_order,
                )
                for flow in slide.get("flows", [])
                if isinstance(flow, Mapping)
            )
            checks.append(
                BenchmarkCheck(
                    slide_number,
                    "process_order",
                    "core",
                    passed,
                    "ordered flow emitted" if passed else "ordered flow missing",
                )
            )

        for key, check_name in (
            ("imageOcr", "image_ocr"),
            ("mixedText", "mixed_text"),
        ):
            values = [str(item) for item in expected.get(key, [])]
            if values:
                _append_term_check(
                    checks,
                    slide_number,
                    check_name,
                    text,
                    values,
                    category="core",
                )

        for key, check_name in (
            ("altText", "alt_text_excluded"),
            ("notes", "speaker_notes_excluded"),
        ):
            forbidden = [str(item) for item in expected.get(key, [])]
            if forbidden:
                leaked = _matching_terms(text, forbidden)
                checks.append(
                    BenchmarkCheck(
                        slide_number,
                        check_name,
                        "safety",
                        not leaked,
                        (
                            "not exposed"
                            if not leaked
                            else f"unexpectedly exposed: {', '.join(leaked)}"
                        ),
                    )
                )

    return BenchmarkScore(tuple(checks))


def _slide_number(slide: Mapping[str, Any]) -> int:
    value = slide.get("slide_number", slide.get("slideNumber"))
    if not isinstance(value, int):
        raise ValueError("Every benchmark slide must have an integer slide number.")
    return value


def _validate_manifest(manifest: Mapping[str, Any]) -> None:
    slides = manifest.get("slides")
    if not isinstance(slides, Sequence) or isinstance(slides, (str, bytes)):
        raise ValueError("Benchmark manifest must contain a slides array.")
    slide_numbers: set[int] = set()
    for index, expected in enumerate(slides, start=1):
        if not isinstance(expected, Mapping):
            raise ValueError(f"Benchmark slide {index} must be an object.")
        _reject_unknown_keys(
            expected,
            _SLIDE_EXPECTATION_KEYS,
            f"benchmark slide {index}",
        )
        slide_number = expected.get("slideNumber")
        if not isinstance(slide_number, int) or isinstance(slide_number, bool):
            raise ValueError(
                f"Benchmark slide {index} must have an integer slideNumber."
            )
        if slide_number in slide_numbers:
            raise ValueError(f"Duplicate benchmark slideNumber: {slide_number}.")
        slide_numbers.add(slide_number)
        if not isinstance(expected.get("title"), str):
            raise ValueError(f"Benchmark slide {index} must have a title.")
        for key in _ARRAY_EXPECTATION_KEYS:
            if key in expected:
                _validate_array(expected[key], f"benchmark slide {index} {key}")

        table = expected.get("table")
        if table is not None:
            if not isinstance(table, Mapping):
                raise ValueError(f"Benchmark slide {index} table must be an object.")
            _reject_unknown_keys(
                table,
                _TABLE_EXPECTATION_KEYS,
                f"benchmark slide {index} table",
            )
            if "expectedValues" in table:
                _validate_array(
                    table["expectedValues"],
                    f"benchmark slide {index} table expectedValues",
                )
            for key in ("rowCount", "columnCount"):
                value = table.get(key)
                if value is not None and (
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or value <= 0
                ):
                    raise ValueError(
                        f"Benchmark slide {index} table {key} "
                        "must be a positive integer."
                    )

        chart = expected.get("chart")
        if chart is not None:
            if not isinstance(chart, Mapping):
                raise ValueError(f"Benchmark slide {index} chart must be an object.")
            _reject_unknown_keys(
                chart,
                _CHART_EXPECTATION_KEYS,
                f"benchmark slide {index} chart",
            )
            categories = _validate_array(
                chart.get("categories", []),
                f"benchmark slide {index} chart categories",
            )
            _validate_array(
                chart.get("concepts", []),
                f"benchmark slide {index} chart concepts",
            )
            series = chart.get("series", {})
            if not isinstance(series, Mapping):
                raise ValueError(
                    f"Benchmark slide {index} chart series must be an object."
                )
            for series_name, values in series.items():
                series_values = _validate_array(
                    values,
                    f"benchmark slide {index} chart series {series_name}",
                )
                if len(series_values) != len(categories):
                    raise ValueError(
                        f"Benchmark slide {index} chart series {series_name} "
                        "does not match category count."
                    )


def _reject_unknown_keys(
    value: Mapping[str, Any],
    allowed: frozenset[str],
    scope: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"Unsupported {scope} keys: {', '.join(unknown)}.")


def _validate_array(value: Any, scope: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{scope} must be an array.")
    return value


def _table_structure_matches(
    tables: Sequence[Mapping[str, Any]],
    expected: Mapping[str, Any],
) -> tuple[bool, str]:
    if not tables:
        return False, "no table emitted"
    expected_rows = expected.get("rowCount")
    expected_columns = expected.get("columnCount")
    if expected_rows is None and expected_columns is None:
        return True, "table emitted"

    for table in tables:
        headers = table.get("headers", [])
        rows = table.get("rows", [])
        if not isinstance(headers, Sequence) or isinstance(headers, (str, bytes)):
            continue
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            continue
        row_count_matches = (
            expected_rows is None or len(rows) == int(expected_rows)
        )
        column_count_matches = (
            expected_columns is None
            or (
                len(headers) == int(expected_columns)
                and all(
                    isinstance(row, Sequence)
                    and not isinstance(row, (str, bytes))
                    and len(row) == int(expected_columns)
                    for row in rows
                )
            )
        )
        if row_count_matches and column_count_matches:
            return True, "table dimensions preserved"
    return (
        False,
        f"expected {expected_rows or '*'} rows x "
        f"{expected_columns or '*'} columns",
    )


def _append_term_check(
    checks: list[BenchmarkCheck],
    slide_number: int,
    name: str,
    text: str,
    expected: Sequence[str],
    *,
    category: str,
) -> None:
    if not expected:
        return
    missing = [
        term
        for term in expected
        if _find_term(text, term) < 0
    ]
    checks.append(
        BenchmarkCheck(
            slide_number,
            name,
            category,
            not missing,
            "all present" if not missing else f"missing: {', '.join(missing)}",
        )
    )


def _matching_terms(text: str, terms: Sequence[str]) -> list[str]:
    return [term for term in terms if _find_term(text, term) >= 0]


def _contains_in_order(text: str, terms: Sequence[str]) -> bool:
    cursor = 0
    for term in terms:
        index = _find_term(text, term, start=cursor)
        if index < 0:
            return False
        cursor = index + len(_normalize(term))
    return True


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _find_term(text: str, term: str, *, start: int = 0) -> int:
    normalized_text = _normalize(text)
    normalized_term = _normalize(term)
    if not normalized_term:
        return -1
    leading = (
        r"(?<![a-z0-9_\-])"
        if re.match(r"[a-z0-9_\-]", normalized_term[0])
        else ""
    )
    trailing = (
        r"(?![a-z0-9_]|-[a-z0-9_])"
        if re.match(r"[a-z0-9_\-]", normalized_term[-1])
        else ""
    )
    match = re.search(
        f"{leading}{re.escape(normalized_term)}{trailing}",
        normalized_text[start:],
    )
    return -1 if match is None else start + match.start()


def _all_text(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _all_text(item)
    elif isinstance(value, Sequence) and not isinstance(
        value, (bytes, bytearray, str)
    ):
        for item in value:
            yield from _all_text(item)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        yield str(value)


def _chart_data_matches(
    charts: Sequence[Mapping[str, Any]],
    expected: Mapping[str, Any],
) -> tuple[bool, str]:
    categories = [str(item) for item in expected.get("categories", [])]
    series = expected.get("series", {})
    if not isinstance(series, Mapping):
        raise ValueError("Benchmark chart series must be an object.")

    expected_points: set[tuple[str, str, str]] = set()
    for series_name, values in series.items():
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            raise ValueError("Benchmark chart series values must be arrays.")
        if len(values) != len(categories):
            raise ValueError(
                f"Benchmark chart series {series_name} does not match category count."
            )
        expected_points.update(
            (
                _normalize(str(series_name)),
                _normalize(category),
                _normalize(str(value)),
            )
            for category, value in zip(categories, values, strict=True)
        )

    actual_points: set[tuple[str, str, str]] = set()
    for chart in charts:
        points = chart.get("data_points", chart.get("dataPoints", []))
        if not isinstance(points, Sequence) or isinstance(points, (str, bytes)):
            continue
        for point in points:
            if not isinstance(point, Mapping):
                continue
            actual_points.add(
                (
                    _normalize(str(point.get("series", ""))),
                    _normalize(str(point.get("label", ""))),
                    _normalize(str(point.get("value", ""))),
                )
            )

    missing = sorted(expected_points - actual_points)
    if not missing:
        return True, "all chart data points preserved"
    formatted = ", ".join(
        f"{series_name}/{category}={value}"
        for series_name, category, value in missing
    )
    return False, f"missing chart points: {formatted}"


def _chart_insights_match(
    charts: Sequence[Mapping[str, Any]],
    expected: Mapping[str, Any],
) -> tuple[bool, str]:
    insights = [
        str(insight)
        for chart in charts
        for insight in chart.get("insights", [])
    ]
    categories = [str(item) for item in expected.get("categories", [])]
    series_names = [str(item) for item in expected.get("series", {})]
    missing: list[str] = []
    for concept_value in expected.get("concepts", []):
        concept = str(concept_value)
        expected_category = next(
            (
                category
                for category in categories
                if _find_term(concept, category) >= 0
            ),
            None,
        )
        expected_series = next(
            (
                series_name
                for series_name in series_names
                if _contains_phrase_tokens(concept, series_name)
            ),
            None,
        )
        polarity = _concept_polarity(concept)
        if expected_category is None or expected_series is None or polarity is None:
            if not any(_find_term(insight, concept) >= 0 for insight in insights):
                missing.append(concept)
            continue
        markers = _INSIGHT_MARKERS[polarity]
        matched = False
        for insight in insights:
            if (
                _find_term(insight, expected_category) < 0
                or not any(_find_term(insight, marker) >= 0 for marker in markers)
            ):
                continue
            mentioned_series = [
                series_name
                for series_name in series_names
                if _contains_phrase_tokens(insight, series_name)
            ]
            if not mentioned_series or expected_series in mentioned_series:
                matched = True
                break
        if not matched:
            missing.append(concept)
    if not missing:
        return True, "chart insights preserve expected extrema"
    return False, f"missing chart insights: {', '.join(missing)}"


_INSIGHT_MARKERS = {
    "highest": ("highest", "maximum", "max", "가장 높", "최고", "최대"),
    "lowest": ("lowest", "minimum", "min", "가장 낮", "최저", "최소"),
}


def _concept_polarity(concept: str) -> str | None:
    for polarity, markers in _INSIGHT_MARKERS.items():
        if any(_find_term(concept, marker) >= 0 for marker in markers):
            return polarity
    return None


def _contains_phrase_tokens(text: str, phrase: str) -> bool:
    def tokens(value: str) -> set[str]:
        result = set(re.findall(r"[a-z0-9가-힣]+", value.casefold()))
        return {
            token[:-1] if len(token) > 3 and token.endswith("s") else token
            for token in result
        }

    phrase_tokens = tokens(phrase)
    return bool(phrase_tokens) and phrase_tokens <= tokens(text)


def _parse_assignment(value: str, *, option: str) -> tuple[str, str]:
    name, separator, assigned = value.partition("=")
    if not separator or not name.strip() or not assigned.strip():
        raise argparse.ArgumentTypeError(f"{option} must use NAME=VALUE.")
    return name.strip(), assigned.strip()


def _parse_model(value: str) -> tuple[str, str]:
    return _parse_assignment(value, option="--model")


def _parse_price(value: str) -> tuple[str, tuple[float, float]]:
    name, prices = _parse_assignment(value, option="--price")
    raw_input, separator, raw_output = prices.partition(",")
    if not separator:
        raise argparse.ArgumentTypeError(
            "--price must use MODEL=INPUT_USD_PER_M,OUTPUT_USD_PER_M."
        )
    try:
        input_price = float(raw_input)
        output_price = float(raw_output)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "--price values must be non-negative numbers."
        ) from exc
    if (
        not math.isfinite(input_price)
        or not math.isfinite(output_price)
        or input_price < 0
        or output_price < 0
    ):
        raise argparse.ArgumentTypeError(
            "--price values must be finite non-negative numbers."
        )
    return name, (input_price, output_price)


def _model_specs(
    models: Sequence[tuple[str, str]],
    prices: Sequence[tuple[str, tuple[float, float]]],
) -> tuple[ModelSpec, ...]:
    if not models:
        raise ValueError("At least one --model NAME=DEPLOYMENT is required.")
    model_labels = [label for label, _ in models]
    if len(model_labels) != len(set(model_labels)):
        raise ValueError("--model labels must be unique.")
    price_labels = [label for label, _ in prices]
    if len(price_labels) != len(set(price_labels)):
        raise ValueError("--price labels must be unique.")
    unknown_prices = sorted(set(price_labels) - set(model_labels))
    if unknown_prices:
        raise ValueError(
            "--price has no matching --model: " + ", ".join(unknown_prices)
        )
    price_by_model = dict(prices)
    return tuple(
        ModelSpec(label, deployment, *(price_by_model.get(label, (None, None))))
        for label, deployment in models
    )


def _token_counts(usage: Mapping[str, Any]) -> tuple[int, int]:
    tokens = usage.get("tokens", {})
    if not isinstance(tokens, Mapping):
        return 0, 0
    input_tokens = sum(
        int(value)
        for key, value in tokens.items()
        if str(key).endswith("-input")
    )
    output_tokens = sum(
        int(value)
        for key, value in tokens.items()
        if str(key).endswith("-output")
    )
    return input_tokens, output_tokens


def _estimated_cost(
    input_tokens: int,
    output_tokens: int,
    spec: ModelSpec,
) -> float | None:
    if (
        spec.input_usd_per_million is None
        or spec.output_usd_per_million is None
    ):
        return None
    return round(
        input_tokens / 1_000_000 * spec.input_usd_per_million
        + output_tokens / 1_000_000 * spec.output_usd_per_million,
        6,
    )


def _build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(
        description="Compare CrewMeal vision models on the bundled benchmark deck."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=root / "benchmark" / "complex-benchmark.pptx",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=root / "benchmark" / "vision_benchmark_manifest.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=root / "result" / "vision-model-benchmark.json",
    )
    parser.add_argument(
        "--endpoint",
        help="Foundry account endpoint; defaults to CONTENTUNDERSTANDING_ENDPOINT.",
    )
    parser.add_argument(
        "--model",
        action="append",
        type=_parse_model,
        default=[],
        metavar="NAME=DEPLOYMENT",
        help="Model label and Azure deployment. Repeat to compare models.",
    )
    parser.add_argument(
        "--price",
        action="append",
        type=_parse_price,
        default=[],
        metavar="NAME=INPUT,OUTPUT",
        help="Optional USD rates per million tokens for cost estimation.",
    )
    parser.add_argument("--reasoning-effort", default="high")
    parser.add_argument("--workers", type=int, default=2)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        specs = _model_specs(args.model, args.price)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if args.workers <= 0:
        raise SystemExit("--workers must be positive.")

    config = AppConfig.from_environment()
    endpoint = (args.endpoint or config.endpoint or "").rstrip("/")
    if not endpoint:
        raise SystemExit(
            "Set CONTENTUNDERSTANDING_ENDPOINT or pass --endpoint."
        )
    config = replace(
        config,
        endpoint=endpoint,
        slide_image_max_workers=args.workers,
    )

    source_bytes = args.input.read_bytes()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if not isinstance(manifest, Mapping):
        raise SystemExit("Benchmark manifest root must be an object.")
    try:
        _validate_manifest(manifest)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    handler = detect_handler(args.input.name)
    handler.validate(
        source_bytes,
        filename=args.input.name,
        max_bytes=config.max_upload_bytes,
    )
    prepared = handler.prepare(
        source_bytes,
        source_name=args.input.name,
        config=config,
        reporter=NullProgressReporter(),
    )

    report: dict[str, Any] = {
        "generatedAt": datetime.now(UTC).isoformat(),
        "source": str(args.input),
        "manifest": str(args.manifest),
        "render": {
            **prepared.stage_timings,
            "dpi": prepared.renderer_manifest.render_dpi,
            "pages": prepared.renderer_manifest.page_count,
        },
        "models": [],
    }

    for spec in specs:
        print(
            f"Benchmarking {spec.label} ({spec.deployment})...",
            file=sys.stderr,
            flush=True,
        )
        model = VisionModelSettings(
            provider=PROVIDER_AZURE_OPENAI,
            model=spec.label,
            deployment=spec.deployment,
            base_url=None,
            reasoning_effort=args.reasoning_effort,
        )
        analysis_started = time.perf_counter()
        try:
            with StructuredSlideAnalysisService(config, model=model) as service:
                analysis = service.analyze(
                    prepared.renderer_manifest.page_images,
                    source_manifest=prepared.source_manifest,
                    source_name=args.input.name,
                    geometry_by_slide=prepared.geometry_by_page,
                )
        except StructuredSlideAnalysisError as exc:
            input_tokens = exc.input_tokens
            output_tokens = exc.output_tokens
            report["models"].append(
                {
                    "model": spec.label,
                    "deployment": spec.deployment,
                    "status": "failed",
                    "error": str(exc),
                    "analysisSeconds": round(
                        time.perf_counter() - analysis_started,
                        3,
                    ),
                    "inputTokens": input_tokens,
                    "outputTokens": output_tokens,
                    "estimatedCostUsd": _estimated_cost(
                        input_tokens,
                        output_tokens,
                        spec,
                    ),
                    "pricingUsdPerMillion": {
                        "input": spec.input_usd_per_million,
                        "output": spec.output_usd_per_million,
                    },
                }
            )
            continue

        slides = [asdict(slide) for slide in analysis.slides]
        score = score_benchmark(manifest, slides)
        input_tokens, output_tokens = _token_counts(analysis.usage)
        report["models"].append(
            {
                "model": spec.label,
                "deployment": spec.deployment,
                "status": "succeeded",
                "quality": score.to_dict(),
                "analysisSeconds": round(analysis.analysis_seconds, 3),
                "inputTokens": input_tokens,
                "outputTokens": output_tokens,
                "estimatedCostUsd": _estimated_cost(
                    input_tokens,
                    output_tokens,
                    spec,
                ),
                "pricingUsdPerMillion": {
                    "input": spec.input_usd_per_million,
                    "output": spec.output_usd_per_million,
                },
                "slides": slides,
            }
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    summary = {
        "output": str(args.output),
        "models": [
            {
                key: value
                for key, value in result.items()
                if key
                in {
                    "model",
                    "deployment",
                    "status",
                    "quality",
                    "analysisSeconds",
                    "inputTokens",
                    "outputTokens",
                    "estimatedCostUsd",
                    "error",
                }
            }
            for result in report["models"]
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if all(item["status"] == "succeeded" for item in report["models"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())

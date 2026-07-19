from __future__ import annotations

from datetime import datetime, timezone

from crewmeal.search_enhancement.database import JobEventRecord
from crewmeal.search_enhancement.web.viewmodels import (
    condense_stages,
    format_duration,
)


def _ev(
    event_id: int,
    stage: str,
    created_at: str,
    *,
    detail: dict | None = None,
    message: str | None = None,
) -> JobEventRecord:
    return JobEventRecord(
        id=event_id,
        job_id="job-1",
        stage=stage,
        message=message,
        detail=detail,
        created_at=created_at,
    )


def test_format_duration_variants() -> None:
    assert format_duration(None) is None
    assert format_duration(0.2) == "1초 미만"
    assert format_duration(5) == "5초"
    assert format_duration(65) == "1분 5초"
    assert format_duration(120) == "2분"
    assert format_duration(3600) == "1시간"
    assert format_duration(3660) == "1시간 1분"


def test_condense_collapses_analyzing_and_measures_duration() -> None:
    events = [
        _ev(1, "CLAIMED", "2026-01-01T00:00:00+00:00"),
        _ev(2, "ANALYZING", "2026-01-01T00:00:10+00:00", detail={"completed": 0, "total": 3}),
        _ev(3, "ANALYZING", "2026-01-01T00:00:20+00:00", detail={"completed": 1, "total": 3, "slide": 1}),
        _ev(4, "ANALYZING", "2026-01-01T00:00:40+00:00", detail={"completed": 3, "total": 3, "slide": 3}),
        _ev(5, "READY", "2026-01-01T00:00:50+00:00"),
    ]

    stages = condense_stages(events)

    # Per-slide ANALYZING events collapse into a single row.
    assert [entry["stage"] for entry in stages] == ["CLAIMED", "ANALYZING", "READY"]
    analyzing = stages[1]
    assert analyzing["slide_completed"] == 3
    assert analyzing["slide_total"] == 3
    # ANALYZING (:10) → READY (:50) = 40s elapsed.
    assert analyzing["duration_seconds"] == 40.0
    assert analyzing["duration_label"] == "40초"
    # CLAIMED (:00) → ANALYZING (:10) = 10s.
    assert stages[0]["duration_label"] == "10초"


def test_condense_active_stage_uses_now() -> None:
    events = [_ev(1, "CONVERTING", "2026-01-01T00:00:00+00:00")]
    now = datetime(2026, 1, 1, 0, 0, 30, tzinfo=timezone.utc)

    stages = condense_stages(events, now=now)

    assert stages[0]["duration_seconds"] == 30.0
    assert stages[0]["terminal"] is False
    assert stages[0]["duration_label"] == "30초"

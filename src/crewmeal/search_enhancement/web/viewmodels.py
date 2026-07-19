"""View-model builders shared by the status page and admin portal."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from crewmeal.search_enhancement.database import (
    DocumentKey,
    DocumentRecord,
    JobEventRecord,
    JobRecord,
    SearchEnhancementRepository,
)
from crewmeal.search_enhancement.pricing import estimate_cost
from crewmeal.search_enhancement.progress import (
    Stage,
    TERMINAL_STAGES,
    stage_label,
    stage_percent,
)

#: A run is "in flight" (reprocessing) until the latest job reaches a terminal
#: stage (READY/FAILED). While in flight, any previously rendered HTML artifact
#: is from an *earlier* run (HTML is only written at the end), so the preview
#: must be treated as stale and hidden.
def _latest_stage(
    repository: SearchEnhancementRepository, job: JobRecord | None
) -> str | None:
    if job is None:
        return None
    events = list(repository.list_job_events(job.job_id))
    return events[-1].stage if events else None


def run_in_flight(
    repository: SearchEnhancementRepository, key: DocumentKey
) -> bool:
    """True when the document's latest job has not yet reached a terminal stage."""

    job = repository.get_latest_job(key)
    if job is None:
        return False
    stage = _latest_stage(repository, job)
    is_terminal = stage in TERMINAL_STAGES if stage else False
    return not is_terminal


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def format_duration(seconds: float | None) -> str | None:
    """Human-friendly Korean duration label for a single step."""

    if seconds is None:
        return None
    total = int(round(seconds))
    if total < 1:
        return "1초 미만"
    if total < 60:
        return f"{total}초"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}분 {sec}초" if sec else f"{minutes}분"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}시간 {minutes}분" if minutes else f"{hours}시간"


def _detail_int(detail: Any, key: str) -> int | None:
    if isinstance(detail, dict):
        value = detail.get(key)
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
    return None


def condense_stages(
    events: list[JobEventRecord], *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Collapse consecutive same-stage events into one row per stage.

    The per-slide ``ANALYZING`` events (one per finished slide) would otherwise
    flood the timeline. Here each stage becomes a single row carrying its
    elapsed time (start → next stage, or → now while still running) and, for
    the analysis stage, the ``completed/total`` slide sub-progress.
    """

    now = now or datetime.now(timezone.utc)
    groups: list[dict[str, Any]] = []
    for event in events:
        if groups and groups[-1]["stage"] == event.stage:
            groups[-1]["events"].append(event)
        else:
            groups.append({"stage": event.stage, "events": [event]})

    stages: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        members: list[JobEventRecord] = group["events"]
        stage = group["stage"]
        first, last = members[0], members[-1]
        start_dt = _parse_iso(first.created_at)
        if index + 1 < len(groups):
            end_dt = _parse_iso(groups[index + 1]["events"][0].created_at)
        elif stage in TERMINAL_STAGES:
            end_dt = _parse_iso(last.created_at)
        else:
            end_dt = now
        duration: float | None = None
        if start_dt is not None and end_dt is not None:
            duration = max(0.0, (end_dt - start_dt).total_seconds())

        completed = total = None
        message = None
        for member in members:
            found_completed = _detail_int(member.detail, "completed")
            found_total = _detail_int(member.detail, "total")
            if found_completed is not None:
                completed = found_completed
            if found_total is not None:
                total = found_total
            if member.message:
                message = member.message

        stages.append(
            {
                "stage": stage,
                "label": stage_label(stage),
                "message": message,
                "started_at": first.created_at,
                "duration_seconds": duration,
                "duration_label": format_duration(duration),
                "failed": stage == Stage.FAILED,
                "terminal": stage in TERMINAL_STAGES,
                "slide_completed": completed,
                "slide_total": total,
            }
        )
    return stages


def build_status_view(
    repository: SearchEnhancementRepository, document: DocumentRecord
) -> dict[str, Any]:
    """Assemble the status-page context for a single document."""

    key = document.key
    job = repository.get_latest_job(key)
    events = list(repository.list_job_events(job.job_id)) if job else []
    current_stage = events[-1].stage if events else None
    timeline = [
        {
            "stage": event.stage,
            "label": stage_label(event.stage),
            "message": event.message,
            "detail": event.detail,
            "created_at": event.created_at,
            "terminal": event.stage in TERMINAL_STAGES,
            "failed": event.stage == Stage.FAILED,
        }
        for event in events
    ]
    stages = condense_stages(events)
    slide_total: int | None = None
    slide_completed: int | None = None
    for entry in stages:
        if entry["slide_total"] is not None:
            slide_total = entry["slide_total"]
    for entry in stages:
        if entry["stage"] == Stage.ANALYZING and entry["slide_completed"] is not None:
            slide_completed = entry["slide_completed"]
    html_artifact = repository.get_latest_artifact(key, "html")
    is_terminal = current_stage in TERMINAL_STAGES if current_stage else False
    is_reprocessing = job is not None and not is_terminal
    has_html = html_artifact is not None
    # A previously rendered preview is stale while a new run is in flight, so
    # only surface it once the current job has finished (or when idle).
    show_html = has_html and not is_reprocessing
    cost = estimate_cost([repository.job_usage(job.job_id) if job else None])
    return {
        "document": document,
        "job": job,
        "timeline": timeline,
        "stages": stages,
        "current_stage": current_stage,
        "current_label": stage_label(current_stage),
        "percent": stage_percent(current_stage),
        "is_terminal": is_terminal,
        "failed": current_stage == Stage.FAILED,
        "analyzing_active": current_stage == Stage.ANALYZING and not is_terminal,
        "slide_total": slide_total,
        "slide_completed": slide_completed,
        "has_html": has_html,
        "show_html": show_html,
        "is_reprocessing": is_reprocessing,
        "cost": cost,
        "notes": repository.correction_notes(key),
        "can_remove": document.source_kind == "sharepoint",
    }

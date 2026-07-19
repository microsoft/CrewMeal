"""Progress reporting for long-running enhancement jobs.

Each job records a timeline of stage events (`job_events`) so the user-facing
status page can show where a request is in the pipeline — including per-slide
analysis progress, which dominates wall-clock time. Reporters are cheap and the
:class:`NullProgressReporter` makes progress opt-in, keeping the processing
pipeline usable (and testable) without a database.
"""

from __future__ import annotations

from typing import Any, Protocol


class Stage:
    """Canonical pipeline stages, in roughly chronological order."""

    RECEIVED = "RECEIVED"
    QUEUED = "QUEUED"
    CLAIMED = "CLAIMED"
    DOWNLOADING = "DOWNLOADING"
    VALIDATING = "VALIDATING"
    CONVERTING = "CONVERTING"
    RENDERING = "RENDERING"
    GEOMETRY = "GEOMETRY"
    ANALYZING = "ANALYZING"
    RENDER_HTML = "RENDER_HTML"
    PUBLISHING = "PUBLISHING"
    READY = "READY"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    REMOVING = "REMOVING"
    REMOVED = "REMOVED"


#: Ordered stages used to compute a coarse percentage on the status page.
STAGE_ORDER: tuple[str, ...] = (
    Stage.RECEIVED,
    Stage.QUEUED,
    Stage.CLAIMED,
    Stage.DOWNLOADING,
    Stage.VALIDATING,
    Stage.CONVERTING,
    Stage.RENDERING,
    Stage.GEOMETRY,
    Stage.ANALYZING,
    Stage.RENDER_HTML,
    Stage.PUBLISHING,
    Stage.READY,
)

TERMINAL_STAGES: frozenset[str] = frozenset(
    {Stage.READY, Stage.FAILED, Stage.CANCELLED, Stage.REMOVED}
)


class ProgressReporter(Protocol):
    def stage(
        self,
        stage: str,
        *,
        message: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None: ...


class NullProgressReporter:
    """No-op reporter used when progress does not need to be persisted."""

    def stage(
        self,
        stage: str,
        *,
        message: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        return None


class JobProgressReporter:
    """Persists stage events for a single job into ``job_events``."""

    def __init__(self, repository: Any, job_id: str) -> None:
        self._repository = repository
        self._job_id = job_id

    def stage(
        self,
        stage: str,
        *,
        message: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self._repository.add_job_event(
            self._job_id, stage=stage, message=message, detail=detail
        )


def stage_percent(stage: str | None) -> int:
    """Coarse completion percentage for a stage (0-100)."""

    if stage is None:
        return 0
    if stage == Stage.READY:
        return 100
    if stage in {Stage.FAILED, Stage.CANCELLED, Stage.REMOVED}:
        return 100
    try:
        index = STAGE_ORDER.index(stage)
    except ValueError:
        return 0
    return int(round((index / (len(STAGE_ORDER) - 1)) * 100))


#: Human-friendly Korean labels for the status-page timeline.
STAGE_LABELS: dict[str, str] = {
    Stage.RECEIVED: "요청 접수",
    Stage.QUEUED: "대기열 등록",
    Stage.CLAIMED: "작업 시작",
    Stage.DOWNLOADING: "원본 다운로드",
    Stage.VALIDATING: "파일 검증",
    Stage.CONVERTING: "PDF 변환",
    Stage.RENDERING: "이미지 렌더링",
    Stage.GEOMETRY: "레이아웃 분석",
    Stage.ANALYZING: "슬라이드 분석",
    Stage.RENDER_HTML: "HTML 생성",
    Stage.PUBLISHING: "인덱스 게시",
    Stage.READY: "완료",
    Stage.FAILED: "실패",
    Stage.CANCELLED: "취소됨",
    Stage.REMOVING: "인덱스 삭제 중",
    Stage.REMOVED: "인덱스 삭제됨",
}


def stage_label(stage: str | None) -> str:
    if stage is None:
        return "대기 중"
    return STAGE_LABELS.get(stage, stage)

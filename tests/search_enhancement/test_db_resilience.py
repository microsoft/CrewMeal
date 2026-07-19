from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from crewmeal.search_enhancement.config import SearchEnhancementConfig
from crewmeal.search_enhancement.db_resilience import (
    is_transient_db_error,
    run_with_db_retry,
)
from crewmeal.search_enhancement.worker import SearchEnhancementWorker, WorkerRun


def _operational_error(message: str = "connection refused") -> OperationalError:
    return OperationalError("SELECT 1", None, Exception(message))


def test_is_transient_db_error_true_for_operational_error() -> None:
    assert is_transient_db_error(_operational_error()) is True


def test_is_transient_db_error_false_for_non_connection_errors() -> None:
    assert is_transient_db_error(ValueError("bad value")) is False
    integrity = IntegrityError("INSERT", None, Exception("duplicate key"))
    assert is_transient_db_error(integrity) is False


def test_run_with_db_retry_retries_then_succeeds() -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    def operation() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise _operational_error()
        return "ok"

    result = run_with_db_retry(
        operation,
        description="test",
        initial_seconds=2,
        max_seconds=60,
        sleep=sleeps.append,
    )

    assert result == "ok"
    assert calls["n"] == 3
    # Capped exponential backoff: 2s then 4s before the third (successful) call.
    assert sleeps == [2, 4]


def test_run_with_db_retry_caps_backoff_at_max() -> None:
    sleeps: list[float] = []
    calls = {"n": 0}

    def operation() -> str:
        calls["n"] += 1
        if calls["n"] < 5:
            raise _operational_error()
        return "done"

    run_with_db_retry(
        operation,
        description="test",
        initial_seconds=2,
        max_seconds=5,
        sleep=sleeps.append,
    )

    assert sleeps == [2, 4, 5, 5]


def test_run_with_db_retry_propagates_non_transient() -> None:
    def operation() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError):
        run_with_db_retry(
            operation,
            description="test",
            initial_seconds=1,
            max_seconds=1,
            sleep=lambda _s: None,
        )


def test_run_with_db_retry_respects_max_attempts() -> None:
    calls = {"n": 0}

    def operation() -> None:
        calls["n"] += 1
        raise _operational_error()

    with pytest.raises(OperationalError):
        run_with_db_retry(
            operation,
            description="test",
            initial_seconds=1,
            max_seconds=1,
            max_attempts=3,
            sleep=lambda _s: None,
        )

    assert calls["n"] == 3


class _FakeStop:
    """Duck-typed threading.Event that never really blocks, used to drive
    ``run_forever`` a bounded number of iterations without real sleeps."""

    def __init__(self, stop_after: int) -> None:
        self._stop_after = stop_after
        self._checks = 0
        self.waits: list[float | None] = []

    def is_set(self) -> bool:
        self._checks += 1
        return self._checks > self._stop_after

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        return False


def _worker_with_config() -> SearchEnhancementWorker:
    config = SearchEnhancementConfig(
        tenant_id="t",
        client_id="c",
        client_secret="s",
        site_id="site",
        drive_id="drive",
        list_id="list",
        site_url="https://example.sharepoint.com",
        db_retry_initial_seconds=2,
        db_retry_max_seconds=8,
    )
    return SearchEnhancementWorker(
        config=config,
        repository=None,  # type: ignore[arg-type]
        control=None,  # type: ignore[arg-type]
        connector=None,  # type: ignore[arg-type]
        processor=None,  # type: ignore[arg-type]
    )


def test_run_forever_recovers_from_transient_db_error() -> None:
    worker = _worker_with_config()
    worker.reconcile_once = lambda: 0  # type: ignore[method-assign]

    attempts = {"n": 0}

    def fake_run_once() -> WorkerRun:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _operational_error()
        return WorkerRun(commands_ingested=0, jobs_processed=0)

    worker.run_once = fake_run_once  # type: ignore[method-assign]
    stop = _FakeStop(stop_after=2)

    worker.run_forever(stop_event=stop)  # must not raise

    assert attempts["n"] == 2
    # First wait is the backoff after the transient error, second is the idle
    # poll after the recovered (no-work) iteration.
    assert stop.waits == [2, worker._config.command_poll_seconds]


def test_run_forever_propagates_non_transient_error() -> None:
    worker = _worker_with_config()
    worker.reconcile_once = lambda: 0  # type: ignore[method-assign]

    def fake_run_once() -> WorkerRun:
        raise ValueError("programming error")

    worker.run_once = fake_run_once  # type: ignore[method-assign]

    with pytest.raises(ValueError):
        worker.run_forever(stop_event=_FakeStop(stop_after=5))

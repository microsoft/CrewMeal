from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from crewmeal.search_enhancement.config import SearchEnhancementConfig
from crewmeal.search_enhancement.database import (
    DocumentKey,
    SearchEnhancementRepository,
)
from crewmeal.search_enhancement.sharepoint_control import (
    SharePointControlClient,
)


def _config() -> SearchEnhancementConfig:
    return SearchEnhancementConfig(
        tenant_id="tenant",
        client_id="client",
        client_secret="secret",
        site_id="site",
        drive_id="drive",
        list_id="list",
        site_url="https://tenant.sharepoint.com/sites/test",
    )


def test_sqlite_jobs_are_idempotent_and_expired_leases_can_resume(
    tmp_path: Path,
) -> None:
    repository = SearchEnhancementRepository(tmp_path / "state.db")
    repository.initialize()
    key = DocumentKey("tenant", "site", "drive", "item")
    request_id = str(uuid4())
    repository.upsert_document(
        key=key,
        list_id="list",
        list_item_id="7",
        web_url="https://example/report.pptx",
        file_name="report.pptx",
        connection_id="connection",
        desired_enabled=True,
        status="Queued",
        request_id=request_id,
    )

    first = repository.enqueue_job(
        key=key,
        request_id=request_id,
        job_type="upsert",
    )
    duplicate = repository.enqueue_job(
        key=key,
        request_id=request_id,
        job_type="upsert",
    )
    assert duplicate == first

    claimed = repository.claim_next_job(worker_id="worker-a", lease_seconds=1)
    assert claimed is not None
    assert claimed.job_id == first
    assert claimed.attempts == 1
    assert repository.claim_next_job(worker_id="worker-b", lease_seconds=1) is None

    repository.complete_job(first, html_bytes=100, request_bytes=200)
    assert repository.claim_next_job(worker_id="worker-b", lease_seconds=1) is None


class FakeGraph:
    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        self.updates: list[dict[str, Any]] = []

    def get_json(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> dict[str, Any]:
        if path.endswith("/driveItem"):
            return {
                "id": "drive-item",
                "name": "report.pptx",
                "webUrl": "https://example/report.pptx",
            }
        return {
            "id": "7",
            "fields": {
                "CrewmealSearchEnabled": True,
                "CrewmealSearchCommand": "Enhance",
                "CrewmealSearchRequestId": self.request_id,
                "CrewmealSearchStatus": "Queued",
            },
        }

    def send_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any],
        expected: tuple[int, ...],
    ) -> dict[str, Any]:
        self.updates.append(body)
        return body


def test_sharepoint_control_checks_request_id_and_updates_explicit_state() -> None:
    request_id = str(uuid4())
    graph = FakeGraph(request_id)
    control = SharePointControlClient(_config(), graph)  # type: ignore[arg-type]

    item = control.get_control_item("7")
    assert item.is_pptx
    assert control.is_current(item, expect_enabled=True)

    control.set_processing(item)
    control.set_ready(item, html_bytes=1234, request_bytes=2345)

    assert graph.updates[0]["CrewmealSearchStatus"] == "Processing"
    assert graph.updates[1]["CrewmealSearchStatus"] == "Ready"
    assert graph.updates[1]["CrewmealSearchCommand"] is None

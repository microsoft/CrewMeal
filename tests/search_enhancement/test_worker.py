from __future__ import annotations

import io
import sqlite3
import zipfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from crewmeal.search_enhancement.acl_mapper import ConnectorAcl
from crewmeal.search_enhancement.config import SearchEnhancementConfig
from crewmeal.search_enhancement.connector_client import (
    ExternalItemError,
    PreparedExternalItem,
)
from crewmeal.search_enhancement.database import (
    DocumentKey,
    SearchEnhancementRepository,
)
from crewmeal.search_enhancement.models import (
    RenderedHtml,
    StructuredAnalysisResult,
)
from crewmeal.search_enhancement.publication import PublicationTarget
from crewmeal.search_enhancement.processor import ProcessedPresentation
from crewmeal.search_enhancement.sharepoint_control import ControlItem
from crewmeal.search_enhancement.worker import SearchEnhancementWorker
from crewmeal.source import pptx_content_fingerprint


USER_ID = "11111111-1111-4111-8111-111111111111"
OTHER_USER_ID = "22222222-2222-4222-8222-222222222222"
ITEM_GUID = "33333333-3333-4333-8333-333333333333"


def _pptx_bytes(marker: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", "<Types/>")
        package.writestr("ppt/presentation.xml", "<p:presentation/>")
        package.writestr("ppt/_rels/presentation.xml.rels", "<Relationships/>")
        package.writestr("ppt/slides/slide1.xml", f"<slide>{marker}</slide>")
    return output.getvalue()


def _config(db_path: Path) -> SearchEnhancementConfig:
    return SearchEnhancementConfig(
        tenant_id="tenant",
        client_id="client",
        client_secret="secret",
        site_id="site",
        drive_id="drive",
        list_id="list",
        site_url="https://tenant.sharepoint.com/sites/test",
        sqlite_path=db_path,
    )


class FakeControl:
    def __init__(self, request_id: str) -> None:
        self.item = ControlItem(
            list_item_id="7",
            drive_item_id="drive-item",
            file_name="report.pptx",
            web_url="https://tenant.sharepoint.com/sites/test/report.pptx",
            enabled=True,
            command="Enhance",
            request_id=request_id,
            status="Queued",
        )
        self.states: list[str] = []
        self.content_tag = '"ctag,1"'
        self.item_tag = '"etag,1"'
        self.content_marker = "source-content-1"
        self.refreshes: list[str] = []
        self.content_html: str | None = None
        self.permissions_calls = 0
        self.permission_user_id = USER_ID
        self.with_command = True

    def list_commands(self) -> tuple[ControlItem, ...]:
        return (self.item,) if self.with_command else ()

    def get_control_item(self, list_item_id: str) -> ControlItem:
        assert list_item_id == "7"
        return self.item

    def get_drive_item(self, item_id: str) -> dict[str, Any]:
        return {
            "id": item_id,
            "name": "report.pptx",
            "webUrl": self.item.web_url,
            "cTag": self.content_tag,
            "eTag": self.item_tag,
            "size": 1024,
            "lastModifiedDateTime": "2026-07-17T00:00:00Z",
            "lastModifiedBy": {"user": {"id": USER_ID, "displayName": "User"}},
            "sharepointIds": {"listItemUniqueId": ITEM_GUID},
            "file": {
                "mimeType": (
                    "application/vnd.openxmlformats-officedocument."
                    "presentationml.presentation"
                )
            },
        }

    def list_permissions(self, item_id: str) -> list[dict[str, Any]]:
        self.permissions_calls += 1
        return [
            {
                "id": "permission",
                "grantedToV2": {"user": {"id": self.permission_user_id}},
            }
        ]

    def download_content(self, item_id: str) -> bytes:
        return _pptx_bytes(self.content_marker)

    def is_current(self, item: ControlItem, *, expect_enabled: bool) -> bool:
        return self.item.request_id == item.request_id and self.item.enabled == expect_enabled

    def set_processing(self, item: ControlItem) -> None:
        self.states.append("Processing")

    def set_ready(
        self,
        item: ControlItem,
        *,
        html_bytes: int,
        request_bytes: int,
    ) -> None:
        self.states.append("Ready")
        self.with_command = False

    def set_failed(
        self,
        item: ControlItem,
        *,
        code: str,
        message: str,
    ) -> None:
        self.states.append("Failed")

    def set_removing(self, item: ControlItem) -> None:
        self.states.append("Removing")

    def set_not_enabled(self, item: ControlItem) -> None:
        self.states.append("NotEnabled")
        self.with_command = False

    def queue_refresh(
        self, list_item_id: str, *, message: str
    ) -> str:
        assert list_item_id == "7"
        assert message
        request_id = str(uuid4())
        self.refreshes.append(request_id)
        return request_id

    def set_search_content(
        self,
        list_item_id: str,
        field_name: str,
        content: str,
    ) -> str:
        assert list_item_id == "7"
        assert field_name == "CrewmealSearchContent"
        self.content_html = content
        return self.content_html

    def get_search_content(
        self,
        list_item_id: str,
        field_name: str,
    ) -> str | None:
        assert list_item_id == "7"
        assert field_name == "CrewmealSearchContent"
        return self.content_html

    def clear_search_content(self, list_item_id: str, field_name: str) -> None:
        assert list_item_id == "7"
        assert field_name == "CrewmealSearchContent"
        self.content_html = None


class FakeProcessor:
    def decrypt_source(
        self, source_bytes: bytes, *, filename: str, content_type: Any = None
    ) -> bytes:
        return source_bytes

    def process(
        self,
        source_bytes: bytes,
        *,
        source_name: str,
        progress: Any = None,
        corrections: Any = None,
    ) -> ProcessedPresentation:
        assert source_bytes.startswith(b"PK")
        content = (
            "<article><header><h1>Report</h1></header>"
            "<section><h2>Slide 1</h2></section></article>"
        )
        rendered = RenderedHtml(
            content=content,
            byte_count=len(content.encode("utf-8")),
            sha256="content-hash",
            slide_titles=("Report",),
            keywords=("Report",),
        )
        analysis = StructuredAnalysisResult(
            source_name=source_name,
            slides=(),
            usage={"slideImages": 1},
            raw_result={},
            warnings=(),
            analysis_seconds=1,
        )
        return ProcessedPresentation(
            rendered=rendered,
            analysis=analysis,
            stage_timings={"analysisSeconds": 1},
        )


class FakeConnector:
    def __init__(self) -> None:
        self.items: list[PreparedExternalItem] = []
        self.deleted: list[str] = []
        self.remote: dict[str, dict[str, Any]] = {}
        self.acl_updates: list[tuple[str, tuple[ConnectorAcl, ...]]] = []
        self.on_upsert: Any = None
        self.on_delete: Any = None

    def prepare_item(
        self,
        *,
        drive_item: dict[str, Any],
        rendered: RenderedHtml,
        acl: tuple[ConnectorAcl, ...],
        source_fingerprint: str,
    ) -> PreparedExternalItem:
        assert acl == (ConnectorAcl(type="user", value=USER_ID),)
        assert source_fingerprint == pptx_content_fingerprint(
            _pptx_bytes("source-content-1")
        )
        return PreparedExternalItem(
            item_id=ITEM_GUID.replace("-", ""),
            source_url=drive_item["webUrl"],
            indexed_url=drive_item["webUrl"] + "?crewmealItemId=" + ITEM_GUID.replace("-", ""),
            body={},
            request_bytes=500,
        )

    def upsert(self, item: PreparedExternalItem) -> None:
        self.items.append(item)
        self.remote[item.item_id] = dict(item.body)
        if self.on_upsert is not None:
            callback = self.on_upsert
            self.on_upsert = None
            callback()

    def delete(self, item_id: str) -> None:
        self.deleted.append(item_id)
        self.remote.pop(item_id, None)
        if self.on_delete is not None:
            callback = self.on_delete
            self.on_delete = None
            callback()

    def get(self, item_id: str) -> dict[str, Any] | None:
        return self.remote.get(item_id)

    def update_acl(
        self,
        item_id: str,
        acl: tuple[ConnectorAcl, ...],
    ) -> None:
        assert item_id in self.remote
        self.acl_updates.append((item_id, acl))

    def update_properties(
        self,
        item_id: str,
        properties: dict[str, Any],
    ) -> None:
        assert item_id in self.remote
        self.remote[item_id].update(properties)


def test_worker_processes_enhance_command_end_to_end(tmp_path: Path) -> None:
    request_id = str(uuid4())
    config = _config(tmp_path / "worker.db")
    repository = SearchEnhancementRepository(config.sqlite_path)
    repository.initialize()
    repository.request_publication_target("copilot_connector")
    control = FakeControl(request_id)
    connector = FakeConnector()
    worker = SearchEnhancementWorker(
        config=config,
        repository=repository,
        control=control,  # type: ignore[arg-type]
        connector=connector,  # type: ignore[arg-type]
        processor=FakeProcessor(),  # type: ignore[arg-type]
        worker_id="test-worker",
    )

    run = worker.run_once()

    assert run.commands_ingested == 1
    assert run.jobs_processed == 1
    assert control.states == ["Processing", "Ready"]
    assert len(connector.items) == 1
    document = repository.list_enabled_documents()[0]
    assert document.status == "Ready"
    assert document.processed_request_id == request_id
    assert document.external_item_id == ITEM_GUID.replace("-", "")
    assert document.source_etag == pptx_content_fingerprint(
        _pptx_bytes("source-content-1")
    )

    control.content_tag = '"ctag,2"'
    control.item_tag = '"etag,2"'
    assert worker.reconcile_once() == 0
    unchanged = repository.get_document(document.key)
    assert unchanged is not None and unchanged.status == "Ready"

    control.content_marker = "source-content-2"
    assert worker.reconcile_once() == 1
    refreshed = repository.get_document(document.key)
    assert refreshed is not None
    assert refreshed.status == "Queued"


def test_reclaimed_job_with_committed_outcome_is_completed_without_republishing(
    tmp_path: Path,
) -> None:
    request_id = str(uuid4())
    config = _config(tmp_path / "worker.db")
    repository = SearchEnhancementRepository(config.sqlite_path)
    repository.initialize()
    repository.request_publication_target("copilot_connector")
    key = DocumentKey("tenant", "site", "drive", "drive-item")
    job_id = repository.upsert_document_and_enqueue(
        key=key,
        list_id="list",
        list_item_id="7",
        web_url="https://tenant.sharepoint.com/sites/test/report.pptx",
        file_name="report.pptx",
        connection_id="connection",
        desired_enabled=True,
        status="Queued",
        request_id=request_id,
        job_type="upsert",
        trigger="test",
    )
    claimed = repository.claim_next_job(worker_id="worker-a", lease_seconds=30)
    assert claimed is not None and claimed.job_id == job_id
    assert repository.record_success(
        key,
        request_id=request_id,
        source_etag="source",
        last_modified_datetime="2026-07-17T00:00:00Z",
        source_size=1024,
        acl_digest="acl",
        external_item_id="external-id",
        web_url="https://tenant.sharepoint.com/sites/test/report.pptx",
        file_name="report.pptx",
        html_bytes=100,
        request_bytes=200,
        content_hash="hash",
        job_id=job_id,
        expected_attempts=claimed.attempts,
        expected_lease_owner="worker-a",
    )
    with sqlite3.connect(config.sqlite_path) as connection:
        connection.execute(
            "UPDATE jobs SET lease_expires_at = ? WHERE job_id = ?",
            ("2000-01-01T00:00:00+00:00", job_id),
        )

    control = FakeControl(request_id)
    control.with_command = False
    connector = FakeConnector()
    worker = SearchEnhancementWorker(
        config=config,
        repository=repository,
        control=control,  # type: ignore[arg-type]
        connector=connector,  # type: ignore[arg-type]
        processor=FakeProcessor(),  # type: ignore[arg-type]
        worker_id="worker-b",
    )

    run = worker.run_once()

    reclaimed = repository.get_job(job_id)
    document = repository.get_document(key)
    assert run.jobs_processed == 1
    assert connector.items == []
    assert reclaimed is not None
    assert reclaimed.status == "completed"
    assert reclaimed.attempts == 2
    assert document is not None and document.enhancement_version == 1


def test_worker_skips_admin_disabled_format(tmp_path: Path) -> None:
    request_id = str(uuid4())
    config = _config(tmp_path / "worker.db")
    repository = SearchEnhancementRepository(config.sqlite_path)
    repository.initialize()
    repository.set_setting("format.pptx.enabled", False)
    control = FakeControl(request_id)
    connector = FakeConnector()
    worker = SearchEnhancementWorker(
        config=config,
        repository=repository,
        control=control,  # type: ignore[arg-type]
        connector=connector,  # type: ignore[arg-type]
        processor=FakeProcessor(),  # type: ignore[arg-type]
        worker_id="test-worker",
    )

    run = worker.run_once()

    assert run.commands_ingested == 0
    assert run.jobs_processed == 0
    assert control.states == []
    assert not repository.list_enabled_documents()


def test_worker_publishes_column_without_copying_acl(tmp_path: Path) -> None:
    request_id = str(uuid4())
    config = _config(tmp_path / "worker.db")
    repository = SearchEnhancementRepository(config.sqlite_path)
    repository.initialize()
    repository.request_publication_target("sharepoint_column")
    repository.set_column_provisioned()
    control = FakeControl(request_id)
    connector = FakeConnector()
    worker = SearchEnhancementWorker(
        config=config,
        repository=repository,
        control=control,  # type: ignore[arg-type]
        connector=connector,  # type: ignore[arg-type]
        processor=FakeProcessor(),  # type: ignore[arg-type]
        worker_id="test-worker",
    )

    run = worker.run_once()

    assert run.jobs_processed == 1
    assert connector.items == []
    assert control.permissions_calls == 0
    assert control.content_html == "# Report\n\n## Slide 1"
    document = repository.list_enabled_documents()[0]
    assert document.external_item_id is None
    publication = repository.get_publication(document.key, "sharepoint_column")
    assert publication is not None
    assert publication.stored_characters == len("# Report\n\n## Slide 1")
    assert publication.stored_bytes == len("# Report\n\n## Slide 1".encode("utf-8"))
    assert repository.get_publication_transition().status == "awaiting_reindex"

    repository.set_reindex_requested()
    repository.set_search_verified()
    repository.set_copilot_verified()
    worker.run_once()
    assert repository.get_publication_transition().active_target.value == (
        "sharepoint_column"
    )

    control.content_html = "<p>tampered</p>"
    assert worker.reconcile_once() == 1
    refreshed = repository.get_document(document.key)
    assert refreshed is not None and refreshed.status == "Queued"


def test_connector_to_column_transition_keeps_old_target_until_verified(
    tmp_path: Path,
) -> None:
    request_id = str(uuid4())
    config = _config(tmp_path / "worker.db")
    repository = SearchEnhancementRepository(config.sqlite_path)
    repository.initialize()
    repository.request_publication_target("copilot_connector")
    control = FakeControl(request_id)
    connector = FakeConnector()
    worker = SearchEnhancementWorker(
        config=config,
        repository=repository,
        control=control,  # type: ignore[arg-type]
        connector=connector,  # type: ignore[arg-type]
        processor=FakeProcessor(),  # type: ignore[arg-type]
        worker_id="test-worker",
    )
    worker.run_once()
    document = repository.list_enabled_documents()[0]
    connector_id = document.external_item_id
    assert connector_id
    assert repository.get_publication_transition().active_target.value == (
        "copilot_connector"
    )

    transition = repository.request_publication_target("sharepoint_column")
    repository.set_column_provisioned()
    repository.queue_refresh(document.key, trigger="admin-publication-transition")
    worker.run_once()

    assert repository.get_publication_transition().status == "awaiting_reindex"
    assert connector.deleted == []
    assert repository.get_publication(document.key, "copilot_connector") is not None
    staged = repository.get_publication(document.key, "sharepoint_column")
    assert staged is not None and staged.generation == transition.generation

    repository.set_reindex_requested()
    repository.set_search_verified()
    repository.set_copilot_verified()
    worker.run_once()

    assert connector.deleted == [connector_id]
    assert repository.get_publication(document.key, "copilot_connector") is None
    assert repository.get_publication(document.key, "sharepoint_column") is not None
    assert repository.get_publication_transition().active_target.value == (
        "sharepoint_column"
    )


def test_column_staging_continues_reconciling_active_connector_acl(
    tmp_path: Path,
) -> None:
    request_id = str(uuid4())
    config = _config(tmp_path / "worker.db")
    repository = SearchEnhancementRepository(config.sqlite_path)
    repository.initialize()
    repository.request_publication_target("copilot_connector")
    control = FakeControl(request_id)
    connector = FakeConnector()
    worker = SearchEnhancementWorker(
        config=config,
        repository=repository,
        control=control,  # type: ignore[arg-type]
        connector=connector,  # type: ignore[arg-type]
        processor=FakeProcessor(),  # type: ignore[arg-type]
        worker_id="test-worker",
    )
    worker.run_once()
    document = repository.list_enabled_documents()[0]

    repository.request_publication_target("sharepoint_column")
    repository.set_column_provisioned()
    repository.queue_refresh(document.key, trigger="admin-publication-transition")
    worker.run_once()
    control.permission_user_id = OTHER_USER_ID

    assert worker.reconcile_once() == 1
    assert connector.acl_updates == [
        (
            ITEM_GUID.replace("-", ""),
            (ConnectorAcl(type="user", value=OTHER_USER_ID),),
        )
    ]


def test_connector_reconciliation_requeues_missing_remote_item(
    tmp_path: Path,
) -> None:
    request_id = str(uuid4())
    config = _config(tmp_path / "worker.db")
    repository = SearchEnhancementRepository(config.sqlite_path)
    repository.initialize()
    repository.request_publication_target("copilot_connector")
    control = FakeControl(request_id)
    connector = FakeConnector()
    worker = SearchEnhancementWorker(
        config=config,
        repository=repository,
        control=control,  # type: ignore[arg-type]
        connector=connector,  # type: ignore[arg-type]
        processor=FakeProcessor(),  # type: ignore[arg-type]
        worker_id="test-worker",
    )
    worker.run_once()
    connector.remote.clear()

    assert worker.reconcile_once() == 1
    refreshed = repository.get_document(
        DocumentKey("tenant", "site", "drive", "drive-item")
    )
    assert refreshed is not None and refreshed.status == "Queued"


def test_worker_delete_removes_publication_and_disables_document(
    tmp_path: Path,
) -> None:
    request_id = str(uuid4())
    config = _config(tmp_path / "worker.db")
    repository = SearchEnhancementRepository(config.sqlite_path)
    repository.initialize()
    repository.request_publication_target("copilot_connector")
    control = FakeControl(request_id)
    connector = FakeConnector()
    worker = SearchEnhancementWorker(
        config=config,
        repository=repository,
        control=control,  # type: ignore[arg-type]
        connector=connector,  # type: ignore[arg-type]
        processor=FakeProcessor(),  # type: ignore[arg-type]
        worker_id="test-worker",
    )
    worker.run_once()
    document = repository.list_enabled_documents()[0]
    removal_request = str(uuid4())
    control.item = ControlItem(
        list_item_id="7",
        drive_item_id="drive-item",
        file_name="report.pptx",
        web_url=control.item.web_url,
        enabled=False,
        command="Remove",
        request_id=removal_request,
        status="Queued",
    )
    control.with_command = True

    worker.run_once()

    removed = repository.get_document(document.key)
    assert removed is not None
    assert removed.status == "NotEnabled"
    assert removed.desired_enabled is False
    assert connector.remote == {}
    assert repository.list_document_publications(document.key) == ()


def test_stale_delete_cannot_overwrite_newer_enhance_success(
    tmp_path: Path,
) -> None:
    request_id = str(uuid4())
    config = _config(tmp_path / "worker.db")
    repository = SearchEnhancementRepository(config.sqlite_path)
    repository.initialize()
    repository.request_publication_target("copilot_connector")
    control = FakeControl(request_id)
    connector = FakeConnector()
    worker = SearchEnhancementWorker(
        config=config,
        repository=repository,
        control=control,  # type: ignore[arg-type]
        connector=connector,  # type: ignore[arg-type]
        processor=FakeProcessor(),  # type: ignore[arg-type]
        worker_id="test-worker",
    )
    worker.run_once()
    document = repository.list_enabled_documents()[0]
    control.item = ControlItem(
        list_item_id="7",
        drive_item_id="drive-item",
        file_name="report.pptx",
        web_url=control.item.web_url,
        enabled=False,
        command="Remove",
        request_id=str(uuid4()),
        status="Queued",
    )
    control.with_command = True

    def enqueue_newer_enhance() -> None:
        repository.queue_refresh(document.key, trigger="newer-enhance")

    connector.on_delete = enqueue_newer_enhance
    worker.run_once()

    current = repository.get_document(document.key)
    assert current is not None
    assert current.status == "Ready"
    assert current.desired_enabled is True
    assert ITEM_GUID.replace("-", "") in connector.remote
    assert repository.get_publication(document.key, "copilot_connector") is not None


def test_superseded_upsert_does_not_delete_stable_publication(
    tmp_path: Path,
) -> None:
    request_id = str(uuid4())
    config = _config(tmp_path / "worker.db")
    repository = SearchEnhancementRepository(config.sqlite_path)
    repository.initialize()
    repository.request_publication_target("copilot_connector")
    control = FakeControl(request_id)
    connector = FakeConnector()
    worker = SearchEnhancementWorker(
        config=config,
        repository=repository,
        control=control,  # type: ignore[arg-type]
        connector=connector,  # type: ignore[arg-type]
        processor=FakeProcessor(),  # type: ignore[arg-type]
        worker_id="test-worker",
    )

    def supersede() -> None:
        repository.queue_refresh(
            DocumentKey("tenant", "site", "drive", "drive-item"),
            trigger="newer-request",
        )

    connector.on_upsert = supersede
    worker.run_once()

    assert connector.deleted == []
    assert ITEM_GUID.replace("-", "") in connector.remote
    assert any(job["status"] == "cancelled" for job in repository.list_recent_jobs())


def test_null_connector_failure_record_does_not_block_cleanup(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "worker.db")
    repository = SearchEnhancementRepository(config.sqlite_path)
    repository.initialize()
    repository.request_publication_target("copilot_connector")
    control = FakeControl(str(uuid4()))
    control.with_command = False
    connector = FakeConnector()
    worker = SearchEnhancementWorker(
        config=config,
        repository=repository,
        control=control,  # type: ignore[arg-type]
        connector=connector,  # type: ignore[arg-type]
        processor=FakeProcessor(),  # type: ignore[arg-type]
        worker_id="test-worker",
    )
    worker.run_once()
    key = DocumentKey("tenant", "site", "drive", "failed-item")
    repository.upsert_document(
        key=key,
        list_id="list",
        list_item_id="8",
        web_url="https://tenant.sharepoint.com/sites/test/failed.pptx",
        file_name="failed.pptx",
        connection_id="connection",
        desired_enabled=True,
        status="Ready",
        request_id=str(uuid4()),
    )
    repository.record_publication_error(
        key,
        target="copilot_connector",
        generation=1,
        code="UPLOAD_FAILED",
        message="No external item was created.",
    )
    transition = repository.request_publication_target("sharepoint_column")
    repository.set_column_provisioned()
    repository.record_publication_success(
        key,
        target="sharepoint_column",
        generation=transition.generation,
        locator="CrewmealSearchContent",
        content_hash="hash",
        original_characters=10,
        stored_characters=10,
        stored_bytes=10,
        truncated=False,
    )
    worker.run_once()
    repository.set_reindex_requested()
    repository.set_search_verified()
    repository.set_copilot_verified()

    worker.run_once()

    assert connector.deleted == []
    assert repository.get_publication(key, "copilot_connector") is None
    assert repository.get_publication_transition().active_target.value == (
        "sharepoint_column"
    )


def test_cleanup_does_not_activate_stale_transition_generation(
    tmp_path: Path,
) -> None:
    request_id = str(uuid4())
    config = _config(tmp_path / "worker.db")
    repository = SearchEnhancementRepository(config.sqlite_path)
    repository.initialize()
    repository.request_publication_target("copilot_connector")
    control = FakeControl(request_id)
    connector = FakeConnector()
    worker = SearchEnhancementWorker(
        config=config,
        repository=repository,
        control=control,  # type: ignore[arg-type]
        connector=connector,  # type: ignore[arg-type]
        processor=FakeProcessor(),  # type: ignore[arg-type]
        worker_id="test-worker",
    )
    worker.run_once()
    document = repository.list_enabled_documents()[0]
    repository.request_publication_target("sharepoint_column")
    repository.set_column_provisioned()
    repository.queue_refresh(document.key, trigger="admin-publication-transition")
    worker.run_once()
    repository.set_reindex_requested()
    repository.set_search_verified()
    repository.set_copilot_verified()

    def roll_back_during_cleanup() -> None:
        repository.request_publication_target("copilot_connector")
        repository.queue_refresh(
            document.key,
            trigger="admin-publication-transition",
        )

    connector.on_delete = roll_back_during_cleanup
    worker.run_once()

    interrupted = repository.get_publication_transition()
    assert interrupted.desired_target.value == "copilot_connector"
    assert interrupted.status == "cleaning"
    interrupted_publication = repository.get_publication(
        document.key,
        "copilot_connector",
    )
    assert interrupted_publication is not None
    assert interrupted_publication.status == "failed"
    assert interrupted_publication.error_code == "REMOVAL_SUPERSEDED"

    worker.run_once()
    final = repository.get_publication_transition()
    assert final.status == "active"
    assert final.active_target.value == "copilot_connector"
    assert ITEM_GUID.replace("-", "") in connector.remote


def test_cleanup_failure_is_recorded_without_terminating_worker(
    tmp_path: Path,
) -> None:
    request_id = str(uuid4())
    config = _config(tmp_path / "worker.db")
    repository = SearchEnhancementRepository(config.sqlite_path)
    repository.initialize()
    repository.request_publication_target("copilot_connector")
    control = FakeControl(request_id)
    connector = FakeConnector()
    worker = SearchEnhancementWorker(
        config=config,
        repository=repository,
        control=control,  # type: ignore[arg-type]
        connector=connector,  # type: ignore[arg-type]
        processor=FakeProcessor(),  # type: ignore[arg-type]
        worker_id="test-worker",
    )
    worker.run_once()
    document = repository.list_enabled_documents()[0]
    repository.request_publication_target("sharepoint_column")
    repository.set_column_provisioned()
    repository.queue_refresh(document.key, trigger="transition")
    worker.run_once()
    repository.set_reindex_requested()
    repository.set_search_verified()
    repository.set_copilot_verified()

    def fail_cleanup() -> None:
        raise ExternalItemError("Graph delete failed")

    connector.on_delete = fail_cleanup
    worker.run_once()

    failed = repository.get_publication_transition()
    assert failed.status == "failed"
    assert failed.last_error_message == "Graph delete failed"

    worker.run_once()
    recovered = repository.get_publication_transition()
    assert recovered.status == "active"
    assert recovered.active_target is PublicationTarget.SHAREPOINT_COLUMN


def test_rollback_cleans_remote_write_recorded_before_transition_change(
    tmp_path: Path,
) -> None:
    request_id = str(uuid4())
    config = _config(tmp_path / "worker.db")
    repository = SearchEnhancementRepository(config.sqlite_path)
    repository.initialize()
    repository.request_publication_target("sharepoint_column")
    repository.set_column_provisioned()
    control = FakeControl(request_id)
    connector = FakeConnector()
    worker = SearchEnhancementWorker(
        config=config,
        repository=repository,
        control=control,  # type: ignore[arg-type]
        connector=connector,  # type: ignore[arg-type]
        processor=FakeProcessor(),  # type: ignore[arg-type]
        worker_id="test-worker",
    )
    worker.run_once()
    repository.set_reindex_requested()
    repository.set_search_verified()
    repository.set_copilot_verified()
    worker.run_once()
    document = repository.list_enabled_documents()[0]

    repository.request_publication_target("copilot_connector")
    repository.queue_refresh(document.key, trigger="admin-publication-transition")

    def roll_back_after_connector_write() -> None:
        repository.request_publication_target("sharepoint_column")
        repository.queue_refresh(
            document.key,
            trigger="admin-publication-transition",
        )

    connector.on_upsert = roll_back_after_connector_write
    worker.run_once()

    final = repository.get_publication_transition()
    assert final.status == "active"
    assert final.active_target.value == "sharepoint_column"
    assert ITEM_GUID.replace("-", "") not in connector.remote
    assert repository.get_publication(document.key, "copilot_connector") is None


def test_transition_can_return_to_active_target_and_remove_staged_values(
    tmp_path: Path,
) -> None:
    request_id = str(uuid4())
    config = _config(tmp_path / "worker.db")
    repository = SearchEnhancementRepository(config.sqlite_path)
    repository.initialize()
    repository.request_publication_target("copilot_connector")
    control = FakeControl(request_id)
    connector = FakeConnector()
    worker = SearchEnhancementWorker(
        config=config,
        repository=repository,
        control=control,  # type: ignore[arg-type]
        connector=connector,  # type: ignore[arg-type]
        processor=FakeProcessor(),  # type: ignore[arg-type]
        worker_id="test-worker",
    )
    worker.run_once()
    document = repository.list_enabled_documents()[0]
    connector_id = document.external_item_id

    repository.request_publication_target("sharepoint_column")
    repository.set_column_provisioned()
    repository.queue_refresh(document.key, trigger="admin-publication-transition")
    worker.run_once()
    assert control.content_html is not None

    cancelled = repository.request_publication_target("copilot_connector")
    assert cancelled.status == "cleaning"
    repository.queue_refresh(document.key, trigger="admin-publication-transition")
    worker.run_once()

    assert control.content_html is None
    assert connector.deleted == []
    assert repository.get_publication(document.key, "sharepoint_column") is None
    assert repository.get_publication(document.key, "copilot_connector") is not None
    final = repository.get_publication_transition()
    assert final.status == "active"
    assert final.active_target.value == "copilot_connector"
    assert connector_id is not None

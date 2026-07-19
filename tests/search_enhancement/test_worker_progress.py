from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from crewmeal.search_enhancement.acl_mapper import ConnectorAcl
from crewmeal.search_enhancement.artifact_store import (
    LocalArtifactStore,
    artifact_path,
)
from crewmeal.search_enhancement.config import SearchEnhancementConfig
from crewmeal.search_enhancement.connector_client import PreparedExternalItem
from crewmeal.search_enhancement.database import SearchEnhancementRepository
from crewmeal.search_enhancement.models import (
    RenderedHtml,
    StructuredAnalysisResult,
)
from crewmeal.search_enhancement.processor import ProcessedPresentation
from crewmeal.search_enhancement.sharepoint_control import ControlItem
from crewmeal.search_enhancement.worker import SearchEnhancementWorker

USER_ID = "11111111-1111-4111-8111-111111111111"
ITEM_GUID = "33333333-3333-4333-8333-333333333333"


def _pptx_bytes(marker: str = "content") -> bytes:
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
    def __init__(self, request_id: str, *, with_command: bool = True) -> None:
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
        self._with_command = with_command
        self.states: list[str] = []

    def list_commands(self) -> tuple[ControlItem, ...]:
        return (self.item,) if self._with_command else ()

    def get_control_item(self, list_item_id: str) -> ControlItem:
        return self.item

    def get_drive_item(self, item_id: str) -> dict[str, Any]:
        return {
            "id": item_id,
            "name": "report.pptx",
            "webUrl": self.item.web_url,
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
        return [{"id": "permission", "grantedToV2": {"user": {"id": USER_ID}}}]

    def download_content(self, item_id: str) -> bytes:
        return _pptx_bytes()

    def is_current(self, item: ControlItem, *, expect_enabled: bool) -> bool:
        return True

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


class RecordingProcessor:
    def __init__(self) -> None:
        self.corrections: Any = None
        self.progress_stages: list[str] = []

    def process(
        self,
        source_bytes: bytes,
        *,
        source_name: str,
        progress: Any = None,
        corrections: Any = None,
    ) -> ProcessedPresentation:
        self.corrections = corrections
        if progress is not None:
            progress.stage("CONVERTING")
        rendered = RenderedHtml(
            content="<article><h1>Report</h1></article>",
            byte_count=39,
            sha256="content-hash",
            slide_titles=("Report",),
            keywords=("Report",),
        )
        analysis = StructuredAnalysisResult(
            source_name=source_name,
            slides=(),
            usage={"slideImages": 1},
            raw_result={"model": "gpt-x", "modelDeployment": "dep-x"},
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

    def prepare_item(
        self,
        *,
        drive_item: dict[str, Any],
        rendered: RenderedHtml,
        acl: tuple[ConnectorAcl, ...],
        source_fingerprint: str,
    ) -> PreparedExternalItem:
        return PreparedExternalItem(
            item_id=ITEM_GUID.replace("-", ""),
            source_url=drive_item["webUrl"],
            indexed_url=drive_item["webUrl"],
            body={},
            request_bytes=500,
        )

    def upsert(self, item: PreparedExternalItem) -> None:
        self.items.append(item)

    def delete(self, item_id: str) -> None:  # pragma: no cover - unused here
        raise AssertionError("delete should not be called")


class UnusedConnector(FakeConnector):
    def prepare_item(self, **_: Any) -> PreparedExternalItem:  # pragma: no cover
        raise AssertionError("connector must not be used for upload jobs")


def _build_repo(tmp_path: Path) -> tuple[SearchEnhancementRepository, LocalArtifactStore]:
    repository = SearchEnhancementRepository(tmp_path / "worker.db")
    repository.initialize()
    store = LocalArtifactStore(tmp_path / "artifacts")
    return repository, store


def test_sharepoint_upsert_records_progress_and_artifacts(tmp_path: Path) -> None:
    request_id = str(uuid4())
    config = _config(tmp_path / "worker.db")
    repository, store = _build_repo(tmp_path)
    control = FakeControl(request_id)
    connector = FakeConnector()
    processor = RecordingProcessor()
    worker = SearchEnhancementWorker(
        config=config,
        repository=repository,
        control=control,  # type: ignore[arg-type]
        connector=connector,  # type: ignore[arg-type]
        processor=processor,  # type: ignore[arg-type]
        artifact_store=store,
        worker_id="test-worker",
    )

    run = worker.run_once()

    assert run.jobs_processed == 1
    document = repository.list_enabled_documents()[0]
    assert document.status == "Ready"
    assert document.enhancement_version == 1

    # progress timeline
    job = repository.get_latest_job(document.key)
    assert job is not None
    stages = [event.stage for event in repository.list_job_events(job.job_id)]
    for expected in ("CLAIMED", "DOWNLOADING", "PUBLISHING", "READY"):
        assert expected in stages, stages

    # artifacts stored and retrievable
    html = repository.get_latest_artifact(document.key, "html")
    assert html is not None and html.enhancement_version == 1
    assert store.get_bytes(html.blob_path) == b"<article><h1>Report</h1></article>"
    assert repository.get_latest_artifact(document.key, "structured_json") is not None


def test_upload_tryout_skips_connector_and_sharepoint(tmp_path: Path) -> None:
    config = _config(tmp_path / "worker.db")
    repository, store = _build_repo(tmp_path)
    document = repository.create_upload_document(
        file_name="deck.pptx", connection_id="conn"
    )
    stored = store.put_bytes(
        artifact_path(
            document.key, version=0, kind="source_pptx", filename="source.pptx"
        ),
        _pptx_bytes(),
    )
    repository.record_artifact(
        document.key,
        kind="source_pptx",
        blob_path=stored.path,
        byte_count=stored.byte_count,
        enhancement_version=0,
    )
    repository.enqueue_job(
        key=document.key,
        request_id=document.request_id,
        job_type="upsert",
        trigger="upload",
    )

    control = FakeControl(str(uuid4()), with_command=False)
    processor = RecordingProcessor()
    worker = SearchEnhancementWorker(
        config=config,
        repository=repository,
        control=control,  # type: ignore[arg-type]
        connector=UnusedConnector(),  # type: ignore[arg-type]
        processor=processor,  # type: ignore[arg-type]
        artifact_store=store,
        worker_id="test-worker",
    )

    run = worker.run_once()

    assert run.jobs_processed == 1
    assert control.states == []  # no SharePoint status writes
    refreshed = repository.get_document(document.key)
    assert refreshed is not None and refreshed.status == "Ready"
    assert repository.get_latest_artifact(document.key, "html") is not None


def test_feedback_comment_is_injected_and_captured(tmp_path: Path) -> None:
    config = _config(tmp_path / "worker.db")
    repository, store = _build_repo(tmp_path)
    document = repository.create_upload_document(
        file_name="deck.pptx", connection_id="conn"
    )
    stored = store.put_bytes(
        artifact_path(
            document.key, version=0, kind="source_pptx", filename="source.pptx"
        ),
        _pptx_bytes(),
    )
    repository.record_artifact(
        document.key, kind="source_pptx", blob_path=stored.path, enhancement_version=0
    )
    # user submits a tuning comment -> rerun
    repository.queue_refresh(
        document.key, trigger="upload", feedback="Group the left column as one hierarchy"
    )

    processor = RecordingProcessor()
    worker = SearchEnhancementWorker(
        config=config,
        repository=repository,
        control=FakeControl(str(uuid4()), with_command=False),  # type: ignore[arg-type]
        connector=UnusedConnector(),  # type: ignore[arg-type]
        processor=processor,  # type: ignore[arg-type]
        artifact_store=store,
        worker_id="test-worker",
    )

    worker.run_once()

    # the comment was injected into the analysis call
    assert processor.corrections == ["Group the left column as one hierarchy"]
    # and captured into the append-only feedback corpus
    records = repository.list_feedback_records()
    assert len(records) == 1
    assert records[0].correction_text == "Group the left column as one hierarchy"
    assert records[0].model == "gpt-x"

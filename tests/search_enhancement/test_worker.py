from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from crewmeal.search_enhancement.acl_mapper import ConnectorAcl
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
from crewmeal.source import pptx_content_fingerprint


USER_ID = "11111111-1111-4111-8111-111111111111"
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

    def list_commands(self) -> tuple[ControlItem, ...]:
        return (self.item,)

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
        return [{"id": "permission", "grantedToV2": {"user": {"id": USER_ID}}}]

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

    def queue_refresh(
        self, list_item_id: str, *, message: str
    ) -> str:
        assert list_item_id == "7"
        assert message
        request_id = str(uuid4())
        self.refreshes.append(request_id)
        return request_id


class FakeProcessor:
    def process(
        self,
        source_bytes: bytes,
        *,
        source_name: str,
        progress: Any = None,
        corrections: Any = None,
    ) -> ProcessedPresentation:
        assert source_bytes.startswith(b"PK")
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


def test_worker_processes_enhance_command_end_to_end(tmp_path: Path) -> None:
    request_id = str(uuid4())
    config = _config(tmp_path / "worker.db")
    repository = SearchEnhancementRepository(config.sqlite_path)
    repository.initialize()
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
    assert control.refreshes == []

    control.content_marker = "source-content-2"
    assert worker.reconcile_once() == 1
    assert len(control.refreshes) == 1
    refreshed = repository.get_document(document.key)
    assert refreshed is not None
    assert refreshed.status == "Queued"


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

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from crewmeal.search_enhancement.artifact_store import LocalArtifactStore
from crewmeal.search_enhancement.config import SearchEnhancementConfig
from crewmeal.search_enhancement.database import (
    DocumentKey,
    SearchEnhancementRepository,
)
from crewmeal.search_enhancement.web import create_app
from crewmeal.search_enhancement.web.auth import IngestAuthError, IngestPrincipal
from crewmeal.search_enhancement.web.config import WebConfig


def _search_config(tmp_path: Path) -> SearchEnhancementConfig:
    return SearchEnhancementConfig(
        tenant_id="tenant",
        client_id="client",
        client_secret="secret",
        site_id="site",
        drive_id="drive",
        list_id="list",
        site_url="https://tenant.sharepoint.com/sites/test",
        sqlite_path=tmp_path / "web.db",
    )


class FakeValidator:
    requires_bearer = True

    def validate(self, token: str) -> IngestPrincipal:
        if token != "good-token":
            raise IngestAuthError("bad token")
        return IngestPrincipal(app_id="spfx-app", name="SPFx")


class FakeControlClient:
    def __init__(self, drive_item: dict) -> None:
        self._drive_item = drive_item
        self.calls: list[str] = []

    def get_drive_item_by_list_item(self, list_item_id: str) -> dict:
        self.calls.append(list_item_id)
        return self._drive_item


def _build(
    tmp_path: Path,
    *,
    require_auth: bool = False,
    validator: object | None = None,
    with_search_config: bool = True,
    control_client: object | None = None,
) -> tuple[FastAPI, SearchEnhancementRepository]:
    repository = SearchEnhancementRepository(tmp_path / "web.db")
    repository.initialize()
    store = LocalArtifactStore(tmp_path / "artifacts")
    web_config = WebConfig(
        base_url="https://status.example",
        session_secret="test-secret",
        require_ingest_auth=require_auth,
        ingest_audience="api://crewmeal",
        ingest_tenant_id="tenant",
    )
    app = create_app(
        repository=repository,
        artifact_store=store,
        web_config=web_config,
        search_config=_search_config(tmp_path) if with_search_config else None,
        ingest_validator=validator,
        control_client=control_client,
    )
    return app, repository


def _payload(command: str = "Enhance", request_id: str | None = None) -> dict:
    return {
        "command": command,
        "requestId": request_id or str(uuid4()),
        "item": {
            "driveItemId": "drive-item-1",
            "listItemId": "7",
            "fileName": "deck.pptx",
            "webUrl": "https://tenant.sharepoint.com/sites/test/deck.pptx",
        },
    }


def _key() -> DocumentKey:
    return DocumentKey(
        tenant_id="tenant", site_id="site", drive_id="drive", item_id="drive-item-1"
    )


def test_enhance_request_enqueues_and_returns_status_url(tmp_path: Path) -> None:
    app, repository = _build(tmp_path)
    client = TestClient(app)

    response = client.post("/api/requests", json=_payload())

    assert response.status_code == 202
    body = response.json()
    assert body["jobType"] == "upsert"
    assert body["status"] == "Queued"
    assert body["statusToken"]
    assert body["statusUrl"] == f"https://status.example/s/{body['statusToken']}"

    document = repository.get_document(_key())
    assert document is not None
    assert document.source_kind == "sharepoint"
    jobs = repository.list_recent_jobs()
    assert len(jobs) == 1
    assert jobs[0]["job_type"] == "upsert" and jobs[0]["trigger"] == "spfx"


def test_remove_request_enqueues_delete(tmp_path: Path) -> None:
    app, repository = _build(tmp_path)
    client = TestClient(app)

    response = client.post("/api/requests", json=_payload(command="Remove"))

    assert response.status_code == 202
    assert response.json()["jobType"] == "delete"
    jobs = repository.list_recent_jobs()
    assert len(jobs) == 1 and jobs[0]["job_type"] == "delete"


def test_repeated_request_is_idempotent(tmp_path: Path) -> None:
    app, repository = _build(tmp_path)
    client = TestClient(app)
    request_id = str(uuid4())

    first = client.post("/api/requests", json=_payload(request_id=request_id))
    second = client.post("/api/requests", json=_payload(request_id=request_id))

    assert first.json()["statusToken"] == second.json()["statusToken"]
    assert len(repository.list_recent_jobs()) == 1


def test_auth_required_rejects_missing_and_bad_tokens(tmp_path: Path) -> None:
    app, _ = _build(tmp_path, require_auth=True, validator=FakeValidator())
    client = TestClient(app)

    assert client.post("/api/requests", json=_payload()).status_code == 401
    bad = client.post(
        "/api/requests",
        json=_payload(),
        headers={"Authorization": "Bearer nope"},
    )
    assert bad.status_code == 401
    good = client.post(
        "/api/requests",
        json=_payload(),
        headers={"Authorization": "Bearer good-token"},
    )
    assert good.status_code == 202


def test_missing_search_config_returns_503(tmp_path: Path) -> None:
    app, _ = _build(tmp_path, with_search_config=False)
    client = TestClient(app)
    assert client.post("/api/requests", json=_payload()).status_code == 503


def test_validation_error_on_bad_command(tmp_path: Path) -> None:
    app, _ = _build(tmp_path)
    client = TestClient(app)
    bad = client.post("/api/requests", json=_payload(command="Nope"))
    assert bad.status_code == 422


def test_resolves_drive_item_from_list_item(tmp_path: Path) -> None:
    control = FakeControlClient(
        {
            "id": "graph-drive-9",
            "name": "resolved.pptx",
            "webUrl": "https://tenant.sharepoint.com/sites/test/resolved.pptx",
        }
    )
    app, repository = _build(tmp_path, control_client=control)
    client = TestClient(app)

    # Client sends only listItemId; the server resolves the Graph driveItem.
    payload = {"command": "Enhance", "requestId": str(uuid4()), "item": {"listItemId": "42"}}
    response = client.post("/api/requests", json=payload)

    assert response.status_code == 202
    assert control.calls == ["42"]
    resolved_key = DocumentKey(
        tenant_id="tenant", site_id="site", drive_id="drive", item_id="graph-drive-9"
    )
    document = repository.get_document(resolved_key)
    assert document is not None
    assert document.file_name == "resolved.pptx"
    assert document.web_url.endswith("/resolved.pptx")


def test_resolution_overrides_client_hints(tmp_path: Path) -> None:
    control = FakeControlClient(
        {"id": "graph-real", "name": "real.pptx", "webUrl": "https://host/real.pptx"}
    )
    app, repository = _build(tmp_path, control_client=control)
    client = TestClient(app)

    # Even if the client lies about driveItemId, the Graph resolution wins.
    response = client.post("/api/requests", json=_payload())
    assert response.status_code == 202
    assert repository.get_document(_key()) is None  # 'drive-item-1' hint ignored
    assert (
        repository.get_document(
            DocumentKey(
                tenant_id="tenant",
                site_id="site",
                drive_id="drive",
                item_id="graph-real",
            )
        )
        is not None
    )


def test_missing_drive_item_without_control_is_400(tmp_path: Path) -> None:
    app, _ = _build(tmp_path)
    client = TestClient(app)
    payload = {"command": "Enhance", "requestId": str(uuid4()), "item": {"listItemId": "42"}}
    response = client.post("/api/requests", json=payload)
    assert response.status_code == 400

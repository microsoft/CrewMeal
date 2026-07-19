from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from crewmeal.search_enhancement.artifact_store import LocalArtifactStore
from crewmeal.search_enhancement.database import (
    DocumentRecord,
    SearchEnhancementRepository,
)
from crewmeal.search_enhancement.web import create_app
from crewmeal.search_enhancement.web.config import WebConfig
from crewmeal.search_enhancement.web.security import (
    get_document_for_token,
    require_admin,
)


def _build(tmp_path: Path, *, admin_key: str | None = None) -> tuple[FastAPI, SearchEnhancementRepository]:
    repository = SearchEnhancementRepository(tmp_path / "web.db")
    repository.initialize()
    store = LocalArtifactStore(tmp_path / "artifacts")
    config = WebConfig(
        base_url="https://status.example",
        admin_key=admin_key,
        session_secret="test-session-secret",
    )
    app = create_app(repository=repository, artifact_store=store, web_config=config)

    @app.get("/_probe/admin")
    def _admin_probe(_: None = Depends(require_admin)) -> dict[str, bool]:
        return {"ok": True}

    @app.get("/_probe/token/{token}")
    def _token_probe(
        document: DocumentRecord = Depends(get_document_for_token),
    ) -> dict[str, str]:
        return {"file_name": document.file_name}

    return app, repository


def test_health_probes(tmp_path: Path) -> None:
    app, _ = _build(tmp_path)
    client = TestClient(app)
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}
    assert client.get("/").json() == {"service": "crewmeal-search-enhancement"}


def test_admin_gate_returns_503_when_unconfigured(tmp_path: Path) -> None:
    app, _ = _build(tmp_path, admin_key=None)
    client = TestClient(app)
    assert client.get("/_probe/admin").status_code == 503


def test_admin_gate_enforces_key(tmp_path: Path) -> None:
    app, _ = _build(tmp_path, admin_key="s3cret-key")
    client = TestClient(app)
    assert client.get("/_probe/admin").status_code == 401
    assert client.get("/_probe/admin", headers={"x-admin-key": "wrong"}).status_code == 401
    ok_header = client.get("/_probe/admin", headers={"x-admin-key": "s3cret-key"})
    assert ok_header.status_code == 200 and ok_header.json() == {"ok": True}
    assert client.get("/_probe/admin", params={"key": "s3cret-key"}).status_code == 200


def test_token_gate_resolves_and_404s(tmp_path: Path) -> None:
    app, repository = _build(tmp_path)
    document = repository.create_upload_document(
        file_name="deck.pptx", connection_id="conn"
    )
    assert document.status_token
    client = TestClient(app)
    resolved = client.get(f"/_probe/token/{document.status_token}")
    assert resolved.status_code == 200
    assert resolved.json() == {"file_name": "deck.pptx"}
    assert client.get("/_probe/token/not-a-real-token").status_code == 404

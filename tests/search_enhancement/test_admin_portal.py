from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from crewmeal.search_enhancement.artifact_store import (
    LocalArtifactStore,
    artifact_path,
)
from crewmeal.search_enhancement.database import (
    DocumentKey,
    DocumentRecord,
    SearchEnhancementRepository,
)
from crewmeal.search_enhancement.web import create_app
from crewmeal.search_enhancement.web.config import WebConfig

ADMIN_KEY = "secret-admin-key"
AUTH = {"X-Admin-Key": ADMIN_KEY}


def _build(
    tmp_path: Path, *, admin_key: str | None = ADMIN_KEY
) -> tuple[FastAPI, SearchEnhancementRepository, LocalArtifactStore]:
    repository = SearchEnhancementRepository(tmp_path / "web.db")
    repository.initialize()
    store = LocalArtifactStore(tmp_path / "artifacts")
    config = WebConfig(
        base_url="https://status.example",
        session_secret="test-secret",
        admin_key=admin_key,
    )
    app = create_app(repository=repository, artifact_store=store, web_config=config)
    return app, repository, store


def _seed_document(
    repository: SearchEnhancementRepository, store: LocalArtifactStore
) -> DocumentRecord:
    key = DocumentKey(
        tenant_id="tenant", site_id="site", drive_id="drive", item_id="item-1"
    )
    request_id = str(uuid4())
    repository.upsert_document(
        key=key,
        list_id="list",
        list_item_id="7",
        web_url="https://tenant.sharepoint.com/sites/test/deck.pptx",
        file_name="deck.pptx",
        connection_id="conn",
        desired_enabled=True,
        status="Ready",
        request_id=request_id,
    )
    job_id = repository.enqueue_job(
        key=key, request_id=request_id, job_type="upsert", trigger="spfx"
    )
    repository.add_job_event(job_id, stage="READY", detail={"version": 1})
    stored = store.put_bytes(
        artifact_path(key, version=1, kind="html", filename="index.html"),
        b"<article>Deck</article>",
        content_type="text/html; charset=utf-8",
    )
    repository.record_artifact(
        key,
        kind="html",
        blob_path=stored.path,
        content_type="text/html; charset=utf-8",
        byte_count=stored.byte_count,
        enhancement_version=1,
    )
    document = repository.get_document(key)
    assert document is not None
    return document


def test_admin_requires_key(tmp_path: Path) -> None:
    app, _, _ = _build(tmp_path)
    client = TestClient(app)

    assert client.get("/admin").status_code == 401
    assert client.get("/admin?key=wrong").status_code == 401
    assert client.get("/admin", headers=AUTH).status_code == 200


def test_admin_disabled_returns_503(tmp_path: Path) -> None:
    app, _, _ = _build(tmp_path, admin_key=None)
    client = TestClient(app)
    assert client.get("/admin").status_code == 503
    assert client.get("/admin/login").status_code == 503


def test_admin_login_sets_session(tmp_path: Path) -> None:
    app, _, _ = _build(tmp_path)
    client = TestClient(app)

    bad = client.post("/admin/login", data={"key": "nope"}, follow_redirects=False)
    assert bad.status_code == 401

    ok = client.post(
        "/admin/login", data={"key": ADMIN_KEY}, follow_redirects=False
    )
    assert ok.status_code == 303
    # Session cookie now carried by the client jar → gated route works keyless.
    assert client.get("/admin").status_code == 200

    client.get("/admin/logout")
    assert client.get("/admin").status_code == 401


def test_dashboard_shows_counts(tmp_path: Path) -> None:
    app, repository, store = _build(tmp_path)
    _seed_document(repository, store)
    client = TestClient(app)

    response = client.get("/admin", headers=AUTH)

    assert response.status_code == 200
    assert "대시보드" in response.text
    assert "deck.pptx" in response.text


def test_dashboard_shows_total_cost(tmp_path: Path) -> None:
    app, repository, store = _build(tmp_path)
    document = _seed_document(repository, store)
    job = repository.get_latest_job(document.key)
    assert job is not None
    repository.complete_job(
        job.job_id,
        usage={"tokens": {"gpt-5.2-input": 2_000_000, "gpt-5.2-output": 1_000_000}},
    )
    client = TestClient(app)

    response = client.get("/admin", headers=AUTH)

    assert response.status_code == 200
    assert "누적 비용(추정)" in response.text


def test_documents_list_and_detail(tmp_path: Path) -> None:
    app, repository, store = _build(tmp_path)
    document = _seed_document(repository, store)
    client = TestClient(app)

    listing = client.get("/admin/documents", headers=AUTH)
    assert listing.status_code == 200
    assert "deck.pptx" in listing.text

    detail = client.get(f"/admin/documents/{document.status_token}", headers=AUTH)
    assert detail.status_code == 200
    assert document.request_id in detail.text

    assert (
        client.get("/admin/documents/nope", headers=AUTH).status_code == 404
    )


def test_admin_rerun_enqueues_job(tmp_path: Path) -> None:
    app, repository, store = _build(tmp_path)
    document = _seed_document(repository, store)
    client = TestClient(app)
    before = len(repository.list_recent_jobs())

    response = client.post(
        f"/admin/documents/{document.status_token}/rerun",
        headers=AUTH,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert len(repository.list_recent_jobs()) == before + 1


def test_settings_roundtrip(tmp_path: Path) -> None:
    app, repository, _ = _build(tmp_path)
    client = TestClient(app)

    saved = client.post(
        "/admin/settings",
        data={"key": "analysis_dpi", "value": "144"},
        headers=AUTH,
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert repository.get_all_settings().get("analysis_dpi") == 144

    page = client.get("/admin/settings", headers=AUTH)
    assert "analysis_dpi" in page.text


def test_feedback_export_is_ndjson(tmp_path: Path) -> None:
    app, repository, store = _build(tmp_path)
    document = _seed_document(repository, store)
    repository.add_feedback_record(
        document.key,
        enhancement_version=1,
        correction_text="간트 헤더를 표로 인식하세요",
        model="gpt-4o",
        created_by="admin",
    )
    client = TestClient(app)

    response = client.get("/admin/feedback/export.jsonl", headers=AUTH)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    lines = [line for line in response.text.splitlines() if line.strip()]
    assert len(lines) == 1
    assert "간트 헤더를 표로 인식하세요" in lines[0]


def test_tryout_upload_creates_upload_job(tmp_path: Path) -> None:
    app, repository, _ = _build(tmp_path)
    client = TestClient(app)

    response = client.post(
        "/admin/tryout",
        files={
            "file": (
                "demo.pptx",
                b"PK\x03\x04 fake pptx bytes",
                "application/vnd.openxmlformats-officedocument."
                "presentationml.presentation",
            )
        },
        data={"comment": ""},
        headers=AUTH,
        follow_redirects=False,
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/s/")

    uploads = repository.list_documents(source_kind="upload")
    assert len(uploads) == 1
    document = uploads[0]
    artifacts = repository.list_artifacts(document.key)
    assert any(a.kind == "source_pptx" for a in artifacts)
    jobs = repository.list_recent_jobs()
    assert any(job["source_kind"] == "upload" for job in jobs)


def test_tryout_rejects_non_pptx(tmp_path: Path) -> None:
    app, repository, _ = _build(tmp_path)
    client = TestClient(app)

    response = client.post(
        "/admin/tryout",
        files={"file": ("notes.txt", b"hello", "text/plain")},
        data={"comment": ""},
        headers=AUTH,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "/admin/tryout" in response.headers["location"]
    assert repository.count_documents(source_kind="upload") == 0


def test_settings_page_lists_format_toggles(tmp_path: Path) -> None:
    app, _, _ = _build(tmp_path)
    client = TestClient(app)

    page = client.get("/admin/settings", headers=AUTH)

    assert page.status_code == 200
    assert "문서 형식 지원" in page.text
    assert 'value="pptx"' in page.text
    assert 'value="pdf"' in page.text


def test_disabling_format_persists_and_gates_tryout(tmp_path: Path) -> None:
    app, repository, _ = _build(tmp_path)
    client = TestClient(app)

    saved = client.post(
        "/admin/settings/formats",
        data={"enabled": ["pptx"]},  # pdf left unchecked -> disabled
        headers=AUTH,
        follow_redirects=False,
    )
    assert saved.status_code == 303

    settings = repository.get_all_settings()
    assert settings.get("format.pptx.enabled") is True
    assert settings.get("format.pdf.enabled") is False

    rejected = client.post(
        "/admin/tryout",
        files={"file": ("demo.pdf", b"%PDF-1.7 fake", "application/pdf")},
        data={"comment": ""},
        headers=AUTH,
        follow_redirects=False,
    )
    assert rejected.status_code == 303
    assert "/admin/tryout" in rejected.headers["location"]
    assert repository.count_documents(source_kind="upload") == 0


def test_settings_page_shows_vision_model_card(tmp_path: Path) -> None:
    app, _, _ = _build(tmp_path)
    client = TestClient(app)

    page = client.get("/admin/settings", headers=AUTH)

    assert page.status_code == 200
    assert "이미지 분석 모델" in page.text
    assert 'name="vision.deployment"' in page.text


def test_vision_model_settings_roundtrip(tmp_path: Path) -> None:
    app, repository, _ = _build(tmp_path)
    client = TestClient(app)

    saved = client.post(
        "/admin/settings/vision",
        data={
            "vision.provider": "openai_compatible",
            "vision.model": "qwen2-vl",
            "vision.deployment": "qwen2-vl-7b",
            "vision.base_url": "https://models.internal/v1/",
            "vision.reasoning_effort": "low",
        },
        headers=AUTH,
        follow_redirects=False,
    )
    assert saved.status_code == 303

    settings = repository.get_all_settings()
    assert settings["vision.provider"] == "openai_compatible"
    assert settings["vision.deployment"] == "qwen2-vl-7b"
    assert settings["vision.reasoning_effort"] == "low"


def test_settings_page_shows_decryption_card(tmp_path: Path) -> None:
    app, _, _ = _build(tmp_path)
    client = TestClient(app)

    page = client.get("/admin/settings", headers=AUTH)

    assert page.status_code == 200
    assert "암호화 문서 복호화" in page.text
    assert 'value="mip"' in page.text


def test_decryption_toggle_roundtrip(tmp_path: Path) -> None:
    app, repository, _ = _build(tmp_path)
    client = TestClient(app)

    saved = client.post(
        "/admin/settings/decryption",
        data={"enabled": ["mip"]},  # generic left off
        headers=AUTH,
        follow_redirects=False,
    )
    assert saved.status_code == 303

    settings = repository.get_all_settings()
    assert settings["decryption.mip.enabled"] is True
    assert settings["decryption.generic.enabled"] is False

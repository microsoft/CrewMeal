from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
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
from crewmeal.search_enhancement.pricing import estimate_cost
from crewmeal.search_enhancement.publication import (
    COLUMN_DISPLAY_NAME_SETTING,
)
from crewmeal.search_enhancement.web import create_app
from crewmeal.search_enhancement.web.config import WebConfig
from crewmeal.search_enhancement.web.routers import admin as admin_router
from crewmeal.search_enhancement.decryption import decryption_setting_key

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
    assert "이번 강화 비용(추정)" not in detail.text

    assert (
        client.get("/admin/documents/nope", headers=AUTH).status_code == 404
    )


def test_document_detail_uses_latest_cost_while_dashboard_stays_cumulative(
    tmp_path: Path,
) -> None:
    app, repository, store = _build(tmp_path)
    document = _seed_document(repository, store)
    first_job = repository.get_latest_job(document.key)
    assert first_job is not None
    first_usage = {
        "tokens": {
            "gpt-5.2-input": 1_000_000,
            "gpt-5.2-output": 500_000,
        }
    }
    repository.complete_job(first_job.job_id, usage=first_usage)
    _, latest_job_id = repository.queue_refresh(document.key, trigger="user")
    latest_usage = {
        "tokens": {
            "gpt-5.6-luna-input": 100_000,
            "gpt-5.6-luna-output": 50_000,
        }
    }
    repository.complete_job(latest_job_id, usage=latest_usage)
    repository.add_job_event(latest_job_id, stage="READY", detail={"version": 2})
    client = TestClient(app)

    detail = client.get(f"/admin/documents/{document.status_token}", headers=AUTH)
    dashboard = client.get("/admin", headers=AUTH)

    latest_cost = estimate_cost([latest_usage])
    assert "이번 강화 비용(추정)" in detail.text
    assert f"약 ₩{latest_cost.krw_display}" in detail.text
    assert "gpt-5.2 + gpt-5.6-luna" not in detail.text

    cumulative_cost = estimate_cost([first_usage, latest_usage])
    assert "누적 비용(추정)" in dashboard.text
    assert f"₩{cumulative_cost.krw_display}" in dashboard.text
    assert "gpt-5.2 + gpt-5.6-luna" in dashboard.text


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


def test_publication_settings_select_target_and_queue_republish(
    tmp_path: Path,
) -> None:
    app, repository, store = _build(tmp_path)
    document = _seed_document(repository, store)
    client = TestClient(app)

    response = client.post(
        "/admin/settings/publication",
        data={
            "target": "copilot_connector",
            "column_display_name_value": "검색 콘텐츠 HTML",
        },
        headers=AUTH,
        follow_redirects=False,
    )

    assert response.status_code == 303
    transition = repository.get_publication_transition()
    assert transition.desired_target.value == "copilot_connector"
    assert transition.status == "staging"
    assert repository.get_setting(COLUMN_DISPLAY_NAME_SETTING) == (
        "검색 콘텐츠 HTML"
    )
    refreshed = repository.get_document(document.key)
    assert refreshed is not None and refreshed.status == "Queued"


def test_publication_target_change_supersedes_an_old_queued_refresh(
    tmp_path: Path,
) -> None:
    app, repository, store = _build(tmp_path)
    document = _seed_document(repository, store)
    repository.queue_refresh(document.key, trigger="older-target")
    queued = repository.get_document(document.key)
    assert queued is not None

    response = TestClient(app).post(
        "/admin/settings/publication",
        data={
            "target": "copilot_connector",
            "column_display_name_value": "검색 콘텐츠 HTML",
        },
        headers=AUTH,
        follow_redirects=False,
    )

    current = repository.get_document(document.key)
    assert response.status_code == 303
    assert current is not None
    assert current.request_id != queued.request_id
    assert repository.has_pending_job(
        document.key,
        request_id=current.request_id,
    )


def test_publication_settings_reject_unknown_target(tmp_path: Path) -> None:
    app, _, _ = _build(tmp_path)
    response = TestClient(app).post(
        "/admin/settings/publication",
        data={
            "target": "not-a-target",
            "column_display_name_value": "검색 콘텐츠 HTML",
        },
        headers=AUTH,
    )
    assert response.status_code == 422


def test_generic_settings_form_rejects_publication_keys(tmp_path: Path) -> None:
    app, repository, _ = _build(tmp_path)
    response = TestClient(app).post(
        "/admin/settings",
        data={"key": "publication.target", "value": '"sharepoint_column"'},
        headers=AUTH,
    )
    assert response.status_code == 422
    assert repository.get_setting("publication.target") is None


def test_settings_page_shows_publication_contract(tmp_path: Path) -> None:
    app, _, _ = _build(tmp_path)
    page = TestClient(app).get("/admin/settings", headers=AUTH)
    assert page.status_code == 200
    assert "검색 콘텐츠 게시 방식" in page.text
    assert "CrewmealSearchContent" in page.text
    assert "63999" in page.text


def test_publication_validation_form_carries_transition_identity(
    tmp_path: Path,
) -> None:
    app, repository, _ = _build(tmp_path)
    transition = repository.request_publication_target("sharepoint_column")

    page = TestClient(app).get("/admin/settings", headers=AUTH)

    assert page.status_code == 200
    assert f'name="generation" value="{transition.generation}"' in page.text
    assert 'name="desired_target" value="sharepoint_column"' in page.text


def test_replaying_column_provisioning_keeps_active_transition_active(
    tmp_path: Path,
) -> None:
    app, repository, _ = _build(tmp_path)
    repository.request_publication_target("sharepoint_column")
    repository.set_column_provisioned()
    repository.set_transition_status("awaiting_reindex")
    repository.set_reindex_requested()
    repository.set_search_verified()
    repository.set_copilot_verified()
    repository.activate_publication_target()
    before = repository.get_publication_transition()

    response = TestClient(app).post(
        "/admin/settings/publication/column-provisioned",
        data={
            "generation": str(before.generation),
            "desired_target": before.desired_target.value,
        },
        headers=AUTH,
        follow_redirects=False,
    )

    after = repository.get_publication_transition()
    assert response.status_code == 303
    assert after.generation == before.generation
    assert after.status == "active"
    assert after.active_target.value == "sharepoint_column"


def test_stale_publication_validation_form_cannot_approve_new_generation(
    tmp_path: Path,
) -> None:
    app, repository, _ = _build(tmp_path)
    stale = repository.request_publication_target("sharepoint_column")
    repository.request_publication_target("copilot_connector")
    current = repository.request_publication_target("sharepoint_column")
    assert current.generation > stale.generation

    response = TestClient(app).post(
        "/admin/settings/publication/column-provisioned",
        data={
            "generation": str(stale.generation),
            "desired_target": stale.desired_target.value,
        },
        headers=AUTH,
        follow_redirects=False,
    )

    after = repository.get_publication_transition()
    assert response.status_code == 409
    assert after.generation == current.generation
    assert after.column_provisioned is False


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


def test_settings_page_shows_analysis_tier_card(tmp_path: Path) -> None:
    app, _, _ = _build(tmp_path)
    client = TestClient(app)

    page = client.get("/admin/settings", headers=AUTH)

    assert page.status_code == 200
    assert "분석 품질 티어" in page.text
    assert 'name="analysis.tier"' in page.text
    assert 'name="analysis.ocr.enabled"' in page.text


def test_analysis_tier_settings_roundtrip(tmp_path: Path) -> None:
    app, repository, _ = _build(tmp_path)
    client = TestClient(app)

    saved = client.post(
        "/admin/settings/analysis",
        data={"analysis.tier": "text_ocr", "analysis.ocr.enabled": "1"},
        headers=AUTH,
        follow_redirects=False,
    )
    assert saved.status_code == 303

    settings = repository.get_all_settings()
    assert settings["analysis.tier"] == "text_ocr"
    assert settings["analysis.ocr.enabled"] is True

    # Unchecked OCR box is absent from the form -> stored as off.
    client.post(
        "/admin/settings/analysis",
        data={"analysis.tier": "vision"},
        headers=AUTH,
        follow_redirects=False,
    )
    settings = repository.get_all_settings()
    assert settings["analysis.tier"] == "vision"
    assert settings["analysis.ocr.enabled"] is False


def test_settings_page_shows_decryption_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CREWMEAL_MIP_SDK_CLI", raising=False)
    app, _, _ = _build(tmp_path)
    client = TestClient(app)

    page = client.get("/admin/settings", headers=AUTH)

    assert page.status_code == 200
    assert "암호화 문서 복호화" in page.text
    assert 'value="mip"' in page.text
    # MIP is implemented but no SDK CLI is configured in this environment.
    assert "SDK 미구성" in page.text


def test_settings_page_shows_mip_available_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "CREWMEAL_MIP_SDK_CLI", "python -m crewmeal.search_enhancement.mip_tool"
    )
    app, _, _ = _build(tmp_path)
    client = TestClient(app)

    page = client.get("/admin/settings", headers=AUTH)

    assert page.status_code == 200
    assert "사용 가능" in page.text


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


def test_mip_live_health_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "CREWMEAL_MIP_SDK_CLI", "python -m crewmeal.search_enhancement.mip_tool"
    )
    # MIP configured but not enabled -> no probe, no network, empty result.
    assert admin_router._mip_live_health({}) == {}


def test_mip_live_health_swallows_credential_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Enabled + configured, but the service-principal env is incomplete: the
    # settings page must still render, so the failure is reported as an
    # unavailable-token health entry rather than raising.
    monkeypatch.setenv(
        "CREWMEAL_MIP_SDK_CLI", "python -m crewmeal.search_enhancement.mip_tool"
    )
    for var in (
        "CREWMEAL_M365_TENANT_ID",
        "CREWMEAL_M365_CLIENT_ID",
        "CREWMEAL_M365_CLIENT_SECRET",
        "CREWMEAL_M365_SITE_ID",
        "CREWMEAL_M365_DRIVE_ID",
        "CREWMEAL_M365_LIST_ID",
        "CREWMEAL_M365_SITE_URL",
    ):
        monkeypatch.delenv(var, raising=False)

    health = admin_router._mip_live_health({decryption_setting_key("mip"): True})

    assert health["mip"]["decrypt_ready"] is False
    assert "unavailable" in health["mip"]["detail"]


def test_settings_page_renders_live_tenant_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        admin_router,
        "_mip_live_health",
        lambda _settings, force=False: {
            "mip": {
                "ok": True,
                "super_user": True,
                "decrypt_ready": True,
                "detail": "RMS token acquired and super-user role present",
            }
        },
    )
    app, _, _ = _build(tmp_path)
    page = TestClient(app).get("/admin/settings", headers=AUTH)

    assert page.status_code == 200
    assert "테넌트 준비됨" in page.text
    assert "super-user role present" in page.text


def test_settings_page_shows_setup_wizard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The wizard is visible on a normal page load (no probe yet): checklist rows
    # plus the pre-filled admin-consent URL and grant commands.
    monkeypatch.setattr(
        admin_router,
        "_config_service_principal_ids",
        lambda: ("tenant-xyz", "client-abc"),
    )
    monkeypatch.setattr(
        admin_router, "_mip_live_health", lambda _settings, force=False: {}
    )
    app, _, _ = _build(tmp_path)
    page = TestClient(app).get("/admin/settings", headers=AUTH)

    assert page.status_code == 200
    assert "MIP 테넌트 준비 마법사" in page.text
    assert "Content.SuperUser" in page.text
    assert (
        "https://login.microsoftonline.com/tenant-xyz/adminconsent"
        "?client_id=client-abc" in page.text
    )
    assert "/admin/settings/decryption/recheck" in page.text


def test_setup_wizard_recheck_forces_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, bool] = {}

    def _fake_health(_settings, force=False):
        seen["force"] = force
        return {
            "mip": {
                "ok": True,
                "super_user": True,
                "decrypt_ready": True,
                "object_id": "sp-oid-123",
                "detail": "RMS token acquired and super-user role present",
            }
        }

    monkeypatch.setattr(admin_router, "_mip_live_health", _fake_health)
    monkeypatch.setattr(
        admin_router,
        "_config_service_principal_ids",
        lambda: ("tenant-xyz", "client-abc"),
    )
    app, _, _ = _build(tmp_path)

    page = TestClient(app).post(
        "/admin/settings/decryption/recheck", headers=AUTH
    )

    assert page.status_code == 200
    assert seen["force"] is True
    # The discovered service-principal object id is pre-filled into the commands.
    assert "sp-oid-123" in page.text
    assert "방금 실시간 점검" in page.text


def test_mip_live_health_force_probes_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # force=True must probe even when the toggle is off and no adapter is wired,
    # so an admin can verify tenant readiness before enabling. With no M365 env
    # the credential build fails and is reported (never raises).
    monkeypatch.delenv("CREWMEAL_MIP_SDK_CLI", raising=False)
    for var in (
        "CREWMEAL_M365_TENANT_ID",
        "CREWMEAL_M365_CLIENT_ID",
        "CREWMEAL_M365_CLIENT_SECRET",
        "CREWMEAL_M365_SITE_ID",
        "CREWMEAL_M365_DRIVE_ID",
        "CREWMEAL_M365_LIST_ID",
        "CREWMEAL_M365_SITE_URL",
    ):
        monkeypatch.delenv(var, raising=False)

    health = admin_router._mip_live_health({}, force=True)

    assert health["mip"]["decrypt_ready"] is False
    assert "unavailable" in health["mip"]["detail"]

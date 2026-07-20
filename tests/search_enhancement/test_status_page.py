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
from crewmeal.search_enhancement.pricing import estimate_cost
from crewmeal.search_enhancement.web import create_app
from crewmeal.search_enhancement.web.config import WebConfig


def _build(tmp_path: Path) -> tuple[FastAPI, SearchEnhancementRepository, LocalArtifactStore]:
    repository = SearchEnhancementRepository(tmp_path / "web.db")
    repository.initialize()
    store = LocalArtifactStore(tmp_path / "artifacts")
    config = WebConfig(base_url="https://status.example", session_secret="test-secret")
    app = create_app(repository=repository, artifact_store=store, web_config=config)
    return app, repository, store


def _seed_ready_document(
    repository: SearchEnhancementRepository,
    store: LocalArtifactStore,
    *,
    html: bytes = b"<article><h1>Deck</h1></article>",
) -> DocumentRecord:
    key = DocumentKey(tenant_id="tenant", site_id="site", drive_id="drive", item_id="item-1")
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
    repository.add_job_event(job_id, stage="CLAIMED")
    repository.add_job_event(job_id, stage="ANALYZING", detail={"completed": 0, "total": 2})
    repository.add_job_event(
        job_id, stage="ANALYZING", detail={"completed": 1, "total": 2, "slide": 1}
    )
    repository.add_job_event(
        job_id, stage="ANALYZING", detail={"completed": 2, "total": 2, "slide": 2}
    )
    repository.add_job_event(job_id, stage="READY", detail={"version": 1})
    stored = store.put_bytes(
        artifact_path(key, version=1, kind="html", filename="index.html"),
        html,
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


def test_status_page_renders_timeline(tmp_path: Path) -> None:
    app, repository, store = _build(tmp_path)
    document = _seed_ready_document(repository, store)
    client = TestClient(app)

    response = client.get(f"/s/{document.status_token}")

    assert response.status_code == 200
    assert "deck.pptx" in response.text
    assert "완료" in response.text  # READY label
    assert "콘텐츠 분석" in response.text  # ANALYZING label
    assert "총 2쪽" in response.text  # slide total surfaced
    assert "2/2쪽" in response.text  # collapsed analyze row shows slide progress


def test_html_preview_wraps_fragment_with_table_styles(tmp_path: Path) -> None:
    app, repository, store = _build(tmp_path)
    document = _seed_ready_document(
        repository,
        store,
        html=b"<article><table><tbody><tr><td>CELL-A</td></tr></tbody></table></article>",
    )
    client = TestClient(app)

    response = client.get(f"/s/{document.status_token}/html")

    assert response.status_code == 200
    assert "CELL-A" in response.text
    assert "border-collapse" in response.text  # preview shell adds table borders
    assert "<!doctype html>" in response.text.lower()


def test_progress_partial_reports_terminal(tmp_path: Path) -> None:
    app, repository, store = _build(tmp_path)
    document = _seed_ready_document(repository, store)
    client = TestClient(app)

    response = client.get(f"/s/{document.status_token}/progress")

    assert response.status_code == 200
    assert 'data-terminal="true"' in response.text


def test_html_preview_returns_content_with_csp(tmp_path: Path) -> None:
    app, repository, store = _build(tmp_path)
    document = _seed_ready_document(repository, store, html=b"<article>PREVIEW-BODY</article>")
    client = TestClient(app)

    response = client.get(f"/s/{document.status_token}/html")

    assert response.status_code == 200
    assert "PREVIEW-BODY" in response.text
    assert "Content-Security-Policy" in response.headers
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]


def test_html_preview_placeholder_when_missing(tmp_path: Path) -> None:
    app, repository, _ = _build(tmp_path)
    document = repository.create_upload_document(file_name="x.pptx", connection_id="conn")
    client = TestClient(app)
    response = client.get(f"/s/{document.status_token}/html")
    assert response.status_code == 200
    assert "아직 추출된 HTML이 없습니다" in response.text


def test_reprocess_hides_stale_preview(tmp_path: Path) -> None:
    app, repository, store = _build(tmp_path)
    document = _seed_ready_document(
        repository, store, html=b"<article>OLD-PREVIEW</article>"
    )
    # Simulate the user clicking "다시 강화": a fresh job is queued and has not yet
    # reached a terminal stage, so the previous HTML is stale.
    repository.queue_refresh(document.key, trigger="user")
    client = TestClient(app)

    page = client.get(f"/s/{document.status_token}")
    assert page.status_code == 200
    assert "재작업이 진행 중입니다" in page.text
    # The stale preview iframe/link must not be rendered while reprocessing.
    assert f"/s/{document.status_token}/html" not in page.text

    preview = client.get(f"/s/{document.status_token}/html")
    assert preview.status_code == 200
    assert "OLD-PREVIEW" not in preview.text  # stale body never served
    assert "재작업이 진행 중입니다" in preview.text


def test_status_page_shows_cost(tmp_path: Path) -> None:
    app, repository, store = _build(tmp_path)
    document = _seed_ready_document(repository, store)
    job = repository.get_latest_job(document.key)
    assert job is not None
    usage = {
        "slideImages": 2,
        "tokens": {"gpt-5.2-input": 1_000_000, "gpt-5.2-output": 500_000},
    }
    repository.complete_job(job.job_id, usage=usage)
    client = TestClient(app)

    response = client.get(f"/s/{document.status_token}")

    assert response.status_code == 200
    assert "이번 강화 비용(추정)" in response.text
    expected = estimate_cost([usage])
    assert f"약 ₩{expected.krw_display}" in response.text


def test_status_page_cost_only_uses_latest_run(tmp_path: Path) -> None:
    app, repository, store = _build(tmp_path)
    document = _seed_ready_document(repository, store)
    first_job = repository.get_latest_job(document.key)
    assert first_job is not None
    repository.complete_job(
        first_job.job_id,
        usage={
            "tokens": {
                "gpt-5.2-input": 1_000_000,
                "gpt-5.2-output": 500_000,
            }
        },
    )
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

    response = client.get(f"/s/{document.status_token}")

    assert response.status_code == 200
    expected = estimate_cost([latest_usage])
    assert f"약 ₩{expected.krw_display}" in response.text
    assert "gpt-5.6-luna 토큰 기준 추정" in response.text
    assert "gpt-5.2 + gpt-5.6-luna" not in response.text


def test_rerun_action_enqueues_job(tmp_path: Path) -> None:
    app, repository, store = _build(tmp_path)
    document = _seed_ready_document(repository, store)
    client = TestClient(app)
    before = len(repository.list_recent_jobs())

    response = client.post(
        f"/s/{document.status_token}/rerun", follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/s/{document.status_token}"
    assert len(repository.list_recent_jobs()) == before + 1


def test_comment_action_records_note_and_feedback(tmp_path: Path) -> None:
    app, repository, store = _build(tmp_path)
    document = _seed_ready_document(repository, store)
    client = TestClient(app)

    response = client.post(
        f"/s/{document.status_token}/comment",
        data={"comment": "왼쪽 열을 하나의 계층으로 묶어주세요"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    notes = repository.correction_notes(document.key)
    assert notes and notes[-1]["text"] == "왼쪽 열을 하나의 계층으로 묶어주세요"
    latest_job = repository.get_latest_job(document.key)
    assert latest_job is not None and latest_job.feedback == "왼쪽 열을 하나의 계층으로 묶어주세요"


def test_remove_action_enqueues_delete(tmp_path: Path) -> None:
    app, repository, store = _build(tmp_path)
    document = _seed_ready_document(repository, store)
    client = TestClient(app)

    response = client.post(
        f"/s/{document.status_token}/remove", follow_redirects=False
    )

    assert response.status_code == 303
    jobs = repository.list_recent_jobs()
    assert any(job["job_type"] == "delete" for job in jobs)


def test_unknown_token_is_404(tmp_path: Path) -> None:
    app, _, _ = _build(tmp_path)
    client = TestClient(app)
    assert client.get("/s/not-real").status_code == 404

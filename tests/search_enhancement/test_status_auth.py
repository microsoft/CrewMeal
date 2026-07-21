from __future__ import annotations

import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from crewmeal.config import ConfigurationError
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
from crewmeal.search_enhancement.web.oidc import SignedInUser
from crewmeal.search_enhancement.web.routers import auth_sso

# Fetch-style requests set this header; the gate returns 401 (not a redirect) so
# the polling JS can reload into the sign-in flow.
_FETCH = {"sec-fetch-dest": "empty"}

# Env vars that steer WebConfig.from_environment; cleared before each config test
# so the ambient shell/CI environment can't leak into the assertions.
_CONFIG_ENV_VARS = (
    "CREWMEAL_WEB_BASE_URL",
    "CREWMEAL_WEB_SESSION_SECRET",
    "CREWMEAL_INGEST_REQUIRE_AUTH",
    "CREWMEAL_STATUS_REQUIRE_AUTH",
    "CREWMEAL_STATUS_SSO_TENANT_ID",
    "CREWMEAL_STATUS_SSO_CLIENT_ID",
    "CREWMEAL_STATUS_SSO_CLIENT_SECRET",
    "CREWMEAL_M365_TENANT_ID",
    "CREWMEAL_M365_CLIENT_ID",
    "CREWMEAL_M365_CLIENT_SECRET",
    "CREWMEAL_ADMIN_KEY",
    "CREWMEAL_INGEST_TENANT_ID",
    "CREWMEAL_INGEST_AUDIENCE",
    "CREWMEAL_INGEST_ALLOWED_APP_IDS",
    "CREWMEAL_INGEST_ALLOWED_ORIGINS",
)


def _build(
    tmp_path: Path, *, require_auth: bool = True
) -> tuple[FastAPI, SearchEnhancementRepository, LocalArtifactStore]:
    repository = SearchEnhancementRepository(tmp_path / "web.db")
    repository.initialize()
    store = LocalArtifactStore(tmp_path / "artifacts")
    config = WebConfig(
        base_url="https://status.example",
        session_secret="test-secret",
        status_require_auth=require_auth,
        sso_tenant_id="tenant-1",
        sso_client_id="client-1",
        sso_client_secret="secret-1",
    )
    app = create_app(repository=repository, artifact_store=store, web_config=config)
    return app, repository, store


def _seed_ready_document(
    repository: SearchEnhancementRepository, store: LocalArtifactStore
) -> DocumentRecord:
    key = DocumentKey(tenant_id="tenant", site_id="site", drive_id="drive", item_id="i1")
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
        b"<article><h1>Deck</h1></article>",
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


def _patch_flow(
    monkeypatch, *, user: SignedInUser | None = None
) -> None:
    """Replace the MSAL round trip so no network/app registration is needed."""

    def fake_begin(config: WebConfig) -> dict:
        return {"auth_uri": "https://login.microsoftonline.com/authorize?x=1", "state": "st"}

    def fake_complete(config: WebConfig, flow: dict, params: dict) -> SignedInUser:
        return user or SignedInUser(
            subject="user-1",
            name="User One",
            username="user1@contoso.com",
            expires_at=int(time.time()) + 3600,
        )

    monkeypatch.setattr(auth_sso, "begin_login", fake_begin)
    monkeypatch.setattr(auth_sso, "complete_login", fake_complete)


def _sign_in(client: TestClient, next_url: str) -> None:
    started = client.get(f"/auth/login?next={next_url}", follow_redirects=False)
    assert started.status_code == 303
    assert started.headers["location"].startswith("https://login.microsoftonline.com")
    client.get("/auth/callback?code=abc&state=st", follow_redirects=False)


# --------------------------------------------------------------------------- #
# Unauthenticated behaviour
# --------------------------------------------------------------------------- #
def test_status_page_redirects_to_login_when_unauthenticated(tmp_path: Path) -> None:
    app, repository, store = _build(tmp_path)
    document = _seed_ready_document(repository, store)
    client = TestClient(app)

    response = client.get(f"/s/{document.status_token}", follow_redirects=False)

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/auth/login?next=")
    assert "%2Fs%2F" in location  # the /s/<token> path, url-encoded


def test_status_progress_fetch_returns_401_when_unauthenticated(tmp_path: Path) -> None:
    app, repository, store = _build(tmp_path)
    document = _seed_ready_document(repository, store)
    client = TestClient(app)

    response = client.get(
        f"/s/{document.status_token}/progress", headers=_FETCH, follow_redirects=False
    )

    assert response.status_code == 401


def test_status_action_returns_401_when_unauthenticated(tmp_path: Path) -> None:
    app, repository, store = _build(tmp_path)
    document = _seed_ready_document(repository, store)
    client = TestClient(app)

    response = client.post(
        f"/s/{document.status_token}/rerun", follow_redirects=False
    )

    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Sign-in flow
# --------------------------------------------------------------------------- #
def test_login_and_callback_grants_access(tmp_path: Path, monkeypatch) -> None:
    app, repository, store = _build(tmp_path)
    document = _seed_ready_document(repository, store)
    _patch_flow(monkeypatch)
    client = TestClient(app)

    started = client.get(
        f"/auth/login?next=/s/{document.status_token}", follow_redirects=False
    )
    assert started.status_code == 303
    assert started.headers["location"].startswith("https://login.microsoftonline.com")

    callback = client.get("/auth/callback?code=abc&state=st", follow_redirects=False)
    assert callback.status_code == 303
    assert callback.headers["location"] == f"/s/{document.status_token}"

    page = client.get(f"/s/{document.status_token}")
    assert page.status_code == 200
    assert "deck.pptx" in page.text


def test_callback_sanitizes_open_redirect_next(tmp_path: Path, monkeypatch) -> None:
    app, _, _ = _build(tmp_path)
    _patch_flow(monkeypatch)
    client = TestClient(app)

    client.get("/auth/login?next=https://evil.example/x", follow_redirects=False)
    callback = client.get("/auth/callback?code=abc&state=st", follow_redirects=False)

    assert callback.status_code == 303
    assert callback.headers["location"] == "/"


def test_expired_session_is_rejected(tmp_path: Path, monkeypatch) -> None:
    app, repository, store = _build(tmp_path)
    document = _seed_ready_document(repository, store)
    _patch_flow(
        monkeypatch,
        user=SignedInUser(
            subject="user-1", name=None, username=None, expires_at=int(time.time()) - 10
        ),
    )
    client = TestClient(app)
    _sign_in(client, f"/s/{document.status_token}")

    response = client.get(f"/s/{document.status_token}", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/auth/login")


def test_logout_clears_session(tmp_path: Path, monkeypatch) -> None:
    app, repository, store = _build(tmp_path)
    document = _seed_ready_document(repository, store)
    _patch_flow(monkeypatch)
    client = TestClient(app)
    _sign_in(client, f"/s/{document.status_token}")
    assert client.get(f"/s/{document.status_token}").status_code == 200

    logout = client.get("/auth/logout", follow_redirects=False)
    assert logout.status_code == 303

    after = client.get(f"/s/{document.status_token}", follow_redirects=False)
    assert after.status_code == 303
    assert after.headers["location"].startswith("/auth/login")


# --------------------------------------------------------------------------- #
# Toggle off preserves the original anonymous behaviour
# --------------------------------------------------------------------------- #
def test_status_page_open_when_auth_disabled(tmp_path: Path) -> None:
    app, repository, store = _build(tmp_path, require_auth=False)
    document = _seed_ready_document(repository, store)
    client = TestClient(app)

    response = client.get(f"/s/{document.status_token}")

    assert response.status_code == 200


def test_login_route_noop_redirect_when_auth_disabled(tmp_path: Path) -> None:
    app, _, _ = _build(tmp_path, require_auth=False)
    client = TestClient(app)

    response = client.get("/auth/login?next=/s/abc", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/s/abc"


# --------------------------------------------------------------------------- #
# safe_next unit guards
# --------------------------------------------------------------------------- #
def test_safe_next_guards() -> None:
    assert auth_sso.safe_next(None) == "/"
    assert auth_sso.safe_next("") == "/"
    assert auth_sso.safe_next("/s/abc") == "/s/abc"
    assert auth_sso.safe_next("/s/abc?x=1") == "/s/abc?x=1"
    assert auth_sso.safe_next("//evil.example") == "/"
    assert auth_sso.safe_next("https://evil.example") == "/"
    assert auth_sso.safe_next("/\\evil") == "/"
    assert auth_sso.safe_next("/path\\to") == "/"


# --------------------------------------------------------------------------- #
# WebConfig.from_environment: the production security wiring
# --------------------------------------------------------------------------- #
def _clear_config_env(monkeypatch) -> None:
    for name in _CONFIG_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_from_environment_enables_status_auth_with_m365_creds(monkeypatch) -> None:
    _clear_config_env(monkeypatch)
    monkeypatch.setenv("CREWMEAL_WEB_BASE_URL", "https://status.example")
    monkeypatch.setenv("CREWMEAL_WEB_SESSION_SECRET", "s3cret")
    monkeypatch.setenv("CREWMEAL_INGEST_REQUIRE_AUTH", "true")
    monkeypatch.setenv("CREWMEAL_M365_TENANT_ID", "tenant-1")
    monkeypatch.setenv("CREWMEAL_M365_CLIENT_ID", "client-1")
    monkeypatch.setenv("CREWMEAL_M365_CLIENT_SECRET", "secret-1")

    config = WebConfig.from_environment()

    assert config.status_require_auth is True
    assert config.sso_enabled is True
    assert config.sso_tenant_id == "tenant-1"
    assert config.secure_cookies is True  # HTTPS base URL


def test_from_environment_raises_when_status_auth_without_creds(monkeypatch) -> None:
    _clear_config_env(monkeypatch)
    monkeypatch.setenv("CREWMEAL_WEB_BASE_URL", "https://status.example")
    monkeypatch.setenv("CREWMEAL_WEB_SESSION_SECRET", "s3cret")
    monkeypatch.setenv("CREWMEAL_STATUS_REQUIRE_AUTH", "true")

    with pytest.raises(ConfigurationError):
        WebConfig.from_environment()


def test_from_environment_status_sso_overrides_take_precedence(monkeypatch) -> None:
    _clear_config_env(monkeypatch)
    monkeypatch.setenv("CREWMEAL_WEB_BASE_URL", "https://status.example")
    monkeypatch.setenv("CREWMEAL_WEB_SESSION_SECRET", "s3cret")
    monkeypatch.setenv("CREWMEAL_STATUS_REQUIRE_AUTH", "true")
    monkeypatch.setenv("CREWMEAL_M365_TENANT_ID", "m365-tenant")
    monkeypatch.setenv("CREWMEAL_M365_CLIENT_ID", "m365-client")
    monkeypatch.setenv("CREWMEAL_M365_CLIENT_SECRET", "m365-secret")
    monkeypatch.setenv("CREWMEAL_STATUS_SSO_TENANT_ID", "sso-tenant")
    monkeypatch.setenv("CREWMEAL_STATUS_SSO_CLIENT_ID", "sso-client")
    monkeypatch.setenv("CREWMEAL_STATUS_SSO_CLIENT_SECRET", "sso-secret")

    config = WebConfig.from_environment()

    assert config.sso_tenant_id == "sso-tenant"
    assert config.sso_client_id == "sso-client"
    assert config.sso_client_secret == "sso-secret"


def test_from_environment_http_base_url_disables_secure_cookies(monkeypatch) -> None:
    _clear_config_env(monkeypatch)
    monkeypatch.setenv("CREWMEAL_WEB_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("CREWMEAL_INGEST_REQUIRE_AUTH", "false")
    monkeypatch.setenv("CREWMEAL_STATUS_REQUIRE_AUTH", "false")

    config = WebConfig.from_environment()

    assert config.secure_cookies is False
    assert config.status_require_auth is False

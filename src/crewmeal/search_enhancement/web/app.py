"""Application factory for the search-enhancement web app."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from crewmeal.search_enhancement.artifact_store import (
    ArtifactStore,
    create_artifact_store,
)
from crewmeal.search_enhancement.config import SearchEnhancementConfig
from crewmeal.search_enhancement.database import SearchEnhancementRepository
from crewmeal.search_enhancement.schema import resolve_database_target
from crewmeal.search_enhancement.web.auth import TokenValidator
from crewmeal.search_enhancement.web.config import WebConfig
from crewmeal.search_enhancement.web.routers import (
    admin,
    auth_sso,
    health,
    ingest,
    status as status_routes,
)
from crewmeal.search_enhancement.web.security import LoginRequired

_PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = _PACKAGE_DIR / "templates"
STATIC_DIR = _PACKAGE_DIR / "static"


def _build_repository() -> SearchEnhancementRepository:
    """Resolve the database target from the environment only.

    The web/status/admin surfaces must boot without SharePoint (M365) settings,
    so we read the SQLite fallback path directly rather than through
    :meth:`SearchEnhancementConfig.from_environment`, which requires M365 vars.
    ``DATABASE_URL`` (Postgres) takes precedence when present.
    """

    sqlite_path = Path(
        os.getenv("CREWMEAL_SEARCH_DB", ".crewmeal/search-enhancement.db")
    ).expanduser()
    repository = SearchEnhancementRepository(resolve_database_target(sqlite_path))
    repository.initialize()
    return repository


def _resolve_search_config() -> SearchEnhancementConfig | None:
    """Build the SharePoint config from the environment, or ``None`` when the
    SharePoint settings are absent (local dev / status-only deployments)."""

    try:
        return SearchEnhancementConfig.from_environment()
    except Exception:  # noqa: BLE001 - missing settings is a supported mode
        return None


def _build_control_client() -> object | None:
    """Build a SharePoint control client from the environment so the ingest API
    can resolve the Graph ``driveItem`` from a list item id. Returns ``None`` when
    SharePoint is not configured, in which case ingest falls back to client hints.
    """

    config = _resolve_search_config()
    if config is None:
        return None
    try:
        from crewmeal.search_enhancement.graph_client import GraphClient
        from crewmeal.search_enhancement.sharepoint_control import (
            SharePointControlClient,
        )

        return SharePointControlClient(config, GraphClient(config))
    except Exception:  # noqa: BLE001 - degrade to hint-based ingest
        return None


def create_app(
    *,
    repository: SearchEnhancementRepository | None = None,
    artifact_store: ArtifactStore | None = None,
    web_config: WebConfig | None = None,
    search_config: SearchEnhancementConfig | None = None,
    ingest_validator: TokenValidator | None = None,
    control_client: object | None = None,
) -> FastAPI:
    """Assemble the FastAPI app.

    All collaborators are optional so tests can inject fakes; anything omitted is
    resolved from the environment. ``search_config`` and ``control_client`` default
    to ``None`` (status/admin surfaces run without SharePoint); the production
    entrypoint :func:`create_app_from_env` wires them from the environment.
    ``ingest_validator`` is resolved lazily on first ingest call.
    """

    config = web_config or WebConfig.from_environment()
    repo = repository if repository is not None else _build_repository()
    artifacts = (
        artifact_store
        if artifact_store is not None
        else create_artifact_store(engine=repo.engine)
    )

    app = FastAPI(
        title="Crewmeal Search Enhancement",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=config.session_secret,
        same_site="lax",
        # Mark the session cookie Secure on HTTPS deployments (production) so the
        # admin key and sign-in identity never ride an insecure connection; keep it
        # relaxed for local http dev. SameSite=Lax already blocks cross-site POSTs
        # from carrying the cookie, which mitigates CSRF on the status actions.
        https_only=config.secure_cookies,
    )
    if config.ingest_allowed_origins:
        # The SPFx command calls POST /api/requests from the SharePoint page
        # origin with an Authorization header, which triggers a CORS preflight.
        # Without this the browser reports "Failed to fetch". Bearer tokens (not
        # cookies) carry auth, so credentials mode stays off.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(config.ingest_allowed_origins),
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["authorization", "content-type", "accept"],
            allow_credentials=False,
            max_age=600,
        )

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.globals["base_url"] = config.base_url

    app.state.repository = repo
    app.state.artifact_store = artifacts
    app.state.web_config = config
    app.state.templates = templates
    app.state.search_config = search_config
    app.state.ingest_validator = ingest_validator
    app.state.control_client = control_client

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(health.router)
    app.include_router(ingest.router)
    app.include_router(auth_sso.router)
    app.include_router(status_routes.router)
    app.include_router(admin.login_router)
    app.include_router(admin.router)

    @app.exception_handler(LoginRequired)
    async def _handle_login_required(
        request: Request, exc: LoginRequired
    ) -> RedirectResponse | JSONResponse:
        # Top-level navigations (loading the status page) get bounced to sign-in
        # and returned to ``next`` afterwards. Browser ``fetch``/XHR (which set
        # ``Sec-Fetch-Dest: empty``) and any non-GET get a 401 so the polling JS
        # can reload the page into the sign-in flow instead of injecting the login
        # HTML into a fragment.
        wants_redirect = (
            request.method == "GET"
            and request.headers.get("sec-fetch-dest") != "empty"
        )
        if wants_redirect:
            login_url = "/auth/login?next=" + quote(exc.next_url, safe="")
            return RedirectResponse(login_url, status_code=303)
        return JSONResponse({"detail": "Sign-in required."}, status_code=401)

    @app.get("/", include_in_schema=False)
    def index() -> dict[str, str]:
        return {"service": "crewmeal-search-enhancement"}

    return app


def create_app_from_env() -> FastAPI:
    """Production entrypoint: resolve SharePoint config + control client from the
    environment and build the app. Used by uvicorn with ``--factory``.
    """

    search_config = _resolve_search_config()
    return create_app(
        search_config=search_config,
        control_client=_build_control_client(),
    )

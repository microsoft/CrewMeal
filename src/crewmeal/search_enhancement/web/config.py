"""Runtime configuration for the web app.

Separate from :class:`SearchEnhancementConfig` because the web tier has its own
concerns (public base URL for building status links, the opaque admin key, the
signing secret for the admin session cookie, and how to validate the Entra token
the SPFx command presents to the ingest API).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from crewmeal.config import ConfigurationError

DEFAULT_BASE_URL = "http://localhost:8000"


def _split_csv(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _bool_environment(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class WebConfig:
    """Web-tier settings, resolved from environment in production."""

    base_url: str = DEFAULT_BASE_URL
    admin_key: str | None = None
    session_secret: str = field(default="dev-insecure-session-secret", repr=False)
    #: Entra tenant that must issue ingest tokens (defaults to the M365 tenant).
    ingest_tenant_id: str | None = None
    #: Accepted ``aud`` claim(s) for ingest tokens. Comma-separated to allow both
    #: our API App ID URI (``api://…``) and the app's client-id GUID (v2 tokens).
    ingest_audience: str | None = None
    #: Optional allow-list of caller ``azp``/``appid`` values (e.g. the SPFx app).
    ingest_allowed_app_ids: tuple[str, ...] = ()
    #: When false, the ingest API accepts unauthenticated calls (local dev only).
    require_ingest_auth: bool = True
    #: Browser origins allowed to call the ingest API (the SPFx/SharePoint
    #: origin, e.g. ``https://contoso.sharepoint.com``). Empty disables CORS.
    ingest_allowed_origins: tuple[str, ...] = ()

    def status_path(self, token: str) -> str:
        return f"/s/{token}"

    def status_url(self, token: str) -> str:
        return f"{self.base_url.rstrip('/')}{self.status_path(token)}"

    @property
    def admin_enabled(self) -> bool:
        return bool(self.admin_key)

    @classmethod
    def from_environment(cls) -> "WebConfig":
        base_url = os.getenv("CREWMEAL_WEB_BASE_URL", DEFAULT_BASE_URL).strip()
        if not base_url:
            base_url = DEFAULT_BASE_URL

        session_secret = os.getenv("CREWMEAL_WEB_SESSION_SECRET", "").strip()
        require_auth = _bool_environment("CREWMEAL_INGEST_REQUIRE_AUTH", True)
        if not session_secret:
            if require_auth:
                # A missing secret in a hardened deployment silently weakens the
                # admin session cookie; fail loudly instead.
                raise ConfigurationError(
                    "CREWMEAL_WEB_SESSION_SECRET must be set when "
                    "CREWMEAL_INGEST_REQUIRE_AUTH is enabled."
                )
            session_secret = "dev-insecure-session-secret"

        return cls(
            base_url=base_url,
            admin_key=(os.getenv("CREWMEAL_ADMIN_KEY", "").strip() or None),
            session_secret=session_secret,
            ingest_tenant_id=(
                os.getenv("CREWMEAL_INGEST_TENANT_ID", "").strip()
                or os.getenv("CREWMEAL_M365_TENANT_ID", "").strip()
                or None
            ),
            ingest_audience=(os.getenv("CREWMEAL_INGEST_AUDIENCE", "").strip() or None),
            ingest_allowed_app_ids=_split_csv(
                os.getenv("CREWMEAL_INGEST_ALLOWED_APP_IDS", "")
            ),
            ingest_allowed_origins=_split_csv(
                os.getenv("CREWMEAL_INGEST_ALLOWED_ORIGINS", "")
            ),
            require_ingest_auth=require_auth,
        )

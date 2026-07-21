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
    #: When true, the user-facing status page (``/s/*``) requires an interactive
    #: Entra ID sign-in. Defaults to False so tests/local dev keep the low-friction
    #: token-only behaviour; :meth:`from_environment` turns it on in production.
    status_require_auth: bool = False
    #: Entra tenant that issues the status-page sign-in (defaults to the M365
    #: tenant). Any member of this tenant may view a status link they possess.
    sso_tenant_id: str | None = None
    #: Confidential-client app id for the status-page sign-in (reuses the M365 app).
    sso_client_id: str | None = None
    #: Client secret for the status-page sign-in (reuses the M365 app secret).
    sso_client_secret: str | None = field(default=None, repr=False)
    #: Mark the session cookie ``Secure`` (HTTPS-only). Defaults to False so tests
    #: and local http dev keep working; :meth:`from_environment` enables it when the
    #: public base URL is HTTPS.
    secure_cookies: bool = False

    def status_path(self, token: str) -> str:
        return f"/s/{token}"

    def status_url(self, token: str) -> str:
        return f"{self.base_url.rstrip('/')}{self.status_path(token)}"

    @property
    def admin_enabled(self) -> bool:
        return bool(self.admin_key)

    @property
    def sso_enabled(self) -> bool:
        """True when the status-page sign-in is fully configured."""

        return bool(
            self.sso_tenant_id and self.sso_client_id and self.sso_client_secret
        )

    @property
    def sso_authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.sso_tenant_id}"

    @property
    def sso_redirect_uri(self) -> str:
        return f"{self.base_url.rstrip('/')}/auth/callback"

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

        # The status-page sign-in reuses the M365 app registration by default; the
        # CREWMEAL_STATUS_SSO_* overrides allow a dedicated app without code changes.
        status_require_auth = _bool_environment(
            "CREWMEAL_STATUS_REQUIRE_AUTH", require_auth
        )
        sso_tenant_id = (
            os.getenv("CREWMEAL_STATUS_SSO_TENANT_ID", "").strip()
            or os.getenv("CREWMEAL_M365_TENANT_ID", "").strip()
            or None
        )
        sso_client_id = (
            os.getenv("CREWMEAL_STATUS_SSO_CLIENT_ID", "").strip()
            or os.getenv("CREWMEAL_M365_CLIENT_ID", "").strip()
            or None
        )
        sso_client_secret = (
            os.getenv("CREWMEAL_STATUS_SSO_CLIENT_SECRET", "").strip()
            or os.getenv("CREWMEAL_M365_CLIENT_SECRET", "").strip()
            or None
        )
        if status_require_auth and not (
            sso_tenant_id and sso_client_id and sso_client_secret
        ):
            # Enforcing sign-in without a configured app would lock every user out;
            # fail loudly at startup instead of per request.
            raise ConfigurationError(
                "Status-page sign-in requires CREWMEAL_M365_TENANT_ID, "
                "CREWMEAL_M365_CLIENT_ID and CREWMEAL_M365_CLIENT_SECRET (or the "
                "CREWMEAL_STATUS_SSO_* overrides) when CREWMEAL_STATUS_REQUIRE_AUTH "
                "is enabled."
            )

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
            status_require_auth=status_require_auth,
            sso_tenant_id=sso_tenant_id,
            sso_client_id=sso_client_id,
            sso_client_secret=sso_client_secret,
            secure_cookies=base_url.lower().startswith("https"),
        )

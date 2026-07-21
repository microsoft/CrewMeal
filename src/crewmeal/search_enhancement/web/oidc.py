"""Entra ID sign-in for the user-facing status page.

The status link is intentionally low-friction -- possession of the opaque token is
a credential -- but leaving it fully anonymous lets anyone with a leaked link view
slide content and trigger destructive actions (rerun / remove-from-index). This
module adds an interactive Entra ID sign-in (OpenID Connect authorization-code
flow) so only members of the tenant may open a status link.

Authorization stays coarse by decision: any signed-in tenant user *plus* a valid
token is allowed; we do not check per-document SharePoint permissions.

MSAL handles the security-sensitive parts (``state``, ``nonce``, PKCE, and
ID-token validation against the tenant JWKS), so this module only wires it to the
web config and the request/session lifecycle.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import msal

from crewmeal.search_enhancement.web.config import WebConfig

# Empty scope list: we only need to authenticate the user. MSAL implicitly adds
# the reserved OIDC scopes (``openid``/``profile``/``offline_access``), so no
# Microsoft Graph permission or admin consent is required for sign-in.
_SCOPES: list[str] = []

#: Session keys used to carry the in-flight flow and the signed-in identity.
FLOW_SESSION_KEY = "sso_flow"
USER_SESSION_KEY = "sso_user"


class SsoError(Exception):
    """Raised when the Entra sign-in flow cannot be completed."""


@dataclass(frozen=True, slots=True)
class SignedInUser:
    """The minimal identity we persist in the signed session cookie."""

    subject: str
    name: str | None
    username: str | None
    expires_at: int

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at

    def to_session(self) -> dict[str, Any]:
        return {
            "sub": self.subject,
            "name": self.name,
            "username": self.username,
            "exp": self.expires_at,
        }

    @classmethod
    def from_session(cls, data: Any) -> "SignedInUser | None":
        if not isinstance(data, dict):
            return None
        subject = data.get("sub")
        expires_at = data.get("exp")
        if not isinstance(subject, str) or not subject:
            return None
        if not isinstance(expires_at, int):
            return None
        name = data.get("name")
        username = data.get("username")
        return cls(
            subject=subject,
            name=name if isinstance(name, str) else None,
            username=username if isinstance(username, str) else None,
            expires_at=expires_at,
        )


def build_msal_app(config: WebConfig) -> msal.ConfidentialClientApplication:
    """Build a confidential-client app bound to the tenant and the M365 app."""

    return msal.ConfidentialClientApplication(
        client_id=config.sso_client_id,
        client_credential=config.sso_client_secret,
        authority=config.sso_authority,
    )


def begin_login(config: WebConfig) -> dict[str, Any]:
    """Start an auth-code flow.

    Returns the MSAL flow dict; the caller stores it in the session and redirects
    the browser to ``flow["auth_uri"]``.
    """

    app = build_msal_app(config)
    return app.initiate_auth_code_flow(
        scopes=_SCOPES,
        redirect_uri=config.sso_redirect_uri,
    )


def complete_login(
    config: WebConfig, flow: dict[str, Any], params: dict[str, Any]
) -> SignedInUser:
    """Exchange the callback query params against the stored flow.

    MSAL validates ``state``/``nonce`` and the ID-token signature internally.
    Raises :class:`SsoError` when the exchange fails or Entra returns an error.
    """

    app = build_msal_app(config)
    result = app.acquire_token_by_auth_code_flow(flow, params)
    if "error" in result:
        detail = (
            result.get("error_description")
            or result.get("error")
            or "sign-in failed"
        )
        raise SsoError(str(detail))
    claims = result.get("id_token_claims") or {}
    subject = claims.get("oid") or claims.get("sub")
    if not subject:
        raise SsoError("The sign-in response did not identify the user.")
    return SignedInUser(
        subject=str(subject),
        name=claims.get("name"),
        username=claims.get("preferred_username"),
        expires_at=int(claims.get("exp") or (time.time() + 3600)),
    )

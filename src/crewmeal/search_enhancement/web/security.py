"""Access gates for the web surfaces.

The status page requires an interactive Entra ID sign-in when
``status_require_auth`` is enabled: a signed-in tenant user *and* possession of
the opaque token in the URL are both required (a PoC decision -- we do not check
per-document SharePoint permissions). When the toggle is off (local dev / tests)
the token alone is sufficient, preserving the original low-friction behaviour. The
admin portal is gated separately by an opaque admin key that may be presented as
an ``X-Admin-Key`` header, a ``?key=`` query parameter, or a signed session cookie
set after logging in once.
"""

from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request, status

from crewmeal.search_enhancement.database import (
    DocumentRecord,
    SearchEnhancementRepository,
)
from crewmeal.search_enhancement.web.config import WebConfig
from crewmeal.search_enhancement.web.dependencies import (
    get_repository,
    get_web_config,
)
from crewmeal.search_enhancement.web.oidc import USER_SESSION_KEY, SignedInUser

ADMIN_SESSION_KEY = "admin_key"


class LoginRequired(Exception):
    """Raised by :func:`require_user` when a status request lacks a valid sign-in.

    Carries the local ``next`` path so the exception handler can send the user to
    the sign-in flow and return them afterwards.
    """

    def __init__(self, next_url: str) -> None:
        super().__init__("Sign-in required.")
        self.next_url = next_url


def _current_next(request: Request) -> str:
    path = request.url.path
    query = request.url.query
    return f"{path}?{query}" if query else path


def signed_in_user(request: Request) -> SignedInUser | None:
    """Return the non-expired signed-in user from the session, if any."""

    session = request.scope.get("session")
    if not isinstance(session, dict):
        return None
    user = SignedInUser.from_session(session.get(USER_SESSION_KEY))
    if user is None or user.is_expired:
        return None
    return user


def require_user(
    request: Request,
    config: WebConfig = Depends(get_web_config),
) -> None:
    """FastAPI dependency enforcing an Entra sign-in on the status surface.

    A no-op when ``status_require_auth`` is disabled so local dev and tests keep
    the token-only behaviour.
    """

    if not config.status_require_auth:
        return
    if signed_in_user(request) is None:
        raise LoginRequired(_current_next(request))


def presented_admin_key(request: Request) -> str | None:
    header = request.headers.get("x-admin-key")
    if header:
        return header
    session = request.scope.get("session")
    if isinstance(session, dict):
        stored = session.get(ADMIN_SESSION_KEY)
        if isinstance(stored, str) and stored:
            return stored
    query = request.query_params.get("key")
    return query or None


def verify_admin_key(config: WebConfig, presented: str | None) -> bool:
    if not config.admin_key or not presented:
        return False
    return hmac.compare_digest(presented, config.admin_key)


def require_admin(
    request: Request,
    config: WebConfig = Depends(get_web_config),
) -> None:
    """FastAPI dependency that enforces admin access."""

    if not config.admin_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The admin portal is not configured.",
        )
    if not verify_admin_key(config, presented_admin_key(request)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid admin key is required.",
        )


def get_document_for_token(
    token: str,
    repository: SearchEnhancementRepository = Depends(get_repository),
) -> DocumentRecord:
    """Resolve a status token to a document or raise 404."""

    document = repository.get_document_by_token(token)
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Unknown or expired status link.",
        )
    return document

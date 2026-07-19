"""Access gates for the web surfaces.

The status page is intentionally low-friction: possession of the opaque token in
the URL is the only credential (a PoC decision). The admin portal is gated by an
opaque admin key that may be presented as an ``X-Admin-Key`` header, a ``?key=``
query parameter, or a signed session cookie set after logging in once.
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

ADMIN_SESSION_KEY = "admin_key"


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

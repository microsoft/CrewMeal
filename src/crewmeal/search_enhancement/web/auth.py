"""Entra ID token validation for the ingest API.

The SharePoint SPFx command calls ``POST /api/requests`` with an Entra token
obtained via ``AadHttpClient`` (audience = our API app). We validate that token's
signature (against the tenant JWKS), issuer, audience, and — optionally — the
calling application id. In local development the check can be disabled via
``CREWMEAL_INGEST_REQUIRE_AUTH=false``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import jwt
from jwt import PyJWKClient

from crewmeal.config import ConfigurationError
from crewmeal.search_enhancement.web.config import WebConfig


class IngestAuthError(Exception):
    """Raised when an ingest token cannot be validated."""


@dataclass(frozen=True, slots=True)
class IngestPrincipal:
    subject: str | None = None
    app_id: str | None = None
    name: str | None = None
    claims: dict[str, Any] = field(default_factory=dict)


class TokenValidator(Protocol):
    def validate(self, token: str) -> IngestPrincipal: ...


class NullTokenValidator:
    """Accepts any request. Used only when ingest auth is disabled."""

    requires_bearer = False

    def validate(self, token: str) -> IngestPrincipal:  # noqa: ARG002
        return IngestPrincipal(name="anonymous")


class EntraTokenValidator:
    """Validates Entra ID v2 access tokens issued for our API app."""

    requires_bearer = True

    def __init__(
        self,
        *,
        tenant_id: str,
        audiences: Sequence[str],
        allowed_app_ids: tuple[str, ...] = (),
    ) -> None:
        # Accept multiple ``aud`` values so the API works whether Entra issues v2
        # tokens (aud = our app's client-id GUID) or v1 tokens (aud = App ID URI).
        self._audiences = [a for a in audiences if a]
        if not self._audiences:
            raise ConfigurationError("At least one ingest audience is required.")
        self._allowed = set(allowed_app_ids)
        self._issuers = {
            f"https://login.microsoftonline.com/{tenant_id}/v2.0",
            f"https://sts.windows.net/{tenant_id}/",
        }
        self._jwk_client = PyJWKClient(
            f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
        )

    def validate(self, token: str) -> IngestPrincipal:
        try:
            signing_key = self._jwk_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._audiences,
                options={"require": ["exp", "iat"]},
            )
        except jwt.PyJWTError as exc:
            raise IngestAuthError(f"Invalid ingest token: {exc}") from exc

        if claims.get("iss") not in self._issuers:
            raise IngestAuthError("Untrusted token issuer.")

        app_id = claims.get("azp") or claims.get("appid")
        if self._allowed and app_id not in self._allowed:
            raise IngestAuthError("Calling application is not allowed.")

        return IngestPrincipal(
            subject=claims.get("sub"),
            app_id=app_id,
            name=claims.get("name") or claims.get("preferred_username"),
            claims=claims,
        )


def create_token_validator(config: WebConfig) -> TokenValidator:
    if not config.require_ingest_auth:
        return NullTokenValidator()
    audiences = tuple(
        part.strip()
        for part in (config.ingest_audience or "").split(",")
        if part.strip()
    )
    if not config.ingest_tenant_id or not audiences:
        raise ConfigurationError(
            "Ingest authentication requires CREWMEAL_INGEST_AUDIENCE and a tenant "
            "id (CREWMEAL_INGEST_TENANT_ID or CREWMEAL_M365_TENANT_ID)."
        )
    return EntraTokenValidator(
        tenant_id=config.ingest_tenant_id,
        audiences=audiences,
        allowed_app_ids=config.ingest_allowed_app_ids,
    )

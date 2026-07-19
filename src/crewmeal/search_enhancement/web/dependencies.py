"""Shared FastAPI dependencies.

The app stores its collaborators (repository, artifact store, config, templates)
on ``app.state`` so tests can inject fakes via :func:`create_app`. These thin
providers surface them to route handlers through the dependency-injection system.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status
from fastapi.templating import Jinja2Templates

from crewmeal.search_enhancement.artifact_store import ArtifactStore
from crewmeal.search_enhancement.config import SearchEnhancementConfig
from crewmeal.search_enhancement.database import SearchEnhancementRepository
from crewmeal.search_enhancement.web.auth import (
    TokenValidator,
    create_token_validator,
)
from crewmeal.search_enhancement.web.config import WebConfig


def get_repository(request: Request) -> SearchEnhancementRepository:
    return request.app.state.repository


def get_artifact_store(request: Request) -> ArtifactStore:
    return request.app.state.artifact_store


def get_web_config(request: Request) -> WebConfig:
    return request.app.state.web_config


def get_templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def get_search_config(request: Request) -> SearchEnhancementConfig:
    config = getattr(request.app.state, "search_config", None)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The ingest API is not configured (missing SharePoint settings).",
        )
    return config


def get_ingest_validator(request: Request) -> TokenValidator:
    validator = getattr(request.app.state, "ingest_validator", None)
    if validator is None:
        validator = create_token_validator(request.app.state.web_config)
        request.app.state.ingest_validator = validator
    return validator


def get_control_client(request: Request) -> object | None:
    """Return the optional SharePoint control client used to resolve the Graph
    ``driveItem`` from a list item id. ``None`` when SharePoint is not configured
    (local dev / tests), in which case the ingest API falls back to client hints.
    """

    return getattr(request.app.state, "control_client", None)

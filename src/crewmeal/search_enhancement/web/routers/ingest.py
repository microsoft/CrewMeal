"""Ingest API: SPFx command -> server DB queue.

``POST /api/requests`` replaces the old approach of writing SharePoint list
columns directly. The SPFx command presents an Entra token, the server upserts
the document and enqueues a job, and returns an opaque ``statusUrl`` that SPFx
writes into the SharePoint hyperlink column so the user can follow progress.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status

from crewmeal.search_enhancement.config import SearchEnhancementConfig
from crewmeal.search_enhancement.database import (
    DocumentKey,
    SearchEnhancementRepository,
)
from crewmeal.search_enhancement.web.auth import (
    IngestAuthError,
    IngestPrincipal,
    TokenValidator,
)
from crewmeal.search_enhancement.web.config import WebConfig
from crewmeal.search_enhancement.web.dependencies import (
    get_control_client,
    get_ingest_validator,
    get_repository,
    get_search_config,
    get_web_config,
)
from crewmeal.search_enhancement.web.schemas import (
    IngestItem,
    IngestRequest,
    IngestResponse,
)

LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["ingest"])


def _resolve_drive_item(
    control: object | None, item: IngestItem
) -> tuple[str, str, str]:
    """Resolve ``(driveItemId, fileName, webUrl)`` for a list item.

    When a SharePoint control client is available the Graph ``driveItem`` is the
    source of truth (matching the worker's reconcile key). Otherwise the caller's
    hints are used (local dev / upload playground / tests).
    """

    drive_item_id = (item.drive_item_id or "").strip()
    file_name = (item.file_name or "").strip()
    web_url = (item.web_url or "").strip()

    if control is not None:
        try:
            resolved = control.get_drive_item_by_list_item(item.list_item_id)
        except Exception as exc:  # noqa: BLE001 - surfaced as a gateway error
            LOGGER.warning(
                "Failed to resolve driveItem for list item %s: %s",
                item.list_item_id,
                exc,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to resolve the SharePoint item from Graph.",
            ) from exc
        drive_item_id = str(resolved.get("id") or "").strip() or drive_item_id
        file_name = str(resolved.get("name") or "").strip() or file_name
        web_url = str(resolved.get("webUrl") or "").strip() or web_url

    if not drive_item_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="driveItemId could not be resolved for the request.",
        )
    return drive_item_id, file_name, web_url


def require_ingest_principal(
    request: Request,
    validator: TokenValidator = Depends(get_ingest_validator),
) -> IngestPrincipal:
    if not getattr(validator, "requires_bearer", True):
        return validator.validate("")
    authorization = request.headers.get("authorization")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A bearer token is required.",
        )
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A bearer token is required.",
        )
    try:
        return validator.validate(token)
    except IngestAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc


@router.post(
    "/requests",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Enqueue an enhancement or removal request",
)
def create_request(
    payload: IngestRequest,
    principal: IngestPrincipal = Depends(require_ingest_principal),
    repository: SearchEnhancementRepository = Depends(get_repository),
    search_config: SearchEnhancementConfig = Depends(get_search_config),
    web_config: WebConfig = Depends(get_web_config),
    control: object | None = Depends(get_control_client),
) -> IngestResponse:
    desired_enabled = payload.command == "Enhance"
    drive_item_id, file_name, web_url = _resolve_drive_item(control, payload.item)
    key = DocumentKey(
        tenant_id=search_config.tenant_id,
        site_id=search_config.site_id,
        drive_id=search_config.drive_id,
        item_id=drive_item_id,
    )
    job_type = "upsert" if desired_enabled else "delete"
    repository.upsert_document_and_enqueue(
        key=key,
        list_id=search_config.list_id,
        list_item_id=payload.item.list_item_id,
        web_url=web_url,
        file_name=file_name,
        connection_id=search_config.connection_id,
        desired_enabled=desired_enabled,
        status="Queued",
        request_id=payload.request_id,
        job_type=job_type,
        trigger="spfx",
    )
    document = repository.get_document(key)
    if document is None or not document.status_token:  # pragma: no cover - defensive
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to persist the request.",
        )
    LOGGER.info(
        "Ingested %s request %s for %s (caller=%s)",
        payload.command,
        payload.request_id,
        file_name or payload.item.list_item_id,
        principal.app_id or principal.name,
    )
    return IngestResponse(
        request_id=payload.request_id,
        status_token=document.status_token,
        status_url=web_config.status_url(document.status_token),
        status=document.status,
        job_type=job_type,
    )

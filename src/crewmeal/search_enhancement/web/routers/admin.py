"""Admin portal: dashboard, document/job management, settings, feedback corpus,
and the upload playground (tryout).

All routes except login/logout require the admin key (header, ``?key=``, or a
signed session cookie set after logging in once).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from crewmeal.search_enhancement.artifact_store import ArtifactStore, artifact_path
from crewmeal.config import AppConfig
from crewmeal.search_enhancement.formats import (
    all_handlers,
    enabled_extensions,
    format_setting_key,
    format_status,
)
from crewmeal.search_enhancement.vision_model import (
    VISION_SETTING_KEYS,
    vision_model_fields,
)
from crewmeal.search_enhancement.decryption import (
    all_providers as all_decryption_providers,
    decryption_setting_key,
    decryption_status,
)
from crewmeal.search_enhancement.database import (
    DocumentRecord,
    FeedbackRecord,
    SearchEnhancementRepository,
)
from crewmeal.search_enhancement.web.config import WebConfig
from crewmeal.search_enhancement.web.dependencies import (
    get_artifact_store,
    get_repository,
    get_templates,
    get_web_config,
)
from crewmeal.search_enhancement.web.security import (
    presented_admin_key,
    require_admin,
    verify_admin_key,
)
from crewmeal.search_enhancement.pricing import estimate_cost
from crewmeal.search_enhancement.web.viewmodels import build_status_view

# Public sub-router (login/logout must be reachable without the gate).
login_router = APIRouter(prefix="/admin", tags=["admin"])
# Everything else is gated at the router level.
router = APIRouter(
    prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)]
)


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
@login_router.get("/login", response_class=HTMLResponse, response_model=None)
def admin_login_form(
    request: Request,
    config: WebConfig = Depends(get_web_config),
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse | RedirectResponse:
    if not config.admin_enabled:
        raise HTTPException(status_code=503, detail="Admin portal is not configured.")
    if verify_admin_key(config, presented_admin_key(request)):
        return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, "admin/login.html", {"error": None})


@login_router.post("/login", response_model=None)
def admin_login(
    request: Request,
    key: str = Form(...),
    config: WebConfig = Depends(get_web_config),
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse | RedirectResponse:
    if not config.admin_enabled:
        raise HTTPException(status_code=503, detail="Admin portal is not configured.")
    if verify_admin_key(config, key):
        request.session["admin_key"] = key
        return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request,
        "admin/login.html",
        {"error": "관리자 키가 올바르지 않습니다."},
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


@login_router.get("/logout")
def admin_logout(request: Request) -> RedirectResponse:
    request.session.pop("admin_key", None)
    return RedirectResponse("/admin/login", status_code=status.HTTP_303_SEE_OTHER)


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def admin_dashboard(
    request: Request,
    repository: SearchEnhancementRepository = Depends(get_repository),
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse:
    context = {
        "document_counts": repository.document_status_counts(),
        "job_counts": repository.job_status_counts(),
        "document_total": repository.count_documents(),
        "feedback_count": repository.count_feedback_records(),
        "recent_jobs": repository.list_recent_jobs(limit=12),
        "total_cost": estimate_cost(repository.job_usages()),
    }
    return templates.TemplateResponse(request, "admin/dashboard.html", context)


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #
@router.get("/documents", response_class=HTMLResponse)
def admin_documents(
    request: Request,
    repository: SearchEnhancementRepository = Depends(get_repository),
    templates: Jinja2Templates = Depends(get_templates),
    source_kind: str | None = None,
    status_filter: str | None = None,
    page: int = 1,
) -> HTMLResponse:
    page = max(page, 1)
    page_size = 25
    documents = repository.list_documents(
        source_kind=source_kind or None,
        status=status_filter or None,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    total = repository.count_documents(
        source_kind=source_kind or None, status=status_filter or None
    )
    context = {
        "documents": documents,
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_next": page * page_size < total,
        "source_kind": source_kind or "",
        "status_filter": status_filter or "",
    }
    return templates.TemplateResponse(request, "admin/documents.html", context)


def _document_or_404(
    token: str, repository: SearchEnhancementRepository
) -> DocumentRecord:
    document = repository.get_document_by_token(token)
    if document is None:
        raise HTTPException(status_code=404, detail="Unknown document.")
    return document


@router.get("/documents/{token}", response_class=HTMLResponse)
def admin_document_detail(
    request: Request,
    token: str,
    repository: SearchEnhancementRepository = Depends(get_repository),
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse:
    document = _document_or_404(token, repository)
    context = build_status_view(repository, document)
    context["token"] = token
    context["artifacts"] = repository.list_artifacts(document.key)
    return templates.TemplateResponse(request, "admin/document_detail.html", context)


@router.post("/documents/{token}/rerun")
def admin_document_rerun(
    token: str,
    repository: SearchEnhancementRepository = Depends(get_repository),
) -> RedirectResponse:
    document = _document_or_404(token, repository)
    repository.queue_refresh(document.key, trigger="admin")
    return RedirectResponse(
        f"/admin/documents/{token}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/documents/{token}/remove")
def admin_document_remove(
    token: str,
    repository: SearchEnhancementRepository = Depends(get_repository),
) -> RedirectResponse:
    document = _document_or_404(token, repository)
    if document.source_kind == "sharepoint":
        repository.queue_removal(document.key, trigger="admin")
    return RedirectResponse(
        f"/admin/documents/{token}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/jobs/{job_id}/retry")
def admin_job_retry(
    job_id: str,
    repository: SearchEnhancementRepository = Depends(get_repository),
) -> RedirectResponse:
    detail = repository.get_job_detail(job_id)
    repository.retry_job(job_id)
    token = detail.get("status_token") if detail else None
    target = f"/admin/documents/{token}" if token else "/admin"
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
@router.get("/settings", response_class=HTMLResponse)
def admin_settings(
    request: Request,
    repository: SearchEnhancementRepository = Depends(get_repository),
    templates: Jinja2Templates = Depends(get_templates),
    saved: bool = False,
) -> HTMLResponse:
    all_settings = repository.get_all_settings()
    context = {
        "settings": all_settings,
        "formats": format_status(all_settings),
        "vision_fields": vision_model_fields(
            AppConfig.from_environment(), all_settings
        ),
        "decryption": decryption_status(all_settings),
        "saved": saved,
    }
    return templates.TemplateResponse(request, "admin/settings.html", context)


@router.post("/settings/decryption")
def admin_settings_decryption_save(
    request: Request,
    enabled: list[str] = Form(default=[]),
    repository: SearchEnhancementRepository = Depends(get_repository),
) -> RedirectResponse:
    """Persist per-provider decryption on/off toggles.

    Providers are off by default; checked boxes arrive in ``enabled``. Enabling
    a provider that is not implemented yet is allowed (so operators can stage
    the rollout) but will fail loudly at processing time.
    """

    checked = set(enabled)
    for provider in all_decryption_providers():
        repository.set_setting(
            decryption_setting_key(provider.provider_id),
            provider.provider_id in checked,
        )
    return RedirectResponse(
        "/admin/settings?saved=1", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/settings/vision")
async def admin_settings_vision_save(
    request: Request,
    repository: SearchEnhancementRepository = Depends(get_repository),
) -> RedirectResponse:
    """Persist the swappable vision-model configuration.

    Each field maps to a ``vision.*`` setting key. Blank text fields are stored
    as empty strings, which the resolver treats as "fall back to the
    environment default".
    """

    form = await request.form()
    for key in VISION_SETTING_KEYS:
        if key in form:
            repository.set_setting(key, str(form[key]).strip())
    return RedirectResponse(
        "/admin/settings?saved=1", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/settings/formats")
def admin_settings_formats_save(
    request: Request,
    enabled: list[str] = Form(default=[]),
    repository: SearchEnhancementRepository = Depends(get_repository),
) -> RedirectResponse:
    """Persist per-format on/off toggles from the settings page.

    Only implemented (``supported``) formats are toggleable; skeleton formats
    are ignored even if posted. Checked boxes arrive in ``enabled``.
    """

    checked = set(enabled)
    for handler in all_handlers():
        if not handler.supported:
            continue
        repository.set_setting(
            format_setting_key(handler.format_id),
            handler.format_id in checked,
        )
    return RedirectResponse(
        "/admin/settings?saved=1", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/settings")
def admin_settings_save(
    key: str = Form(...),
    value: str = Form(""),
    repository: SearchEnhancementRepository = Depends(get_repository),
) -> RedirectResponse:
    parsed: Any = value.strip()
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        parsed = value.strip()
    if key.strip():
        repository.set_setting(key.strip(), parsed)
    return RedirectResponse(
        "/admin/settings?saved=1", status_code=status.HTTP_303_SEE_OTHER
    )


# --------------------------------------------------------------------------- #
# Feedback corpus (P6b)
# --------------------------------------------------------------------------- #
def _feedback_to_dict(record: FeedbackRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "tenantId": record.document_key.tenant_id,
        "siteId": record.document_key.site_id,
        "driveId": record.document_key.drive_id,
        "itemId": record.document_key.item_id,
        "enhancementVersion": record.enhancement_version,
        "sourceArtifactPath": record.source_artifact_path,
        "beforeHtmlPath": record.before_html_path,
        "beforeJsonPath": record.before_json_path,
        "correctionText": record.correction_text,
        "afterHtmlPath": record.after_html_path,
        "afterJsonPath": record.after_json_path,
        "category": record.category,
        "tags": record.tags,
        "model": record.model,
        "deployment": record.deployment,
        "createdBy": record.created_by,
        "createdAt": record.created_at,
    }


@router.get("/feedback", response_class=HTMLResponse)
def admin_feedback(
    request: Request,
    repository: SearchEnhancementRepository = Depends(get_repository),
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse:
    context = {
        "records": repository.list_feedback_records(limit=200),
        "total": repository.count_feedback_records(),
    }
    return templates.TemplateResponse(request, "admin/feedback.html", context)


@router.get("/feedback/export.jsonl")
def admin_feedback_export(
    repository: SearchEnhancementRepository = Depends(get_repository),
) -> StreamingResponse:
    def _stream() -> Any:
        for record in repository.iter_feedback_records():
            yield json.dumps(_feedback_to_dict(record), ensure_ascii=False) + "\n"

    return StreamingResponse(
        _stream(),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": 'attachment; filename="feedback-corpus.jsonl"'
        },
    )


# --------------------------------------------------------------------------- #
# Upload playground / tryout (P7b)
# --------------------------------------------------------------------------- #
@router.get("/tryout", response_class=HTMLResponse)
def admin_tryout_form(
    request: Request,
    repository: SearchEnhancementRepository = Depends(get_repository),
    templates: Jinja2Templates = Depends(get_templates),
    error: str | None = None,
) -> HTMLResponse:
    accept = ",".join(sorted(enabled_extensions(repository.get_all_settings())))
    return templates.TemplateResponse(
        request,
        "admin/tryout.html",
        {
            "error": error,
            "accept_extensions": accept,
        },
    )


@router.post("/tryout")
async def admin_tryout_submit(
    request: Request,
    file: UploadFile = File(...),
    comment: str = Form(""),
    repository: SearchEnhancementRepository = Depends(get_repository),
    artifacts: ArtifactStore = Depends(get_artifact_store),
) -> RedirectResponse:
    filename = file.filename or "upload.pptx"
    if Path(filename).suffix.lower() not in enabled_extensions(
        repository.get_all_settings()
    ):
        return RedirectResponse(
            "/admin/tryout?error=지원하지+않거나+비활성화된+문서+형식입니다.",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    data = await file.read()
    search_config = getattr(request.app.state, "search_config", None)
    connection_id = getattr(search_config, "connection_id", None) or "tryout"

    document = repository.create_upload_document(
        file_name=filename, connection_id=connection_id, created_by="admin"
    )
    stored = artifacts.put_bytes(
        artifact_path(
            document.key, version=0, kind="source_pptx", filename="source.pptx"
        ),
        data,
        content_type=(
            "application/vnd.openxmlformats-officedocument."
            "presentationml.presentation"
        ),
    )
    repository.record_artifact(
        document.key,
        kind="source_pptx",
        blob_path=stored.path,
        content_type=stored.content_type,
        byte_count=stored.byte_count,
        enhancement_version=0,
    )
    note = comment.strip()
    if note:
        repository.queue_refresh(
            document.key, trigger="admin", feedback=note, note_author="admin"
        )
    else:
        repository.enqueue_job(
            key=document.key,
            request_id=document.request_id,
            job_type="upsert",
            trigger="admin",
        )
    return RedirectResponse(
        f"/s/{document.status_token}", status_code=status.HTTP_303_SEE_OTHER
    )

"""User-facing status page reachable via an opaque token link.

Requirements covered here:
* stage-by-stage progress timeline (polled) for a long-running job,
* a sandboxed preview of the extracted HTML,
* three actions: rerun, rerun-with-comment (tuning), and remove-from-index.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from crewmeal.search_enhancement.artifact_store import ArtifactStore
from crewmeal.search_enhancement.database import (
    DocumentRecord,
    SearchEnhancementRepository,
)
from crewmeal.search_enhancement.web.dependencies import (
    get_artifact_store,
    get_repository,
    get_templates,
)
from crewmeal.search_enhancement.web.security import (
    get_document_for_token,
    require_user,
)
from crewmeal.search_enhancement.web.viewmodels import build_status_view, run_in_flight

# Every status surface (view, polled partials, and the destructive actions) is
# gated by ``require_user``; it is a no-op unless status-page sign-in is enabled.
router = APIRouter(
    prefix="/s", tags=["status"], dependencies=[Depends(require_user)]
)

# The extracted-HTML preview is untrusted content rendered in a sandboxed iframe;
# lock it down so it cannot script, navigate, or exfiltrate.
_PREVIEW_CSP = (
    "default-src 'none'; img-src data: https:; style-src 'unsafe-inline'; "
    "font-src data: https:"
)

# The extracted HTML is an attribute-free fragment (the renderer allowlist
# forbids class/style), so table borders and spacing can only be supplied at
# preview time. This shell is inlined; `style-src 'unsafe-inline'` permits it.
_PREVIEW_STYLE = """
  :root { color-scheme: light dark; }
  body { font-family: "Segoe UI", system-ui, -apple-system, sans-serif;
         line-height: 1.55; color: #1b1b1f; margin: 1rem 1.1rem; }
  h1 { font-size: 1.35rem; } h2 { font-size: 1.12rem; margin-top: 1.4rem; }
  h3 { font-size: 1rem; color: #0b5cab; margin-bottom: .3rem; }
  table { border-collapse: collapse; width: 100%; margin: .55rem 0 1.1rem;
          font-size: .92rem; }
  caption { text-align: left; font-weight: 600; margin-bottom: .35rem; }
  th, td { border: 1px solid #c7c9d1; padding: .4rem .55rem; text-align: left;
           vertical-align: top; }
  thead th { background: #eef2f8; }
  tbody tr:nth-child(even) td { background: #fafbfc; }
  ul, ol { margin: .4rem 0 .9rem 1.2rem; padding: 0; }
  section { padding-top: .4rem; }
  @media (prefers-color-scheme: dark) {
    body { color: #e7e7ea; }
    h3 { color: #5aa0ea; }
    th, td { border-color: #3a3c44; }
    thead th { background: #2a2c33; }
    tbody tr:nth-child(even) td { background: #212228; }
  }
"""


def _wrap_preview_html(inner: str) -> str:
    return (
        "<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<style>{_PREVIEW_STYLE}</style></head><body>{inner}</body></html>"
    )


def _redirect_to_status(token: str) -> RedirectResponse:
    return RedirectResponse(
        url=f"/s/{token}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/{token}", response_class=HTMLResponse, summary="Status page")
def status_page(
    request: Request,
    document: DocumentRecord = Depends(get_document_for_token),
    repository: SearchEnhancementRepository = Depends(get_repository),
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse:
    context = build_status_view(repository, document)
    context["token"] = document.status_token
    return templates.TemplateResponse(request, "status.html", context)


@router.get(
    "/{token}/progress",
    response_class=HTMLResponse,
    summary="Progress timeline partial (polled)",
)
def status_progress(
    request: Request,
    document: DocumentRecord = Depends(get_document_for_token),
    repository: SearchEnhancementRepository = Depends(get_repository),
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse:
    context = build_status_view(repository, document)
    context["token"] = document.status_token
    return templates.TemplateResponse(request, "_timeline.html", context)


@router.get(
    "/{token}/html",
    response_class=HTMLResponse,
    summary="Extracted HTML preview (sandboxed)",
)
def status_html(
    document: DocumentRecord = Depends(get_document_for_token),
    repository: SearchEnhancementRepository = Depends(get_repository),
    artifacts: ArtifactStore = Depends(get_artifact_store),
) -> HTMLResponse:
    artifact = repository.get_latest_artifact(document.key, "html")
    if run_in_flight(repository, document.key):
        # A run is in flight; any existing artifact is from an earlier run, so
        # never serve the stale preview (mirrors the status page).
        inner = (
            "<p style='color:#6b6c73'>재작업이 진행 중입니다. "
            "완료되면 갱신된 미리보기가 표시됩니다.</p>"
        )
    elif artifact is None:
        inner = (
            "<p style='color:#6b6c73'>아직 추출된 HTML이 없습니다. "
            "작업이 완료되면 이곳에 표시됩니다.</p>"
        )
    else:
        inner = artifacts.get_bytes(artifact.blob_path).decode("utf-8", "replace")
    return HTMLResponse(
        content=_wrap_preview_html(inner),
        headers={
            "Content-Security-Policy": _PREVIEW_CSP,
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@router.post("/{token}/rerun", summary="Re-run enhancement")
def status_rerun(
    document: DocumentRecord = Depends(get_document_for_token),
    repository: SearchEnhancementRepository = Depends(get_repository),
) -> RedirectResponse:
    repository.queue_refresh(document.key, trigger="user")
    return _redirect_to_status(document.status_token or "")


@router.post("/{token}/comment", summary="Re-run with a tuning comment")
def status_comment(
    comment: str = Form(...),
    document: DocumentRecord = Depends(get_document_for_token),
    repository: SearchEnhancementRepository = Depends(get_repository),
) -> RedirectResponse:
    text = comment.strip()
    if text:
        repository.queue_refresh(
            document.key, trigger="user", feedback=text, note_author="user"
        )
    return _redirect_to_status(document.status_token or "")


@router.post("/{token}/remove", summary="Remove from the search index")
def status_remove(
    document: DocumentRecord = Depends(get_document_for_token),
    repository: SearchEnhancementRepository = Depends(get_repository),
) -> RedirectResponse:
    if document.source_kind == "sharepoint":
        repository.queue_removal(document.key, trigger="user")
    return _redirect_to_status(document.status_token or "")

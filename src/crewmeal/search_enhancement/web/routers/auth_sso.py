"""Entra ID sign-in routes for the user-facing status page.

Three endpoints implement an OpenID Connect authorization-code flow:

* ``GET /auth/login``    -- start the flow and redirect to Entra,
* ``GET /auth/callback`` -- complete the flow and persist the signed-in identity,
* ``GET /auth/logout``   -- clear the identity.

These routes are deliberately *not* behind :func:`require_user` (that would create
a redirect loop). ``next`` is validated to a local path to prevent open redirects.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from crewmeal.search_enhancement.web.config import WebConfig
from crewmeal.search_enhancement.web.dependencies import get_web_config
from crewmeal.search_enhancement.web.oidc import (
    FLOW_SESSION_KEY,
    USER_SESSION_KEY,
    SsoError,
    begin_login,
    complete_login,
)
from crewmeal.search_enhancement.web.security import signed_in_user

router = APIRouter(prefix="/auth", tags=["auth"])

_NEXT_SESSION_KEY = "sso_next"
_DEFAULT_NEXT = "/"


def safe_next(raw: str | None) -> str:
    """Return a safe same-origin path, or ``/`` for anything suspicious.

    Guards against open redirects: only absolute paths on this origin are allowed
    (``/...``), never protocol-relative (``//host``), backslash-obfuscated, or
    absolute URLs.
    """

    if not raw:
        return _DEFAULT_NEXT
    candidate = raw.strip()
    if not candidate.startswith("/"):
        return _DEFAULT_NEXT
    if candidate.startswith("//") or candidate.startswith("/\\"):
        return _DEFAULT_NEXT
    if "\\" in candidate or "\n" in candidate or "\r" in candidate:
        return _DEFAULT_NEXT
    return candidate


def _sso_unavailable() -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html lang='ko'><meta charset='utf-8'>"
        "<body style='font-family:system-ui;margin:2rem'>"
        "<h1>로그인을 사용할 수 없습니다</h1>"
        "<p>상태 페이지 로그인이 구성되지 않았습니다. 관리자에게 문의하세요.</p>"
        "</body></html>",
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


def _sign_in_error(detail: str, next_url: str) -> HTMLResponse:
    safe_target = safe_next(next_url)
    return HTMLResponse(
        "<!doctype html><html lang='ko'><meta charset='utf-8'>"
        "<body style='font-family:system-ui;margin:2rem'>"
        "<h1>로그인에 실패했습니다</h1>"
        f"<p>{detail}</p>"
        f"<p><a href='/auth/login?next={safe_target}'>다시 시도</a></p>"
        "</body></html>",
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


@router.get("/login", response_model=None)
def auth_login(
    request: Request,
    next: str = _DEFAULT_NEXT,
    config: WebConfig = Depends(get_web_config),
) -> RedirectResponse | HTMLResponse:
    target = safe_next(next)
    # Nothing to enforce when the gate is off; just send the user onward.
    if not config.status_require_auth:
        return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
    if not config.sso_enabled:
        return _sso_unavailable()
    # Already signed in: skip the round trip to Entra.
    if signed_in_user(request) is not None:
        return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
    try:
        flow = begin_login(config)
    except Exception:  # noqa: BLE001 - surface config/network errors as 401
        return _sign_in_error("로그인을 시작할 수 없습니다.", target)
    request.session[FLOW_SESSION_KEY] = flow
    request.session[_NEXT_SESSION_KEY] = target
    return RedirectResponse(flow["auth_uri"], status_code=status.HTTP_303_SEE_OTHER)


@router.get("/callback", response_model=None)
def auth_callback(
    request: Request,
    config: WebConfig = Depends(get_web_config),
) -> RedirectResponse | HTMLResponse:
    if not config.sso_enabled:
        return _sso_unavailable()
    flow = request.session.pop(FLOW_SESSION_KEY, None)
    target = safe_next(request.session.pop(_NEXT_SESSION_KEY, None))
    if not isinstance(flow, dict):
        # No in-flight flow (expired session, bookmarked callback, replay).
        return _sign_in_error("로그인 세션이 만료되었습니다.", target)
    try:
        user = complete_login(config, flow, dict(request.query_params))
    except SsoError as exc:
        return _sign_in_error(str(exc), target)
    except Exception:  # noqa: BLE001 - network/library failure
        return _sign_in_error("로그인 처리 중 오류가 발생했습니다.", target)
    request.session[USER_SESSION_KEY] = user.to_session()
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/logout", response_model=None)
def auth_logout(request: Request) -> RedirectResponse:
    request.session.pop(USER_SESSION_KEY, None)
    request.session.pop(FLOW_SESSION_KEY, None)
    request.session.pop(_NEXT_SESSION_KEY, None)
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

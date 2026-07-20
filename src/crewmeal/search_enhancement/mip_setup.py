"""Guided tenant-setup helper for MIP decryption (admin portal, Tier A).

Enabling MIP decryption needs privileged, tenant-side preparation -- activating
Azure Rights Management, granting the CrewMeal service principal the
``Content.SuperUser`` (and ``UnifiedPolicy.Tenant.Read``) app roles, and admin
consent. Those are directory-administrator actions and must **not** be performed
by the app itself (a document processor that can self-grant super-user is a
privilege-escalation risk). So instead of *doing* the work, this module produces
everything an administrator needs to do it themselves, in one place:

* a per-prerequisite readiness checklist (fed by the live token probe),
* the exact admin-consent URL (pre-filled with tenant / client id), and
* copy-paste PowerShell that grants the app roles, pre-filled with the tenant's
  service-principal object id when a probe has discovered it.

Everything here is pure/string-building; the app holds no elevated privileges.
The GUIDs below are Microsoft's published, tenant-independent identifiers for
the Azure RMS and MIP Sync Service first-party apps and their app roles.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Well-known, tenant-independent Microsoft first-party identifiers.
RMS_APP_ID = "00000012-0000-0000-c000-000000000000"  # Azure Rights Management Services
CONTENT_SUPER_USER_ROLE_ID = "7347eb49-7a1a-43c5-8eac-a5cd1d1c7cf0"  # Content.SuperUser
MIP_SYNC_APP_ID = "870c4f2e-85b6-4d43-bdda-6ed9a579b725"  # Microsoft Information Protection Sync Service
UNIFIED_POLICY_TENANT_READ_ROLE_ID = "8b2071cd-015a-4025-8052-1c0dba2d3f64"  # UnifiedPolicy.Tenant.Read

_SP_PLACEHOLDER = "<CrewMeal 서비스 주체 object id>"


def admin_consent_url(tenant_id: str | None, client_id: str | None) -> str | None:
    """The interactive admin-consent URL for the CrewMeal app, or ``None``.

    A Global Administrator opens this and consents *as themselves*; the app
    never grants consent on its own behalf. Requires the app registration to
    already list the RMS/MIP permissions as required resource access.
    """

    if not tenant_id or not client_id:
        return None
    return (
        f"https://login.microsoftonline.com/{tenant_id}/adminconsent"
        f"?client_id={client_id}"
    )


def _checklist(
    *,
    tenant_id: str | None,
    client_id: str | None,
    adapter_configured: bool,
    health: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Per-prerequisite rows: ``ok`` is True/False, or ``None`` when unknown.

    ``None`` (unknown) is used for the tenant-side items until a live probe has
    actually run, so the UI can distinguish "not ready" from "not yet checked".
    """

    creds_present = bool(tenant_id and client_id)
    probed = health is not None
    token_ok = bool(health.get("ok")) if probed else None
    super_user = bool(health.get("super_user")) if probed else None
    detail = (health or {}).get("detail")

    return [
        {
            "label": "서비스 주체 자격증명",
            "ok": creds_present,
            "detail": (
                "CREWMEAL_M365_TENANT_ID / _CLIENT_ID / _CLIENT_SECRET 확인됨"
                if creds_present
                else "환경변수 CREWMEAL_M365_TENANT_ID / _CLIENT_ID / _CLIENT_SECRET 를 설정하세요."
            ),
        },
        {
            "label": "RMS 토큰 발급 (테넌트 RMS 활성화)",
            "ok": token_ok,
            "detail": (
                detail
                if probed
                else "'다시 점검'을 눌러 실제 토큰 발급을 확인하세요 (아래 1단계 필요)."
            ),
        },
        {
            "label": "Content.SuperUser 롤 (복호화 권한)",
            "ok": super_user,
            "detail": (
                "토큰에 super-user 롤 확인됨"
                if super_user
                else (
                    "super-user 롤이 토큰에 없습니다. 아래 3단계로 부여하세요 "
                    "(그룹 멤버십으로 부여된 경우 롤 클레임에 안 보일 수 있음)."
                    if probed
                    else "토큰 점검 후 확인됩니다."
                )
            ),
        },
        {
            "label": "복호화 어댑터 CLI 구성",
            "ok": adapter_configured,
            "detail": (
                "CREWMEAL_MIP_SDK_CLI 설정됨"
                if adapter_configured
                else "CREWMEAL_MIP_SDK_CLI 에 MIP File SDK 어댑터 경로를 지정하세요."
            ),
        },
    ]


def _commands(sp_object_id: str | None) -> list[dict[str, str]]:
    """Copy-paste PowerShell for the administrator, pre-filled where possible.

    The administrator runs these in *their own* privileged session (Connect-*
    as an admin); CrewMeal never executes them.
    """

    sp = sp_object_id or _SP_PLACEHOLDER
    return [
        {
            "title": "1. Azure RMS 활성화 + super-user 기능 켜기 (AIPService PowerShell)",
            "code": (
                "Install-Module AIPService -Scope CurrentUser\n"
                "Connect-AipService\n"
                "Enable-AipService\n"
                "Enable-AipServiceSuperUserFeature"
            ),
        },
        {
            "title": "2. Microsoft Graph 연결 (관리자 권한)",
            "code": (
                "Connect-MgGraph -Scopes "
                "AppRoleAssignment.ReadWrite.All,Application.Read.All\n"
                f'$crewmeal = "{sp}"'
            ),
        },
        {
            "title": "3. 앱에 Content.SuperUser 부여 (복호화)",
            "code": (
                f"$rms = (Get-MgServicePrincipal -Filter \"appId eq '{RMS_APP_ID}'\").Id\n"
                "New-MgServicePrincipalAppRoleAssignment -ServicePrincipalId $crewmeal "
                "-PrincipalId $crewmeal -ResourceId $rms "
                f"-AppRoleId {CONTENT_SUPER_USER_ROLE_ID}"
            ),
        },
        {
            "title": "4. 앱에 UnifiedPolicy.Tenant.Read 부여 (엔진 생성)",
            "code": (
                f"$sync = (Get-MgServicePrincipal -Filter \"appId eq '{MIP_SYNC_APP_ID}'\").Id\n"
                "New-MgServicePrincipalAppRoleAssignment -ServicePrincipalId $crewmeal "
                "-PrincipalId $crewmeal -ResourceId $sync "
                f"-AppRoleId {UNIFIED_POLICY_TENANT_READ_ROLE_ID}"
            ),
        },
    ]


def build_setup_guide(
    *,
    tenant_id: str | None,
    client_id: str | None,
    sp_object_id: str | None,
    adapter_configured: bool,
    health: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Assemble the admin setup wizard payload for the settings template.

    ``health`` is the live probe result (``None`` when no probe has run yet);
    ``sp_object_id`` is the service-principal object id discovered by the probe
    (``None`` until then, in which case the commands show a placeholder).
    """

    checklist = _checklist(
        tenant_id=tenant_id,
        client_id=client_id,
        adapter_configured=adapter_configured,
        health=health,
    )
    return {
        "ready": all(item["ok"] for item in checklist),
        "checklist": checklist,
        "consent_url": admin_consent_url(tenant_id, client_id),
        "commands": _commands(sp_object_id),
        "sp_object_id": sp_object_id,
    }

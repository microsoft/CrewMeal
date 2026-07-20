from __future__ import annotations

from crewmeal.search_enhancement.mip_setup import (
    CONTENT_SUPER_USER_ROLE_ID,
    RMS_APP_ID,
    UNIFIED_POLICY_TENANT_READ_ROLE_ID,
    admin_consent_url,
    build_setup_guide,
)


def test_admin_consent_url_format():
    url = admin_consent_url("tenant-1", "client-1")
    assert (
        url
        == "https://login.microsoftonline.com/tenant-1/adminconsent?client_id=client-1"
    )


def test_admin_consent_url_none_when_ids_missing():
    assert admin_consent_url(None, "client-1") is None
    assert admin_consent_url("tenant-1", None) is None


def test_setup_guide_unprobed_marks_tenant_items_unknown():
    guide = build_setup_guide(
        tenant_id="t",
        client_id="c",
        sp_object_id=None,
        adapter_configured=False,
        health=None,
    )
    assert guide["ready"] is False
    labels = {item["label"]: item["ok"] for item in guide["checklist"]}
    # Credentials are known-present; adapter known-absent; tenant items unknown.
    assert labels["서비스 주체 자격증명"] is True
    assert labels["복호화 어댑터 CLI 구성"] is False
    token_item = next(i for i in guide["checklist"] if i["ok"] is None)
    assert token_item is not None
    # No probe yet -> commands carry a placeholder, not a real object id.
    assert guide["sp_object_id"] is None
    assert any("<CrewMeal" in cmd["code"] for cmd in guide["commands"])


def test_setup_guide_ready_when_all_prerequisites_met():
    health = {
        "ok": True,
        "super_user": True,
        "decrypt_ready": True,
        "detail": "ready",
    }
    guide = build_setup_guide(
        tenant_id="t",
        client_id="c",
        sp_object_id="sp-oid",
        adapter_configured=True,
        health=health,
    )
    assert guide["ready"] is True
    assert all(item["ok"] for item in guide["checklist"])
    assert guide["sp_object_id"] == "sp-oid"


def test_setup_guide_commands_prefill_object_id_and_wellknown_ids():
    guide = build_setup_guide(
        tenant_id="t",
        client_id="c",
        sp_object_id="sp-oid-9",
        adapter_configured=True,
        health={"ok": True, "super_user": True, "decrypt_ready": True},
    )
    joined = "\n".join(cmd["code"] for cmd in guide["commands"])
    assert "sp-oid-9" in joined
    assert RMS_APP_ID in joined
    assert CONTENT_SUPER_USER_ROLE_ID in joined
    assert UNIFIED_POLICY_TENANT_READ_ROLE_ID in joined
    # The app never self-grants: guidance is PowerShell the admin runs.
    assert "New-MgServicePrincipalAppRoleAssignment" in joined


def test_setup_guide_flags_missing_super_user_after_probe():
    health = {
        "ok": True,
        "super_user": False,
        "decrypt_ready": False,
        "detail": "no super-user role",
    }
    guide = build_setup_guide(
        tenant_id="t",
        client_id="c",
        sp_object_id="sp-oid",
        adapter_configured=True,
        health=health,
    )
    assert guide["ready"] is False
    super_item = next(
        i for i in guide["checklist"] if "SuperUser" in i["label"]
    )
    assert super_item["ok"] is False

import pytest

from crewmeal.search_enhancement.decryption import (
    DecryptionNotImplementedError,
    decryption_setting_key,
    decryption_status,
    enabled_providers,
    is_decryption_enabled,
    maybe_decrypt,
)

_MIP_PAYLOAD = b"....MSIP_Label{guid}...." + b"\x00" * 16
_PLAIN_PAYLOAD = b"%PDF-1.7 normal document"


def test_providers_are_off_by_default():
    assert enabled_providers({}) == ()
    assert is_decryption_enabled("mip", {}) is False
    assert is_decryption_enabled("generic", {}) is False


def test_maybe_decrypt_is_noop_when_disabled():
    # Even a MIP-marked payload passes through untouched when the toggle is off.
    assert maybe_decrypt(_MIP_PAYLOAD, filename="secret.pptx", settings={}) is _MIP_PAYLOAD


def test_enabling_mip_detects_marker_and_fails_clearly():
    settings = {decryption_setting_key("mip"): True}
    with pytest.raises(DecryptionNotImplementedError):
        maybe_decrypt(_MIP_PAYLOAD, filename="secret.pptx", settings=settings)


def test_enabled_mip_ignores_plain_documents():
    settings = {decryption_setting_key("mip"): True}
    # No MIP marker -> provider does not match -> bytes returned unchanged.
    assert (
        maybe_decrypt(_PLAIN_PAYLOAD, filename="deck.pptx", settings=settings)
        is _PLAIN_PAYLOAD
    )


def test_enabling_generic_is_inert_until_implemented():
    settings = {decryption_setting_key("generic"): True}
    # Generic detection is intentionally inert, so nothing is decrypted or raised.
    assert (
        maybe_decrypt(_MIP_PAYLOAD, filename="x.pptx", settings=settings)
        is _MIP_PAYLOAD
    )


def test_decryption_status_rows():
    rows = {row["provider_id"]: row for row in decryption_status({})}
    assert set(rows) == {"mip", "generic"}
    assert rows["mip"]["implemented"] is False
    assert rows["mip"]["enabled"] is False

    enabled_rows = {
        row["provider_id"]: row
        for row in decryption_status({decryption_setting_key("mip"): True})
    }
    assert enabled_rows["mip"]["enabled"] is True

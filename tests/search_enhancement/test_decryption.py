import pytest

from crewmeal.config import AppConfig
from crewmeal.search_enhancement.decryption import (
    DecryptionFailedError,
    DecryptionUnavailableError,
    MipDecryptionProvider,
    decryption_setting_key,
    decryption_status,
    enabled_providers,
    is_decryption_enabled,
    maybe_decrypt,
)
from crewmeal.search_enhancement.mip_sdk import (
    MipSdkExecutionError,
    MipSdkUnavailableError,
)
from crewmeal.search_enhancement.processor import PresentationProcessor

_MIP_PAYLOAD = b"....MSIP_Label{guid}...." + b"\x00" * 16
_PLAIN_PAYLOAD = b"%PDF-1.7 normal document"


class _StubRunner:
    """Records the payload it was asked to decrypt and returns a canned result."""

    def __init__(self, result):
        self._result = result
        self.calls: list[tuple[bytes, str]] = []

    def run(self, data: bytes, *, filename: str) -> bytes:
        self.calls.append((data, filename))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def test_providers_are_off_by_default():
    assert enabled_providers({}) == ()
    assert is_decryption_enabled("mip", {}) is False
    assert is_decryption_enabled("generic", {}) is False


def test_maybe_decrypt_is_noop_when_disabled():
    # Even a MIP-marked payload passes through untouched when the toggle is off.
    assert maybe_decrypt(_MIP_PAYLOAD, filename="secret.pptx", settings={}) is _MIP_PAYLOAD


def test_enabled_mip_without_runner_fails_clearly():
    settings = {decryption_setting_key("mip"): True}
    with pytest.raises(DecryptionUnavailableError):
        maybe_decrypt(_MIP_PAYLOAD, filename="secret.pptx", settings=settings)


def test_enabled_mip_decrypts_via_runner():
    settings = {decryption_setting_key("mip"): True}
    runner = _StubRunner(b"decrypted-bytes")
    result = maybe_decrypt(
        _MIP_PAYLOAD,
        filename="secret.pptx",
        settings=settings,
        mip_runner=runner,
    )
    assert result == b"decrypted-bytes"
    assert runner.calls == [(_MIP_PAYLOAD, "secret.pptx")]


def test_runner_unavailable_maps_to_unavailable_error():
    settings = {decryption_setting_key("mip"): True}
    runner = _StubRunner(MipSdkUnavailableError("cli missing"))
    with pytest.raises(DecryptionUnavailableError):
        maybe_decrypt(
            _MIP_PAYLOAD, filename="s.pptx", settings=settings, mip_runner=runner
        )


def test_runner_execution_failure_maps_to_failed_error():
    settings = {decryption_setting_key("mip"): True}
    runner = _StubRunner(MipSdkExecutionError("exit 3: boom"))
    with pytest.raises(DecryptionFailedError):
        maybe_decrypt(
            _MIP_PAYLOAD, filename="s.pptx", settings=settings, mip_runner=runner
        )


def test_enabled_mip_ignores_plain_documents():
    settings = {decryption_setting_key("mip"): True}
    runner = _StubRunner(b"should-not-be-used")
    # No MIP marker -> provider does not match -> bytes returned unchanged and the
    # runner is never invoked.
    assert (
        maybe_decrypt(
            _PLAIN_PAYLOAD, filename="deck.pptx", settings=settings, mip_runner=runner
        )
        is _PLAIN_PAYLOAD
    )
    assert runner.calls == []


def test_mip_detect_matches_known_markers():
    provider = MipDecryptionProvider()
    for marker in (b"MicrosoftIRMServices", b"MSIP_Label", b"\x09DRMContent"):
        payload = b"zzz" + marker + b"zzz"
        assert provider.detect(payload, filename="x", content_type=None) is True
    assert provider.detect(_PLAIN_PAYLOAD, filename="x", content_type=None) is False


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
    assert rows["mip"]["implemented"] is True
    assert rows["mip"]["configured"] is False
    assert rows["mip"]["enabled"] is False

    enabled_rows = {
        row["provider_id"]: row
        for row in decryption_status(
            {decryption_setting_key("mip"): True},
            configured={"mip": True},
        )
    }
    assert enabled_rows["mip"]["enabled"] is True
    assert enabled_rows["mip"]["configured"] is True


def _processor(settings, runner=None) -> PresentationProcessor:
    return PresentationProcessor(
        AppConfig(endpoint=None, max_upload_bytes=1024, soffice_path=None),
        decryption_settings=settings,
        mip_runner=runner,
    )


def test_processor_decrypt_source_is_the_worker_boundary():
    # The worker calls decrypt_source(...) before fingerprinting/format handling.
    # Disabled -> passthrough; enabled+runner -> decrypted via the wired runner.
    settings = {decryption_setting_key("mip"): True}
    runner = _StubRunner(b"decrypted-bytes")

    assert (
        _processor({}).decrypt_source(_MIP_PAYLOAD, filename="s.pptx") is _MIP_PAYLOAD
    )

    processor = _processor(settings, runner)
    assert processor.decrypt_source(_MIP_PAYLOAD, filename="s.pptx") == b"decrypted-bytes"
    assert runner.calls == [(_MIP_PAYLOAD, "s.pptx")]


def test_processor_decrypt_source_without_runner_fails_loudly():
    settings = {decryption_setting_key("mip"): True}
    with pytest.raises(DecryptionUnavailableError):
        _processor(settings).decrypt_source(_MIP_PAYLOAD, filename="s.pptx")

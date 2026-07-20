"""Tests for the reference MIP protect/unprotect tool (demo backend)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from crewmeal.search_enhancement.decryption import MipDecryptionProvider
from crewmeal.search_enhancement.mip_tool import (
    ReferenceMipError,
    main,
    protect_bytes,
    resolve_release_secret,
    unprotect_bytes,
)


def test_protect_unprotect_roundtrip():
    payload = b"PK\x03\x04 pretend office bytes " * 8
    secret = b"unit-secret"
    envelope = protect_bytes(payload, filename="deck.pptx", secret=secret)
    assert unprotect_bytes(envelope, secret=secret) == payload


def test_protected_envelope_is_detected_by_provider():
    envelope = protect_bytes(b"anything", secret=b"s")
    provider = MipDecryptionProvider()
    assert provider.detect(envelope[:4096], filename="x.pptx", content_type=None) is True


def test_unprotect_rejects_non_envelope():
    with pytest.raises(ReferenceMipError):
        unprotect_bytes(b"not an envelope", secret=b"s")


def test_unprotect_wrong_secret_fails():
    envelope = protect_bytes(b"payload", secret=b"correct")
    with pytest.raises(ReferenceMipError):
        unprotect_bytes(envelope, secret=b"wrong")


def test_tampered_ciphertext_fails():
    envelope = bytearray(protect_bytes(b"payload", secret=b"s"))
    envelope[-1] ^= 0xFF  # flip a byte in the ciphertext
    with pytest.raises(ReferenceMipError):
        unprotect_bytes(bytes(envelope), secret=b"s")


def test_resolve_release_secret_prefers_env():
    assert resolve_release_secret({"CREWMEAL_MIP_RELEASE_SECRET": "abc"}) == b"abc"


def test_resolve_release_secret_defaults_when_unset():
    # A default is used so the demo works out of the box; both protect and
    # unprotect resolve the same value, so a round-trip still succeeds.
    default = resolve_release_secret({})
    assert default
    envelope = protect_bytes(b"data", secret=default)
    assert unprotect_bytes(envelope, secret=default) == b"data"


def test_cli_roundtrip_via_main(tmp_path: Path):
    plain = tmp_path / "plain.bin"
    enc = tmp_path / "enc.bin"
    dec = tmp_path / "dec.bin"
    token = tmp_path / "token.txt"
    plain.write_bytes(b"hello e2e")
    token.write_text("fake-rms-token", encoding="utf-8")

    rc = main(["protect", "--in", str(plain), "--out", str(enc), "--token-file", str(token)])
    assert rc == 0
    assert enc.read_bytes().startswith(b"MicrosoftIRMServices")

    rc = main(["unprotect", "--in", str(enc), "--out", str(dec), "--token-file", str(token)])
    assert rc == 0
    assert dec.read_bytes() == b"hello e2e"


def test_cli_rejects_empty_token_file(tmp_path: Path):
    plain = tmp_path / "plain.bin"
    enc = tmp_path / "enc.bin"
    token = tmp_path / "token.txt"
    plain.write_bytes(b"data")
    token.write_text("   ", encoding="utf-8")
    rc = main(["protect", "--in", str(plain), "--out", str(enc), "--token-file", str(token)])
    assert rc == 1


def test_module_is_executable_as_subprocess(tmp_path: Path):
    """The tool runs as `python -m ...`, matching the SDK CLI contract."""
    plain = tmp_path / "plain.bin"
    enc = tmp_path / "enc.bin"
    dec = tmp_path / "dec.bin"
    plain.write_bytes(b"subprocess payload")

    protect = subprocess.run(
        [sys.executable, "-m", "crewmeal.search_enhancement.mip_tool",
         "protect", "--in", str(plain), "--out", str(enc)],
        capture_output=True,
    )
    assert protect.returncode == 0, protect.stderr
    unprotect = subprocess.run(
        [sys.executable, "-m", "crewmeal.search_enhancement.mip_tool",
         "unprotect", "--in", str(enc), "--out", str(dec)],
        capture_output=True,
    )
    assert unprotect.returncode == 0, unprotect.stderr
    assert dec.read_bytes() == b"subprocess payload"

"""Reference MIP protect/unprotect CLI — a runnable stand-in for the real SDK.

.. warning::

   **This is NOT Microsoft Information Protection.** It performs no RMS calls and
   provides none of MIP's security guarantees. It exists so CrewMeal's decryption
   seam has a *runnable* backend in local development, CI, and the end-to-end
   demo, where the native Microsoft MIP File SDK and a live Azure RMS tenant are
   not available.

It speaks the exact contract the production seam
(:class:`~crewmeal.search_enhancement.mip_sdk.SubprocessMipSdkRunner`) expects::

    python -m crewmeal.search_enhancement.mip_tool unprotect \
        --in <input> --out <output> --token-file <token>

    python -m crewmeal.search_enhancement.mip_tool protect \
        --in <input> --out <output> [--token-file <token>]

* exit ``0`` -> success; result bytes are written to ``--out``.
* nonzero   -> failure; ``stderr`` explains why.

Envelope
--------
``protect`` wraps the payload in an envelope whose first bytes carry the same
MIP/IRM markers real protected files carry, so
:meth:`~crewmeal.search_enhancement.decryption.MipDecryptionProvider.detect`
recognises it. The payload is encrypted with a random AES-256-GCM content key;
that content key is wrapped with a key-encryption key derived (scrypt) from a
local *release secret*. In production the equivalent step is Azure RMS releasing
the content key to a super-user service principal — here a shared secret
simulates that authority. ``unprotect`` reverses the process.

The release secret is read from ``CREWMEAL_MIP_RELEASE_SECRET`` (a fixed default
is used when unset so the demo works out of the box). ``protect`` and
``unprotect`` must resolve the same secret for a round-trip to succeed.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

RELEASE_SECRET_ENV = "CREWMEAL_MIP_RELEASE_SECRET"
# Used only by the reference tool when the operator has not set a secret. Real
# MIP key release never uses a hard-coded secret.
_DEFAULT_RELEASE_SECRET = "crewmeal-reference-mip-secret"

# Literal markers that MIP/IRM wrappers carry and that ``detect`` scans for. The
# raw tab byte (0x09) before ``DRMContent`` matters, so we build the header as
# raw bytes rather than via JSON (which would escape the tab).
_MARKER_HEADER = b"MicrosoftIRMServices|MSIP_Label|\x09DRMContent"
_ENVELOPE_VERSION = 1

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LENGTH = 32


class ReferenceMipError(RuntimeError):
    """A protect/unprotect operation could not be completed."""


def resolve_release_secret(env: dict[str, str] | None = None) -> bytes:
    """Return the release secret bytes, falling back to the demo default."""

    source = env if env is not None else os.environ
    raw = (source.get(RELEASE_SECRET_ENV) or "").strip()
    return (raw or _DEFAULT_RELEASE_SECRET).encode("utf-8")


def _derive_kek(secret: bytes, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=_KEY_LENGTH, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return kdf.derive(secret)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def protect_bytes(data: bytes, *, filename: str = "", secret: bytes | None = None) -> bytes:
    """Encrypt ``data`` into a detectable reference-MIP envelope."""

    secret = secret if secret is not None else resolve_release_secret()
    salt = os.urandom(16)
    kek = _derive_kek(secret, salt)

    content_key = AESGCM.generate_key(bit_length=256)
    payload_nonce = os.urandom(12)
    ciphertext = AESGCM(content_key).encrypt(payload_nonce, data, None)

    key_nonce = os.urandom(12)
    wrapped_key = AESGCM(kek).encrypt(key_nonce, content_key, None)

    metadata = {
        "v": _ENVELOPE_VERSION,
        "alg": "AES-256-GCM+scrypt",
        "filename": filename,
        "salt": _b64(salt),
        "key_nonce": _b64(key_nonce),
        "wrapped_key": _b64(wrapped_key),
        "payload_nonce": _b64(payload_nonce),
        "ciphertext": _b64(ciphertext),
    }
    body = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
    return _MARKER_HEADER + b"\n" + body


def unprotect_bytes(data: bytes, *, secret: bytes | None = None) -> bytes:
    """Reverse :func:`protect_bytes`, returning the original plaintext."""

    secret = secret if secret is not None else resolve_release_secret()
    if not data.startswith(_MARKER_HEADER):
        raise ReferenceMipError(
            "Input is not a reference-MIP envelope (missing marker header). This "
            "tool cannot decrypt real MIP/RMS content; configure "
            "CREWMEAL_MIP_SDK_CLI with the Microsoft MIP File SDK CLI for that."
        )
    newline = data.find(b"\n")
    if newline == -1:
        raise ReferenceMipError("Malformed reference-MIP envelope: no metadata section.")
    try:
        metadata = json.loads(data[newline + 1 :].decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ReferenceMipError(f"Malformed reference-MIP envelope: {exc}") from exc

    try:
        salt = _unb64(metadata["salt"])
        key_nonce = _unb64(metadata["key_nonce"])
        wrapped_key = _unb64(metadata["wrapped_key"])
        payload_nonce = _unb64(metadata["payload_nonce"])
        ciphertext = _unb64(metadata["ciphertext"])
    except (KeyError, ValueError, TypeError) as exc:
        raise ReferenceMipError(f"Malformed reference-MIP envelope: {exc}") from exc

    kek = _derive_kek(secret, salt)
    try:
        content_key = AESGCM(kek).decrypt(key_nonce, wrapped_key, None)
        return AESGCM(content_key).decrypt(payload_nonce, ciphertext, None)
    except Exception as exc:  # noqa: BLE001 - all crypto failures map to one error
        raise ReferenceMipError(
            "Failed to unwrap/decrypt the payload. The release secret "
            f"({RELEASE_SECRET_ENV}) likely does not match the one used to protect it."
        ) from exc


def _read_token(token_file: str | None) -> None:
    """Mirror the real contract's token requirement.

    The reference tool does not need a token cryptographically, but when a token
    file is supplied it must be non-empty — the production SDK requires an RMS
    bearer token to release keys, and CrewMeal always supplies one.
    """

    if token_file is None:
        return
    try:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ReferenceMipError(f"Could not read token file {token_file!r}: {exc}") from exc
    if not token:
        raise ReferenceMipError(
            f"Token file {token_file!r} is empty; an RMS token is required to release keys."
        )


def _run(args: argparse.Namespace) -> int:
    try:
        _read_token(args.token_file)
        data = Path(args.input).read_bytes()
        if args.command == "protect":
            result = protect_bytes(data, filename=Path(args.input).name)
        else:
            result = unprotect_bytes(data)
        Path(args.output).write_bytes(result)
    except ReferenceMipError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"I/O error: {exc}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m crewmeal.search_enhancement.mip_tool",
        description="Reference MIP protect/unprotect (demo only, not real RMS).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("protect", "Wrap a file in a detectable reference-MIP envelope."),
        ("unprotect", "Decrypt a reference-MIP envelope back to plaintext."),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("--in", dest="input", required=True, help="Input file path.")
        sub.add_argument("--out", dest="output", required=True, help="Output file path.")
        sub.add_argument(
            "--token-file",
            dest="token_file",
            default=None,
            help="Path to a file containing the RMS bearer token (contract parity).",
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return _run(args)


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(main())

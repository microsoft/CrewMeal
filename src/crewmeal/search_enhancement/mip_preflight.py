"""Real-tenant preflight for MIP decryption.

Usage::

    python -m crewmeal.search_enhancement.mip_preflight [--scope SCOPE]
        [--sample FILE] [--out FILE]

Validates, against the *real* tenant, that the pieces the unattended decryption
path depends on are actually in place:

1. The configured M365 service principal (``CREWMEAL_M365_TENANT_ID`` /
   ``CREWMEAL_M365_CLIENT_ID`` / ``CREWMEAL_M365_CLIENT_SECRET``) can obtain an
   Azure Rights Management app-only token for the RMS resource
   (``CREWMEAL_MIP_RMS_SCOPE``, default ``https://aadrm.com/.default``). The
   decryption-relevant, *non-sensitive* JWT claims are reported (``aud``,
   ``appid``, ``oid``, ``tid``, ``roles``, ``exp``).
2. An RMS *super user* role appears to be granted. Decrypting tenant-protected
   content regardless of policy requires the service principal to be an RMS
   super user; the check warns loudly when no such role is present.
3. Optionally, that the configured ``CREWMEAL_MIP_SDK_CLI`` adapter can actually
   decrypt a real MIP-protected ``--sample`` file through the very same seam the
   worker uses (:func:`crewmeal.search_enhancement.mip_sdk.build_runner`).

The bearer token is a credential: this tool never prints or persists it, only the
non-sensitive claims decoded from its payload.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
from dataclasses import dataclass
from pathlib import Path

from azure.identity import ClientSecretCredential

from crewmeal.search_enhancement.mip_sdk import (
    DEFAULT_RMS_SCOPE,
    MipSdkConfig,
    build_runner,
)

_SP_ENV = {
    "tenant_id": "CREWMEAL_M365_TENANT_ID",
    "client_id": "CREWMEAL_M365_CLIENT_ID",
    "client_secret": "CREWMEAL_M365_CLIENT_SECRET",
}


class PreflightError(RuntimeError):
    """A preflight step could not be completed."""


def _service_principal() -> tuple[str, str, str]:
    values = {key: os.getenv(env, "").strip() for key, env in _SP_ENV.items()}
    missing = sorted(env for key, env in _SP_ENV.items() if not values[key])
    if missing:
        raise PreflightError(
            "Missing service-principal settings: " + ", ".join(missing)
        )
    return values["tenant_id"], values["client_id"], values["client_secret"]


def _credential() -> ClientSecretCredential:
    tenant_id, client_id, client_secret = _service_principal()
    return ClientSecretCredential(
        tenant_id=tenant_id, client_id=client_id, client_secret=client_secret
    )


def decode_claims(token: str) -> dict:
    """Return the (signature-unverified) JWT payload claims for reporting.

    Only the middle segment is decoded so non-sensitive claims can be shown; the
    token itself is never printed or stored.
    """

    parts = token.split(".")
    if len(parts) < 2:
        raise PreflightError("RMS token is not a JWT")
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload)
        return json.loads(raw)
    except (binascii.Error, ValueError) as exc:
        raise PreflightError(f"Could not decode RMS token claims: {exc}") from exc


def has_super_user_role(claims: dict) -> bool:
    """Whether the token carries an RMS super-user *application role* claim.

    Note: super-user granted via *group membership* is not reflected as a role
    claim, so a ``False`` result is a warning to verify, not proof of absence.
    """

    roles = claims.get("roles") or []
    if isinstance(roles, str):
        roles = [roles]
    return any("superuser" in str(role).lower() for role in roles)


@dataclass(frozen=True)
class TokenReport:
    aud: object
    appid: object
    oid: object
    tid: object
    roles: object
    exp: object
    super_user: bool


def check_token(credential: ClientSecretCredential, scope: str) -> TokenReport:
    try:
        token = credential.get_token(scope).token
    except Exception as exc:  # noqa: BLE001 - surfaced as a preflight failure
        raise PreflightError(
            f"Failed to acquire an RMS token for scope {scope!r}: {exc}"
        ) from exc
    claims = decode_claims(token)
    return TokenReport(
        aud=claims.get("aud"),
        appid=claims.get("appid"),
        oid=claims.get("oid"),
        tid=claims.get("tid"),
        roles=claims.get("roles"),
        exp=claims.get("exp"),
        super_user=has_super_user_role(claims),
    )


def _check_adapter(
    credential: ClientSecretCredential,
    config: MipSdkConfig,
    *,
    sample: str | None,
    out: str | None,
) -> None:
    if not config.is_configured:
        print(
            "  adapter        : CREWMEAL_MIP_SDK_CLI is NOT set - no real "
            "decryption backend is wired (only the reference tool exists)."
        )
        return
    print(f"  adapter        : {' '.join(config.command)}")
    if not sample:
        print(
            "  sample decrypt : skipped (pass --sample <protected-file> to "
            "exercise a real decrypt through the adapter)."
        )
        return
    runner = build_runner(config, credential)
    if runner is None:  # pragma: no cover - defensive; both inputs are present
        raise PreflightError("MIP runner could not be built despite configuration.")
    data = Path(sample).read_bytes()
    plaintext = runner.run(data, filename=Path(sample).name)
    looks_like_zip = plaintext[:2] == b"PK"
    print(
        f"  sample decrypt : OK - {len(plaintext)} bytes "
        f"(office-zip={looks_like_zip})"
    )
    if out:
        Path(out).write_bytes(plaintext)
        print(f"                   wrote plaintext to {out}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Preflight the real-tenant MIP decryption dependencies."
    )
    parser.add_argument(
        "--scope",
        default=os.getenv("CREWMEAL_MIP_RMS_SCOPE", "").strip() or DEFAULT_RMS_SCOPE,
        help="RMS token scope (default: the configured CREWMEAL_MIP_RMS_SCOPE).",
    )
    parser.add_argument(
        "--sample",
        help="Path to a real MIP-protected file to decrypt through the adapter.",
    )
    parser.add_argument(
        "--out",
        help="Where to write the decrypted --sample output (optional).",
    )
    args = parser.parse_args(argv)

    print(f"MIP preflight - RMS scope {args.scope}")

    try:
        credential = _credential()
    except PreflightError as exc:
        print(f"  service principal: FAIL - {exc}")
        return 2

    try:
        try:
            report = check_token(credential, args.scope)
        except PreflightError as exc:
            print(f"  token          : FAIL - {exc}")
            return 2

        print("  token          : OK")
        print(f"    aud          : {report.aud}")
        print(f"    appid        : {report.appid}")
        print(f"    oid          : {report.oid}")
        print(f"    tid          : {report.tid}")
        print(f"    roles        : {report.roles}")
        if report.super_user:
            print("  super-user     : role present in token")
        else:
            print(
                "  super-user     : NOT in token - grant the app 'Content.SuperUser' "
                "on Azure RMS (app permission + admin consent) or add SP object id "
                f"{report.oid} to the RMS super-user group. Group-based grants do "
                "not appear as a role claim; verify with the AIPService module."
            )

        try:
            _check_adapter(
                credential,
                MipSdkConfig.from_environment(),
                sample=args.sample,
                out=args.out,
            )
        except PreflightError as exc:
            print(f"  sample decrypt : FAIL - {exc}")
            return 3
        except Exception as exc:  # noqa: BLE001 - adapter/IO failures are reportable
            print(f"  sample decrypt : FAIL - {type(exc).__name__}: {exc}")
            return 3
    finally:
        close = getattr(credential, "close", None)
        if callable(close):
            close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

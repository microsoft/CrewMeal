"""Unit tests for the MIP real-tenant preflight (network-free)."""

from __future__ import annotations

import base64
import json

import pytest

from crewmeal.search_enhancement.mip_preflight import (
    PreflightError,
    decode_claims,
    has_super_user_role,
)


def _jwt(payload: dict) -> str:
    """Build an unsigned JWT whose payload base64url-decodes to ``payload``."""

    def seg(data: dict) -> str:
        raw = json.dumps(data).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{seg({'alg': 'none'})}.{seg(payload)}.sig"


def test_decode_claims_round_trips_padding_stripped_payload():
    claims = {"aud": "https://aadrm.com", "appid": "abc", "roles": None}
    token = _jwt(claims)
    # Sanity: the payload segment has no '=' padding, exercising the pad logic.
    assert "=" not in token.split(".")[1]
    assert decode_claims(token) == claims


def test_decode_claims_rejects_non_jwt():
    with pytest.raises(PreflightError):
        decode_claims("not-a-jwt")


def test_decode_claims_rejects_undecodable_payload():
    with pytest.raises(PreflightError):
        decode_claims("header.@@@notbase64@@@.sig")


@pytest.mark.parametrize(
    ("roles", "expected"),
    [
        (None, False),
        ([], False),
        (["Reader"], False),
        (["Content.SuperUser"], True),
        ("Content.SuperUser", True),
        (["a", "b", "content.superuser"], True),
    ],
)
def test_has_super_user_role(roles, expected):
    assert has_super_user_role({"roles": roles}) is expected

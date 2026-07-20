"""Tests for the worker CLI startup MIP preflight (advisory logging only)."""

from __future__ import annotations

import base64
import json
import logging

from crewmeal.search_enhancement.cli import _log_mip_preflight
from crewmeal.search_enhancement.decryption import decryption_setting_key
from crewmeal.search_enhancement.mip_sdk import MipSdkConfig

_ENABLED = {decryption_setting_key("mip"): True}


def _jwt(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.sig"


class _Token:
    def __init__(self, token: str) -> None:
        self.token = token


class _Cred:
    def __init__(self, token: str = "", *, fail: bool = False) -> None:
        self._token = token
        self._fail = fail

    def get_token(self, *_scopes: str, **_kwargs):
        if self._fail:
            raise RuntimeError("token acquisition failed")
        return _Token(self._token)


def test_preflight_silent_when_disabled(caplog) -> None:
    caplog.set_level(logging.INFO)
    _log_mip_preflight({}, MipSdkConfig(command=("mip-cli",)), _Cred(_jwt({})))
    assert "MIP decryption" not in caplog.text


def test_preflight_warns_when_enabled_but_cli_missing(caplog) -> None:
    caplog.set_level(logging.INFO)
    _log_mip_preflight(_ENABLED, MipSdkConfig(), _Cred(_jwt({})))
    assert "CREWMEAL_MIP_SDK_CLI is not set" in caplog.text


def test_preflight_ok_when_ready(caplog) -> None:
    caplog.set_level(logging.INFO)
    cred = _Cred(_jwt({"roles": ["Content.SuperUser"]}))
    _log_mip_preflight(_ENABLED, MipSdkConfig(command=("mip-cli",)), cred)
    assert "preflight OK" in caplog.text


def test_preflight_warns_when_not_ready(caplog) -> None:
    caplog.set_level(logging.INFO)
    cred = _Cred(_jwt({"roles": ["Content.Writer"]}))
    _log_mip_preflight(_ENABLED, MipSdkConfig(command=("mip-cli",)), cred)
    assert "not ready" in caplog.text
    assert "mip_preflight" in caplog.text


def test_preflight_warns_on_token_failure(caplog) -> None:
    caplog.set_level(logging.INFO)
    _log_mip_preflight(_ENABLED, MipSdkConfig(command=("mip-cli",)), _Cred(fail=True))
    assert "not ready" in caplog.text

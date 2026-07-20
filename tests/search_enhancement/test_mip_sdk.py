"""Unit tests for the MIP SDK subprocess seam (argv, env, exit-code mapping)."""

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path

import pytest

from crewmeal.search_enhancement.mip_sdk import (
    DEFAULT_RMS_SCOPE,
    MipSdkConfig,
    MipSdkExecutionError,
    MipSdkUnavailableError,
    RmsHealth,
    SubprocessMipSdkRunner,
    build_runner,
    decode_token_claims,
    probe_rms_health,
    token_has_super_user,
)


class _FakeToken:
    def __init__(self, token: str):
        self.token = token


class _FakeCredential:
    def __init__(self, token: str = "app-only-token", *, fail: bool = False):
        self._token = token
        self._fail = fail
        self.scopes: list[str] = []

    def get_token(self, *scopes: str, **_kwargs):
        self.scopes.append(scopes[0] if scopes else "")
        if self._fail:
            raise RuntimeError("token acquisition failed")
        return _FakeToken(self._token)


class _FakeRun:
    """Simulates a CLI: reads --in/--token-file, writes --out, returns a result."""

    def __init__(self, *, returncode: int = 0, write_output: bool = True, stderr: bytes = b""):
        self.returncode = returncode
        self.write_output = write_output
        self.stderr = stderr
        self.argv: list[str] | None = None
        self.env: dict[str, str] | None = None
        self.token_seen: str | None = None
        self.input_seen: bytes | None = None

    def __call__(self, argv, *, capture_output, timeout, env):
        self.argv = list(argv)
        self.env = dict(env)
        values = {}
        for flag in ("--in", "--out", "--token-file"):
            values[flag] = argv[argv.index(flag) + 1]
        self.input_seen = Path(values["--in"]).read_bytes()
        self.token_seen = Path(values["--token-file"]).read_text(encoding="utf-8")
        if self.write_output and self.returncode == 0:
            Path(values["--out"]).write_bytes(b"PLAIN:" + self.input_seen)
        return subprocess.CompletedProcess(argv, self.returncode, b"", self.stderr)


def _config(**overrides) -> MipSdkConfig:
    base = dict(command=("mip-cli",), lib_dir="/opt/mip/lib")
    base.update(overrides)
    return MipSdkConfig(**base)


def test_runner_builds_expected_argv_and_returns_output():
    fake = _FakeRun()
    credential = _FakeCredential("secret-token")
    runner = SubprocessMipSdkRunner(_config(), credential, subprocess_run=fake)

    result = runner.run(b"ciphertext", filename="deck.pptx")

    assert result == b"PLAIN:ciphertext"
    assert fake.argv[:2] == ["mip-cli", "unprotect"]
    assert "--in" in fake.argv and "--out" in fake.argv and "--token-file" in fake.argv
    # The token is delivered via a file, never on the argv/process list.
    assert "secret-token" not in fake.argv
    assert fake.token_seen == "secret-token"
    assert fake.input_seen == b"ciphertext"
    assert credential.scopes == [DEFAULT_RMS_SCOPE]


def test_runner_prepends_lib_dir_to_search_paths():
    fake = _FakeRun()
    runner = SubprocessMipSdkRunner(_config(lib_dir="/opt/mip/lib"), _FakeCredential(), subprocess_run=fake)
    runner.run(b"x", filename="f.pptx")
    for var in ("PATH", "LD_LIBRARY_PATH"):
        assert fake.env[var].split(os.pathsep)[0] == "/opt/mip/lib"


def test_runner_uses_custom_scope_and_subcommand():
    fake = _FakeRun()
    credential = _FakeCredential()
    runner = SubprocessMipSdkRunner(
        _config(scope="https://custom/.default", subcommand="decrypt"),
        credential,
        subprocess_run=fake,
    )
    runner.run(b"x", filename="f.pptx")
    assert fake.argv[1] == "decrypt"
    assert credential.scopes == ["https://custom/.default"]


def test_runner_unconfigured_raises_unavailable():
    runner = SubprocessMipSdkRunner(MipSdkConfig(), _FakeCredential())
    with pytest.raises(MipSdkUnavailableError):
        runner.run(b"x", filename="f.pptx")


def test_runner_nonzero_exit_raises_execution_error_with_stderr():
    fake = _FakeRun(returncode=7, write_output=False, stderr=b"boom happened")
    runner = SubprocessMipSdkRunner(_config(), _FakeCredential(), subprocess_run=fake)
    with pytest.raises(MipSdkExecutionError) as excinfo:
        runner.run(b"x", filename="f.pptx")
    assert "boom happened" in str(excinfo.value)


def test_runner_missing_output_despite_success_raises():
    fake = _FakeRun(returncode=0, write_output=False)
    runner = SubprocessMipSdkRunner(_config(), _FakeCredential(), subprocess_run=fake)
    with pytest.raises(MipSdkExecutionError):
        runner.run(b"x", filename="f.pptx")


def test_runner_missing_executable_raises_unavailable():
    def _raise(*_args, **_kwargs):
        raise FileNotFoundError("no such file")

    runner = SubprocessMipSdkRunner(_config(), _FakeCredential(), subprocess_run=_raise)
    with pytest.raises(MipSdkUnavailableError):
        runner.run(b"x", filename="f.pptx")


def test_runner_timeout_raises_execution_error():
    def _timeout(argv, **_kwargs):
        raise subprocess.TimeoutExpired(argv, 120)

    runner = SubprocessMipSdkRunner(_config(), _FakeCredential(), subprocess_run=_timeout)
    with pytest.raises(MipSdkExecutionError):
        runner.run(b"x", filename="f.pptx")


def test_runner_token_failure_raises_execution_error():
    fake = _FakeRun()
    runner = SubprocessMipSdkRunner(_config(), _FakeCredential(fail=True), subprocess_run=fake)
    with pytest.raises(MipSdkExecutionError):
        runner.run(b"x", filename="f.pptx")
    assert fake.argv is None  # never reached the subprocess


def test_build_runner_returns_none_when_unconfigured():
    assert build_runner(MipSdkConfig(), _FakeCredential()) is None


def test_build_runner_returns_none_without_credential():
    assert build_runner(_config(), None) is None


def test_build_runner_returns_runner_when_ready():
    runner = build_runner(_config(), _FakeCredential())
    assert isinstance(runner, SubprocessMipSdkRunner)


def test_config_from_environment_parses_values(monkeypatch):
    monkeypatch.setenv("CREWMEAL_MIP_SDK_CLI", "python -m crewmeal.search_enhancement.mip_tool")
    monkeypatch.setenv("CREWMEAL_MIP_RMS_SCOPE", "https://aadrm.com/.default")
    monkeypatch.setenv("CREWMEAL_MIP_SDK_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("CREWMEAL_MIP_SDK_LIB_DIR", "/opt/mip/lib")
    monkeypatch.setenv("CREWMEAL_MIP_SDK_SUBCOMMAND", "unprotect")

    config = MipSdkConfig.from_environment()
    assert config.command[-1] == "crewmeal.search_enhancement.mip_tool"
    assert "-m" in config.command
    assert config.is_configured is True
    assert config.scope == "https://aadrm.com/.default"
    assert config.timeout_seconds == 45
    assert config.lib_dir == "/opt/mip/lib"
    assert config.subcommand == "unprotect"


def test_config_from_environment_defaults(monkeypatch):
    for var in (
        "CREWMEAL_MIP_SDK_CLI",
        "CREWMEAL_MIP_RMS_SCOPE",
        "CREWMEAL_MIP_SDK_TIMEOUT_SECONDS",
        "CREWMEAL_MIP_SDK_LIB_DIR",
        "CREWMEAL_MIP_SDK_SUBCOMMAND",
    ):
        monkeypatch.delenv(var, raising=False)
    config = MipSdkConfig.from_environment()
    assert config.command == ()
    assert config.is_configured is False
    assert config.scope == DEFAULT_RMS_SCOPE
    assert config.subcommand == "unprotect"


# --------------------------------------------------------------------------- #
# Tenant readiness probe
# --------------------------------------------------------------------------- #
def _jwt(claims: dict) -> str:
    """A signature-less JWT whose middle segment encodes ``claims``."""

    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def test_decode_token_claims_and_super_user_detection():
    claims = decode_token_claims(_jwt({"roles": ["Content.SuperUser"], "appid": "abc"}))
    assert claims["appid"] == "abc"
    assert token_has_super_user(claims) is True
    # A single string role and non-super-user roles are handled.
    assert token_has_super_user({"roles": "Content.SuperUser"}) is True
    assert token_has_super_user({"roles": ["Content.Writer"]}) is False
    assert token_has_super_user({}) is False
    # Anything that is not a decodable JWT yields empty claims, never raises.
    assert decode_token_claims("not-a-jwt") == {}
    assert decode_token_claims("header.@@@notbase64@@@.sig") == {}


def test_probe_rms_health_ready_when_super_user_present():
    credential = _FakeCredential(_jwt({"roles": ["Content.SuperUser"], "appid": "app-1"}))
    health = probe_rms_health(credential, DEFAULT_RMS_SCOPE)
    assert isinstance(health, RmsHealth)
    assert health.ok is True
    assert health.super_user is True
    assert health.decrypt_ready is True
    assert health.app_id == "app-1"
    assert health.roles == ("Content.SuperUser",)
    assert credential.scopes == [DEFAULT_RMS_SCOPE]


def test_probe_rms_health_token_ok_but_no_super_user():
    credential = _FakeCredential(_jwt({"roles": ["Content.Writer"]}))
    health = probe_rms_health(credential)
    assert health.ok is True
    assert health.super_user is False
    assert health.decrypt_ready is False
    assert "super-user" in health.describe()


def test_probe_rms_health_token_acquisition_failure():
    health = probe_rms_health(_FakeCredential(fail=True))
    assert health.ok is False
    assert health.decrypt_ready is False
    assert "token acquisition failed" in (health.error or "")
    assert "unavailable" in health.describe()


def test_probe_rms_health_without_credential():
    health = probe_rms_health(None)
    assert health.ok is False
    assert health.decrypt_ready is False
    assert "credential" in (health.error or "")

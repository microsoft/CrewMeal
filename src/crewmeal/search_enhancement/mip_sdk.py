"""MIP File SDK subprocess seam.

CrewMeal decrypts MIP/RMS-protected documents by shelling out to a command-line
tool that speaks a small, fixed contract::

    <cli...> unprotect --in <input> --out <output> --token-file <token>

  * exit 0  -> success; the decrypted bytes are written to ``<output>``.
  * nonzero -> failure; ``stderr`` explains why.

The tool is deployment-provided via the ``CREWMEAL_MIP_SDK_CLI`` environment
variable. In production this points at (a thin wrapper around) the official
Microsoft Information Protection File SDK. For local development, CI, and the
end-to-end demo it points at the bundled *reference* tool
(``python -m crewmeal.search_enhancement.mip_tool``), which is a runnable
stand-in — **not** real MIP/RMS cryptography.

Authentication is unattended. CrewMeal acquires an app-only bearer token for the
Azure Rights Management resource (``https://aadrm.com/.default`` by default)
using the existing M365 service principal, which must be an Azure RMS *super
user* so it can decrypt any protected content in the tenant regardless of the
document's rights policy. The token is handed to the tool through a temp file
(never on the argv / process list).

This module deliberately has **no dependency on** :mod:`decryption`; the
higher-level provider there maps our internal :class:`MipSdkError` subclasses to
the public ``DecryptionError`` hierarchy.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from azure.core.credentials import TokenCredential

LOGGER = logging.getLogger(__name__)

DEFAULT_RMS_SCOPE = "https://aadrm.com/.default"
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_SUBCOMMAND = "unprotect"


class MipSdkError(RuntimeError):
    """Base error for the MIP SDK subprocess seam."""


class MipSdkUnavailableError(MipSdkError):
    """The MIP SDK CLI is not configured or could not be launched."""


class MipSdkExecutionError(MipSdkError):
    """The MIP SDK CLI ran but failed to decrypt the payload."""


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        LOGGER.warning("%s is not an integer (%r); using default %d", name, raw, default)
        return default
    return value if value > 0 else default


@dataclass(frozen=True, slots=True)
class MipSdkConfig:
    """Deployment configuration for the MIP SDK CLI seam."""

    command: tuple[str, ...] = ()
    scope: str = DEFAULT_RMS_SCOPE
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    lib_dir: str | None = None
    subcommand: str = DEFAULT_SUBCOMMAND

    @property
    def is_configured(self) -> bool:
        """Whether a CLI command has been provided."""

        return bool(self.command)

    @classmethod
    def from_environment(cls) -> "MipSdkConfig":
        raw = os.getenv("CREWMEAL_MIP_SDK_CLI", "").strip()
        # ``posix=False`` on Windows preserves backslash paths; production runs
        # on Linux where posix splitting handles quoting correctly.
        command = tuple(shlex.split(raw, posix=(os.name != "nt"))) if raw else ()
        scope = os.getenv("CREWMEAL_MIP_RMS_SCOPE", "").strip() or DEFAULT_RMS_SCOPE
        timeout = _positive_int(
            "CREWMEAL_MIP_SDK_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS
        )
        lib_dir = os.getenv("CREWMEAL_MIP_SDK_LIB_DIR", "").strip() or None
        subcommand = (
            os.getenv("CREWMEAL_MIP_SDK_SUBCOMMAND", "").strip() or DEFAULT_SUBCOMMAND
        )
        return cls(
            command=command,
            scope=scope,
            timeout_seconds=timeout,
            lib_dir=lib_dir,
            subcommand=subcommand,
        )


@runtime_checkable
class MipSdkRunner(Protocol):
    """Decrypts one MIP-protected payload, returning the plaintext bytes."""

    def run(self, data: bytes, *, filename: str) -> bytes: ...


def _decode(stream: object) -> str:
    if isinstance(stream, bytes):
        return stream.decode("utf-8", "replace").strip()
    if stream is None:
        return ""
    return str(stream).strip()


class SubprocessMipSdkRunner:
    """Shell out to the configured MIP SDK CLI to decrypt a payload.

    ``subprocess_run`` is injectable so tests can exercise argv construction and
    exit-code mapping without a real binary.
    """

    def __init__(
        self,
        config: MipSdkConfig,
        credential: TokenCredential,
        *,
        subprocess_run=subprocess.run,
    ) -> None:
        self._config = config
        self._credential = credential
        self._subprocess_run = subprocess_run

    def _acquire_token(self) -> str:
        try:
            return self._credential.get_token(self._config.scope).token
        except Exception as exc:  # noqa: BLE001 - surfaced as an execution error
            raise MipSdkExecutionError(
                f"Failed to acquire an RMS token for scope {self._config.scope!r}: {exc}"
            ) from exc

    def _subprocess_env(self) -> dict[str, str]:
        env = dict(os.environ)
        lib_dir = self._config.lib_dir
        if lib_dir:
            for var in ("PATH", "LD_LIBRARY_PATH"):
                existing = env.get(var, "")
                env[var] = lib_dir + (os.pathsep + existing if existing else "")
        return env

    def run(self, data: bytes, *, filename: str) -> bytes:
        if not self._config.is_configured:
            raise MipSdkUnavailableError(
                "MIP SDK CLI is not configured. Set CREWMEAL_MIP_SDK_CLI to the "
                "decryption tool (production: the Microsoft MIP File SDK CLI; "
                "local/CI: 'python -m crewmeal.search_enhancement.mip_tool')."
            )
        token = self._acquire_token()
        with tempfile.TemporaryDirectory(prefix="crewmeal-mip-") as workspace:
            work = Path(workspace)
            in_path = work / "input.bin"
            out_path = work / "output.bin"
            token_path = work / "token.txt"
            in_path.write_bytes(data)
            token_path.write_text(token, encoding="utf-8")

            argv = [
                *self._config.command,
                self._config.subcommand,
                "--in",
                str(in_path),
                "--out",
                str(out_path),
                "--token-file",
                str(token_path),
            ]
            LOGGER.info("Invoking MIP SDK CLI for %s", filename)
            try:
                completed = self._subprocess_run(
                    argv,
                    capture_output=True,
                    timeout=self._config.timeout_seconds,
                    env=self._subprocess_env(),
                )
            except FileNotFoundError as exc:
                raise MipSdkUnavailableError(
                    f"MIP SDK CLI executable not found: {self._config.command[0]!r}"
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise MipSdkExecutionError(
                    f"MIP SDK CLI timed out after {self._config.timeout_seconds}s "
                    f"decrypting {filename!r}."
                ) from exc

            returncode = getattr(completed, "returncode", 1)
            if returncode != 0:
                stderr = _decode(getattr(completed, "stderr", b""))
                raise MipSdkExecutionError(
                    f"MIP SDK CLI failed (exit {returncode}) decrypting {filename!r}: "
                    f"{stderr or '<no stderr>'}"
                )
            if not out_path.exists():
                raise MipSdkExecutionError(
                    f"MIP SDK CLI reported success but wrote no output for {filename!r}."
                )
            return out_path.read_bytes()


def build_runner(
    config: MipSdkConfig, credential: TokenCredential | None
) -> SubprocessMipSdkRunner | None:
    """Return a runner when both a CLI and credential are available, else ``None``.

    A ``None`` result means MIP decryption, if enabled, will fail loudly with a
    clear "not configured" error rather than silently passing the payload
    through.
    """

    if not config.is_configured or credential is None:
        return None
    return SubprocessMipSdkRunner(config, credential)

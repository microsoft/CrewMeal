from __future__ import annotations

import email.utils
import time
from collections.abc import Callable, Collection, Mapping
from datetime import datetime, timezone
from typing import Any

import requests
from azure.core.credentials import TokenCredential
from azure.identity import ClientSecretCredential

from crewmeal.search_enhancement.config import SearchEnhancementConfig


GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_DELAY_SECONDS = 120.0


class GraphRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retryable = retryable


class GraphClient:
    def __init__(
        self,
        config: SearchEnhancementConfig,
        *,
        session: requests.Session | None = None,
        credential: TokenCredential | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._session = session or requests.Session()
        self._owns_session = session is None
        self._credential = credential or ClientSecretCredential(
            tenant_id=config.tenant_id,
            client_id=config.client_id,
            client_secret=config.client_secret,
        )
        self._owns_credential = credential is None
        self._sleeper = sleeper

    def close(self) -> None:
        if self._owns_session:
            self._session.close()
        close = getattr(self._credential, "close", None)
        if self._owns_credential and callable(close):
            close()

    def __enter__(self) -> "GraphClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def request(
        self,
        method: str,
        path_or_url: str,
        *,
        expected: Collection[int],
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        data: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 120,
    ) -> requests.Response:
        url = (
            path_or_url
            if path_or_url.startswith(("https://", "http://"))
            else f"{GRAPH_ROOT}/{path_or_url.lstrip('/')}"
        )
        delay = 1.0
        last_transport_error: requests.RequestException | None = None

        for attempt in range(1, self._config.graph_max_attempts + 1):
            token = self._credential.get_token(GRAPH_SCOPE).token
            request_headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                **dict(headers or {}),
            }
            try:
                response = self._session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    data=data,
                    headers=request_headers,
                    timeout=timeout,
                )
            except requests.RequestException as exc:
                last_transport_error = exc
                if attempt == self._config.graph_max_attempts:
                    break
                self._sleeper(delay)
                delay = min(delay * 2, 30)
                continue

            if response.status_code in expected:
                return response
            if (
                response.status_code not in RETRYABLE_STATUS_CODES
                or attempt == self._config.graph_max_attempts
            ):
                raise _response_error(response)

            wait_seconds = _retry_after_seconds(response.headers.get("Retry-After"))
            if response.raw is not None:
                response.close()
            self._sleeper(
                min(
                    wait_seconds if wait_seconds is not None else delay,
                    MAX_RETRY_DELAY_SECONDS,
                )
            )
            delay = min(delay * 2, 30)

        raise GraphRequestError(
            "Microsoft Graph request failed after transport retries.",
            code=type(last_transport_error).__name__ if last_transport_error else None,
            retryable=True,
        ) from last_transport_error

    def get_json(
        self,
        path_or_url: str,
        *,
        params: Mapping[str, Any] | None = None,
        expected: Collection[int] = (200,),
    ) -> dict[str, Any]:
        response = self.request(
            "GET",
            path_or_url,
            expected=expected,
            params=params,
        )
        value = response.json()
        if not isinstance(value, dict):
            raise GraphRequestError("Microsoft Graph returned a non-object JSON body.")
        return value

    def send_json(
        self,
        method: str,
        path_or_url: str,
        *,
        body: Mapping[str, Any],
        expected: Collection[int],
    ) -> dict[str, Any] | None:
        response = self.request(
            method,
            path_or_url,
            expected=expected,
            json_body=body,
        )
        if response.status_code == 204 or not response.content:
            return None
        value = response.json()
        if not isinstance(value, dict):
            raise GraphRequestError("Microsoft Graph returned a non-object JSON body.")
        return value

    def send_bytes(
        self,
        method: str,
        path_or_url: str,
        *,
        body: bytes,
        expected: Collection[int],
    ) -> dict[str, Any] | None:
        response = self.request(
            method,
            path_or_url,
            expected=expected,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        if response.status_code == 204 or not response.content:
            return None
        value = response.json()
        if not isinstance(value, dict):
            raise GraphRequestError("Microsoft Graph returned a non-object JSON body.")
        return value

    def get_bytes(self, path_or_url: str) -> bytes:
        response = self.request(
            "GET",
            path_or_url,
            expected=(200,),
            headers={"Accept": "application/octet-stream"},
            timeout=300,
        )
        return response.content


def _response_error(response: requests.Response) -> GraphRequestError:
    code: str | None = None
    try:
        value = response.json()
    except requests.JSONDecodeError:
        value = None
    if isinstance(value, dict):
        error = value.get("error")
        if isinstance(error, dict):
            raw_code = error.get("code")
            code = str(raw_code) if raw_code else None
    return GraphRequestError(
        f"Microsoft Graph returned HTTP {response.status_code}"
        + (f" ({code})" if code else "")
        + ".",
        status_code=response.status_code,
        code=code,
        retryable=response.status_code in RETRYABLE_STATUS_CODES,
    )


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(float(value), 0)
    except ValueError:
        try:
            retry_at = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max((retry_at - datetime.now(timezone.utc)).total_seconds(), 0)

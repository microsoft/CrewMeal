from __future__ import annotations

import pytest

from crewmeal.config import ConfigurationError
from crewmeal.search_enhancement.web.auth import (
    EntraTokenValidator,
    NullTokenValidator,
    create_token_validator,
)
from crewmeal.search_enhancement.web.config import WebConfig


def _config(**overrides: object) -> WebConfig:
    base = {
        "base_url": "https://status.example",
        "session_secret": "test-secret",
        "require_ingest_auth": True,
        "ingest_tenant_id": "tenant",
        "ingest_audience": "api://crewmeal-ingest-poc",
    }
    base.update(overrides)
    return WebConfig(**base)  # type: ignore[arg-type]


def test_disabled_auth_returns_null_validator() -> None:
    validator = create_token_validator(_config(require_ingest_auth=False))
    assert isinstance(validator, NullTokenValidator)
    assert validator.requires_bearer is False


def test_single_audience_is_parsed() -> None:
    validator = create_token_validator(_config())
    assert isinstance(validator, EntraTokenValidator)
    assert validator._audiences == ["api://crewmeal-ingest-poc"]


def test_csv_audiences_accept_uri_and_guid() -> None:
    validator = create_token_validator(
        _config(
            ingest_audience=" api://crewmeal-ingest-poc , 01154558-3346-47a4-9b8e-a32bf5a7e473 "
        )
    )
    assert isinstance(validator, EntraTokenValidator)
    assert validator._audiences == [
        "api://crewmeal-ingest-poc",
        "01154558-3346-47a4-9b8e-a32bf5a7e473",
    ]


def test_missing_audience_raises() -> None:
    with pytest.raises(ConfigurationError):
        create_token_validator(_config(ingest_audience=""))


def test_missing_tenant_raises() -> None:
    with pytest.raises(ConfigurationError):
        create_token_validator(_config(ingest_tenant_id=None))

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from crewmeal.config import ConfigurationError


DEFAULT_CONNECTION_ID = "crewpptsearchpoc"
DEFAULT_ICON_URL = (
    "https://res-1.cdn.office.net/files/fabric/assets/item-types/96/pptx.png"
)


@dataclass(frozen=True, slots=True)
class SearchEnhancementConfig:
    tenant_id: str
    client_id: str
    client_secret: str = field(repr=False)
    site_id: str
    drive_id: str
    list_id: str
    site_url: str
    connection_id: str = DEFAULT_CONNECTION_ID
    sqlite_path: Path = Path(".crewmeal/search-enhancement.db")
    icon_url: str = DEFAULT_ICON_URL
    command_poll_seconds: int = 10
    reconciliation_seconds: int = 300
    job_lease_seconds: int = 900
    graph_max_attempts: int = 5
    db_retry_initial_seconds: int = 2
    db_retry_max_seconds: int = 60

    @classmethod
    def from_environment(cls) -> "SearchEnhancementConfig":
        required = {
            "tenant_id": "CREWMEAL_M365_TENANT_ID",
            "client_id": "CREWMEAL_M365_CLIENT_ID",
            "client_secret": "CREWMEAL_M365_CLIENT_SECRET",
            "site_id": "CREWMEAL_M365_SITE_ID",
            "drive_id": "CREWMEAL_M365_DRIVE_ID",
            "list_id": "CREWMEAL_M365_LIST_ID",
            "site_url": "CREWMEAL_M365_SITE_URL",
        }
        values = {field_name: os.getenv(name, "").strip() for field_name, name in required.items()}
        missing = [
            environment_name
            for field_name, environment_name in required.items()
            if not values[field_name]
        ]
        if missing:
            raise ConfigurationError(
                "Missing Microsoft 365 settings: " + ", ".join(sorted(missing))
            )

        return cls(
            **values,
            connection_id=os.getenv(
                "CREWMEAL_M365_CONNECTION_ID", DEFAULT_CONNECTION_ID
            ).strip()
            or DEFAULT_CONNECTION_ID,
            sqlite_path=Path(
                os.getenv(
                    "CREWMEAL_SEARCH_DB",
                    ".crewmeal/search-enhancement.db",
                )
            ).expanduser(),
            icon_url=os.getenv("CREWMEAL_PPT_ICON_URL", DEFAULT_ICON_URL).strip()
            or DEFAULT_ICON_URL,
            command_poll_seconds=_positive_environment(
                "CREWMEAL_COMMAND_POLL_SECONDS", 10
            ),
            reconciliation_seconds=_positive_environment(
                "CREWMEAL_RECONCILIATION_SECONDS", 300
            ),
            job_lease_seconds=_positive_environment(
                "CREWMEAL_JOB_LEASE_SECONDS", 900
            ),
            graph_max_attempts=_positive_environment(
                "CREWMEAL_GRAPH_MAX_ATTEMPTS", 5
            ),
            db_retry_initial_seconds=_positive_environment(
                "CREWMEAL_DB_RETRY_INITIAL_SECONDS", 2
            ),
            db_retry_max_seconds=_positive_environment(
                "CREWMEAL_DB_RETRY_MAX_SECONDS", 60
            ),
        )


def _positive_environment(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a positive integer.") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be a positive integer.")
    return value

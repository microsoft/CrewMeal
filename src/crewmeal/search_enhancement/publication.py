from __future__ import annotations

from enum import StrEnum
from typing import Any, Mapping


class PublicationTarget(StrEnum):
    UNSET = "unset"
    SHAREPOINT_COLUMN = "sharepoint_column"
    COPILOT_CONNECTOR = "copilot_connector"


DEFAULT_COLUMN_INTERNAL_NAME = "CrewmealSearchContent"
DEFAULT_COLUMN_DISPLAY_NAME = "CrewMeal 검색 콘텐츠"
COLUMN_DISPLAY_NAME_SETTING = "publication.column.display_name"
SHAREPOINT_COLUMN_MAX_CHARACTERS = 63_999


def parse_publication_target(value: Any) -> PublicationTarget:
    try:
        return PublicationTarget(str(value))
    except ValueError as exc:
        choices = ", ".join(target.value for target in PublicationTarget)
        raise ValueError(
            f"Unsupported publication target {value!r}; expected one of {choices}."
        ) from exc


def validate_column_display_name(value: Any) -> str:
    display_name = str(value).strip()
    if not display_name:
        raise ValueError("Column display name must not be empty.")
    if len(display_name) > 255:
        raise ValueError("Column display name must be 255 characters or fewer.")
    return display_name


def publication_column_display_name(settings: Mapping[str, Any]) -> str:
    value = settings.get(
        COLUMN_DISPLAY_NAME_SETTING,
        DEFAULT_COLUMN_DISPLAY_NAME,
    )
    return validate_column_display_name(value)

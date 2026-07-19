"""Request/response models for the ingest API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class IngestItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    list_item_id: str = Field(alias="listItemId", min_length=1)
    # The server resolves these from Graph using ``listItemId``; clients may send
    # them as hints/fallback (used by the upload playground and tests).
    drive_item_id: str | None = Field(default=None, alias="driveItemId")
    file_name: str | None = Field(default=None, alias="fileName")
    web_url: str | None = Field(default=None, alias="webUrl")
    list_item_unique_id: str | None = Field(default=None, alias="listItemUniqueId")


class IngestRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    command: Literal["Enhance", "Remove"]
    request_id: str = Field(alias="requestId", min_length=1)
    item: IngestItem
    # Optional client-supplied context, recorded for traceability but not trusted
    # for the document key (the server uses its configured site/drive/tenant).
    site_id: str | None = Field(default=None, alias="siteId")
    drive_id: str | None = Field(default=None, alias="driveId")
    list_id: str | None = Field(default=None, alias="listId")


class IngestResponse(BaseModel):
    request_id: str = Field(serialization_alias="requestId")
    status_token: str = Field(serialization_alias="statusToken")
    status_url: str = Field(serialization_alias="statusUrl")
    status: str
    job_type: str = Field(serialization_alias="jobType")

    model_config = ConfigDict(populate_by_name=True)

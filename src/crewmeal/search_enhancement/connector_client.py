from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

from crewmeal.search_enhancement.acl_mapper import ConnectorAcl
from crewmeal.search_enhancement.config import SearchEnhancementConfig
from crewmeal.search_enhancement.graph_client import GraphClient, GraphRequestError
from crewmeal.search_enhancement.models import RenderedHtml


MAX_EXTERNAL_ITEM_REQUEST_BYTES = 4_000_000
ENHANCEMENT_VERSION = "1"
SCHEMA_PROPERTY_NAMES = frozenset(
    {
        "title",
        "url",
        "iconUrl",
        "fileName",
        "fileExtension",
        "lastModifiedDateTime",
        "lastModifiedBy",
        "slideTitles",
        "keywords",
        "sourceItemId",
        "sourceETag",
        "enhancementVersion",
    }
)


class ExternalItemError(RuntimeError):
    """Raised when an external item cannot satisfy the connector contract."""


class ExternalItemTooLargeError(ExternalItemError):
    """Raised before Graph receives an oversized external item request."""


@dataclass(frozen=True, slots=True)
class PreparedExternalItem:
    item_id: str
    source_url: str
    indexed_url: str
    body: dict[str, Any]
    request_bytes: int


class ConnectorClient:
    def __init__(
        self,
        config: SearchEnhancementConfig,
        graph: GraphClient,
    ) -> None:
        self._config = config
        self._graph = graph

    def prepare_item(
        self,
        *,
        drive_item: dict[str, Any],
        rendered: RenderedHtml,
        acl: tuple[ConnectorAcl, ...],
        source_fingerprint: str,
    ) -> PreparedExternalItem:
        item_id = external_item_id(drive_item)
        source_url = _required_string(drive_item, "webUrl")
        indexed_url = resolver_url(source_url, item_id)
        name = _required_string(drive_item, "name")
        modified_by = _modified_by(drive_item)
        properties = {
            "title": name,
            "url": indexed_url,
            "iconUrl": self._config.icon_url,
            "fileName": name,
            "fileExtension": "pptx",
            "lastModifiedDateTime": _required_string(
                drive_item, "lastModifiedDateTime"
            ),
            "lastModifiedBy": modified_by,
            "slideTitles@odata.type": "Collection(String)",
            "slideTitles": list(_metadata_values(rendered.slide_titles)),
            "keywords@odata.type": "Collection(String)",
            "keywords": list(_metadata_values(rendered.keywords)),
            "sourceItemId": _required_string(drive_item, "id"),
            "sourceETag": _required_value(
                source_fingerprint,
                code="SOURCE_FINGERPRINT_MISSING",
            ),
            "enhancementVersion": ENHANCEMENT_VERSION,
        }
        body = {
            "acl": [entry.as_dict() for entry in acl],
            "properties": properties,
            "content": {
                "type": "html",
                "value": rendered.content,
            },
        }
        payload = _json_bytes(body)
        if len(payload) >= MAX_EXTERNAL_ITEM_REQUEST_BYTES:
            raise ExternalItemTooLargeError(
                f"EXTERNAL_ITEM_TOO_LARGE: request is {len(payload):,} bytes; "
                f"limit is below {MAX_EXTERNAL_ITEM_REQUEST_BYTES:,} bytes."
            )
        return PreparedExternalItem(
            item_id=item_id,
            source_url=source_url,
            indexed_url=indexed_url,
            body=body,
            request_bytes=len(payload),
        )

    def upsert(self, item: PreparedExternalItem) -> dict[str, Any] | None:
        return self._graph.send_bytes(
            "PUT",
            self._item_path(item.item_id),
            body=_json_bytes(item.body),
            expected=(200, 201),
        )

    def update_acl(
        self,
        item_id: str,
        acl: tuple[ConnectorAcl, ...],
    ) -> dict[str, Any] | None:
        current = self._current_item(item_id)
        return self._graph.send_json(
            "PUT",
            self._item_path(item_id),
            body={
                "acl": [entry.as_dict() for entry in acl],
                "properties": _with_collection_types(
                    _current_schema_properties(current)
                ),
                "content": _current_content(current),
            },
            expected=(200,),
        )

    def update_properties(
        self,
        item_id: str,
        properties: dict[str, Any],
    ) -> dict[str, Any] | None:
        unknown = set(properties) - SCHEMA_PROPERTY_NAMES
        if unknown:
            raise ExternalItemError(
                "EXTERNAL_ITEM_PROPERTIES_INVALID: unsupported properties: "
                + ", ".join(sorted(unknown))
                + "."
            )
        current = self._current_item(item_id)
        merged = {**_current_schema_properties(current), **properties}
        return self._graph.send_json(
            "PUT",
            self._item_path(item_id),
            body={
                "acl": _current_acl(current),
                "properties": _with_collection_types(merged),
                "content": _current_content(current),
            },
            expected=(200,),
        )

    def _current_item(self, item_id: str) -> dict[str, Any]:
        current = self.get(item_id)
        if current is None:
            raise ExternalItemError(
                "EXTERNAL_ITEM_NOT_FOUND: cannot update item."
            )
        return current

    def delete(self, item_id: str) -> None:
        try:
            self._graph.request(
                "DELETE",
                self._item_path(item_id),
                expected=(200, 204),
            )
        except GraphRequestError as exc:
            if exc.status_code != 404:
                raise

    def get(self, item_id: str) -> dict[str, Any] | None:
        try:
            return self._graph.get_json(self._item_path(item_id))
        except GraphRequestError as exc:
            if exc.status_code == 404:
                return None
            raise

    def _item_path(self, item_id: str) -> str:
        return (
            f"/external/connections/{self._config.connection_id}/items/{item_id}"
        )


def external_item_id(drive_item: dict[str, Any]) -> str:
    sharepoint_ids = drive_item.get("sharepointIds")
    if not isinstance(sharepoint_ids, dict):
        raise ExternalItemError("SOURCE_ID_MISSING: sharepointIds is unavailable.")
    unique_id = sharepoint_ids.get("listItemUniqueId")
    try:
        return UUID(str(unique_id)).hex
    except (ValueError, TypeError, AttributeError) as exc:
        raise ExternalItemError(
            "SOURCE_ID_INVALID: listItemUniqueId must be a GUID."
        ) from exc


def resolver_url(web_url: str, item_id: str) -> str:
    parsed = urlsplit(web_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ExternalItemError("SOURCE_URL_INVALID: webUrl must be HTTPS.")
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key != "crewmealItemId"
    ]
    query.append(("crewmealItemId", item_id))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def _modified_by(drive_item: dict[str, Any]) -> str:
    identity_set = drive_item.get("lastModifiedBy")
    if not isinstance(identity_set, dict):
        raise ExternalItemError("SOURCE_MODIFIER_MISSING: lastModifiedBy is unavailable.")
    for identity_type in ("user", "application", "device"):
        identity = identity_set.get(identity_type)
        if not isinstance(identity, dict):
            continue
        for key in ("displayName", "email", "id"):
            value = identity.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    raise ExternalItemError(
        "SOURCE_MODIFIER_INVALID: lastModifiedBy has no usable identity."
    )


def _required_string(value: dict[str, Any], key: str) -> str:
    raw = value.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise ExternalItemError(f"SOURCE_PROPERTY_MISSING: {key}.")
    return raw.strip()


def _required_value(value: str, *, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExternalItemError(f"{code}: value is empty.")
    return value.strip()


def _json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _with_collection_types(properties: dict[str, Any]) -> dict[str, Any]:
    typed = dict(properties)
    for name in ("slideTitles", "keywords"):
        if isinstance(typed.get(name), list):
            typed[f"{name}@odata.type"] = "Collection(String)"
    return typed


def _current_schema_properties(current: dict[str, Any]) -> dict[str, Any]:
    raw = current.get("properties")
    if not isinstance(raw, dict):
        raise ExternalItemError(
            "EXTERNAL_ITEM_PROPERTIES_INVALID: property bag is unavailable."
        )
    properties = {
        name: raw[name]
        for name in SCHEMA_PROPERTY_NAMES
        if name in raw
    }
    if not properties:
        raise ExternalItemError(
            "EXTERNAL_ITEM_PROPERTIES_INVALID: property bag has no schema fields."
        )
    return properties


def _current_acl(current: dict[str, Any]) -> list[dict[str, str]]:
    raw = current.get("acl")
    if not isinstance(raw, list) or not raw:
        raise ExternalItemError(
            "EXTERNAL_ITEM_ACL_INVALID: ACL is unavailable."
        )
    acl: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ExternalItemError(
                "EXTERNAL_ITEM_ACL_INVALID: ACL entry is not an object."
            )
        normalized = {
            name: str(entry.get(name) or "").strip()
            for name in ("type", "value", "accessType")
        }
        if not all(normalized.values()):
            raise ExternalItemError(
                "EXTERNAL_ITEM_ACL_INVALID: ACL entry is incomplete."
            )
        acl.append(normalized)
    return acl


def _current_content(current: dict[str, Any]) -> dict[str, str]:
    raw = current.get("content")
    value = raw.get("value") if isinstance(raw, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise ExternalItemError(
            "EXTERNAL_ITEM_CONTENT_INVALID: indexed content is unavailable."
        )
    return {"type": "html", "value": value}


def _metadata_values(
    values: tuple[str, ...],
    *,
    max_values: int = 256,
    max_characters: int = 512,
) -> tuple[str, ...]:
    return tuple(value[:max_characters] for value in values[:max_values])

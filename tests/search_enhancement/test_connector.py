from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
import requests

from crewmeal.search_enhancement.acl_mapper import (
    ConnectorAcl,
    UnsupportedAclError,
    acl_hash,
    map_drive_item_permissions,
)
from crewmeal.search_enhancement.config import SearchEnhancementConfig
from crewmeal.search_enhancement.connector_client import (
    MAX_EXTERNAL_ITEM_REQUEST_BYTES,
    ConnectorClient,
    ExternalItemTooLargeError,
    external_item_id,
)
from crewmeal.search_enhancement.graph_client import GraphClient
from crewmeal.search_enhancement.models import RenderedHtml


USER_ID = "11111111-1111-4111-8111-111111111111"
GROUP_ID = "22222222-2222-4222-8222-222222222222"
ITEM_GUID = "33333333-3333-4333-8333-333333333333"


def _config(**overrides: Any) -> SearchEnhancementConfig:
    values: dict[str, Any] = {
        "tenant_id": "tenant",
        "client_id": "client",
        "client_secret": "secret",
        "site_id": "site",
        "drive_id": "drive",
        "list_id": "list",
        "site_url": "https://tenant.sharepoint.com/sites/test",
        "graph_max_attempts": 3,
    }
    values.update(overrides)
    return SearchEnhancementConfig(**values)


def _drive_item() -> dict[str, Any]:
    return {
        "id": "drive-item-id",
        "name": "실적 보고서.pptx",
        "webUrl": "https://tenant.sharepoint.com/sites/test/Documents/report.pptx?web=1",
        "cTag": '"ctag,1"',
        "eTag": '"etag,1"',
        "lastModifiedDateTime": "2026-07-17T00:00:00Z",
        "lastModifiedBy": {"user": {"id": USER_ID, "displayName": "Test User"}},
        "sharepointIds": {"listItemUniqueId": ITEM_GUID},
        "file": {
            "mimeType": (
                "application/vnd.openxmlformats-officedocument."
                "presentationml.presentation"
            ),
            "hashes": {"quickXorHash": "source-content-hash"},
        },
    }


def _rendered(content: str = "<article><h1>보고서</h1></article>") -> RenderedHtml:
    return RenderedHtml(
        content=content,
        byte_count=len(content.encode("utf-8")),
        sha256="hash",
        slide_titles=("개요",),
        keywords=("매출",),
    )


class FakeGraph:
    def __init__(self) -> None:
        self.byte_calls: list[tuple[str, str, bytes]] = []
        self.json_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.deleted: list[str] = []
        self.current_item: dict[str, Any] = {
            "properties": {
                "title": "Old title",
                "iconUrl": "https://example/icon.png",
                "slideTitles": ["Slide 1"],
                "keywords": ["keyword"],
                "sourceItemId": "source",
                "IsDGBasedSecurityEnabled": True,
            },
            "acl": [
                {
                    "type": "user",
                    "value": USER_ID,
                    "accessType": "grant",
                }
            ],
            "content": {
                "type": "text",
                "value": "<article><h1>Existing content</h1></article>",
            },
        }

    def send_bytes(
        self, method: str, path: str, *, body: bytes, expected: tuple[int, ...]
    ) -> dict[str, Any]:
        assert expected == (200, 201)
        self.byte_calls.append((method, path, body))
        return {"id": path.rsplit("/", 1)[-1]}

    def send_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any],
        expected: tuple[int, ...],
    ) -> None:
        assert expected == (200,)
        self.json_calls.append((method, path, body))

    def request(self, method: str, path: str, *, expected: tuple[int, ...]) -> None:
        assert expected == (200, 204)
        self.deleted.append(path)

    def get_json(self, path: str) -> dict[str, Any]:
        return self.current_item


def test_acl_mapping_is_deterministic_and_fails_closed() -> None:
    permissions = [
        {
            "id": "g",
            "grantedToV2": {"group": {"id": GROUP_ID}},
            "grantedTo": {"user": {"id": GROUP_ID}},
        },
        {"id": "u", "grantedToV2": {"user": {"id": USER_ID}}},
        {"id": "duplicate", "grantedTo": {"user": {"id": USER_ID}}},
    ]
    mapped = map_drive_item_permissions(permissions)

    assert mapped == (
        ConnectorAcl(type="group", value=GROUP_ID),
        ConnectorAcl(type="user", value=USER_ID),
    )
    assert acl_hash(mapped) == acl_hash(tuple(reversed(mapped)))

    with pytest.raises(UnsupportedAclError, match="ACL_UNSUPPORTED"):
        map_drive_item_permissions(
            [
                *permissions,
                {"id": "link", "link": {"scope": "anonymous", "type": "view"}},
            ]
        )


def test_external_item_uses_stable_guid_resolver_url_and_size_preflight() -> None:
    graph = FakeGraph()
    client = ConnectorClient(_config(), graph)  # type: ignore[arg-type]
    acl = (ConnectorAcl(type="user", value=USER_ID),)

    prepared = client.prepare_item(
        drive_item=_drive_item(),
        rendered=_rendered(),
        acl=acl,
        source_fingerprint="pptx-sha256:source-content-hash",
    )
    assert prepared.item_id == UUID(ITEM_GUID).hex
    assert prepared.source_url.endswith("report.pptx?web=1")
    assert prepared.indexed_url.endswith(
        f"web=1&crewmealItemId={UUID(ITEM_GUID).hex}"
    )
    assert prepared.body["content"]["type"] == "html"
    assert prepared.body["properties"]["url"] == prepared.indexed_url
    assert prepared.body["properties"]["title"] == "실적 보고서.pptx"
    assert (
        prepared.body["properties"]["sourceETag"]
        == "pptx-sha256:source-content-hash"
    )
    assert (
        prepared.body["properties"]["slideTitles@odata.type"]
        == "Collection(String)"
    )
    assert prepared.request_bytes < MAX_EXTERNAL_ITEM_REQUEST_BYTES

    client.upsert(prepared)
    method, path, payload = graph.byte_calls[0]
    assert method == "PUT"
    assert path.endswith(prepared.item_id)
    assert json.loads(payload)["acl"][0]["type"] == "user"

    replacement_acl = (ConnectorAcl(type="group", value=GROUP_ID),)
    client.update_acl(prepared.item_id, replacement_acl)
    acl_update = graph.json_calls[-1][2]
    assert acl_update["acl"] == [
        {"type": "group", "value": GROUP_ID, "accessType": "grant"}
    ]
    assert acl_update["properties"]["title"] == "Old title"
    assert "IsDGBasedSecurityEnabled" not in acl_update["properties"]
    assert acl_update["content"] == {
        "type": "html",
        "value": "<article><h1>Existing content</h1></article>",
    }
    assert (
        acl_update["properties"]["slideTitles@odata.type"]
        == "Collection(String)"
    )

    client.update_properties(
        prepared.item_id,
        {"title": "New title", "url": prepared.indexed_url},
    )
    merged = graph.json_calls[-1][2]["properties"]
    assert merged["title"] == "New title"
    assert merged["iconUrl"] == "https://example/icon.png"
    assert merged["slideTitles"] == ["Slide 1"]
    assert merged["slideTitles@odata.type"] == "Collection(String)"
    assert graph.json_calls[-1][2]["acl"] == graph.current_item["acl"]
    assert graph.json_calls[-1][2]["content"]["value"].startswith("<article>")

    with pytest.raises(ExternalItemTooLargeError, match="EXTERNAL_ITEM_TOO_LARGE"):
        client.prepare_item(
            drive_item=_drive_item(),
            rendered=_rendered("x" * MAX_EXTERNAL_ITEM_REQUEST_BYTES),
            acl=acl,
            source_fingerprint="pptx-sha256:source-content-hash",
        )


class FakeCredential:
    def get_token(self, *scopes: str, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(token="token")


class RetrySession:
    def __init__(self) -> None:
        self.calls = 0

    def request(self, *args: Any, **kwargs: Any) -> requests.Response:
        self.calls += 1
        response = requests.Response()
        response.status_code = 429 if self.calls == 1 else 200
        response.headers["Retry-After"] = "2"
        response._content = b'{"value":"ok"}'
        return response


def test_graph_client_honors_retry_after() -> None:
    session = RetrySession()
    waits: list[float] = []
    graph = GraphClient(
        _config(),
        session=session,  # type: ignore[arg-type]
        credential=FakeCredential(),  # type: ignore[arg-type]
        sleeper=waits.append,
    )
    result = graph.get_json("/test")

    assert result == {"value": "ok"}
    assert session.calls == 2
    assert waits == [2]


def test_external_item_id_requires_sharepoint_unique_guid() -> None:
    with pytest.raises(Exception, match="SOURCE_ID_MISSING"):
        external_item_id({"id": "only-drive-id"})

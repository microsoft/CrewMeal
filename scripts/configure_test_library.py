from __future__ import annotations

import argparse
from html import escape as xml_escape
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from crewmeal.search_enhancement.config import SearchEnhancementConfig
from crewmeal.search_enhancement.graph_client import GraphClient
from crewmeal.search_enhancement.publication import (
    DEFAULT_COLUMN_DISPLAY_NAME,
    DEFAULT_COLUMN_INTERNAL_NAME,
)


ROOT = Path(__file__).resolve().parents[1]
FORMATTER_PATH = ROOT / "sharepoint" / "status-column-formatting.json"
DEFAULT_VIEW_EXCLUDED_FIELDS = (
    "CrewmealSearchEnabled",
    "CrewmealSearchCommand",
    "CrewmealSearchRequestId",
    "CrewmealSearchRequestedAt",
    "CrewmealSearchProcessedAt",
    "CrewmealSearchStatus",
)

COLUMNS: tuple[dict[str, Any], ...] = (
    {
        "name": "CrewmealSearchEnabled",
        "displayName": "검색강화 사용",
        "description": "검색강화의 원하는 최종 활성 상태",
        "hidden": True,
        "boolean": {},
        "defaultValue": {"value": "false"},
    },
    {
        "name": "CrewmealSearchCommand",
        "displayName": "검색강화 명령",
        "hidden": True,
        "choice": {
            "allowTextEntry": False,
            "displayAs": "dropDownMenu",
            "choices": ["Enhance", "Remove"],
        },
    },
    {
        "name": "CrewmealSearchRequestId",
        "displayName": "검색강화 요청 ID",
        "hidden": True,
        "text": {"allowMultipleLines": False, "maxLength": 64},
    },
    {
        "name": "CrewmealSearchStatus",
        "displayName": "Copilot 검색강화",
        "description": "Copilot 커넥터 검색강화 처리 상태",
        "hidden": False,
        "choice": {
            "allowTextEntry": False,
            "displayAs": "dropDownMenu",
            "choices": [
                "NotEnabled",
                "Queued",
                "Processing",
                "Ready",
                "Stale",
                "Removing",
                "Failed",
            ],
        },
        "defaultValue": {"value": "NotEnabled"},
    },
    {
        "name": "CrewmealSearchRequestedAt",
        "displayName": "검색강화 요청 시각",
        "hidden": True,
        "dateTime": {"displayAs": "default", "format": "dateTime"},
    },
    {
        "name": "CrewmealSearchProcessedAt",
        "displayName": "검색강화 처리 시각",
        "hidden": True,
        "dateTime": {"displayAs": "default", "format": "dateTime"},
    },
    {
        "name": "CrewmealSearchMessage",
        "displayName": "검색강화 메시지",
        "hidden": False,
        "text": {"allowMultipleLines": False, "maxLength": 255},
    },
)

CONTENT_SITE_COLUMN: dict[str, Any] = {
    "name": DEFAULT_COLUMN_INTERNAL_NAME,
    "displayName": DEFAULT_COLUMN_DISPLAY_NAME,
    "description": "CrewMeal이 생성한 검색용 구조화 Markdown",
    "columnGroup": "CrewMeal",
    "hidden": False,
    "indexed": False,
    "text": {
        "allowMultipleLines": True,
        "appendChangesToExistingText": False,
        "linesForEditing": 12,
        "textType": "plain",
    },
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Provision Crewmeal search-enhancement columns and formatting."
    )
    parser.add_argument(
        "--apply-formatting",
        action="store_true",
        help=(
            "Apply SharePoint column formatting using the short-lived token in "
            "CREWMEAL_M365_SHAREPOINT_ACCESS_TOKEN."
        ),
    )
    parser.add_argument(
        "--apply-content-column",
        action="store_true",
        help=(
            "Attach and normalize the content site column using the short-lived "
            "token in CREWMEAL_M365_SHAREPOINT_ACCESS_TOKEN."
        ),
    )
    args = parser.parse_args()

    config = SearchEnhancementConfig.from_environment()
    with GraphClient(config) as graph:
        created = _ensure_columns(graph, config)
        content_column = _ensure_content_site_column(graph, config)
    if args.apply_formatting or args.apply_content_column:
        token = os.environ.get("CREWMEAL_M365_SHAREPOINT_ACCESS_TOKEN")
        if not token:
            parser.error(
                "--apply-formatting/--apply-content-column requires "
                "CREWMEAL_M365_SHAREPOINT_ACCESS_TOKEN"
            )
    if args.apply_content_column:
        content_column.update(
            _configure_sharepoint_content_column(
                config,
                token,
                field_id=str(content_column["id"]),
                display_name=str(content_column["displayName"]),
            )
        )
    if args.apply_formatting:
        _configure_sharepoint_formatting(config, token)
    print(
        json.dumps(
            {
                "createdColumns": created,
                "contentSiteColumn": content_column,
                "formattingApplied": args.apply_formatting,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _ensure_columns(
    graph: GraphClient,
    config: SearchEnhancementConfig,
) -> list[str]:
    path = f"/sites/{config.site_id}/lists/{config.list_id}/columns"
    page = graph.get_json(
        path,
        params={"$select": "id,name,hidden", "$top": "200"},
    )
    existing = {
        str(column.get("name"))
        for column in page.get("value", [])
        if isinstance(column, dict)
    }
    created: list[str] = []
    for column in COLUMNS:
        name = str(column["name"])
        if name in existing:
            continue
        graph.send_json("POST", path, body=column, expected=(201,))
        created.append(name)
    return created


def _ensure_content_site_column(
    graph: GraphClient,
    config: SearchEnhancementConfig,
    *,
    display_name: str = DEFAULT_COLUMN_DISPLAY_NAME,
) -> dict[str, Any]:
    definition = {
        **CONTENT_SITE_COLUMN,
        "displayName": display_name,
    }
    site_path = f"/sites/{config.site_id}/columns"
    site_columns = graph.get_json(
        site_path,
        params={
            "$select": "id,name,displayName,hidden,indexed,text,columnGroup",
            "$top": "200",
        },
    )
    site_column = _find_column(site_columns, DEFAULT_COLUMN_INTERNAL_NAME)
    created = False
    renamed = False
    if site_column is None:
        value = graph.send_json(
            "POST",
            site_path,
            body=definition,
            expected=(201,),
        )
        if not isinstance(value, dict):
            raise RuntimeError("Graph did not return the created site column.")
        site_column = value
        created = True
    else:
        _validate_content_column(site_column)

    updates: dict[str, Any] = {}
    if str(site_column.get("displayName") or "") != display_name:
        updates["displayName"] = display_name
        renamed = True
    if updates:
        value = graph.send_json(
            "PATCH",
            f"{site_path}/{site_column['id']}",
            body=updates,
            expected=(200,),
        )
        if isinstance(value, dict):
            site_column = value
        else:
            site_column = {**site_column, **updates}

    refreshed = graph.get_json(
        f"{site_path}/{site_column['id']}",
        params={
            "$select": "id,name,displayName,hidden,indexed,text,columnGroup",
        },
    )
    site_column = refreshed
    _validate_content_column(site_column)

    list_path = f"/sites/{config.site_id}/lists/{config.list_id}/columns"
    list_columns = graph.get_json(
        list_path,
        params={
            "$select": "id,name,displayName,hidden,indexed,text",
            "$top": "200",
        },
    )
    list_column = _find_column(list_columns, DEFAULT_COLUMN_INTERNAL_NAME)
    attached = list_column is not None
    if list_column is not None:
        _validate_content_column(list_column)
        if str(list_column.get("id") or "").lower() != str(
            site_column.get("id") or ""
        ).lower():
            raise RuntimeError(
                "CONTENT_COLUMN_SOURCE_MISMATCH: the library column does not use "
                "the CrewMeal site-column definition."
            )

    return {
        "id": str(site_column["id"]),
        "name": DEFAULT_COLUMN_INTERNAL_NAME,
        "displayName": display_name,
        "appendChangesToExistingText": bool(
            site_column.get("text", {}).get("appendChangesToExistingText")
        ),
        "created": created,
        "attached": attached,
        "renamed": renamed,
    }


def _find_column(
    page: dict[str, Any],
    name: str,
) -> dict[str, Any] | None:
    for column in page.get("value", []):
        if isinstance(column, dict) and str(column.get("name") or "") == name:
            return column
    return None


def _validate_content_column(column: dict[str, Any]) -> None:
    text = column.get("text")
    if not isinstance(text, dict):
        raise RuntimeError(
            "CONTENT_COLUMN_TYPE_MISMATCH: expected a text site column."
        )
    if text.get("allowMultipleLines") is not True:
        raise RuntimeError(
            "CONTENT_COLUMN_TYPE_MISMATCH: expected multiple lines of text."
        )
    text_type = str(text.get("textType") or "plain")
    if text_type != "plain":
        raise RuntimeError(
            "CONTENT_COLUMN_TYPE_MISMATCH: expected plain-text storage."
        )


def _configure_sharepoint_content_column(
    config: SearchEnhancementConfig,
    access_token: str,
    *,
    field_id: str,
    display_name: str,
) -> dict[str, Any]:
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json;odata=nometadata",
            "Content-Type": "application/json;odata=nometadata",
        }
    )
    try:
        digest = _request_digest(session, config.site_url)
        site_field_url = (
            f"{config.site_url}/_api/web/fields(guid'{field_id}')"
        )
        normalize = session.post(
            site_field_url,
            headers={
                "IF-MATCH": "*",
                "X-HTTP-Method": "MERGE",
                "X-RequestDigest": digest,
            },
            json={
                "Title": display_name,
                "AppendOnly": False,
                "RichText": False,
            },
            timeout=60,
        )
        if normalize.status_code not in {200, 204}:
            normalize.raise_for_status()

        list_field_url = (
            f"{config.site_url}/_api/web/lists(guid'{config.list_id}')/"
            f"fields(guid'{field_id}')"
        )
        current = session.get(
            list_field_url,
            params={
                "$select": (
                    "Id,InternalName,Title,TypeAsString,RichText,AppendOnly"
                )
            },
            timeout=60,
        )
        created = current.status_code == 404
        if created:
            schema = (
                f'<Field Type="Note" ID="{{{field_id}}}" '
                f'Name="{DEFAULT_COLUMN_INTERNAL_NAME}" '
                f'StaticName="{DEFAULT_COLUMN_INTERNAL_NAME}" '
                f'DisplayName="{xml_escape(display_name, quote=True)}" '
                'Group="CrewMeal" RichText="FALSE" '
                'AppendOnly="FALSE" NumLines="12" />'
            )
            response = session.post(
                (
                    f"{config.site_url}/_api/web/lists(guid'{config.list_id}')/"
                    "fields/createfieldasxml"
                ),
                headers={"X-RequestDigest": digest},
                json={
                    "parameters": {
                        "SchemaXml": schema,
                        # SP.AddFieldOptions.AddFieldInternalNameHint
                        "Options": 8,
                    }
                },
                timeout=60,
            )
            response.raise_for_status()
        elif not current.ok:
            current.raise_for_status()

        normalize_list = session.post(
            list_field_url,
            headers={
                "IF-MATCH": "*",
                "X-HTTP-Method": "MERGE",
                "X-RequestDigest": digest,
            },
            json={
                "Title": display_name,
                "AppendOnly": False,
                "RichText": False,
            },
            timeout=60,
        )
        if normalize_list.status_code not in {200, 204}:
            normalize_list.raise_for_status()

        verified = session.get(
            list_field_url,
            params={
                "$select": (
                    "Id,InternalName,Title,TypeAsString,RichText,AppendOnly"
                )
            },
            timeout=60,
        )
        verified.raise_for_status()
        value = verified.json()
        if (
            str(value.get("InternalName") or "") != DEFAULT_COLUMN_INTERNAL_NAME
            or str(value.get("TypeAsString") or "") != "Note"
            or value.get("RichText") is not False
            or value.get("AppendOnly") is not False
        ):
            raise RuntimeError(
                "CONTENT_COLUMN_TYPE_MISMATCH: SharePoint did not preserve "
                "the expected plain-text replacement field contract."
            )
        return {
            "attached": True,
            "sharePointFieldCreated": created,
            "appendChangesToExistingText": False,
        }
    finally:
        session.close()


def _request_digest(session: requests.Session, site_url: str) -> str:
    context_response = session.post(
        f"{site_url}/_api/contextinfo",
        timeout=60,
    )
    context_response.raise_for_status()
    digest = context_response.json().get("FormDigestValue")
    if not isinstance(digest, str) or not digest:
        raise RuntimeError("SharePoint did not return a form digest.")
    return digest


def _configure_sharepoint_formatting(
    config: SearchEnhancementConfig,
    access_token: str,
) -> None:
    formatter = json.loads(FORMATTER_PATH.read_text(encoding="utf-8"))

    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json;odata=nometadata",
            "Content-Type": "application/json;odata=nometadata",
        }
    )
    try:
        digest = _request_digest(session, config.site_url)
        field_name = quote("CrewmealSearchStatus", safe="")
        field_response = session.post(
            (
                f"{config.site_url}/_api/web/lists(guid'{config.list_id}')/"
                f"fields/getbyinternalnameortitle('{field_name}')"
            ),
            headers={
                "IF-MATCH": "*",
                "X-HTTP-Method": "MERGE",
                "X-RequestDigest": digest,
            },
            json={"CustomFormatter": json.dumps(formatter, ensure_ascii=False)},
            timeout=60,
        )
        field_response.raise_for_status()
        view_fields_url = (
            f"{config.site_url}/_api/web/lists(guid'{config.list_id}')/"
            "defaultview/viewfields"
        )
        current_view_response = session.get(view_fields_url, timeout=60)
        current_view_response.raise_for_status()
        current_items = current_view_response.json().get("Items", [])
        if isinstance(current_items, dict):
            current_items = current_items.get("results", [])
        if not isinstance(current_items, list):
            raise ValueError("SharePoint returned an invalid default-view field list.")
        for field in DEFAULT_VIEW_EXCLUDED_FIELDS:
            for _ in range(current_items.count(field)):
                remove_response = session.post(
                    f"{view_fields_url}/removeviewfield('{field}')",
                    headers={"X-RequestDigest": digest},
                    timeout=60,
                )
                if remove_response.status_code not in {200, 204}:
                    remove_response.raise_for_status()
        add_response = session.post(
            f"{view_fields_url}/addviewfield('CrewmealSearchStatus')",
            headers={"X-RequestDigest": digest},
            timeout=60,
        )
        if add_response.status_code not in {200, 204}:
            add_response.raise_for_status()
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())

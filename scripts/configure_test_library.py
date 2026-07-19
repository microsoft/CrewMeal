from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from crewmeal.search_enhancement.config import SearchEnhancementConfig
from crewmeal.search_enhancement.graph_client import GraphClient


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
    args = parser.parse_args()

    config = SearchEnhancementConfig.from_environment()
    with GraphClient(config) as graph:
        created = _ensure_columns(graph, config)
    if args.apply_formatting:
        token = os.environ.get("CREWMEAL_M365_SHAREPOINT_ACCESS_TOKEN")
        if not token:
            parser.error(
                "--apply-formatting requires "
                "CREWMEAL_M365_SHAREPOINT_ACCESS_TOKEN"
            )
        _configure_sharepoint_formatting(config, token)
    print(
        json.dumps(
            {
                "createdColumns": created,
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
        context_response = session.post(
            f"{config.site_url}/_api/contextinfo",
            timeout=60,
        )
        context_response.raise_for_status()
        digest = context_response.json()["FormDigestValue"]
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

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any

from crewmeal.search_enhancement.config import SearchEnhancementConfig
from crewmeal.search_enhancement.graph_client import GraphClient, GraphRequestError


SCHEMA_PROPERTIES: tuple[dict[str, Any], ...] = (
    {
        "name": "title",
        "type": "String",
        "isSearchable": True,
        "isQueryable": True,
        "isRetrievable": True,
        "labels": ["title"],
    },
    {
        "name": "url",
        "type": "String",
        "isRetrievable": True,
        "labels": ["url"],
    },
    {
        "name": "iconUrl",
        "type": "String",
        "isRetrievable": True,
        "labels": ["iconUrl"],
    },
    {
        "name": "fileName",
        "type": "String",
        "isSearchable": True,
        "isQueryable": True,
        "isRetrievable": True,
        "labels": ["fileName"],
    },
    {
        "name": "fileExtension",
        "type": "String",
        "isQueryable": True,
        "isRetrievable": True,
        "isExactMatchRequired": True,
        "labels": ["fileExtension"],
    },
    {
        "name": "lastModifiedDateTime",
        "type": "DateTime",
        "isQueryable": True,
        "isRetrievable": True,
        "labels": ["lastModifiedDateTime"],
    },
    {
        "name": "lastModifiedBy",
        "type": "String",
        "isSearchable": True,
        "isQueryable": True,
        "isRetrievable": True,
        "labels": ["lastModifiedBy"],
    },
    {
        "name": "slideTitles",
        "type": "StringCollection",
        "isSearchable": True,
        "isRetrievable": True,
    },
    {
        "name": "keywords",
        "type": "StringCollection",
        "isSearchable": True,
        "isRetrievable": True,
    },
    {
        "name": "sourceItemId",
        "type": "String",
        "isQueryable": True,
        "isRetrievable": True,
        "isExactMatchRequired": True,
    },
    {
        "name": "sourceETag",
        "type": "String",
        "isRetrievable": True,
    },
    {
        "name": "enhancementVersion",
        "type": "String",
        "isRetrievable": True,
    },
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create or update the Crewmeal Copilot connector connection."
    )
    parser.add_argument(
        "--wait-minutes",
        type=int,
        default=20,
        help="Maximum time to wait for asynchronous schema registration.",
    )
    args = parser.parse_args()
    if args.wait_minutes <= 0:
        parser.error("--wait-minutes must be positive.")

    config = SearchEnhancementConfig.from_environment()
    with GraphClient(config) as graph:
        created = _ensure_connection(graph, config)
        operation = _register_schema(graph, config)
        status = _wait_for_operation(
            graph,
            operation,
            timeout_seconds=args.wait_minutes * 60,
        )
    print(
        json.dumps(
            {
                "connectionId": config.connection_id,
                "created": created,
                "schemaStatus": status,
            },
            indent=2,
        )
    )
    return 0


def _ensure_connection(
    graph: GraphClient,
    config: SearchEnhancementConfig,
) -> bool:
    path = f"/external/connections/{config.connection_id}"
    body = {
        "name": "Crewmeal PowerPoint Search Enhancement PoC",
        "description": (
            "Structured slide, chart, table, diagram, image, and speaker-note "
            "content for selected SharePoint PowerPoint files. Results open the "
            "original PPT."
        ),
        "contentCategory": "fileRepository",
        "activitySettings": {
            "urlToItemResolvers": [
                {
                    "@odata.type": (
                        "#microsoft.graph.externalConnectors.itemIdResolver"
                    ),
                    "priority": 1,
                    "itemId": "{itemId}",
                    "urlMatchInfo": {
                        "baseUrls": [config.site_url.rstrip("/")],
                        "urlPattern": (
                            "^"
                            + re.escape(config.site_url.rstrip("/"))
                            + r"/.*[?&]crewmealItemId=(?<itemId>[a-f0-9]{32})"
                            + r"(?:&.*)?$"
                        ),
                    },
                }
            ]
        },
    }
    try:
        graph.get_json(path)
    except GraphRequestError as exc:
        if exc.status_code != 404:
            raise
        graph.send_json(
            "POST",
            "/external/connections",
            body={"id": config.connection_id, **body},
            expected=(201,),
        )
        return True
    graph.send_json("PATCH", path, body=body, expected=(200,))
    return False


def _register_schema(
    graph: GraphClient,
    config: SearchEnhancementConfig,
) -> str:
    response = graph.request(
        "PATCH",
        f"/external/connections/{config.connection_id}/schema",
        expected=(202,),
        json_body={
            "baseType": "microsoft.graph.externalItem",
            "properties": list(SCHEMA_PROPERTIES),
        },
    )
    location = response.headers.get("Location")
    if not location:
        raise RuntimeError("Schema registration did not return an operation URL.")
    return location


def _wait_for_operation(
    graph: GraphClient,
    operation_url: str,
    *,
    timeout_seconds: int,
) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        operation = graph.get_json(operation_url)
        status = str(operation.get("status") or "").lower()
        if status == "completed":
            return status
        if status == "failed":
            raise RuntimeError(
                "Connector schema registration failed: "
                + json.dumps(operation.get("error"), ensure_ascii=False)
            )
        time.sleep(15)
    raise TimeoutError("Connector schema registration did not finish in time.")


if __name__ == "__main__":
    raise SystemExit(main())

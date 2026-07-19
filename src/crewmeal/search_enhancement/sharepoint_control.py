from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from crewmeal.search_enhancement.config import SearchEnhancementConfig
from crewmeal.search_enhancement.formats import supported_extensions
from crewmeal.search_enhancement.graph_client import GraphClient


CONTROL_FIELDS = (
    "CrewmealSearchEnabled",
    "CrewmealSearchCommand",
    "CrewmealSearchRequestId",
    "CrewmealSearchStatus",
    "CrewmealSearchRequestedAt",
    "CrewmealSearchProcessedAt",
    "CrewmealSearchMessage",
)
VALID_STATUSES = frozenset(
    {
        "NotEnabled",
        "Queued",
        "Processing",
        "Ready",
        "Stale",
        "Removing",
        "Failed",
    }
)


@dataclass(frozen=True, slots=True)
class ControlItem:
    list_item_id: str
    drive_item_id: str
    file_name: str
    web_url: str
    enabled: bool
    command: str
    request_id: str
    status: str

    @property
    def is_pptx(self) -> bool:
        return self.file_name.lower().endswith(".pptx")

    @property
    def is_supported_document(self) -> bool:
        return Path(self.file_name).suffix.lower() in supported_extensions()


class SharePointControlClient:
    def __init__(
        self,
        config: SearchEnhancementConfig,
        graph: GraphClient,
    ) -> None:
        self._config = config
        self._graph = graph

    def list_commands(self) -> tuple[ControlItem, ...]:
        items: list[ControlItem] = []
        path: str | None = self._items_path()
        params: dict[str, str] | None = {
            "$select": "id",
            "$expand": f"fields($select={','.join(CONTROL_FIELDS)})",
            "$top": "200",
        }
        while path:
            page = self._graph.get_json(path, params=params)
            params = None
            for raw in page.get("value", []):
                if not isinstance(raw, dict):
                    continue
                fields = raw.get("fields")
                if not isinstance(fields, dict):
                    continue
                status = str(fields.get("CrewmealSearchStatus") or "").strip()
                command = str(fields.get("CrewmealSearchCommand") or "").strip()
                if (
                    status not in {"Queued", "Removing"}
                    or command not in {"Enhance", "Remove"}
                ):
                    continue
                control = self._parse_list_item(raw)
                if control.is_supported_document:
                    items.append(control)
            next_link = page.get("@odata.nextLink")
            path = next_link if isinstance(next_link, str) else None
        return tuple(items)

    def get_control_item(self, list_item_id: str) -> ControlItem:
        raw = self._graph.get_json(
            f"{self._items_path()}/{list_item_id}",
            params={
                "$select": "id",
                "$expand": f"fields($select={','.join(CONTROL_FIELDS)})",
            },
        )
        return self._parse_list_item(raw)

    def get_drive_item_by_list_item(self, list_item_id: str) -> dict[str, Any]:
        return self._graph.get_json(
            f"{self._items_path()}/{list_item_id}/driveItem",
            params={
                "$select": (
                    "id,name,webUrl,cTag,eTag,size,lastModifiedDateTime,lastModifiedBy,"
                    "sharepointIds,file,parentReference"
                )
            },
        )

    def get_drive_item(self, item_id: str) -> dict[str, Any]:
        return self._graph.get_json(
            f"/drives/{self._config.drive_id}/items/{item_id}",
            params={
                "$select": (
                    "id,name,webUrl,cTag,eTag,size,lastModifiedDateTime,lastModifiedBy,"
                    "sharepointIds,file,parentReference"
                )
            },
        )

    def download_content(self, item_id: str) -> bytes:
        return self._graph.get_bytes(
            f"/drives/{self._config.drive_id}/items/{item_id}/content"
        )

    def list_permissions(self, item_id: str) -> list[dict[str, Any]]:
        permissions: list[dict[str, Any]] = []
        path: str | None = (
            f"/drives/{self._config.drive_id}/items/{item_id}/permissions"
        )
        while path:
            page = self._graph.get_json(path)
            permissions.extend(
                value for value in page.get("value", []) if isinstance(value, dict)
            )
            next_link = page.get("@odata.nextLink")
            path = next_link if isinstance(next_link, str) else None
        return permissions

    def set_processing(self, item: ControlItem) -> None:
        self._update_fields(
            item.list_item_id,
            {
                "CrewmealSearchStatus": "Processing",
                "CrewmealSearchMessage": "검색강화 처리 중",
            },
        )

    def set_ready(
        self,
        item: ControlItem,
        *,
        html_bytes: int,
        request_bytes: int,
    ) -> None:
        self._update_fields(
            item.list_item_id,
            {
                "CrewmealSearchCommand": None,
                "CrewmealSearchStatus": "Ready",
                "CrewmealSearchProcessedAt": _utc_now(),
                "CrewmealSearchMessage": (
                    f"검색강화 완료 (HTML {html_bytes:,} bytes, "
                    f"요청 {request_bytes:,} bytes)"
                )[:255],
            },
        )

    def set_failed(
        self,
        item: ControlItem,
        *,
        code: str,
        message: str,
    ) -> None:
        self._update_fields(
            item.list_item_id,
            {
                "CrewmealSearchStatus": "Failed",
                "CrewmealSearchMessage": f"{code}: {message}"[:255],
            },
        )

    def set_removing(self, item: ControlItem) -> None:
        self._update_fields(
            item.list_item_id,
            {
                "CrewmealSearchStatus": "Removing",
                "CrewmealSearchMessage": "검색강화 항목 삭제 중",
            },
        )

    def set_not_enabled(self, item: ControlItem) -> None:
        self._update_fields(
            item.list_item_id,
            {
                "CrewmealSearchEnabled": False,
                "CrewmealSearchCommand": None,
                "CrewmealSearchStatus": "NotEnabled",
                "CrewmealSearchProcessedAt": _utc_now(),
                "CrewmealSearchMessage": "검색강화가 해제되었습니다.",
                # Graph cannot *set* a hyperlink column, but clearing it to
                # null works — so the removal job resets SharePoint fully,
                # including the status link the SPFx command wrote.
                "CrewmealSearchStatusLink": None,
            },
        )

    def queue_refresh(
        self,
        list_item_id: str,
        *,
        message: str,
    ) -> str:
        request_id = str(uuid4())
        self._update_fields(
            list_item_id,
            {
                "CrewmealSearchEnabled": True,
                "CrewmealSearchCommand": "Enhance",
                "CrewmealSearchRequestId": request_id,
                "CrewmealSearchStatus": "Queued",
                "CrewmealSearchRequestedAt": _utc_now(),
                "CrewmealSearchMessage": message[:255],
            },
        )
        return request_id

    def is_current(self, item: ControlItem, *, expect_enabled: bool) -> bool:
        current = self.get_control_item(item.list_item_id)
        return (
            current.request_id == item.request_id
            and current.enabled is expect_enabled
            and current.command == item.command
        )

    def _update_fields(
        self,
        list_item_id: str,
        fields: dict[str, Any],
    ) -> None:
        self._graph.send_json(
            "PATCH",
            f"{self._items_path()}/{list_item_id}/fields",
            body=fields,
            expected=(200,),
        )

    def _parse_list_item(self, raw: dict[str, Any]) -> ControlItem:
        list_item_id = str(raw.get("id") or "")
        fields = raw.get("fields")
        if not list_item_id or not isinstance(fields, dict):
            raise ValueError("SharePoint list item is missing its fields.")

        request_id = str(fields.get("CrewmealSearchRequestId") or "").strip()
        try:
            UUID(request_id)
        except (ValueError, AttributeError) as exc:
            raise ValueError(
                f"List item {list_item_id} has an invalid request ID."
            ) from exc

        status = str(fields.get("CrewmealSearchStatus") or "").strip()
        if status not in VALID_STATUSES:
            raise ValueError(
                f"List item {list_item_id} has an invalid search status: {status!r}."
            )

        drive_item = self.get_drive_item_by_list_item(list_item_id)
        return ControlItem(
            list_item_id=list_item_id,
            drive_item_id=str(drive_item.get("id") or ""),
            file_name=str(drive_item.get("name") or ""),
            web_url=str(drive_item.get("webUrl") or ""),
            enabled=bool(fields.get("CrewmealSearchEnabled")),
            command=str(fields.get("CrewmealSearchCommand") or "").strip(),
            request_id=request_id,
            status=status,
        )

    def _items_path(self) -> str:
        return (
            f"/sites/{self._config.site_id}/lists/{self._config.list_id}/items"
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

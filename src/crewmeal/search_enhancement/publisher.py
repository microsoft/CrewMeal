from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

from crewmeal.search_enhancement.acl_mapper import ConnectorAcl, acl_hash
from crewmeal.search_enhancement.connector_client import (
    ConnectorClient,
    external_item_id,
    resolver_url,
)
from crewmeal.search_enhancement.database import (
    DocumentRecord,
    PublicationRecord,
)
from crewmeal.search_enhancement.graph_client import GraphRequestError
from crewmeal.search_enhancement.html_renderer import (
    render_sharepoint_column_markdown,
    sharepoint_character_count,
)
from crewmeal.search_enhancement.models import RenderedHtml
from crewmeal.search_enhancement.publication import (
    DEFAULT_COLUMN_INTERNAL_NAME,
    PublicationTarget,
)
from crewmeal.search_enhancement.sharepoint_control import SharePointControlClient


class PublicationError(RuntimeError):
    """Raised when a publication target cannot honor its contract."""


@dataclass(frozen=True, slots=True)
class PublicationResult:
    target: PublicationTarget
    locator: str | None
    source_url: str
    content_hash: str
    original_characters: int
    stored_characters: int
    stored_bytes: int
    request_bytes: int
    truncated: bool
    acl_digest: str


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    needs_republish: bool = False
    acl_digest: str | None = None


class Publisher(Protocol):
    target: PublicationTarget
    label: str
    requires_acl: bool

    def locator(
        self,
        *,
        document: DocumentRecord,
        drive_item: dict[str, object],
    ) -> str | None: ...

    def publish(
        self,
        *,
        document: DocumentRecord,
        drive_item: dict[str, object],
        rendered: RenderedHtml,
        acl: tuple[ConnectorAcl, ...],
        source_fingerprint: str,
    ) -> PublicationResult: ...

    def remove(
        self,
        *,
        document: DocumentRecord,
        locator: str | None,
        drive_item: dict[str, object] | None = None,
    ) -> None: ...

    def reconcile(
        self,
        *,
        document: DocumentRecord,
        publication: PublicationRecord,
        drive_item: dict[str, object],
        acl: tuple[ConnectorAcl, ...],
        source_fingerprint: str,
    ) -> ReconcileResult: ...


class CopilotConnectorPublisher:
    target = PublicationTarget.COPILOT_CONNECTOR
    label = "Copilot connector externalItem"
    requires_acl = True

    def __init__(self, connector: ConnectorClient) -> None:
        self._connector = connector

    def locator(
        self,
        *,
        document: DocumentRecord,
        drive_item: dict[str, object],
    ) -> str:
        del document
        return external_item_id(drive_item)

    def publish(
        self,
        *,
        document: DocumentRecord,
        drive_item: dict[str, object],
        rendered: RenderedHtml,
        acl: tuple[ConnectorAcl, ...],
        source_fingerprint: str,
    ) -> PublicationResult:
        del document
        prepared = self._connector.prepare_item(
            drive_item=drive_item,
            rendered=rendered,
            acl=acl,
            source_fingerprint=source_fingerprint,
        )
        self._connector.upsert(prepared)
        characters = sharepoint_character_count(rendered.content)
        return PublicationResult(
            target=self.target,
            locator=prepared.item_id,
            source_url=prepared.source_url,
            content_hash=rendered.sha256,
            original_characters=characters,
            stored_characters=characters,
            stored_bytes=rendered.byte_count,
            request_bytes=prepared.request_bytes,
            truncated=False,
            acl_digest=acl_hash(acl),
        )

    def remove(
        self,
        *,
        document: DocumentRecord,
        locator: str | None,
        drive_item: dict[str, object] | None = None,
    ) -> None:
        item_id = locator
        if not item_id:
            if drive_item is None:
                return
            item_id = external_item_id(drive_item)
        self._connector.delete(item_id)

    def reconcile(
        self,
        *,
        document: DocumentRecord,
        publication: PublicationRecord,
        drive_item: dict[str, object],
        acl: tuple[ConnectorAcl, ...],
        source_fingerprint: str,
    ) -> ReconcileResult:
        item_id = publication.locator
        if not item_id:
            raise PublicationError(
                "CONNECTOR_LOCATOR_MISSING: cannot reconcile external item."
            )
        if self._connector.get(item_id) is None:
            return ReconcileResult(needs_republish=True)
        digest = acl_hash(acl)
        changed_acl = digest != document.acl_hash
        if changed_acl:
            self._connector.update_acl(item_id, acl)

        web_url = _required_string(drive_item, "webUrl")
        file_name = _required_string(drive_item, "name")
        modified = _required_string(drive_item, "lastModifiedDateTime")
        if (
            web_url != document.web_url
            or file_name != document.file_name
            or modified != document.last_modified_datetime
        ):
            self._connector.update_properties(
                item_id,
                {
                    "title": file_name,
                    "url": resolver_url(web_url, item_id),
                    "fileName": file_name,
                    "lastModifiedDateTime": modified,
                    "lastModifiedBy": _modified_by(drive_item),
                    "sourceETag": source_fingerprint,
                },
            )
        return ReconcileResult(
            acl_digest=digest if changed_acl else None,
        )


class SharePointColumnPublisher:
    target = PublicationTarget.SHAREPOINT_COLUMN
    label = "SharePoint content column"
    requires_acl = False

    def __init__(
        self,
        control: SharePointControlClient,
        *,
        column_name: str = DEFAULT_COLUMN_INTERNAL_NAME,
    ) -> None:
        self._control = control
        self._column_name = column_name

    def locator(
        self,
        *,
        document: DocumentRecord,
        drive_item: dict[str, object],
    ) -> str:
        del document, drive_item
        return self._column_name

    def publish(
        self,
        *,
        document: DocumentRecord,
        drive_item: dict[str, object],
        rendered: RenderedHtml,
        acl: tuple[ConnectorAcl, ...],
        source_fingerprint: str,
    ) -> PublicationResult:
        del acl, source_fingerprint
        column = render_sharepoint_column_markdown(rendered)
        stored = self._control.set_search_content(
            document.list_item_id,
            self._column_name,
            column.content,
        )
        stored_bytes = stored.encode("utf-8")
        return PublicationResult(
            target=self.target,
            locator=self._column_name,
            source_url=_required_string(drive_item, "webUrl"),
            content_hash=hashlib.sha256(stored_bytes).hexdigest(),
            original_characters=column.original_character_count,
            stored_characters=sharepoint_character_count(stored),
            stored_bytes=len(stored_bytes),
            request_bytes=len(column.content.encode("utf-8")),
            truncated=column.truncated,
            acl_digest="",
        )

    def remove(
        self,
        *,
        document: DocumentRecord,
        locator: str | None,
        drive_item: dict[str, object] | None = None,
    ) -> None:
        del locator, drive_item
        try:
            self._control.clear_search_content(
                document.list_item_id,
                self._column_name,
            )
        except GraphRequestError as exc:
            if exc.status_code != 404:
                raise

    def reconcile(
        self,
        *,
        document: DocumentRecord,
        publication: PublicationRecord,
        drive_item: dict[str, object],
        acl: tuple[ConnectorAcl, ...],
        source_fingerprint: str,
    ) -> ReconcileResult:
        del drive_item, acl, source_fingerprint
        stored = self._control.get_search_content(
            document.list_item_id,
            self._column_name,
        )
        if stored is None:
            return ReconcileResult(needs_republish=True)
        digest = hashlib.sha256(stored.encode("utf-8")).hexdigest()
        return ReconcileResult(
            needs_republish=digest != publication.content_hash,
        )


def _required_string(value: dict[str, object], key: str) -> str:
    raw = value.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise PublicationError(f"SOURCE_PROPERTY_MISSING: {key}.")
    return raw.strip()


def _modified_by(drive_item: dict[str, object]) -> str:
    identity_set = drive_item.get("lastModifiedBy")
    if not isinstance(identity_set, dict):
        raise PublicationError("SOURCE_MODIFIER_MISSING: lastModifiedBy is unavailable.")
    for identity_type in ("user", "application", "device"):
        identity = identity_set.get(identity_type)
        if not isinstance(identity, dict):
            continue
        for key in ("displayName", "email", "id"):
            value = identity.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    raise PublicationError("SOURCE_MODIFIER_INVALID: no usable identity.")

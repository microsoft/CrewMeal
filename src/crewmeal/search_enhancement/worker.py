from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4

from crewmeal.libreoffice import LibreOfficeConversionError
from crewmeal.search_enhancement.acl_mapper import (
    UnsupportedAclError,
    acl_hash,
    map_drive_item_permissions,
)
from crewmeal.search_enhancement.artifact_store import (
    ArtifactStore,
    artifact_path,
    document_prefix,
)
from crewmeal.search_enhancement.config import SearchEnhancementConfig
from crewmeal.search_enhancement.connector_client import (
    ConnectorClient,
    ExternalItemError,
    external_item_id,
    resolver_url,
)
from crewmeal.search_enhancement.database import (
    DocumentKey,
    DocumentRecord,
    JobRecord,
    SearchEnhancementRepository,
)
from crewmeal.search_enhancement.db_resilience import is_transient_db_error
from crewmeal.search_enhancement.decryption import DecryptionError
from crewmeal.search_enhancement.formats import (
    InvalidDocumentError,
    UnsupportedFormatError,
    content_fingerprint,
    enabled_extensions,
    supported_content_types,
    supported_extensions,
)
from crewmeal.search_enhancement.graph_client import GraphRequestError
from crewmeal.search_enhancement.html_renderer import ContentTooLargeError
from crewmeal.search_enhancement.processor import (
    PresentationProcessor,
    ProcessedPresentation,
    ProcessingFidelityError,
)
from crewmeal.search_enhancement.progress import (
    JobProgressReporter,
    ProgressReporter,
    Stage,
)
from crewmeal.search_enhancement.sharepoint_control import (
    ControlItem,
    SharePointControlClient,
)
from crewmeal.search_enhancement.structured_analysis import (
    StructuredSlideAnalysisError,
)
from crewmeal.source import InvalidPresentationError


LOGGER = logging.getLogger(__name__)


class StaleRequestError(RuntimeError):
    """Raised when a newer SharePoint command supersedes a running job."""


KNOWN_PROCESSING_ERRORS = (
    ContentTooLargeError,
    DecryptionError,
    ExternalItemError,
    GraphRequestError,
    InvalidDocumentError,
    InvalidPresentationError,
    LibreOfficeConversionError,
    ProcessingFidelityError,
    StructuredSlideAnalysisError,
    UnsupportedAclError,
    UnsupportedFormatError,
)


@dataclass(frozen=True, slots=True)
class WorkerRun:
    commands_ingested: int
    jobs_processed: int


class SearchEnhancementWorker:
    def __init__(
        self,
        *,
        config: SearchEnhancementConfig,
        repository: SearchEnhancementRepository,
        control: SharePointControlClient,
        connector: ConnectorClient,
        processor: PresentationProcessor,
        artifact_store: ArtifactStore | None = None,
        worker_id: str | None = None,
    ) -> None:
        self._config = config
        self._repository = repository
        self._control = control
        self._connector = connector
        self._processor = processor
        self._artifacts = artifact_store
        self._worker_id = worker_id or f"local-{uuid4()}"

    def run_once(self) -> WorkerRun:
        commands = self._ingest_commands()
        jobs = 0
        while job := self._repository.claim_next_job(
            worker_id=self._worker_id,
            lease_seconds=self._config.job_lease_seconds,
        ):
            self._process_job(job)
            jobs += 1
        return WorkerRun(commands_ingested=commands, jobs_processed=jobs)

    def run_forever(self, stop_event: Event | None = None) -> None:
        stop = stop_event or Event()
        next_reconciliation = time.monotonic()
        backoff = float(self._config.db_retry_initial_seconds)
        while not stop.is_set():
            try:
                run = self.run_once()
                now = time.monotonic()
                if now >= next_reconciliation:
                    self.reconcile_once()
                    next_reconciliation = now + self._config.reconciliation_seconds
            except Exception as exc:  # noqa: BLE001 - re-raised unless transient
                if not is_transient_db_error(exc):
                    raise
                LOGGER.warning(
                    "Database unavailable; backing off %.0fs before retrying: %s",
                    backoff,
                    exc,
                )
                stop.wait(backoff)
                backoff = min(
                    backoff * 2, float(self._config.db_retry_max_seconds)
                )
                continue
            # A healthy iteration resets the backoff so a later outage starts
            # from the initial delay again.
            backoff = float(self._config.db_retry_initial_seconds)
            if not run.commands_ingested and not run.jobs_processed:
                stop.wait(self._config.command_poll_seconds)

    def reconcile_once(self) -> int:
        changes = 0
        for document in self._repository.list_enabled_documents():
            try:
                drive_item = self._control.get_drive_item(document.key.item_id)
            except GraphRequestError as exc:
                if exc.status_code != 404:
                    raise
                if document.external_item_id:
                    self._connector.delete(document.external_item_id)
                self._repository.record_removed(
                    document.key,
                    request_id=document.request_id,
                )
                changes += 1
                continue

            source_bytes = self._control.download_content(document.key.item_id)
            source_fingerprint = content_fingerprint(
                source_bytes, filename=document.file_name
            )
            if source_fingerprint != document.source_etag:
                request_id = self._control.queue_refresh(
                    document.list_item_id,
                    message="원본 PPT 변경 감지: 검색강화 갱신 대기",
                )
                self._repository.record_queued_refresh(
                    document.key,
                    request_id=request_id,
                )
                changes += 1
                continue

            permissions = self._control.list_permissions(document.key.item_id)
            acl = map_drive_item_permissions(permissions)
            digest = acl_hash(acl)
            if digest != document.acl_hash:
                if not document.external_item_id:
                    raise ExternalItemError(
                        "EXTERNAL_ITEM_ID_MISSING: cannot update ACL."
                    )
                self._connector.update_acl(document.external_item_id, acl)
                self._repository.record_acl_update(
                    document.key,
                    acl_digest=digest,
                )
                changes += 1

            web_url = _required_string(drive_item, "webUrl")
            file_name = _required_string(drive_item, "name")
            modified = _required_string(drive_item, "lastModifiedDateTime")
            if (
                web_url != document.web_url
                or file_name != document.file_name
                or modified != document.last_modified_datetime
            ):
                if not document.external_item_id:
                    raise ExternalItemError(
                        "EXTERNAL_ITEM_ID_MISSING: cannot update source metadata."
                    )
                self._connector.update_properties(
                    document.external_item_id,
                    {
                        "title": file_name,
                        "url": resolver_url(web_url, document.external_item_id),
                        "fileName": file_name,
                        "lastModifiedDateTime": modified,
                        "lastModifiedBy": _modified_by(drive_item),
                        "sourceETag": source_fingerprint,
                    },
                )
                self._repository.record_source_metadata(
                    document.key,
                    web_url=web_url,
                    file_name=file_name,
                    last_modified_datetime=modified,
                )
                changes += 1
        return changes

    def _ingest_commands(self) -> int:
        count = 0
        enabled_exts = enabled_extensions(self._repository.get_all_settings())
        for item in self._control.list_commands():
            if Path(item.file_name).suffix.lower() not in enabled_exts:
                continue
            desired_enabled = item.command == "Enhance"
            key = DocumentKey(
                tenant_id=self._config.tenant_id,
                site_id=self._config.site_id,
                drive_id=self._config.drive_id,
                item_id=item.drive_item_id,
            )
            self._repository.upsert_document(
                key=key,
                list_id=self._config.list_id,
                list_item_id=item.list_item_id,
                web_url=item.web_url,
                file_name=item.file_name,
                connection_id=self._config.connection_id,
                desired_enabled=desired_enabled,
                status=item.status,
                request_id=item.request_id,
            )
            self._repository.enqueue_job(
                key=key,
                request_id=item.request_id,
                job_type="upsert" if desired_enabled else "delete",
            )
            count += 1
        return count

    def _process_job(self, job: JobRecord) -> None:
        document = self._repository.get_document(job.document_key)
        if document is None:
            raise RuntimeError(f"Job {job.job_id} has no document record.")
        reporter = JobProgressReporter(self._repository, job.job_id)
        reporter.stage(
            Stage.CLAIMED,
            message=job.trigger,
            detail={"attempt": job.attempts, "jobType": job.job_type},
        )
        is_upload = document.source_kind == "upload"
        try:
            if is_upload:
                self._process_upload(job, document, reporter)
            elif job.job_type == "upsert":
                item = self._current_control(document, job)
                self._process_upsert(job, document, item, reporter)
            else:
                item = self._current_control(document, job)
                self._process_delete(job, document, item, reporter)
        except StaleRequestError as exc:
            reporter.stage(Stage.CANCELLED, message=str(exc))
            self._repository.cancel_job(job.job_id, message=str(exc))
        except KNOWN_PROCESSING_ERRORS as exc:
            code = _error_code(exc)
            message = str(exc)
            reporter.stage(Stage.FAILED, message=message, detail={"code": code})
            self._repository.fail_job(job.job_id, code=code, message=message)
            self._repository.record_document_error(
                job.document_key,
                code=code,
                message=message,
            )
            if is_upload:
                return
            try:
                current = self._control.get_control_item(document.list_item_id)
                if current.request_id == job.request_id:
                    self._control.set_failed(
                        current,
                        code=code,
                        message=message,
                    )
            except GraphRequestError as report_error:
                LOGGER.error(
                    "Could not report failed job %s to SharePoint: %s",
                    job.job_id,
                    report_error,
                )

    def _process_upsert(
        self,
        job: JobRecord,
        document: DocumentRecord,
        item: ControlItem,
        reporter: ProgressReporter,
    ) -> None:
        if not self._still_current(job):
            raise StaleRequestError("Enhance request is no longer active.")
        self._control.set_processing(item)
        reporter.stage(Stage.DOWNLOADING, message="SharePoint driveItem")
        drive_item = self._control.get_drive_item(document.key.item_id)
        if not _is_supported_document(drive_item):
            raise InvalidPresentationError(
                "The selected DriveItem is not a supported document type."
            )

        permissions = self._control.list_permissions(document.key.item_id)
        acl = map_drive_item_permissions(permissions)
        source_name = _required_string(drive_item, "name")
        source_bytes = self._control.download_content(document.key.item_id)
        source_fingerprint = content_fingerprint(source_bytes, filename=source_name)
        corrections = self._document_corrections(document)
        processed = self._processor.process(
            source_bytes,
            source_name=source_name,
            progress=reporter,
            corrections=corrections or None,
        )

        if not self._still_current(job):
            raise StaleRequestError(
                "A newer command replaced this request before publication."
            )
        reporter.stage(Stage.PUBLISHING, message="Copilot connector externalItem")
        prepared = self._connector.prepare_item(
            drive_item=drive_item,
            rendered=processed.rendered,
            acl=acl,
            source_fingerprint=source_fingerprint,
        )
        self._connector.upsert(prepared)
        if not self._still_current(job):
            self._connector.delete(prepared.item_id)
            raise StaleRequestError(
                "A newer command replaced this request during publication."
            )

        version = document.enhancement_version + 1
        self._store_artifacts(document, processed, version=version)
        self._repository.record_success(
            job.document_key,
            request_id=job.request_id,
            source_etag=source_fingerprint,
            last_modified_datetime=_required_string(
                drive_item, "lastModifiedDateTime"
            ),
            source_size=len(source_bytes),
            acl_digest=acl_hash(acl),
            external_item_id=prepared.item_id,
            web_url=prepared.source_url,
            file_name=_required_string(drive_item, "name"),
            html_bytes=processed.rendered.byte_count,
            request_bytes=prepared.request_bytes,
            content_hash=processed.rendered.sha256,
        )
        self._maybe_capture_feedback(job, document, processed, version)
        self._control.set_ready(
            item,
            html_bytes=processed.rendered.byte_count,
            request_bytes=prepared.request_bytes,
        )
        reporter.stage(
            Stage.READY,
            detail={
                "htmlBytes": processed.rendered.byte_count,
                "requestBytes": prepared.request_bytes,
                "version": version,
            },
        )
        self._repository.complete_job(
            job.job_id,
            stage_timings=processed.stage_timings,
            usage=processed.analysis.usage,
            html_bytes=processed.rendered.byte_count,
            request_bytes=prepared.request_bytes,
        )

    def _process_upload(
        self,
        job: JobRecord,
        document: DocumentRecord,
        reporter: ProgressReporter,
    ) -> None:
        """Process an admin upload (tryout): same pipeline, no connector/SharePoint."""

        reporter.stage(Stage.DOWNLOADING, message="uploaded source")
        source_bytes = self._load_upload_source(document)
        source_fingerprint = content_fingerprint(
            source_bytes, filename=document.file_name
        )
        corrections = self._document_corrections(document)
        processed = self._processor.process(
            source_bytes,
            source_name=document.file_name,
            progress=reporter,
            corrections=corrections or None,
        )
        version = document.enhancement_version + 1
        self._store_artifacts(document, processed, version=version)
        self._repository.record_success(
            job.document_key,
            request_id=job.request_id,
            source_etag=source_fingerprint,
            last_modified_datetime=_utc_iso(),
            source_size=len(source_bytes),
            acl_digest="",
            external_item_id="",
            web_url=document.web_url,
            file_name=document.file_name,
            html_bytes=processed.rendered.byte_count,
            request_bytes=processed.rendered.byte_count,
            content_hash=processed.rendered.sha256,
        )
        self._maybe_capture_feedback(job, document, processed, version)
        reporter.stage(
            Stage.READY,
            detail={"htmlBytes": processed.rendered.byte_count, "version": version},
        )
        self._repository.complete_job(
            job.job_id,
            stage_timings=processed.stage_timings,
            usage=processed.analysis.usage,
            html_bytes=processed.rendered.byte_count,
            request_bytes=processed.rendered.byte_count,
        )

    def _load_upload_source(self, document: DocumentRecord) -> bytes:
        if self._artifacts is None:
            raise InvalidPresentationError(
                "UPLOAD_SOURCE_UNAVAILABLE: no artifact store configured."
            )
        source = self._repository.get_latest_artifact(document.key, "source_pptx")
        if source is None:
            raise InvalidPresentationError(
                "UPLOAD_SOURCE_MISSING: no uploaded source found."
            )
        return self._artifacts.get_bytes(source.blob_path)

    def _store_artifacts(
        self,
        document: DocumentRecord,
        processed: ProcessedPresentation,
        *,
        version: int,
    ) -> None:
        if self._artifacts is None:
            return
        html_bytes = processed.rendered.content.encode("utf-8")
        html = self._artifacts.put_bytes(
            artifact_path(
                document.key, version=version, kind="html", filename="index.html"
            ),
            html_bytes,
            content_type="text/html; charset=utf-8",
        )
        self._repository.record_artifact(
            document.key,
            kind="html",
            blob_path=html.path,
            content_type=html.content_type,
            content_hash=html.content_hash,
            byte_count=html.byte_count,
            enhancement_version=version,
        )
        analysis_bytes = json.dumps(
            processed.analysis.raw_result, ensure_ascii=False, indent=2
        ).encode("utf-8")
        analysis = self._artifacts.put_bytes(
            artifact_path(
                document.key,
                version=version,
                kind="structured_json",
                filename="analysis.json",
            ),
            analysis_bytes,
            content_type="application/json; charset=utf-8",
        )
        self._repository.record_artifact(
            document.key,
            kind="structured_json",
            blob_path=analysis.path,
            content_type=analysis.content_type,
            content_hash=analysis.content_hash,
            byte_count=analysis.byte_count,
            enhancement_version=version,
        )

    def _document_corrections(self, document: DocumentRecord) -> list[str]:
        notes = self._repository.correction_notes(document.key)
        return [str(note.get("text", "")).strip() for note in notes if note.get("text")]

    def _maybe_capture_feedback(
        self,
        job: JobRecord,
        document: DocumentRecord,
        processed: ProcessedPresentation,
        version: int,
    ) -> None:
        """Append a feedback-corpus record when this job carried a tuning comment."""

        if not job.feedback:
            return
        raw = processed.analysis.raw_result
        model = raw.get("model") if isinstance(raw, dict) else None
        deployment = raw.get("modelDeployment") if isinstance(raw, dict) else None
        html = self._repository.get_latest_artifact(document.key, "html")
        structured = self._repository.get_latest_artifact(
            document.key, "structured_json"
        )
        source = self._repository.get_latest_artifact(document.key, "source_pptx")
        self._repository.add_feedback_record(
            document.key,
            correction_text=job.feedback,
            enhancement_version=version,
            source_artifact_path=source.blob_path if source else None,
            after_html_path=html.blob_path if html else None,
            after_json_path=structured.blob_path if structured else None,
            model=model,
            deployment=deployment,
            created_by=job.trigger,
        )

    def _process_delete(
        self,
        job: JobRecord,
        document: DocumentRecord,
        item: ControlItem,
        reporter: ProgressReporter,
    ) -> None:
        if not self._still_current(job):
            raise StaleRequestError("Remove request is no longer active.")
        self._control.set_removing(item)
        reporter.stage(Stage.REMOVING, message="Copilot connector delete")
        item_id = document.external_item_id
        if not item_id:
            drive_item = self._control.get_drive_item(document.key.item_id)
            item_id = external_item_id(drive_item)
        self._connector.delete(item_id)
        if not self._still_current(job):
            raise StaleRequestError(
                "A newer command replaced this request during deletion."
            )
        if self._artifacts is not None:
            self._artifacts.delete_prefix(document_prefix(document.key))
        self._repository.record_removed(
            job.document_key,
            request_id=job.request_id,
        )
        self._control.set_not_enabled(item)
        reporter.stage(Stage.REMOVED)
        self._repository.complete_job(job.job_id)

    def _current_control(
        self,
        document: DocumentRecord,
        job: JobRecord,
    ) -> ControlItem:
        item = self._control.get_control_item(document.list_item_id)
        if document.request_id != job.request_id:
            raise StaleRequestError("A newer request superseded this job.")
        return item

    def _still_current(self, job: JobRecord) -> bool:
        """Whether ``job`` is still the document's active request.

        The DB queue (not the SharePoint control columns) is the source of
        truth: SPFx and the web/admin portal enqueue jobs via the ingest API
        without writing ``CrewmealSearchCommand``/``CrewmealSearchEnabled``, so
        staleness must be judged from ``documents.request_id``.
        """

        current = self._repository.get_document(job.document_key)
        return current is not None and current.request_id == job.request_id


def _utc_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _required_string(value: dict[str, Any], key: str) -> str:
    raw = value.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise ExternalItemError(f"SOURCE_PROPERTY_MISSING: {key}.")
    return raw.strip()


def _required_int(value: dict[str, Any], key: str) -> int:
    raw = value.get(key)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ExternalItemError(f"SOURCE_PROPERTY_INVALID: {key}.")
    return raw


def _is_supported_document(drive_item: dict[str, Any]) -> bool:
    name = str(drive_item.get("name") or "")
    suffix = Path(name).suffix.lower()
    if suffix not in supported_extensions():
        return False
    file_value = drive_item.get("file")
    mime_type = file_value.get("mimeType") if isinstance(file_value, dict) else None
    return mime_type is None or mime_type in supported_content_types()


def _modified_by(drive_item: dict[str, Any]) -> str:
    identity_set = drive_item.get("lastModifiedBy")
    if isinstance(identity_set, dict):
        for identity_type in ("user", "application", "device"):
            identity = identity_set.get(identity_type)
            if isinstance(identity, dict):
                for key in ("displayName", "email", "id"):
                    value = identity.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
    raise ExternalItemError("SOURCE_MODIFIER_INVALID: no usable identity.")


def _error_code(error: Exception) -> str:
    if isinstance(error, GraphRequestError):
        return error.code or f"GRAPH_HTTP_{error.status_code or 'UNKNOWN'}"
    message = str(error)
    prefix = message.split(":", 1)[0]
    if prefix and prefix.replace("_", "").isalnum() and prefix.upper() == prefix:
        return prefix[:80]
    return type(error).__name__.upper()[:80]

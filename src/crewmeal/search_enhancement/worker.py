from __future__ import annotations

import hashlib
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
)
from crewmeal.search_enhancement.database import (
    DocumentKey,
    DocumentRecord,
    JobRecord,
    PublicationRecord,
    PublicationTransition,
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
from crewmeal.search_enhancement.html_renderer import ColumnContentTooLargeError
from crewmeal.search_enhancement.publication import PublicationTarget
from crewmeal.search_enhancement.publisher import (
    CopilotConnectorPublisher,
    PublicationError,
    PublicationResult,
    Publisher,
    SharePointColumnPublisher,
)
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
    SharePointColumnError,
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
    ColumnContentTooLargeError,
    DecryptionError,
    ExternalItemError,
    GraphRequestError,
    InvalidDocumentError,
    InvalidPresentationError,
    LibreOfficeConversionError,
    ProcessingFidelityError,
    PublicationError,
    SharePointColumnError,
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
        publishers: dict[PublicationTarget, Publisher] | None = None,
        artifact_store: ArtifactStore | None = None,
        worker_id: str | None = None,
    ) -> None:
        self._config = config
        self._repository = repository
        self._control = control
        self._connector = connector
        self._publishers = publishers or {
            PublicationTarget.COPILOT_CONNECTOR: CopilotConnectorPublisher(connector),
            PublicationTarget.SHAREPOINT_COLUMN: SharePointColumnPublisher(control),
        }
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
        self._advance_publication_transition()
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
                for publication in self._repository.list_document_publications(
                    document.key
                ):
                    with self._repository.publication_operation_lock(
                        document.key,
                        publication.target,
                    ):
                        self._claim_and_remove_publication(
                            document=document,
                            publication=publication,
                            operation_id=f"source-missing:{uuid4()}",
                            remove_remote=(
                                publication.target
                                is PublicationTarget.COPILOT_CONNECTOR
                            ),
                        )
                if self._repository.list_document_publications(document.key):
                    continue
                removed = self._repository.record_removed(
                    document.key,
                    request_id=document.request_id,
                )
                if removed:
                    changes += 1
                continue

            source_bytes = self._control.download_content(document.key.item_id)
            source_bytes = self._processor.decrypt_source(
                source_bytes, filename=document.file_name
            )
            source_fingerprint = content_fingerprint(
                source_bytes, filename=document.file_name
            )
            if source_fingerprint != document.source_etag:
                if self._queue_reconcile_refresh(
                    document,
                    message="원본 PPT 변경 감지: 검색강화 갱신 대기",
                ):
                    changes += 1
                continue

            transition = self._repository.get_publication_transition()
            targets = self._publication_targets(transition)
            if not targets:
                continue
            for target in targets:
                with self._repository.publication_operation_lock(
                    document.key,
                    target,
                ):
                    current_transition = (
                        self._repository.get_publication_transition()
                    )
                    if target not in self._publication_targets(current_transition):
                        continue
                    publisher = self._publisher(target)
                    publication = self._repository.get_publication(
                        document.key,
                        target,
                    )
                    requires_current_generation = (
                        current_transition.status != "active"
                        and target is current_transition.desired_target
                    )
                    if publication is None or (
                        requires_current_generation
                        and publication.generation
                        != current_transition.generation
                    ):
                        queued = self._queue_reconcile_refresh(
                            document,
                            message="게시 대상 누락 감지: 검색강화 갱신 대기",
                        )
                        if queued:
                            changes += 1
                        break
                    if publication.status != "ready":
                        continue
                    acl = self._publication_acl(
                        publisher,
                        document.key.item_id,
                    )
                    reconciled = publisher.reconcile(
                        document=document,
                        publication=publication,
                        drive_item=drive_item,
                        acl=acl,
                        source_fingerprint=source_fingerprint,
                    )
                    if reconciled.needs_republish:
                        queued = self._queue_reconcile_refresh(
                            document,
                            message="게시 콘텐츠 변경 감지: 검색강화 갱신 대기",
                        )
                        if queued:
                            changes += 1
                        break
                    if reconciled.acl_digest is not None:
                        self._repository.record_acl_update(
                            document.key,
                            acl_digest=reconciled.acl_digest,
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
        if self._repository.publication_target_for_jobs() is PublicationTarget.UNSET:
            return 0
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
            self._repository.upsert_document_and_enqueue(
                key=key,
                list_id=self._config.list_id,
                list_item_id=item.list_item_id,
                web_url=item.web_url,
                file_name=item.file_name,
                connection_id=self._config.connection_id,
                desired_enabled=desired_enabled,
                status=item.status,
                request_id=item.request_id,
                job_type="upsert" if desired_enabled else "delete",
                trigger="sharepoint-command",
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
        attempt_transition = (
            self._repository.get_publication_transition()
            if not is_upload and job.job_type == "upsert"
            else None
        )
        try:
            if document.processed_request_id == job.request_id:
                reporter.stage(
                    Stage.REMOVED if job.job_type == "delete" else Stage.READY,
                    message="Recovered a job whose document outcome was already committed.",
                )
                self._repository.complete_job(
                    job.job_id,
                    expected_attempts=job.attempts,
                    expected_lease_owner=self._worker_id,
                )
                return
            if is_upload:
                self._process_upload(job, document, reporter)
            elif job.job_type == "upsert":
                item = self._current_control(document, job)
                assert attempt_transition is not None
                self._process_upsert(
                    job,
                    document,
                    item,
                    reporter,
                    transition=attempt_transition,
                )
            else:
                item = self._current_control(document, job)
                self._process_delete(job, document, item, reporter)
        except StaleRequestError as exc:
            self._repository.fail_pending_publications_for_operation(
                self._publication_operation_id(job),
                code="STALE_REQUEST",
                message=str(exc),
            )
            reporter.stage(Stage.CANCELLED, message=str(exc))
            self._repository.cancel_job(
                job.job_id,
                message=str(exc),
                expected_attempts=job.attempts,
                expected_lease_owner=self._worker_id,
            )
        except KNOWN_PROCESSING_ERRORS as exc:
            code = _error_code(exc)
            message = str(exc)
            if not self._still_current(job) or (
                attempt_transition is not None
                and not self._repository.publication_transition_matches(
                    attempt_transition
                )
            ):
                self._repository.fail_pending_publications_for_operation(
                    self._publication_operation_id(job),
                    code="STALE_REQUEST",
                    message=message,
                )
                reporter.stage(Stage.CANCELLED, message=message)
                self._repository.cancel_job(
                    job.job_id,
                    message=message,
                    expected_attempts=job.attempts,
                    expected_lease_owner=self._worker_id,
                )
                return
            recorded = self._repository.record_document_error(
                job.document_key,
                code=code,
                message=message,
                request_id=job.request_id,
                job_id=job.job_id,
                expected_attempts=job.attempts,
                expected_lease_owner=self._worker_id,
            )
            if not recorded:
                LOGGER.info(
                    "Ignoring failure from superseded job %s: %s",
                    job.job_id,
                    message,
                )
                return
            reporter.stage(Stage.FAILED, message=message, detail={"code": code})
            self._repository.fail_job(
                job.job_id,
                code=code,
                message=message,
                expected_attempts=job.attempts,
                expected_lease_owner=self._worker_id,
            )
            target = (
                attempt_transition.effective_target
                if attempt_transition is not None
                else PublicationTarget.UNSET
            )
            if target is not PublicationTarget.UNSET:
                self._repository.record_publication_error(
                    job.document_key,
                    target=target,
                    generation=attempt_transition.generation,
                    code=code,
                    message=message,
                    operation_id=self._publication_operation_id(job),
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
        *,
        transition: PublicationTransition,
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

        target = transition.effective_target
        if target is PublicationTarget.UNSET:
            raise PublicationError(
                "PUBLICATION_TARGET_UNSET: choose a publication target first."
            )
        publication_targets = self._publication_targets(transition)
        source_name = _required_string(drive_item, "name")
        source_bytes = self._control.download_content(document.key.item_id)
        source_bytes = self._processor.decrypt_source(
            source_bytes, filename=source_name
        )
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
        if not self._repository.publication_transition_matches(transition):
            raise StaleRequestError(
                "The publication target changed before publication."
            )
        reporter.stage(
            Stage.PUBLISHING,
            message=", ".join(
                self._publisher(candidate).label
                for candidate in publication_targets
            ),
        )
        publications: dict[PublicationTarget, PublicationResult] = {}
        operation_id = self._publication_operation_id(job)
        for publication_target in publication_targets:
            publisher = self._publisher(publication_target)
            acl = self._publication_acl(publisher, document.key.item_id)
            locator = publisher.locator(
                document=document,
                drive_item=drive_item,
            )
            with self._repository.publication_operation_lock(
                job.document_key,
                publication_target,
            ):
                if not self._job_claim_is_current(job):
                    raise StaleRequestError(
                        "The job lease changed before the remote write."
                    )
                if not self._repository.publication_transition_matches(transition):
                    raise StaleRequestError(
                        "The publication target changed before the remote write."
                    )
                if not self._repository.record_publication_pending(
                    job.document_key,
                    transition=transition,
                    target=publication_target,
                    locator=locator,
                    operation_id=operation_id,
                    request_id=job.request_id,
                    job_id=job.job_id,
                    expected_attempts=job.attempts,
                    expected_lease_owner=self._worker_id,
                ):
                    raise StaleRequestError(
                        "The publication target changed before the remote write."
                    )
                try:
                    publication = publisher.publish(
                        document=document,
                        drive_item=drive_item,
                        rendered=processed.rendered,
                        acl=acl,
                        source_fingerprint=source_fingerprint,
                    )
                except KNOWN_PROCESSING_ERRORS as exc:
                    self._repository.record_publication_error(
                        job.document_key,
                        target=publication_target,
                        generation=transition.generation,
                        code=_error_code(exc),
                        message=str(exc),
                        operation_id=operation_id,
                    )
                    raise
                if not self._still_current(job) or not self._job_claim_is_current(job):
                    self._repository.record_publication_error(
                        job.document_key,
                        target=publication_target,
                        generation=transition.generation,
                        code="STALE_REQUEST",
                        message="A newer request or lease superseded this publication.",
                        operation_id=operation_id,
                    )
                    raise StaleRequestError(
                        "A newer command replaced this request during publication."
                    )
                if not self._repository.publication_transition_matches(transition):
                    self._repository.record_publication_error(
                        job.document_key,
                        target=publication_target,
                        generation=transition.generation,
                        code="STALE_PUBLICATION_TRANSITION",
                        message="The publication target changed during publication.",
                        operation_id=operation_id,
                    )
                    raise StaleRequestError(
                        "The publication target changed during publication."
                    )
                if not self._repository.record_publication_success(
                    job.document_key,
                    target=publication_target,
                    generation=transition.generation,
                    locator=publication.locator,
                    content_hash=publication.content_hash,
                    original_characters=publication.original_characters,
                    stored_characters=publication.stored_characters,
                    stored_bytes=publication.stored_bytes,
                    truncated=publication.truncated,
                    operation_id=operation_id,
                ):
                    raise StaleRequestError(
                        "The publication operation was superseded before commit."
                    )
            publications[publication_target] = publication

        publication = publications[target]
        connector_publication = publications.get(
            PublicationTarget.COPILOT_CONNECTOR
        )

        version = document.enhancement_version + 1
        self._store_artifacts(document, processed, version=version)
        if not self._repository.record_success(
            job.document_key,
            request_id=job.request_id,
            source_etag=source_fingerprint,
            last_modified_datetime=_required_string(
                drive_item, "lastModifiedDateTime"
            ),
            source_size=len(source_bytes),
            acl_digest=(
                connector_publication.acl_digest
                if connector_publication is not None
                else publication.acl_digest
            ),
            external_item_id=(
                connector_publication.locator
                if connector_publication is not None
                else None
            ),
            web_url=publication.source_url,
            file_name=_required_string(drive_item, "name"),
            html_bytes=processed.rendered.byte_count,
            request_bytes=publication.request_bytes,
            content_hash=processed.rendered.sha256,
            job_id=job.job_id,
            expected_attempts=job.attempts,
            expected_lease_owner=self._worker_id,
        ):
            raise StaleRequestError(
                "A newer request completed before this result was committed."
            )
        self._maybe_capture_feedback(job, document, processed, version)
        self._control.set_ready(
            item,
            html_bytes=processed.rendered.byte_count,
            request_bytes=publication.request_bytes,
        )
        reporter.stage(
            Stage.READY,
            detail={
                "htmlBytes": processed.rendered.byte_count,
                "requestBytes": publication.request_bytes,
                "publicationTarget": target.value,
                "publicationTargets": [
                    candidate.value for candidate in publication_targets
                ],
                "originalCharacters": publication.original_characters,
                "storedCharacters": publication.stored_characters,
                "truncated": publication.truncated,
                "version": version,
            },
        )
        self._repository.complete_job(
            job.job_id,
            stage_timings=processed.stage_timings,
            usage=processed.analysis.usage,
            html_bytes=processed.rendered.byte_count,
            request_bytes=publication.request_bytes,
            expected_attempts=job.attempts,
            expected_lease_owner=self._worker_id,
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
        source_bytes = self._processor.decrypt_source(
            source_bytes, filename=document.file_name
        )
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
        if not self._repository.record_success(
            job.document_key,
            request_id=job.request_id,
            source_etag=source_fingerprint,
            last_modified_datetime=_utc_iso(),
            source_size=len(source_bytes),
            acl_digest="",
            external_item_id=None,
            web_url=document.web_url,
            file_name=document.file_name,
            html_bytes=processed.rendered.byte_count,
            request_bytes=processed.rendered.byte_count,
            content_hash=processed.rendered.sha256,
            job_id=job.job_id,
            expected_attempts=job.attempts,
            expected_lease_owner=self._worker_id,
        ):
            raise StaleRequestError(
                "A newer upload request completed before this result was committed."
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
            expected_attempts=job.attempts,
            expected_lease_owner=self._worker_id,
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
        if not self._still_current(job) or not self._job_claim_is_current(job):
            raise StaleRequestError("Remove request is no longer active.")
        self._control.set_removing(item)
        publications = self._repository.list_document_publications(document.key)
        reporter.stage(Stage.REMOVING, message="게시 콘텐츠 삭제")
        drive_item: dict[str, Any] | None = None
        if not publications:
            target = self._repository.publication_target_for_jobs()
            if target is not PublicationTarget.UNSET:
                try:
                    drive_item = self._control.get_drive_item(document.key.item_id)
                except GraphRequestError as exc:
                    if exc.status_code != 404:
                        raise
                with self._repository.publication_operation_lock(
                    document.key,
                    target,
                ):
                    if (
                        not self._still_current(job)
                        or not self._job_claim_is_current(job)
                    ):
                        raise StaleRequestError(
                            "A newer request superseded this removal."
                        )
                    current = self._repository.get_publication(
                        document.key,
                        target,
                    )
                    if current is None:
                        self._publisher(target).remove(
                            document=document,
                            locator=document.external_item_id,
                            drive_item=drive_item,
                        )
                    elif not self._claim_and_remove_publication(
                        document=document,
                        publication=current,
                        operation_id=job.request_id,
                        drive_item=drive_item,
                    ):
                        raise PublicationError(
                            "PUBLICATION_REMOVAL_CONFLICT: "
                            "the publication changed before deletion."
                        )
        for publication in publications:
            with self._repository.publication_operation_lock(
                document.key,
                publication.target,
            ):
                if (
                    not self._still_current(job)
                    or not self._job_claim_is_current(job)
                ):
                    raise StaleRequestError(
                        "A newer request superseded this removal."
                    )
                current = self._repository.get_publication(
                    document.key,
                    publication.target,
                )
                if current is None:
                    continue
                if not self._claim_and_remove_publication(
                    document=document,
                    publication=current,
                    operation_id=job.request_id,
                    drive_item=drive_item,
                ):
                    if (
                        not self._still_current(job)
                        or not self._job_claim_is_current(job)
                    ):
                        raise StaleRequestError(
                            "A newer request superseded publication deletion."
                        )
                    raise PublicationError(
                        "PUBLICATION_REMOVAL_CONFLICT: "
                        "the publication changed before deletion."
                    )
        if not self._still_current(job) or not self._job_claim_is_current(job):
            raise StaleRequestError(
                "A newer command replaced this request during deletion."
            )
        if not self._repository.record_removed(
            job.document_key,
            request_id=job.request_id,
            job_id=job.job_id,
            expected_attempts=job.attempts,
            expected_lease_owner=self._worker_id,
        ):
            raise StaleRequestError(
                "A newer request completed before removal was committed."
            )
        if self._artifacts is not None:
            self._artifacts.delete_prefix(document_prefix(document.key))
        self._control.set_not_enabled(item)
        reporter.stage(Stage.REMOVED)
        self._repository.complete_job(
            job.job_id,
            expected_attempts=job.attempts,
            expected_lease_owner=self._worker_id,
        )

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

    def _job_claim_is_current(self, job: JobRecord) -> bool:
        return self._repository.job_claim_is_current(
            job.job_id,
            attempts=job.attempts,
            lease_owner=self._worker_id,
        )

    def _publication_operation_id(self, job: JobRecord) -> str:
        raw = f"{job.job_id}:{job.attempts}:{self._worker_id}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _publisher(self, target: PublicationTarget) -> Publisher:
        try:
            return self._publishers[target]
        except KeyError as exc:
            raise PublicationError(
                f"PUBLICATION_TARGET_UNAVAILABLE: {target.value}."
            ) from exc

    def _claim_and_remove_publication(
        self,
        *,
        document: DocumentRecord,
        publication: PublicationRecord,
        operation_id: str,
        drive_item: dict[str, Any] | None = None,
        remove_remote: bool = True,
    ) -> bool:
        if not self._repository.claim_publication_removal(
            document.key,
            publication.target,
            expected_updated_at=publication.updated_at,
            operation_id=operation_id,
        ):
            return False
        current = self._repository.get_publication(
            document.key,
            publication.target,
        )
        if (
            current is None
            or current.status != "removing"
            or current.operation_id != operation_id
        ):
            return False
        if remove_remote:
            self._publisher(publication.target).remove(
                document=document,
                locator=current.locator,
                drive_item=drive_item,
            )
        return self._repository.remove_publication_record(
            document.key,
            publication.target,
            operation_id=operation_id,
        )

    def _publication_acl(
        self,
        publisher: Publisher,
        item_id: str,
    ) -> tuple[Any, ...]:
        if not publisher.requires_acl:
            return ()
        permissions = self._control.list_permissions(item_id)
        return map_drive_item_permissions(permissions)

    @staticmethod
    def _publication_targets(
        transition: PublicationTransition,
    ) -> tuple[PublicationTarget, ...]:
        effective = transition.effective_target
        if effective is PublicationTarget.UNSET:
            return ()
        if (
            transition.status not in {"active", "cleaning"}
            and transition.active_target is not PublicationTarget.UNSET
            and transition.active_target is not effective
        ):
            return (transition.active_target, effective)
        return (effective,)

    def _queue_reconcile_refresh(
        self,
        document: DocumentRecord,
        *,
        message: str,
    ) -> bool:
        queued = self._repository.queue_refresh_if_current(
            document.key,
            expected_request_id=document.request_id,
            trigger=f"reconcile: {message}",
        )
        return queued is not None

    def _advance_publication_transition(self) -> None:
        transition = self._repository.get_publication_transition()
        if transition.status == "active":
            return
        target = transition.desired_target
        if target is PublicationTarget.UNSET:
            if not self._repository.set_transition_status(
                "cleaning",
                expected_generation=transition.generation,
                expected_status=transition.status,
                expected_desired_target=transition.desired_target,
                expected_active_target=transition.active_target,
            ):
                return
            if not self._cleanup_previous_target_safely(
                transition.active_target,
                transition,
            ):
                return
            self._repository.activate_publication_target(
                expected_generation=transition.generation,
                expected_desired_target=transition.desired_target,
                expected_active_target=transition.active_target,
            )
            return

        if (
            target is PublicationTarget.SHAREPOINT_COLUMN
            and not transition.column_provisioned
        ):
            self._repository.set_transition_status(
                "failed",
                code="CONTENT_COLUMN_NOT_PROVISIONED",
                message="Provision the SharePoint content site column first.",
                expected_generation=transition.generation,
                expected_status=transition.status,
                expected_desired_target=transition.desired_target,
                expected_active_target=transition.active_target,
            )
            return

        documents = self._repository.list_desired_sharepoint_documents()
        waiting_for_jobs = False
        for document in documents:
            if document.status == "Queued":
                if not self._repository.has_pending_job(
                    document.key,
                    request_id=document.request_id,
                ):
                    self._repository.queue_refresh(
                        document.key,
                        trigger="worker-publication-transition-recovery",
                    )
                waiting_for_jobs = True
            elif document.status != "Ready":
                waiting_for_jobs = True
        if waiting_for_jobs:
            return

        ready = {
            publication.document_key
            for publication in self._repository.list_publications(
                target=target,
                generation=transition.generation,
            )
            if publication.status == "ready"
        }
        missing = [document for document in documents if document.key not in ready]
        for document in missing:
            publication = self._repository.get_publication(document.key, target)
            if publication is not None and publication.status == "failed":
                continue
            if document.status == "Ready":
                self._repository.queue_refresh(
                    document.key,
                    trigger="worker-publication-transition-recovery",
                )
        if missing:
            return

        if (
            transition.status == "cleaning"
            and transition.desired_target is transition.active_target
        ):
            for candidate in (
                PublicationTarget.SHAREPOINT_COLUMN,
                PublicationTarget.COPILOT_CONNECTOR,
            ):
                if candidate is transition.active_target:
                    continue
                if not self._cleanup_previous_target_safely(candidate, transition):
                    return
            self._repository.activate_publication_target(
                expected_generation=transition.generation,
                expected_desired_target=transition.desired_target,
                expected_active_target=transition.active_target,
            )
            return

        if target is PublicationTarget.SHAREPOINT_COLUMN:
            if not transition.reindex_requested:
                self._repository.set_transition_status(
                    "awaiting_reindex",
                    expected_generation=transition.generation,
                    expected_status=transition.status,
                    expected_desired_target=transition.desired_target,
                    expected_active_target=transition.active_target,
                )
                return
            if not transition.search_verified:
                self._repository.set_transition_status(
                    "awaiting_search",
                    expected_generation=transition.generation,
                    expected_status=transition.status,
                    expected_desired_target=transition.desired_target,
                    expected_active_target=transition.active_target,
                )
                return
            if not transition.copilot_verified:
                self._repository.set_transition_status(
                    "awaiting_copilot",
                    expected_generation=transition.generation,
                    expected_status=transition.status,
                    expected_desired_target=transition.desired_target,
                    expected_active_target=transition.active_target,
                )
                return

        if not self._repository.set_transition_status(
            "cleaning",
            expected_generation=transition.generation,
            expected_status=transition.status,
            expected_desired_target=transition.desired_target,
            expected_active_target=transition.active_target,
        ):
            return
        if not self._cleanup_previous_target_safely(
            transition.active_target,
            transition,
        ):
            return
        self._repository.activate_publication_target(
            expected_generation=transition.generation,
            expected_desired_target=transition.desired_target,
            expected_active_target=transition.active_target,
        )

    def _cleanup_previous_target_safely(
        self,
        target: PublicationTarget,
        transition: PublicationTransition,
    ) -> bool:
        try:
            return self._cleanup_previous_target(target, transition)
        except KNOWN_PROCESSING_ERRORS as exc:
            code = _error_code(exc)
            message = str(exc)
            self._repository.set_transition_status(
                "failed",
                code=code,
                message=message,
                expected_generation=transition.generation,
                expected_status="cleaning",
                expected_desired_target=transition.desired_target,
                expected_active_target=transition.active_target,
            )
            LOGGER.error(
                "Publication transition cleanup failed for %s: %s",
                target.value,
                message,
            )
            return False

    def _cleanup_previous_target(
        self,
        target: PublicationTarget,
        transition: PublicationTransition,
    ) -> bool:
        if target is PublicationTarget.UNSET:
            return self._repository.publication_transition_matches(
                transition,
                status="cleaning",
            )
        if target is transition.desired_target:
            return False
        publications = self._repository.list_publications(target=target)
        if any(publication.status == "pending" for publication in publications):
            return False
        for publication in publications:
            with self._repository.publication_operation_lock(
                publication.document_key,
                target,
            ):
                if not self._repository.publication_transition_matches(
                    transition,
                    status="cleaning",
                ):
                    return False
                current = self._repository.get_publication(
                    publication.document_key,
                    target,
                )
                if current is None:
                    continue
                if current.status == "pending":
                    return False
                document = self._repository.get_document(current.document_key)
                if document is None:
                    continue
                operation_id = (
                    f"transition-cleanup:{transition.generation}:{uuid4()}"
                )
                if not self._claim_and_remove_publication(
                    document=document,
                    publication=current,
                    operation_id=operation_id,
                ):
                    return False
        return self._repository.publication_transition_matches(
            transition,
            status="cleaning",
        )


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


def _error_code(error: Exception) -> str:
    if isinstance(error, GraphRequestError):
        return error.code or f"GRAPH_HTTP_{error.status_code or 'UNKNOWN'}"
    message = str(error)
    prefix = message.split(":", 1)[0]
    if prefix and prefix.replace("_", "").isalnum() and prefix.upper() == prefix:
        return prefix[:80]
    return type(error).__name__.upper()[:80]

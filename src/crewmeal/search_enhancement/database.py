"""Repository for the search-enhancement queue.

Historically this module spoke raw ``sqlite3``. It now runs on SQLAlchemy Core
so the same logical model can target SQLite (local development and the test
suite) and PostgreSQL (production on Azure). The public surface used by the
worker and the existing test-suite is preserved verbatim; the additional
methods power the web portal (status pages, admin, feedback corpus).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, delete, func, insert, inspect, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine

from crewmeal.search_enhancement.schema import (
    METADATA,
    UPLOAD_TENANT_ID,
    artifacts,
    create_db_engine,
    document_publications,
    documents,
    feedback_records,
    job_events,
    jobs,
    publication_transitions,
    settings,
)
from crewmeal.search_enhancement.publication import (
    PublicationTarget,
    parse_publication_target,
)

LEGACY_PUBLICATION_MIGRATION_SETTING = (
    "migration.publication.legacy_connector_pending"
)
_INITIALIZATION_ADVISORY_LOCK_ID = 0x435245574D45414C
_LOCAL_INITIALIZATION_LOCK = Lock()
_LOCAL_PUBLICATION_LOCKS_GUARD = Lock()
_LOCAL_PUBLICATION_LOCKS: dict[str, Lock] = {}


# ---------------------------------------------------------------------------
# Data transfer objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DocumentKey:
    tenant_id: str
    site_id: str
    drive_id: str
    item_id: str


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    key: DocumentKey
    list_id: str
    list_item_id: str
    web_url: str
    file_name: str
    desired_enabled: bool
    status: str
    request_id: str
    source_etag: str | None
    last_modified_datetime: str | None
    source_size: int | None
    acl_hash: str | None
    external_item_id: str | None
    processed_request_id: str | None
    html_bytes: int | None
    request_bytes: int | None
    content_hash: str | None
    status_token: str | None = None
    source_kind: str = "sharepoint"
    enhancement_version: int = 0
    correction_notes: str | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    updated_at: str | None = None
    processed_at: str | None = None


@dataclass(frozen=True, slots=True)
class JobRecord:
    job_id: str
    document_key: DocumentKey
    request_id: str
    job_type: str
    status: str
    attempts: int
    lease_owner: str | None
    trigger: str | None = None
    feedback: str | None = None


@dataclass(frozen=True, slots=True)
class JobEventRecord:
    id: int
    job_id: str
    stage: str
    message: str | None
    detail: dict[str, Any] | None
    created_at: str


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    id: int
    document_key: DocumentKey
    kind: str
    blob_path: str
    content_type: str | None
    content_hash: str | None
    byte_count: int | None
    enhancement_version: int | None
    created_at: str


@dataclass(frozen=True, slots=True)
class FeedbackRecord:
    id: int
    document_key: DocumentKey
    enhancement_version: int | None
    source_artifact_path: str | None
    before_html_path: str | None
    before_json_path: str | None
    correction_text: str
    after_html_path: str | None
    after_json_path: str | None
    category: str | None
    tags: list[str]
    model: str | None
    deployment: str | None
    created_by: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class PublicationRecord:
    document_key: DocumentKey
    target: PublicationTarget
    generation: int
    locator: str | None
    status: str
    operation_id: str | None
    content_hash: str | None
    original_characters: int | None
    stored_characters: int | None
    stored_bytes: int | None
    truncated: bool
    error_code: str | None
    error_message: str | None
    published_at: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class PublicationTransition:
    active_target: PublicationTarget
    desired_target: PublicationTarget
    generation: int
    status: str
    column_provisioned: bool
    reindex_requested: bool
    search_verified: bool
    copilot_verified: bool
    last_error_code: str | None
    last_error_message: str | None
    created_at: str
    updated_at: str

    @property
    def effective_target(self) -> PublicationTarget:
        if self.status == "active":
            return self.active_target
        if (
            self.desired_target is PublicationTarget.SHAREPOINT_COLUMN
            and not self.column_provisioned
        ):
            return self.active_target
        return self.desired_target


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class SearchEnhancementRepository:
    """Queue + document store backed by SQLAlchemy Core."""

    def __init__(self, target: "str | Path | Engine") -> None:
        self._engine = create_db_engine(target)

    @classmethod
    def from_url(cls, url: str) -> "SearchEnhancementRepository":
        return cls(url)

    @property
    def engine(self) -> Engine:
        return self._engine

    @property
    def dialect(self) -> str:
        return self._engine.dialect.name

    def initialize(self) -> None:
        with self._initialization_lock():
            inspector = inspect(self._engine)
            existing_install = inspector.has_table(documents.name) and not (
                inspector.has_table(publication_transitions.name)
            )
            migration_pending = self._legacy_publication_migration_pending(
                inspector
            )
            if existing_install:
                self._set_legacy_publication_migration_pending()
            METADATA.create_all(self._engine)
            self._ensure_publication_transition(
                existing_install=existing_install or migration_pending
            )
            self._backfill_legacy_publications()
            self._clear_legacy_publication_migration_pending()

    def dispose(self) -> None:
        self._engine.dispose()

    @contextmanager
    def _initialization_lock(self) -> Iterator[None]:
        if self.dialect != "postgresql":
            with _LOCAL_INITIALIZATION_LOCK:
                yield
            return
        with self._engine.connect() as conn:
            conn.execute(
                text("SELECT pg_advisory_lock(:lock_id)"),
                {"lock_id": _INITIALIZATION_ADVISORY_LOCK_ID},
            )
            try:
                yield
            finally:
                conn.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": _INITIALIZATION_ADVISORY_LOCK_ID},
                )

    @contextmanager
    def publication_operation_lock(
        self,
        key: DocumentKey,
        target: PublicationTarget | str,
    ) -> Iterator[None]:
        parsed = parse_publication_target(target)
        lock_name = "\x1f".join(
            (
                key.tenant_id,
                key.site_id,
                key.drive_id,
                key.item_id,
                parsed.value,
            )
        )
        if self.dialect != "postgresql":
            with _LOCAL_PUBLICATION_LOCKS_GUARD:
                lock = _LOCAL_PUBLICATION_LOCKS.setdefault(lock_name, Lock())
            with lock:
                yield
            return

        lock_id = int.from_bytes(
            hashlib.sha256(lock_name.encode("utf-8")).digest()[:8],
            byteorder="big",
            signed=True,
        )
        with self._engine.connect() as conn:
            conn.execute(
                text("SELECT pg_advisory_lock(:lock_id)"),
                {"lock_id": lock_id},
            )
            try:
                yield
            finally:
                conn.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": lock_id},
                )

    # -- documents --------------------------------------------------------

    def upsert_document(
        self,
        *,
        key: DocumentKey,
        list_id: str,
        list_item_id: str,
        web_url: str,
        file_name: str,
        connection_id: str,
        desired_enabled: bool,
        status: str,
        request_id: str,
    ) -> None:
        with self._engine.begin() as conn:
            self._upsert_document(
                conn,
                key=key,
                list_id=list_id,
                list_item_id=list_item_id,
                web_url=web_url,
                file_name=file_name,
                connection_id=connection_id,
                desired_enabled=desired_enabled,
                status=status,
                request_id=request_id,
            )

    def upsert_document_and_enqueue(
        self,
        *,
        key: DocumentKey,
        list_id: str,
        list_item_id: str,
        web_url: str,
        file_name: str,
        connection_id: str,
        desired_enabled: bool,
        status: str,
        request_id: str,
        job_type: str,
        trigger: str | None = None,
    ) -> str:
        if job_type not in {"upsert", "delete"}:
            raise ValueError(f"Unsupported job type: {job_type}")
        with self._engine.begin() as conn:
            self._upsert_document(
                conn,
                key=key,
                list_id=list_id,
                list_item_id=list_item_id,
                web_url=web_url,
                file_name=file_name,
                connection_id=connection_id,
                desired_enabled=desired_enabled,
                status=status,
                request_id=request_id,
            )
            self._set_publication_request_intent(
                conn,
                key=key,
                request_id=request_id,
                removing=job_type == "delete",
            )
            return self._enqueue_job(
                conn,
                key=key,
                request_id=request_id,
                job_type=job_type,
                trigger=trigger,
            )

    @staticmethod
    def _set_publication_request_intent(
        conn: Connection,
        *,
        key: DocumentKey,
        request_id: str,
        removing: bool,
    ) -> None:
        stmt = update(document_publications).where(
            _key_where(document_publications, key)
        )
        if removing:
            conn.execute(
                stmt.values(
                    status="removing",
                    operation_id=request_id,
                    error_code="PENDING_DELETE",
                    error_message=request_id,
                    updated_at=_utc_now(),
                )
            )
            return
        conn.execute(
            stmt.where(document_publications.c.status == "removing").values(
                status="failed",
                operation_id=None,
                error_code="REMOVAL_SUPERSEDED",
                error_message="A newer enhance request superseded removal.",
                updated_at=_utc_now(),
            )
        )

    def _upsert_document(
        self,
        conn: Connection,
        *,
        key: DocumentKey,
        list_id: str,
        list_item_id: str,
        web_url: str,
        file_name: str,
        connection_id: str,
        desired_enabled: bool,
        status: str,
        request_id: str,
    ) -> None:
        now = _utc_now()
        stmt = self._insert(documents).values(
            tenant_id=key.tenant_id,
            site_id=key.site_id,
            drive_id=key.drive_id,
            item_id=key.item_id,
            list_id=list_id,
            list_item_id=list_item_id,
            web_url=web_url,
            file_name=file_name,
            connection_id=connection_id,
            desired_enabled=int(desired_enabled),
            status=status,
            request_id=request_id,
            status_token=_new_token(),
            source_kind="sharepoint",
            enhancement_version=0,
            created_at=now,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                documents.c.tenant_id,
                documents.c.site_id,
                documents.c.drive_id,
                documents.c.item_id,
            ],
            set_={
                "list_id": stmt.excluded.list_id,
                "list_item_id": stmt.excluded.list_item_id,
                "web_url": stmt.excluded.web_url,
                "file_name": stmt.excluded.file_name,
                "connection_id": stmt.excluded.connection_id,
                "desired_enabled": stmt.excluded.desired_enabled,
                "status": stmt.excluded.status,
                "request_id": stmt.excluded.request_id,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        conn.execute(stmt)

    def get_document(self, key: DocumentKey) -> DocumentRecord | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(documents).where(_key_where(documents, key))
            ).mappings().first()
        return _document_from_row(row) if row else None

    def get_document_by_token(self, status_token: str) -> DocumentRecord | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(documents).where(documents.c.status_token == status_token)
            ).mappings().first()
        return _document_from_row(row) if row else None

    def list_enabled_documents(self) -> tuple[DocumentRecord, ...]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(documents)
                .where(
                    documents.c.desired_enabled == 1,
                    documents.c.status == "Ready",
                    documents.c.source_kind == "sharepoint",
                )
                .order_by(documents.c.updated_at)
            ).mappings().all()
        return tuple(_document_from_row(row) for row in rows)

    def list_desired_sharepoint_documents(self) -> tuple[DocumentRecord, ...]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(documents)
                .where(
                    documents.c.desired_enabled == 1,
                    documents.c.source_kind == "sharepoint",
                )
                .order_by(documents.c.updated_at)
            ).mappings().all()
        return tuple(_document_from_row(row) for row in rows)

    def list_documents(
        self,
        *,
        source_kind: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[DocumentRecord, ...]:
        stmt = select(documents)
        if source_kind is not None:
            stmt = stmt.where(documents.c.source_kind == source_kind)
        if status is not None:
            stmt = stmt.where(documents.c.status == status)
        stmt = stmt.order_by(documents.c.updated_at.desc()).limit(limit).offset(offset)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return tuple(_document_from_row(row) for row in rows)

    def count_documents(
        self, *, source_kind: str | None = None, status: str | None = None
    ) -> int:
        stmt = select(func.count()).select_from(documents)
        if source_kind is not None:
            stmt = stmt.where(documents.c.source_kind == source_kind)
        if status is not None:
            stmt = stmt.where(documents.c.status == status)
        with self._engine.connect() as conn:
            return int(conn.execute(stmt).scalar_one())

    def document_status_counts(self) -> dict[str, int]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(documents.c.status, func.count()).group_by(documents.c.status)
            ).all()
        return {str(status): int(count) for status, count in rows}

    def delete_document(self, key: DocumentKey) -> None:
        with self._engine.begin() as conn:
            conn.execute(delete(documents).where(_key_where(documents, key)))

    def create_upload_document(
        self,
        *,
        file_name: str,
        connection_id: str,
        correction_note: str | None = None,
        created_by: str | None = None,
    ) -> DocumentRecord:
        """Create a synthetic document for an admin upload (tryout) job."""

        key = DocumentKey(UPLOAD_TENANT_ID, "upload", "upload", str(uuid4()))
        token = _new_token()
        request_id = str(uuid4())
        now = _utc_now()
        notes = _as_notes_list(correction_note, now, created_by)
        with self._engine.begin() as conn:
            conn.execute(
                insert(documents).values(
                    tenant_id=key.tenant_id,
                    site_id=key.site_id,
                    drive_id=key.drive_id,
                    item_id=key.item_id,
                    list_id="upload",
                    list_item_id=key.item_id,
                    web_url="",
                    file_name=file_name,
                    connection_id=connection_id,
                    desired_enabled=1,
                    status="Queued",
                    request_id=request_id,
                    status_token=token,
                    source_kind="upload",
                    enhancement_version=0,
                    correction_notes=_json_or_none(notes) if notes else None,
                    created_at=now,
                    updated_at=now,
                )
            )
        record = self.get_document(key)
        assert record is not None
        return record

    # -- job queue --------------------------------------------------------

    def enqueue_job(
        self,
        *,
        key: DocumentKey,
        request_id: str,
        job_type: str,
        trigger: str | None = None,
        feedback: str | None = None,
    ) -> str:
        if job_type not in {"upsert", "delete"}:
            raise ValueError(f"Unsupported job type: {job_type}")
        with self._engine.begin() as conn:
            return self._enqueue_job(
                conn,
                key=key,
                request_id=request_id,
                job_type=job_type,
                trigger=trigger,
                feedback=feedback,
            )

    def _enqueue_job(
        self,
        conn: Connection,
        *,
        key: DocumentKey,
        request_id: str,
        job_type: str,
        trigger: str | None = None,
        feedback: str | None = None,
    ) -> str:
        job_id = str(uuid4())
        stmt = self._insert(jobs).values(
            job_id=job_id,
            tenant_id=key.tenant_id,
            site_id=key.site_id,
            drive_id=key.drive_id,
            item_id=key.item_id,
            request_id=request_id,
            job_type=job_type,
            status="queued",
            trigger=trigger,
            feedback=feedback,
            queued_at=_utc_now(),
        ).on_conflict_do_nothing(
            index_elements=[
                jobs.c.tenant_id,
                jobs.c.site_id,
                jobs.c.drive_id,
                jobs.c.item_id,
                jobs.c.request_id,
                jobs.c.job_type,
            ]
        )
        conn.execute(stmt)
        row = conn.execute(
            select(jobs.c.job_id).where(
                jobs.c.tenant_id == key.tenant_id,
                jobs.c.site_id == key.site_id,
                jobs.c.drive_id == key.drive_id,
                jobs.c.item_id == key.item_id,
                jobs.c.request_id == request_id,
                jobs.c.job_type == job_type,
            )
        ).first()
        if row is None:
            raise RuntimeError("The idempotent job insert could not be read back.")
        return str(row[0])

    def claim_next_job(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> JobRecord | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive.")
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        expires = (now + timedelta(seconds=lease_seconds)).isoformat()
        if self.dialect == "postgresql":
            return self._claim_postgres(worker_id, now_text, expires)
        return self._claim_sqlite(worker_id, now_text, expires)

    def has_pending_job(self, key: DocumentKey, *, request_id: str) -> bool:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(jobs.c.job_id).where(
                    _key_where(jobs, key),
                    jobs.c.request_id == request_id,
                    jobs.c.status.in_(("queued", "processing")),
                )
            ).first()
        return row is not None

    def job_claim_is_current(
        self,
        job_id: str,
        *,
        attempts: int,
        lease_owner: str,
    ) -> bool:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(jobs.c.job_id).where(
                    jobs.c.job_id == job_id,
                    jobs.c.status == "processing",
                    jobs.c.attempts == attempts,
                    jobs.c.lease_owner == lease_owner,
                )
            ).first()
        return row is not None

    def _claim_postgres(
        self, worker_id: str, now_text: str, expires: str
    ) -> JobRecord | None:
        with self._engine.begin() as conn:
            candidate = conn.execute(
                select(jobs.c.job_id)
                .where(
                    or_(
                        jobs.c.status == "queued",
                        and_(
                            jobs.c.status == "processing",
                            jobs.c.lease_expires_at <= now_text,
                        ),
                    )
                )
                .order_by(jobs.c.queued_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            ).first()
            if candidate is None:
                return None
            job_id = candidate[0]
            conn.execute(
                update(jobs)
                .where(jobs.c.job_id == job_id)
                .values(
                    status="processing",
                    attempts=jobs.c.attempts + 1,
                    lease_owner=worker_id,
                    lease_expires_at=expires,
                    started_at=func.coalesce(jobs.c.started_at, now_text),
                )
            )
            row = conn.execute(
                select(jobs).where(jobs.c.job_id == job_id)
            ).mappings().first()
        return _job_from_row(row) if row else None

    def _claim_sqlite(
        self, worker_id: str, now_text: str, expires: str
    ) -> JobRecord | None:
        raw = self._engine.raw_connection()
        try:
            dbapi: sqlite3.Connection = raw.driver_connection  # type: ignore[assignment]
            dbapi.row_factory = sqlite3.Row
            dbapi.execute("BEGIN IMMEDIATE")
            try:
                row = dbapi.execute(
                    """
                    SELECT * FROM jobs
                    WHERE status = 'queued'
                       OR (status = 'processing' AND lease_expires_at <= ?)
                    ORDER BY queued_at
                    LIMIT 1
                    """,
                    (now_text,),
                ).fetchone()
                if row is None:
                    dbapi.commit()
                    return None
                dbapi.execute(
                    """
                    UPDATE jobs
                    SET status = 'processing',
                        attempts = attempts + 1,
                        lease_owner = ?,
                        lease_expires_at = ?,
                        started_at = COALESCE(started_at, ?)
                    WHERE job_id = ?
                    """,
                    (worker_id, expires, now_text, row["job_id"]),
                )
                claimed = dbapi.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)
                ).fetchone()
                dbapi.commit()
            except BaseException:
                dbapi.rollback()
                raise
        finally:
            raw.close()
        return _job_from_row(claimed)

    def get_job(self, job_id: str) -> JobRecord | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(jobs).where(jobs.c.job_id == job_id)
            ).mappings().first()
        return _job_from_row(row) if row else None

    def get_job_detail(self, job_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(jobs, documents.c.file_name, documents.c.status_token)
                .select_from(jobs.join(documents, _join_on(jobs, documents)))
                .where(jobs.c.job_id == job_id)
            ).mappings().first()
        return dict(row) if row else None

    def get_latest_job(
        self, key: DocumentKey, *, job_type: str | None = None
    ) -> JobRecord | None:
        stmt = select(jobs).where(_key_where(jobs, key))
        if job_type is not None:
            stmt = stmt.where(jobs.c.job_type == job_type)
        stmt = stmt.order_by(jobs.c.queued_at.desc()).limit(1)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return _job_from_row(row) if row else None

    def list_recent_jobs(
        self, *, status: str | None = None, limit: int = 50
    ) -> tuple[dict[str, Any], ...]:
        stmt = (
            select(
                jobs.c.job_id,
                jobs.c.job_type,
                jobs.c.status,
                jobs.c.attempts,
                jobs.c.trigger,
                jobs.c.queued_at,
                jobs.c.started_at,
                jobs.c.completed_at,
                jobs.c.error_code,
                jobs.c.error_message,
                jobs.c.tenant_id,
                jobs.c.site_id,
                jobs.c.drive_id,
                jobs.c.item_id,
                documents.c.file_name,
                documents.c.status_token,
                documents.c.source_kind,
            )
            .select_from(jobs.join(documents, _join_on(jobs, documents)))
            .order_by(jobs.c.queued_at.desc())
            .limit(limit)
        )
        if status is not None:
            stmt = stmt.where(jobs.c.status == status)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return tuple(dict(row) for row in rows)

    def job_status_counts(self) -> dict[str, int]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(jobs.c.status, func.count()).group_by(jobs.c.status)
            ).all()
        return {str(status): int(count) for status, count in rows}

    def job_usages(
        self, key: DocumentKey | None = None
    ) -> tuple[dict[str, Any], ...]:
        """Return parsed ``usage_json`` dicts for cost estimation.

        With ``key`` it returns usage for a single document's jobs (its cumulative
        enhancement cost, including reruns); without a key it returns usage for
        every job (the fleet-wide total).
        """

        stmt = select(jobs.c.usage_json)
        if key is not None:
            stmt = stmt.where(_key_where(jobs, key))
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        usages: list[dict[str, Any]] = []
        for (raw,) in rows:
            parsed = _json_loads_or_none(raw)
            if isinstance(parsed, dict):
                usages.append(parsed)
        return tuple(usages)

    def job_usage(self, job_id: str) -> dict[str, Any] | None:
        """Return parsed token usage for one job, if it has completed."""

        with self._engine.connect() as conn:
            raw = conn.execute(
                select(jobs.c.usage_json).where(jobs.c.job_id == job_id)
            ).scalar_one_or_none()
        parsed = _json_loads_or_none(raw)
        return parsed if isinstance(parsed, dict) else None

    def complete_job(
        self,
        job_id: str,
        *,
        stage_timings: dict[str, float] | None = None,
        usage: dict[str, Any] | None = None,
        html_bytes: int | None = None,
        request_bytes: int | None = None,
        expected_attempts: int | None = None,
        expected_lease_owner: str | None = None,
    ) -> bool:
        stmt = update(jobs).where(jobs.c.job_id == job_id)
        if expected_attempts is not None:
            stmt = stmt.where(
                jobs.c.status == "processing",
                jobs.c.attempts == expected_attempts,
                jobs.c.lease_owner == expected_lease_owner,
            )
        with self._engine.begin() as conn:
            result = conn.execute(
                stmt.values(
                    status="completed",
                    lease_owner=None,
                    lease_expires_at=None,
                    completed_at=_utc_now(),
                    stage_timings_json=_json_or_none(stage_timings),
                    usage_json=_json_or_none(usage),
                    html_bytes=html_bytes,
                    request_bytes=request_bytes,
                    error_code=None,
                    error_message=None,
                )
            )
        return result.rowcount == 1

    def fail_job(
        self,
        job_id: str,
        *,
        code: str,
        message: str,
        expected_attempts: int | None = None,
        expected_lease_owner: str | None = None,
    ) -> bool:
        stmt = update(jobs).where(
            jobs.c.job_id == job_id,
            jobs.c.status == "processing",
        )
        if expected_attempts is not None:
            stmt = stmt.where(
                jobs.c.attempts == expected_attempts,
                jobs.c.lease_owner == expected_lease_owner,
            )
        with self._engine.begin() as conn:
            result = conn.execute(
                stmt.values(
                    status="failed",
                    lease_owner=None,
                    lease_expires_at=None,
                    completed_at=_utc_now(),
                    error_code=code,
                    error_message=message[:2000],
                )
            )
        return result.rowcount == 1

    def cancel_job(
        self,
        job_id: str,
        *,
        message: str,
        expected_attempts: int | None = None,
        expected_lease_owner: str | None = None,
    ) -> bool:
        stmt = update(jobs).where(
            jobs.c.job_id == job_id,
            jobs.c.status == "processing",
        )
        if expected_attempts is not None:
            stmt = stmt.where(
                jobs.c.attempts == expected_attempts,
                jobs.c.lease_owner == expected_lease_owner,
            )
        with self._engine.begin() as conn:
            result = conn.execute(
                stmt.values(
                    status="cancelled",
                    lease_owner=None,
                    lease_expires_at=None,
                    completed_at=_utc_now(),
                    error_code="STALE_REQUEST",
                    error_message=message[:2000],
                )
            )
        return result.rowcount == 1

    def retry_job(self, job_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(jobs)
                .where(jobs.c.job_id == job_id)
                .values(
                    status="queued",
                    lease_owner=None,
                    lease_expires_at=None,
                    completed_at=None,
                    error_code=None,
                    error_message=None,
                )
            )

    # -- job events (progress timeline) -----------------------------------

    def add_job_event(
        self,
        job_id: str,
        *,
        stage: str,
        message: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                insert(job_events).values(
                    job_id=job_id,
                    stage=stage,
                    message=message,
                    detail_json=_json_or_none(detail),
                    created_at=_utc_now(),
                )
            )

    def list_job_events(self, job_id: str) -> tuple[JobEventRecord, ...]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(job_events)
                .where(job_events.c.job_id == job_id)
                .order_by(job_events.c.id)
            ).mappings().all()
        return tuple(_job_event_from_row(row) for row in rows)

    # -- documents: outcomes ---------------------------------------------

    def record_success(
        self,
        key: DocumentKey,
        *,
        request_id: str,
        source_etag: str,
        last_modified_datetime: str,
        source_size: int,
        acl_digest: str,
        external_item_id: str | None,
        web_url: str,
        file_name: str,
        html_bytes: int,
        request_bytes: int,
        content_hash: str,
        job_id: str | None = None,
        expected_attempts: int | None = None,
        expected_lease_owner: str | None = None,
    ) -> bool:
        now = _utc_now()
        stmt = update(documents).where(
            _key_where(documents, key),
            documents.c.request_id == request_id,
        )
        if job_id is not None and expected_attempts is not None:
            stmt = stmt.where(
                _job_claim_exists(
                    job_id,
                    attempts=expected_attempts,
                    lease_owner=expected_lease_owner,
                )
            )
        with self._engine.begin() as conn:
            result = conn.execute(
                stmt.values(
                    source_etag=source_etag,
                    last_modified_datetime=last_modified_datetime,
                    source_size=source_size,
                    acl_hash=acl_digest,
                    external_item_id=external_item_id,
                    web_url=web_url,
                    file_name=file_name,
                    desired_enabled=1,
                    status="Ready",
                    processed_request_id=request_id,
                    html_bytes=html_bytes,
                    request_bytes=request_bytes,
                    content_hash=content_hash,
                    enhancement_version=documents.c.enhancement_version + 1,
                    last_error_code=None,
                    last_error_message=None,
                    processed_at=now,
                    updated_at=now,
                )
            )
        return result.rowcount == 1

    # -- publication targets ---------------------------------------------

    def get_publication_transition(self) -> PublicationTransition:
        self._ensure_publication_transition()
        with self._engine.connect() as conn:
            row = conn.execute(
                select(publication_transitions).where(
                    publication_transitions.c.transition_id == "default"
                )
            ).mappings().one()
        return _publication_transition_from_row(row)

    def publication_target_for_jobs(self) -> PublicationTarget:
        return self.get_publication_transition().effective_target

    def request_publication_target(
        self,
        target: PublicationTarget | str,
    ) -> PublicationTransition:
        parsed = parse_publication_target(target)
        for _ in range(5):
            current = self.get_publication_transition()
            if parsed == current.desired_target and current.status != "failed":
                return current
            status = "cleaning" if parsed == current.active_target else "staging"
            changed = self._set_transition_values(
                desired_target=parsed.value,
                generation=current.generation + 1,
                status=status,
                reindex_requested=0,
                search_verified=0,
                copilot_verified=0,
                last_error_code=None,
                last_error_message=None,
                expected_generation=current.generation,
                expected_status=current.status,
                expected_desired_target=current.desired_target,
                expected_active_target=current.active_target,
            )
            if changed:
                return self.get_publication_transition()
        raise RuntimeError("The publication target changed too frequently; retry.")

    def _expected_publication_transition(
        self,
        *,
        expected_generation: int | None,
        expected_desired_target: PublicationTarget | str | None,
    ) -> PublicationTransition:
        current = self.get_publication_transition()
        parsed_target = (
            parse_publication_target(expected_desired_target)
            if expected_desired_target is not None
            else None
        )
        if (
            expected_generation is not None
            and current.generation != expected_generation
        ) or (
            parsed_target is not None
            and current.desired_target is not parsed_target
        ):
            raise ValueError(
                "The publication transition changed after this form was opened."
            )
        return current

    def set_column_provisioned(
        self,
        provisioned: bool = True,
        *,
        expected_generation: int | None = None,
        expected_desired_target: PublicationTarget | str | None = None,
    ) -> None:
        current = self._expected_publication_transition(
            expected_generation=expected_generation,
            expected_desired_target=expected_desired_target,
        )
        if current.desired_target is not PublicationTarget.SHAREPOINT_COLUMN:
            raise ValueError("Column mode is not selected.")
        if current.column_provisioned == provisioned:
            return
        status = current.status
        if current.active_target is not current.desired_target:
            status = "staging" if provisioned else "failed"
        changed = self._set_transition_values(
            column_provisioned=int(provisioned),
            status=status,
            last_error_code=None if provisioned else "CONTENT_COLUMN_NOT_PROVISIONED",
            last_error_message=None,
            expected_generation=(
                expected_generation
                if expected_generation is not None
                else current.generation
            ),
            expected_status=current.status,
            expected_desired_target=(
                parse_publication_target(expected_desired_target)
                if expected_desired_target is not None
                else current.desired_target
            ),
        )
        if not changed:
            raise ValueError("The publication transition changed concurrently.")

    def set_reindex_requested(
        self,
        *,
        expected_generation: int | None = None,
        expected_desired_target: PublicationTarget | str | None = None,
    ) -> None:
        current = self._expected_publication_transition(
            expected_generation=expected_generation,
            expected_desired_target=expected_desired_target,
        )
        if current.status != "awaiting_reindex":
            raise ValueError(
                "The transition is not ready for a library reindex request."
            )
        changed = self._set_transition_values(
            reindex_requested=1,
            status="awaiting_search",
            expected_generation=(
                expected_generation
                if expected_generation is not None
                else current.generation
            ),
            expected_status=current.status,
            expected_desired_target=(
                parse_publication_target(expected_desired_target)
                if expected_desired_target is not None
                else current.desired_target
            ),
        )
        if not changed:
            raise ValueError("The publication transition changed concurrently.")

    def set_search_verified(
        self,
        *,
        expected_generation: int | None = None,
        expected_desired_target: PublicationTarget | str | None = None,
    ) -> None:
        current = self._expected_publication_transition(
            expected_generation=expected_generation,
            expected_desired_target=expected_desired_target,
        )
        if current.status != "awaiting_search":
            raise ValueError("The transition is not awaiting Microsoft Search.")
        changed = self._set_transition_values(
            search_verified=1,
            status="awaiting_copilot",
            expected_generation=(
                expected_generation
                if expected_generation is not None
                else current.generation
            ),
            expected_status=current.status,
            expected_desired_target=(
                parse_publication_target(expected_desired_target)
                if expected_desired_target is not None
                else current.desired_target
            ),
        )
        if not changed:
            raise ValueError("The publication transition changed concurrently.")

    def set_copilot_verified(
        self,
        *,
        expected_generation: int | None = None,
        expected_desired_target: PublicationTarget | str | None = None,
    ) -> None:
        current = self._expected_publication_transition(
            expected_generation=expected_generation,
            expected_desired_target=expected_desired_target,
        )
        if current.status != "awaiting_copilot":
            raise ValueError("The transition is not awaiting scoped Copilot.")
        changed = self._set_transition_values(
            copilot_verified=1,
            status="cleaning",
            expected_generation=(
                expected_generation
                if expected_generation is not None
                else current.generation
            ),
            expected_status=current.status,
            expected_desired_target=(
                parse_publication_target(expected_desired_target)
                if expected_desired_target is not None
                else current.desired_target
            ),
        )
        if not changed:
            raise ValueError("The publication transition changed concurrently.")

    def set_transition_status(
        self,
        status: str,
        *,
        code: str | None = None,
        message: str | None = None,
        expected_generation: int | None = None,
        expected_status: str | None = None,
        expected_desired_target: PublicationTarget | None = None,
        expected_active_target: PublicationTarget | None = None,
    ) -> bool:
        return self._set_transition_values(
            status=status,
            last_error_code=code,
            last_error_message=message[:2000] if message else None,
            expected_generation=expected_generation,
            expected_status=expected_status,
            expected_desired_target=expected_desired_target,
            expected_active_target=expected_active_target,
        )

    def activate_publication_target(
        self,
        *,
        expected_generation: int | None = None,
        expected_desired_target: PublicationTarget | None = None,
        expected_active_target: PublicationTarget | None = None,
    ) -> bool:
        return self._set_transition_values(
            active_target=publication_transitions.c.desired_target,
            status="active",
            last_error_code=None,
            last_error_message=None,
            expected_generation=expected_generation,
            expected_status="cleaning" if expected_generation is not None else None,
            expected_desired_target=expected_desired_target,
            expected_active_target=expected_active_target,
            require_no_pending_generation=expected_generation,
            require_documents_ready=expected_generation is not None,
        )

    def publication_transition_matches(
        self,
        transition: PublicationTransition,
        *,
        status: str | None = None,
    ) -> bool:
        stmt = select(publication_transitions.c.transition_id).where(
            publication_transitions.c.transition_id == "default",
            publication_transitions.c.generation == transition.generation,
            publication_transitions.c.active_target == transition.active_target.value,
            publication_transitions.c.desired_target == transition.desired_target.value,
        )
        stmt = stmt.where(
            publication_transitions.c.status
            == (status if status is not None else transition.status)
        )
        with self._engine.connect() as conn:
            return conn.execute(stmt).first() is not None

    def record_publication_success(
        self,
        key: DocumentKey,
        *,
        target: PublicationTarget | str,
        generation: int,
        locator: str | None,
        content_hash: str,
        original_characters: int,
        stored_characters: int,
        stored_bytes: int,
        truncated: bool,
        operation_id: str | None = None,
    ) -> bool:
        parsed = parse_publication_target(target)
        if parsed is PublicationTarget.UNSET:
            raise ValueError("Cannot record an unset publication target.")
        now = _utc_now()
        values = {
            "generation": generation,
            "locator": locator,
            "status": "ready",
            "operation_id": None,
            "content_hash": content_hash,
            "original_characters": original_characters,
            "stored_characters": stored_characters,
            "stored_bytes": stored_bytes,
            "truncated": int(truncated),
            "error_code": None,
            "error_message": None,
            "published_at": now,
            "updated_at": now,
        }
        if operation_id is not None:
            with self._engine.begin() as conn:
                result = conn.execute(
                    update(document_publications)
                    .where(
                        _key_where(document_publications, key),
                        document_publications.c.target == parsed.value,
                        document_publications.c.generation == generation,
                        document_publications.c.status == "pending",
                        document_publications.c.operation_id == operation_id,
                    )
                    .values(**values)
                )
            return result.rowcount == 1
        stmt = self._insert(document_publications).values(
            tenant_id=key.tenant_id,
            site_id=key.site_id,
            drive_id=key.drive_id,
            item_id=key.item_id,
            target=parsed.value,
            generation=generation,
            locator=locator,
            status="ready",
            operation_id=None,
            content_hash=content_hash,
            original_characters=original_characters,
            stored_characters=stored_characters,
            stored_bytes=stored_bytes,
            truncated=int(truncated),
            error_code=None,
            error_message=None,
            published_at=now,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                document_publications.c.tenant_id,
                document_publications.c.site_id,
                document_publications.c.drive_id,
                document_publications.c.item_id,
                document_publications.c.target,
            ],
            set_=values,
            where=and_(
                document_publications.c.generation <= generation,
                document_publications.c.status != "removing",
            ),
        )
        with self._engine.begin() as conn:
            active_document = conn.execute(
                select(documents.c.item_id)
                .where(
                    _key_where(documents, key),
                    documents.c.desired_enabled == 1,
                )
                .with_for_update()
            ).first()
            if active_document is None:
                return False
            result = conn.execute(stmt)
        return result.rowcount == 1

    def record_publication_pending(
        self,
        key: DocumentKey,
        *,
        transition: PublicationTransition,
        target: PublicationTarget | str,
        locator: str | None,
        operation_id: str,
        request_id: str | None = None,
        job_id: str | None = None,
        expected_attempts: int | None = None,
        expected_lease_owner: str | None = None,
    ) -> bool:
        parsed = parse_publication_target(target)
        if parsed is PublicationTarget.UNSET:
            raise ValueError("Cannot record an unset publication target.")
        now = _utc_now()
        with self._engine.begin() as conn:
            if request_id is not None:
                document_stmt = select(documents.c.item_id).where(
                    _key_where(documents, key),
                    documents.c.request_id == request_id,
                    documents.c.desired_enabled == 1,
                )
                if job_id is not None and expected_attempts is not None:
                    document_stmt = document_stmt.where(
                        _job_claim_exists(
                            job_id,
                            attempts=expected_attempts,
                            lease_owner=expected_lease_owner,
                        )
                    )
                active_document = conn.execute(
                    document_stmt.with_for_update()
                ).first()
                if active_document is None:
                    return False
            current = conn.execute(
                select(publication_transitions.c.transition_id)
                .where(
                    publication_transitions.c.transition_id == "default",
                    publication_transitions.c.generation == transition.generation,
                    publication_transitions.c.active_target
                    == transition.active_target.value,
                    publication_transitions.c.desired_target
                    == transition.desired_target.value,
                    publication_transitions.c.status == transition.status,
                )
                .with_for_update()
            ).first()
            if current is None:
                return False
            existing = conn.execute(
                select(
                    document_publications.c.generation,
                    document_publications.c.status,
                )
                .where(
                    _key_where(document_publications, key),
                    document_publications.c.target == parsed.value,
                )
                .with_for_update()
            ).first()
            if existing is not None and (
                int(existing.generation) > transition.generation
                or existing.status == "removing"
            ):
                return False
            if existing is None:
                conn.execute(
                    insert(document_publications).values(
                        tenant_id=key.tenant_id,
                        site_id=key.site_id,
                        drive_id=key.drive_id,
                        item_id=key.item_id,
                        target=parsed.value,
                        generation=transition.generation,
                        locator=locator,
                        status="pending",
                        operation_id=operation_id,
                        truncated=0,
                        error_code="PENDING_JOB",
                        error_message=operation_id,
                        updated_at=now,
                    )
                )
            else:
                conn.execute(
                    update(document_publications)
                    .where(
                        _key_where(document_publications, key),
                        document_publications.c.target == parsed.value,
                    )
                    .values(
                        generation=transition.generation,
                        locator=locator,
                        status="pending",
                        operation_id=operation_id,
                        error_code="PENDING_JOB",
                        error_message=operation_id,
                        updated_at=now,
                    )
                )
        return True

    def fail_pending_publications_for_operation(
        self,
        operation_id: str,
        *,
        code: str,
        message: str,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(document_publications)
                .where(
                    document_publications.c.status == "pending",
                    document_publications.c.error_code == "PENDING_JOB",
                    document_publications.c.operation_id == operation_id,
                )
                .values(
                    status="failed",
                    operation_id=None,
                    error_code=code,
                    error_message=message[:2000],
                    updated_at=_utc_now(),
                )
            )

    def record_publication_error(
        self,
        key: DocumentKey,
        *,
        target: PublicationTarget | str,
        generation: int,
        code: str,
        message: str,
        operation_id: str | None = None,
    ) -> bool:
        parsed = parse_publication_target(target)
        if parsed is PublicationTarget.UNSET:
            raise ValueError("Cannot record an unset publication target.")
        now = _utc_now()
        values = {
            "generation": generation,
            "status": "failed",
            "operation_id": None,
            "error_code": code,
            "error_message": message[:2000],
            "updated_at": now,
        }
        if operation_id is not None:
            with self._engine.begin() as conn:
                result = conn.execute(
                    update(document_publications)
                    .where(
                        _key_where(document_publications, key),
                        document_publications.c.target == parsed.value,
                        document_publications.c.generation == generation,
                        document_publications.c.status == "pending",
                        document_publications.c.operation_id == operation_id,
                    )
                    .values(**values)
                )
            return result.rowcount == 1
        stmt = self._insert(document_publications).values(
            tenant_id=key.tenant_id,
            site_id=key.site_id,
            drive_id=key.drive_id,
            item_id=key.item_id,
            target=parsed.value,
            generation=generation,
            status="failed",
            operation_id=None,
            truncated=0,
            error_code=code,
            error_message=message[:2000],
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                document_publications.c.tenant_id,
                document_publications.c.site_id,
                document_publications.c.drive_id,
                document_publications.c.item_id,
                document_publications.c.target,
            ],
            set_=values,
            where=document_publications.c.generation < generation,
        )
        with self._engine.begin() as conn:
            result = conn.execute(stmt)
        return result.rowcount == 1

    def get_publication(
        self,
        key: DocumentKey,
        target: PublicationTarget | str,
    ) -> PublicationRecord | None:
        parsed = parse_publication_target(target)
        with self._engine.connect() as conn:
            row = conn.execute(
                select(document_publications).where(
                    _key_where(document_publications, key),
                    document_publications.c.target == parsed.value,
                )
            ).mappings().first()
        return _publication_from_row(row) if row else None

    def list_publications(
        self,
        *,
        target: PublicationTarget | str | None = None,
        generation: int | None = None,
    ) -> tuple[PublicationRecord, ...]:
        stmt = select(document_publications)
        if target is not None:
            parsed = parse_publication_target(target)
            stmt = stmt.where(document_publications.c.target == parsed.value)
        if generation is not None:
            stmt = stmt.where(document_publications.c.generation == generation)
        stmt = stmt.order_by(document_publications.c.updated_at)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return tuple(_publication_from_row(row) for row in rows)

    def list_document_publications(
        self,
        key: DocumentKey,
    ) -> tuple[PublicationRecord, ...]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(document_publications)
                .where(_key_where(document_publications, key))
                .order_by(document_publications.c.updated_at)
            ).mappings().all()
        return tuple(_publication_from_row(row) for row in rows)

    def claim_publication_removal(
        self,
        key: DocumentKey,
        target: PublicationTarget | str,
        *,
        expected_updated_at: str,
        operation_id: str,
    ) -> bool:
        parsed = parse_publication_target(target)
        with self._engine.begin() as conn:
            result = conn.execute(
                update(document_publications)
                .where(
                    _key_where(document_publications, key),
                    document_publications.c.target == parsed.value,
                    document_publications.c.updated_at == expected_updated_at,
                    document_publications.c.status != "pending",
                )
                .values(
                    status="removing",
                    operation_id=operation_id,
                    error_code="REMOVING",
                    error_message=operation_id,
                    updated_at=_utc_now(),
                )
            )
        return result.rowcount == 1

    def remove_publication_record(
        self,
        key: DocumentKey,
        target: PublicationTarget | str,
        *,
        operation_id: str | None = None,
    ) -> bool:
        parsed = parse_publication_target(target)
        with self._engine.begin() as conn:
            stmt = delete(document_publications).where(
                _key_where(document_publications, key),
                document_publications.c.target == parsed.value,
            )
            if operation_id is not None:
                stmt = stmt.where(
                    document_publications.c.status == "removing",
                    document_publications.c.operation_id == operation_id,
                )
            result = conn.execute(stmt)
            if (
                result.rowcount == 1
                and parsed is PublicationTarget.COPILOT_CONNECTOR
            ):
                conn.execute(
                    update(documents)
                    .where(_key_where(documents, key))
                    .values(external_item_id=None, acl_hash=None, updated_at=_utc_now())
                )
        return result.rowcount == 1

    def publication_progress(
        self,
        *,
        target: PublicationTarget | str,
        generation: int,
    ) -> dict[str, int]:
        parsed = parse_publication_target(target)
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(
                    document_publications.c.status,
                    func.count(),
                    func.sum(document_publications.c.truncated),
                )
                .where(
                    document_publications.c.target == parsed.value,
                    document_publications.c.generation == generation,
                )
                .group_by(document_publications.c.status)
            ).all()
        result = {"ready": 0, "failed": 0, "pending": 0, "truncated": 0}
        for status, count, truncated in rows:
            result[str(status)] = int(count)
            result["truncated"] += int(truncated or 0)
        return result

    def record_acl_update(self, key: DocumentKey, *, acl_digest: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(documents)
                .where(_key_where(documents, key))
                .values(acl_hash=acl_digest, updated_at=_utc_now())
            )

    def record_source_metadata(
        self,
        key: DocumentKey,
        *,
        web_url: str,
        file_name: str,
        last_modified_datetime: str,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(documents)
                .where(_key_where(documents, key))
                .values(
                    web_url=web_url,
                    file_name=file_name,
                    last_modified_datetime=last_modified_datetime,
                    updated_at=_utc_now(),
                )
            )

    def record_queued_refresh(self, key: DocumentKey, *, request_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(documents)
                .where(_key_where(documents, key))
                .values(status="Queued", request_id=request_id, updated_at=_utc_now())
            )

    def record_removed(
        self,
        key: DocumentKey,
        *,
        request_id: str,
        job_id: str | None = None,
        expected_attempts: int | None = None,
        expected_lease_owner: str | None = None,
    ) -> bool:
        now = _utc_now()
        stmt = update(documents).where(
            _key_where(documents, key),
            documents.c.request_id == request_id,
        )
        if job_id is not None and expected_attempts is not None:
            stmt = stmt.where(
                _job_claim_exists(
                    job_id,
                    attempts=expected_attempts,
                    lease_owner=expected_lease_owner,
                )
            )
        with self._engine.begin() as conn:
            result = conn.execute(
                stmt.values(
                    desired_enabled=0,
                    status="NotEnabled",
                    processed_request_id=request_id,
                    external_item_id=None,
                    last_error_code=None,
                    last_error_message=None,
                    processed_at=now,
                    updated_at=now,
                )
            )
        return result.rowcount == 1

    def record_document_error(
        self,
        key: DocumentKey,
        *,
        code: str,
        message: str,
        request_id: str | None = None,
        job_id: str | None = None,
        expected_attempts: int | None = None,
        expected_lease_owner: str | None = None,
    ) -> bool:
        stmt = update(documents).where(_key_where(documents, key))
        if request_id is not None:
            stmt = stmt.where(
                documents.c.request_id == request_id,
                or_(
                    documents.c.processed_request_id.is_(None),
                    documents.c.processed_request_id != request_id,
                ),
            )
        if job_id is not None and expected_attempts is not None:
            stmt = stmt.where(
                _job_claim_exists(
                    job_id,
                    attempts=expected_attempts,
                    lease_owner=expected_lease_owner,
                )
            )
        with self._engine.begin() as conn:
            result = conn.execute(
                stmt.values(
                    status="Failed",
                    last_error_code=code,
                    last_error_message=message[:2000],
                    updated_at=_utc_now(),
                )
            )
        return result.rowcount == 1

    # -- user-driven queue actions ---------------------------------------

    def queue_refresh(
        self,
        key: DocumentKey,
        *,
        trigger: str,
        feedback: str | None = None,
        note_author: str | None = None,
    ) -> tuple[str, str]:
        """Re-enqueue an upsert (optionally with a tuning comment)."""

        request_id = str(uuid4())
        now = _utc_now()
        values: dict[str, Any] = {
            "desired_enabled": 1,
            "status": "Queued",
            "request_id": request_id,
            "updated_at": now,
        }
        if feedback:
            values["correction_notes"] = _json_or_none(
                self._append_note(key, feedback, now, note_author)
            )
        with self._engine.begin() as conn:
            conn.execute(
                update(documents).where(_key_where(documents, key)).values(**values)
            )
            self._set_publication_request_intent(
                conn,
                key=key,
                request_id=request_id,
                removing=False,
            )
            job_id = self._enqueue_job(
                conn,
                key=key,
                request_id=request_id,
                job_type="upsert",
                trigger=trigger,
                feedback=feedback,
            )
        self.add_job_event(job_id, stage="RECEIVED", message=trigger)
        return request_id, job_id

    def queue_refresh_if_current(
        self,
        key: DocumentKey,
        *,
        expected_request_id: str,
        trigger: str,
    ) -> tuple[str, str] | None:
        """Queue reconciliation only while the inspected Ready state is current."""

        request_id = str(uuid4())
        now = _utc_now()
        with self._engine.begin() as conn:
            result = conn.execute(
                update(documents)
                .where(
                    _key_where(documents, key),
                    documents.c.request_id == expected_request_id,
                    documents.c.processed_request_id == expected_request_id,
                    documents.c.desired_enabled == 1,
                    documents.c.status == "Ready",
                )
                .values(status="Queued", request_id=request_id, updated_at=now)
            )
            if result.rowcount != 1:
                return None
            self._set_publication_request_intent(
                conn,
                key=key,
                request_id=request_id,
                removing=False,
            )
            job_id = self._enqueue_job(
                conn,
                key=key,
                request_id=request_id,
                job_type="upsert",
                trigger=trigger,
            )
        self.add_job_event(job_id, stage="RECEIVED", message=trigger)
        return request_id, job_id

    def queue_removal(self, key: DocumentKey, *, trigger: str) -> tuple[str, str]:
        request_id = str(uuid4())
        with self._engine.begin() as conn:
            conn.execute(
                update(documents)
                .where(_key_where(documents, key))
                .values(
                    desired_enabled=0,
                    status="Queued",
                    request_id=request_id,
                    updated_at=_utc_now(),
                )
            )
            self._set_publication_request_intent(
                conn,
                key=key,
                request_id=request_id,
                removing=True,
            )
            job_id = self._enqueue_job(
                conn,
                key=key,
                request_id=request_id,
                job_type="delete",
                trigger=trigger,
            )
        self.add_job_event(job_id, stage="RECEIVED", message=trigger)
        return request_id, job_id

    def _append_note(
        self,
        key: DocumentKey,
        note: str,
        now: str,
        author: str | None,
    ) -> list[dict[str, Any]]:
        record = self.get_document(key)
        notes = list(_json_loads_or_none(record.correction_notes) or []) if record else []
        notes.append({"text": note, "author": author, "createdAt": now})
        return notes

    def correction_notes(self, key: DocumentKey) -> list[dict[str, Any]]:
        record = self.get_document(key)
        if record is None:
            return []
        return list(_json_loads_or_none(record.correction_notes) or [])

    # -- artifacts --------------------------------------------------------

    def record_artifact(
        self,
        key: DocumentKey,
        *,
        kind: str,
        blob_path: str,
        content_type: str | None = None,
        content_hash: str | None = None,
        byte_count: int | None = None,
        enhancement_version: int | None = None,
    ) -> int:
        with self._engine.begin() as conn:
            result = conn.execute(
                insert(artifacts).values(
                    tenant_id=key.tenant_id,
                    site_id=key.site_id,
                    drive_id=key.drive_id,
                    item_id=key.item_id,
                    kind=kind,
                    blob_path=blob_path,
                    content_type=content_type,
                    content_hash=content_hash,
                    byte_count=byte_count,
                    enhancement_version=enhancement_version,
                    created_at=_utc_now(),
                )
            )
        return int(result.inserted_primary_key[0])

    def get_latest_artifact(
        self, key: DocumentKey, kind: str
    ) -> ArtifactRecord | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(artifacts)
                .where(_key_where(artifacts, key), artifacts.c.kind == kind)
                .order_by(artifacts.c.id.desc())
                .limit(1)
            ).mappings().first()
        return _artifact_from_row(row) if row else None

    def list_artifacts(self, key: DocumentKey) -> tuple[ArtifactRecord, ...]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(artifacts)
                .where(_key_where(artifacts, key))
                .order_by(artifacts.c.id.desc())
            ).mappings().all()
        return tuple(_artifact_from_row(row) for row in rows)

    # -- settings ---------------------------------------------------------

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(settings.c.value).where(settings.c.key == key)
            ).first()
        if row is None:
            return default
        return _json_loads_or_none(row[0])

    def get_all_settings(self) -> dict[str, Any]:
        with self._engine.connect() as conn:
            rows = conn.execute(select(settings.c.key, settings.c.value)).all()
        return {str(key): _json_loads_or_none(value) for key, value in rows}

    def set_setting(self, key: str, value: Any) -> None:
        stmt = self._insert(settings).values(
            key=key, value=_json_or_none(value), updated_at=_utc_now()
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[settings.c.key],
            set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at},
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def _ensure_publication_transition(
        self,
        *,
        existing_install: bool = True,
    ) -> None:
        with self._engine.begin() as conn:
            current = conn.execute(
                select(publication_transitions.c.transition_id).where(
                    publication_transitions.c.transition_id == "default"
                )
            ).first()
            if current is not None:
                return
            legacy_count = conn.execute(
                select(func.count())
                .select_from(documents)
                .where(
                    documents.c.external_item_id.is_not(None),
                    documents.c.external_item_id != "",
                )
            ).scalar_one()
            initial = (
                PublicationTarget.COPILOT_CONNECTOR
                if existing_install or legacy_count
                else PublicationTarget.UNSET
            )
            now = _utc_now()
            conn.execute(
                insert(publication_transitions).values(
                    transition_id="default",
                    active_target=initial.value,
                    desired_target=initial.value,
                    generation=0,
                    status="active",
                    column_provisioned=0,
                    reindex_requested=0,
                    search_verified=0,
                    copilot_verified=0,
                    created_at=now,
                    updated_at=now,
                )
            )

    def _legacy_publication_migration_pending(self, inspector: Any) -> bool:
        if not inspector.has_table(settings.name):
            return False
        with self._engine.connect() as conn:
            row = conn.execute(
                select(settings.c.value).where(
                    settings.c.key == LEGACY_PUBLICATION_MIGRATION_SETTING
                )
            ).first()
        return row is not None and bool(_json_loads_or_none(row[0]))

    def _set_legacy_publication_migration_pending(self) -> None:
        self.set_setting(LEGACY_PUBLICATION_MIGRATION_SETTING, True)

    def _clear_legacy_publication_migration_pending(self) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                delete(settings).where(
                    settings.c.key == LEGACY_PUBLICATION_MIGRATION_SETTING
                )
            )

    def _backfill_legacy_publications(self) -> None:
        with self._engine.begin() as conn:
            rows = conn.execute(
                select(
                    documents.c.tenant_id,
                    documents.c.site_id,
                    documents.c.drive_id,
                    documents.c.item_id,
                    documents.c.external_item_id,
                    documents.c.content_hash,
                    documents.c.html_bytes,
                    documents.c.processed_at,
                    documents.c.updated_at,
                ).where(
                    documents.c.external_item_id.is_not(None),
                    documents.c.external_item_id != "",
                )
            ).mappings().all()
            for row in rows:
                stmt = self._insert(document_publications).values(
                    tenant_id=row["tenant_id"],
                    site_id=row["site_id"],
                    drive_id=row["drive_id"],
                    item_id=row["item_id"],
                    target=PublicationTarget.COPILOT_CONNECTOR.value,
                    generation=0,
                    locator=row["external_item_id"],
                    status="ready",
                    content_hash=row["content_hash"],
                    stored_bytes=row["html_bytes"],
                    truncated=0,
                    published_at=row["processed_at"],
                    updated_at=row["updated_at"],
                ).on_conflict_do_nothing(
                    index_elements=[
                        document_publications.c.tenant_id,
                        document_publications.c.site_id,
                        document_publications.c.drive_id,
                        document_publications.c.item_id,
                        document_publications.c.target,
                    ]
                )
                conn.execute(stmt)

    def _set_transition_values(
        self,
        *,
        expected_generation: int | None = None,
        expected_status: str | None = None,
        expected_desired_target: PublicationTarget | None = None,
        expected_active_target: PublicationTarget | None = None,
        require_no_pending_generation: int | None = None,
        require_documents_ready: bool = False,
        **values: Any,
    ) -> bool:
        values["updated_at"] = _utc_now()
        stmt = update(publication_transitions).where(
            publication_transitions.c.transition_id == "default"
        )
        if expected_generation is not None:
            stmt = stmt.where(
                publication_transitions.c.generation == expected_generation
            )
        if expected_status is not None:
            stmt = stmt.where(publication_transitions.c.status == expected_status)
        if expected_desired_target is not None:
            stmt = stmt.where(
                publication_transitions.c.desired_target
                == expected_desired_target.value
            )
        if expected_active_target is not None:
            stmt = stmt.where(
                publication_transitions.c.active_target == expected_active_target.value
            )
        if require_no_pending_generation is not None:
            pending = select(document_publications.c.item_id).where(
                document_publications.c.generation
                == require_no_pending_generation,
                document_publications.c.status == "pending",
            )
            stmt = stmt.where(~pending.exists())
        if require_documents_ready:
            not_ready = select(documents.c.item_id).where(
                documents.c.desired_enabled == 1,
                documents.c.source_kind == "sharepoint",
                documents.c.status != "Ready",
            )
            stmt = stmt.where(~not_ready.exists())
        with self._engine.begin() as conn:
            result = conn.execute(stmt.values(**values))
        return result.rowcount == 1

    # -- feedback corpus --------------------------------------------------

    def add_feedback_record(
        self,
        key: DocumentKey,
        *,
        correction_text: str,
        enhancement_version: int | None = None,
        source_artifact_path: str | None = None,
        before_html_path: str | None = None,
        before_json_path: str | None = None,
        after_html_path: str | None = None,
        after_json_path: str | None = None,
        category: str | None = None,
        tags: Sequence[str] | None = None,
        model: str | None = None,
        deployment: str | None = None,
        created_by: str | None = None,
    ) -> int:
        with self._engine.begin() as conn:
            result = conn.execute(
                insert(feedback_records).values(
                    tenant_id=key.tenant_id,
                    site_id=key.site_id,
                    drive_id=key.drive_id,
                    item_id=key.item_id,
                    enhancement_version=enhancement_version,
                    source_artifact_path=source_artifact_path,
                    before_html_path=before_html_path,
                    before_json_path=before_json_path,
                    correction_text=correction_text,
                    after_html_path=after_html_path,
                    after_json_path=after_json_path,
                    category=category,
                    tags_json=_json_or_none(list(tags)) if tags else None,
                    model=model,
                    deployment=deployment,
                    created_by=created_by,
                    created_at=_utc_now(),
                )
            )
        return int(result.inserted_primary_key[0])

    def list_feedback_records(
        self, *, limit: int = 100, offset: int = 0
    ) -> tuple[FeedbackRecord, ...]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(feedback_records)
                .order_by(feedback_records.c.id.desc())
                .limit(limit)
                .offset(offset)
            ).mappings().all()
        return tuple(_feedback_from_row(row) for row in rows)

    def count_feedback_records(self) -> int:
        with self._engine.connect() as conn:
            return int(
                conn.execute(
                    select(func.count()).select_from(feedback_records)
                ).scalar_one()
            )

    def iter_feedback_records(self) -> Iterator[FeedbackRecord]:
        with self._engine.connect() as conn:
            result = conn.execute(
                select(feedback_records).order_by(feedback_records.c.id)
            )
            for row in result.mappings():
                yield _feedback_from_row(row)

    # -- helpers ----------------------------------------------------------

    def _insert(self, table: Any) -> Any:
        if self.dialect == "postgresql":
            return pg_insert(table)
        return sqlite_insert(table)


# ---------------------------------------------------------------------------
# Row mappers / helpers
# ---------------------------------------------------------------------------


def _key_where(table: Any, key: DocumentKey) -> Any:
    return and_(
        table.c.tenant_id == key.tenant_id,
        table.c.site_id == key.site_id,
        table.c.drive_id == key.drive_id,
        table.c.item_id == key.item_id,
    )


def _job_claim_exists(
    job_id: str,
    *,
    attempts: int,
    lease_owner: str | None,
) -> Any:
    return (
        select(jobs.c.job_id)
        .where(
            jobs.c.job_id == job_id,
            jobs.c.status == "processing",
            jobs.c.attempts == attempts,
            jobs.c.lease_owner == lease_owner,
        )
        .exists()
    )


def _join_on(left: Any, right: Any) -> Any:
    return and_(
        left.c.tenant_id == right.c.tenant_id,
        left.c.site_id == right.c.site_id,
        left.c.drive_id == right.c.drive_id,
        left.c.item_id == right.c.item_id,
    )


def _document_from_row(row: Mapping[str, Any]) -> DocumentRecord:
    return DocumentRecord(
        key=DocumentKey(
            tenant_id=row["tenant_id"],
            site_id=row["site_id"],
            drive_id=row["drive_id"],
            item_id=row["item_id"],
        ),
        list_id=row["list_id"],
        list_item_id=row["list_item_id"],
        web_url=row["web_url"],
        file_name=row["file_name"],
        desired_enabled=bool(row["desired_enabled"]),
        status=row["status"],
        request_id=row["request_id"],
        source_etag=row["source_etag"],
        last_modified_datetime=row["last_modified_datetime"],
        source_size=row["source_size"],
        acl_hash=row["acl_hash"],
        external_item_id=row["external_item_id"],
        processed_request_id=row["processed_request_id"],
        html_bytes=row["html_bytes"],
        request_bytes=row["request_bytes"],
        content_hash=row["content_hash"],
        status_token=row["status_token"],
        source_kind=row["source_kind"],
        enhancement_version=row["enhancement_version"],
        correction_notes=row["correction_notes"],
        last_error_code=row["last_error_code"],
        last_error_message=row["last_error_message"],
        updated_at=row["updated_at"],
        processed_at=row["processed_at"],
    )


def _publication_from_row(row: Mapping[str, Any]) -> PublicationRecord:
    return PublicationRecord(
        document_key=DocumentKey(
            tenant_id=row["tenant_id"],
            site_id=row["site_id"],
            drive_id=row["drive_id"],
            item_id=row["item_id"],
        ),
        target=parse_publication_target(row["target"]),
        generation=int(row["generation"]),
        locator=row["locator"],
        status=row["status"],
        operation_id=row["operation_id"],
        content_hash=row["content_hash"],
        original_characters=row["original_characters"],
        stored_characters=row["stored_characters"],
        stored_bytes=row["stored_bytes"],
        truncated=bool(row["truncated"]),
        error_code=row["error_code"],
        error_message=row["error_message"],
        published_at=row["published_at"],
        updated_at=row["updated_at"],
    )


def _publication_transition_from_row(
    row: Mapping[str, Any],
) -> PublicationTransition:
    return PublicationTransition(
        active_target=parse_publication_target(row["active_target"]),
        desired_target=parse_publication_target(row["desired_target"]),
        generation=int(row["generation"]),
        status=row["status"],
        column_provisioned=bool(row["column_provisioned"]),
        reindex_requested=bool(row["reindex_requested"]),
        search_verified=bool(row["search_verified"]),
        copilot_verified=bool(row["copilot_verified"]),
        last_error_code=row["last_error_code"],
        last_error_message=row["last_error_message"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _job_from_row(row: Mapping[str, Any]) -> JobRecord:
    return JobRecord(
        job_id=row["job_id"],
        document_key=DocumentKey(
            tenant_id=row["tenant_id"],
            site_id=row["site_id"],
            drive_id=row["drive_id"],
            item_id=row["item_id"],
        ),
        request_id=row["request_id"],
        job_type=row["job_type"],
        status=row["status"],
        attempts=row["attempts"],
        lease_owner=row["lease_owner"],
        trigger=row["trigger"],
        feedback=row["feedback"],
    )


def _job_event_from_row(row: Mapping[str, Any]) -> JobEventRecord:
    return JobEventRecord(
        id=row["id"],
        job_id=row["job_id"],
        stage=row["stage"],
        message=row["message"],
        detail=_json_loads_or_none(row["detail_json"]),
        created_at=row["created_at"],
    )


def _artifact_from_row(row: Mapping[str, Any]) -> ArtifactRecord:
    return ArtifactRecord(
        id=row["id"],
        document_key=DocumentKey(
            tenant_id=row["tenant_id"],
            site_id=row["site_id"],
            drive_id=row["drive_id"],
            item_id=row["item_id"],
        ),
        kind=row["kind"],
        blob_path=row["blob_path"],
        content_type=row["content_type"],
        content_hash=row["content_hash"],
        byte_count=row["byte_count"],
        enhancement_version=row["enhancement_version"],
        created_at=row["created_at"],
    )


def _feedback_from_row(row: Mapping[str, Any]) -> FeedbackRecord:
    return FeedbackRecord(
        id=row["id"],
        document_key=DocumentKey(
            tenant_id=row["tenant_id"],
            site_id=row["site_id"],
            drive_id=row["drive_id"],
            item_id=row["item_id"],
        ),
        enhancement_version=row["enhancement_version"],
        source_artifact_path=row["source_artifact_path"],
        before_html_path=row["before_html_path"],
        before_json_path=row["before_json_path"],
        correction_text=row["correction_text"],
        after_html_path=row["after_html_path"],
        after_json_path=row["after_json_path"],
        category=row["category"],
        tags=list(_json_loads_or_none(row["tags_json"]) or []),
        model=row["model"],
        deployment=row["deployment"],
        created_by=row["created_by"],
        created_at=row["created_at"],
    )


def _as_notes_list(
    text: str | None, now: str, author: str | None = None
) -> list[dict[str, Any]]:
    if not text:
        return []
    return [{"text": text, "author": author, "createdAt": now}]


def _new_token() -> str:
    return secrets.token_urlsafe(24)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_loads_or_none(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)

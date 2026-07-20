"""SQLAlchemy Core schema shared by the SQLite (local/tests) and PostgreSQL
(production) backends.

The historical local worker used raw ``sqlite3``. To host the service on Azure
we keep the exact same logical model but target both SQLite and PostgreSQL
through one dialect-neutral schema. Timestamps are stored as ISO-8601 UTC text
so lexical comparisons (used by the lease/claim logic) behave identically on
both engines.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import NullPool


METADATA = MetaData()

DOCUMENT_KEY_COLUMNS = ("tenant_id", "site_id", "drive_id", "item_id")

UPLOAD_TENANT_ID = "upload"

documents = Table(
    "documents",
    METADATA,
    Column("tenant_id", String(200), primary_key=True),
    Column("site_id", String(200), primary_key=True),
    Column("drive_id", String(200), primary_key=True),
    Column("item_id", String(200), primary_key=True),
    Column("list_id", Text, nullable=False),
    Column("list_item_id", Text, nullable=False),
    Column("web_url", Text, nullable=False),
    Column("file_name", Text, nullable=False),
    Column("source_etag", Text),
    Column("last_modified_datetime", Text),
    Column("source_size", Integer),
    Column("acl_hash", Text),
    Column("external_item_id", Text),
    Column("connection_id", Text, nullable=False),
    Column("desired_enabled", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("request_id", Text, nullable=False),
    Column("processed_request_id", Text),
    Column("html_bytes", Integer),
    Column("request_bytes", Integer),
    Column("content_hash", Text),
    Column("last_error_code", Text),
    Column("last_error_message", Text),
    Column("status_token", String(64), nullable=False, unique=True),
    Column("source_kind", Text, nullable=False, server_default="sharepoint"),
    Column("enhancement_version", Integer, nullable=False, server_default="0"),
    Column("correction_notes", Text),
    Column("processed_at", Text),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    CheckConstraint(
        "desired_enabled IN (0, 1)", name="documents_desired_enabled_bool"
    ),
)


jobs = Table(
    "jobs",
    METADATA,
    Column("job_id", String(64), primary_key=True),
    Column("tenant_id", String(200), nullable=False),
    Column("site_id", String(200), nullable=False),
    Column("drive_id", String(200), nullable=False),
    Column("item_id", String(200), nullable=False),
    Column("request_id", Text, nullable=False),
    Column("job_type", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("lease_owner", Text),
    Column("lease_expires_at", Text),
    Column("trigger", Text),
    Column("feedback", Text),
    Column("queued_at", Text, nullable=False),
    Column("started_at", Text),
    Column("completed_at", Text),
    Column("stage_timings_json", Text),
    Column("usage_json", Text),
    Column("html_bytes", Integer),
    Column("request_bytes", Integer),
    Column("error_code", Text),
    Column("error_message", Text),
    UniqueConstraint(
        "tenant_id",
        "site_id",
        "drive_id",
        "item_id",
        "request_id",
        "job_type",
        name="jobs_idempotent",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "site_id", "drive_id", "item_id"],
        [
            "documents.tenant_id",
            "documents.site_id",
            "documents.drive_id",
            "documents.item_id",
        ],
        ondelete="CASCADE",
        name="jobs_document_fk",
    ),
    CheckConstraint(
        "job_type IN ('upsert', 'delete')", name="jobs_job_type"
    ),
    CheckConstraint(
        "status IN ('queued', 'processing', 'completed', 'failed', 'cancelled')",
        name="jobs_status",
    ),
    Index("jobs_claim_idx", "status", "lease_expires_at", "queued_at"),
)


job_events = Table(
    "job_events",
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column(
        "job_id",
        String(64),
        ForeignKey("jobs.job_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("stage", Text, nullable=False),
    Column("message", Text),
    Column("detail_json", Text),
    Column("created_at", Text, nullable=False),
    Index("job_events_job_idx", "job_id", "id"),
)


artifacts = Table(
    "artifacts",
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("tenant_id", String(200), nullable=False),
    Column("site_id", String(200), nullable=False),
    Column("drive_id", String(200), nullable=False),
    Column("item_id", String(200), nullable=False),
    Column("kind", Text, nullable=False),
    Column("blob_path", Text, nullable=False),
    Column("content_type", Text),
    Column("content_hash", Text),
    Column("byte_count", Integer),
    Column("enhancement_version", Integer),
    Column("created_at", Text, nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "site_id", "drive_id", "item_id"],
        [
            "documents.tenant_id",
            "documents.site_id",
            "documents.drive_id",
            "documents.item_id",
        ],
        ondelete="CASCADE",
        name="artifacts_document_fk",
    ),
    Index(
        "artifacts_doc_kind_idx",
        "tenant_id",
        "site_id",
        "drive_id",
        "item_id",
        "kind",
    ),
)


settings = Table(
    "settings",
    METADATA,
    Column("key", String(200), primary_key=True),
    Column("value", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
)


document_publications = Table(
    "document_publications",
    METADATA,
    Column("tenant_id", String(200), primary_key=True),
    Column("site_id", String(200), primary_key=True),
    Column("drive_id", String(200), primary_key=True),
    Column("item_id", String(200), primary_key=True),
    Column("target", String(40), primary_key=True),
    Column("generation", Integer, nullable=False),
    Column("locator", Text),
    Column("status", String(32), nullable=False),
    Column("operation_id", String(200)),
    Column("content_hash", Text),
    Column("original_characters", Integer),
    Column("stored_characters", Integer),
    Column("stored_bytes", Integer),
    Column("truncated", Integer, nullable=False, server_default="0"),
    Column("error_code", Text),
    Column("error_message", Text),
    Column("published_at", Text),
    Column("updated_at", Text, nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "site_id", "drive_id", "item_id"],
        [
            "documents.tenant_id",
            "documents.site_id",
            "documents.drive_id",
            "documents.item_id",
        ],
        ondelete="CASCADE",
        name="document_publications_document_fk",
    ),
    CheckConstraint(
        "target IN ('sharepoint_column', 'copilot_connector')",
        name="document_publications_target",
    ),
    CheckConstraint(
        "status IN ('pending', 'ready', 'removing', 'failed')",
        name="document_publications_status",
    ),
    CheckConstraint(
        "truncated IN (0, 1)",
        name="document_publications_truncated_bool",
    ),
    Index(
        "document_publications_target_generation_idx",
        "target",
        "generation",
        "status",
    ),
)


publication_transitions = Table(
    "publication_transitions",
    METADATA,
    Column("transition_id", String(40), primary_key=True),
    Column("active_target", String(40), nullable=False),
    Column("desired_target", String(40), nullable=False),
    Column("generation", Integer, nullable=False),
    Column("status", String(40), nullable=False),
    Column("column_provisioned", Integer, nullable=False, server_default="0"),
    Column("reindex_requested", Integer, nullable=False, server_default="0"),
    Column("search_verified", Integer, nullable=False, server_default="0"),
    Column("copilot_verified", Integer, nullable=False, server_default="0"),
    Column("last_error_code", Text),
    Column("last_error_message", Text),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    CheckConstraint(
        "active_target IN ('unset', 'sharepoint_column', 'copilot_connector')",
        name="publication_transitions_active_target",
    ),
    CheckConstraint(
        "desired_target IN ('unset', 'sharepoint_column', 'copilot_connector')",
        name="publication_transitions_desired_target",
    ),
    CheckConstraint(
        "status IN ("
        "'active', 'staging', 'awaiting_reindex', 'awaiting_search', "
        "'awaiting_copilot', 'cleaning', 'failed'"
        ")",
        name="publication_transitions_status",
    ),
    CheckConstraint(
        "column_provisioned IN (0, 1) AND "
        "reindex_requested IN (0, 1) AND "
        "search_verified IN (0, 1) AND "
        "copilot_verified IN (0, 1)",
        name="publication_transitions_bool_flags",
    ),
)


# Raw artifact bytes (rendered HTML, structured JSON, uploaded decks). Used when
# object storage is unavailable — e.g. an Azure Policy disables Storage public
# network access — but the relational database is reachable by both the web and
# worker containers. Complements the ``artifacts`` metadata/pointer table above.
artifact_blobs = Table(
    "artifact_blobs",
    METADATA,
    Column("path", String(1024), primary_key=True),
    Column("data", LargeBinary, nullable=False),
    Column("content_type", Text),
    Column("content_hash", Text, nullable=False),
    Column("byte_count", Integer, nullable=False),
    Column("created_at", Text, nullable=False),
)


feedback_records = Table(
    "feedback_records",
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("tenant_id", String(200), nullable=False),
    Column("site_id", String(200), nullable=False),
    Column("drive_id", String(200), nullable=False),
    Column("item_id", String(200), nullable=False),
    Column("enhancement_version", Integer),
    Column("source_artifact_path", Text),
    Column("before_html_path", Text),
    Column("before_json_path", Text),
    Column("correction_text", Text, nullable=False),
    Column("after_html_path", Text),
    Column("after_json_path", Text),
    Column("category", Text),
    Column("tags_json", Text),
    Column("model", Text),
    Column("deployment", Text),
    Column("created_by", Text),
    Column("created_at", Text, nullable=False),
    Index(
        "feedback_doc_idx",
        "tenant_id",
        "site_id",
        "drive_id",
        "item_id",
    ),
)


def create_db_engine(target: "str | Path | Engine") -> Engine:
    """Build an :class:`~sqlalchemy.engine.Engine` for a SQLite file path or a
    SQLAlchemy URL.

    A ``Path`` (or a plain filesystem string without a scheme) yields a SQLite
    engine; anything that looks like a URL (``scheme://``) is used verbatim so
    production can pass ``postgresql+psycopg://...``.
    """

    if isinstance(target, Engine):
        return target

    if isinstance(target, Path):
        target.parent.mkdir(parents=True, exist_ok=True)
        url = make_url(f"sqlite:///{target}")
    elif "://" in str(target):
        url = make_url(str(target))
    else:
        path = Path(str(target)).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        url = make_url(f"sqlite:///{path}")

    connect_args: dict[str, object] = {}
    engine_kwargs: dict[str, object] = {"future": True, "pool_pre_ping": True}
    if url.get_backend_name() == "sqlite":
        connect_args["timeout"] = 30
        # Close connections eagerly so Windows can delete the SQLite file
        # between tests, mirroring the original close-per-call behaviour.
        engine_kwargs["poolclass"] = NullPool
        engine_kwargs.pop("pool_pre_ping", None)

    engine = create_engine(url, connect_args=connect_args, **engine_kwargs)

    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _configure_sqlite(dbapi_connection: object, _record: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.close()

    return engine


def resolve_database_target(sqlite_path: Path) -> "str | Path":
    """Return the production database URL from ``DATABASE_URL`` when present,
    otherwise the local SQLite path."""

    url = os.getenv("DATABASE_URL", "").strip()
    return url or sqlite_path

"""Artifact storage abstraction.

Rendered HTML, structured JSON, slide thumbnails and uploaded source decks are
persisted outside the relational store. Locally (and in tests) they live on the
filesystem; in production they live in Azure Blob Storage. Both backends share
the :class:`ArtifactStore` protocol so the worker and web app are agnostic to
the deployment target.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from crewmeal.search_enhancement.database import DocumentKey

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    path: str
    byte_count: int
    content_hash: str
    content_type: str | None


class ArtifactStore(Protocol):
    def put_bytes(
        self, path: str, data: bytes, *, content_type: str | None = None
    ) -> StoredArtifact: ...

    def get_bytes(self, path: str) -> bytes: ...

    def exists(self, path: str) -> bool: ...

    def delete_prefix(self, prefix: str) -> None: ...


class LocalArtifactStore:
    """Filesystem-backed store used for local development and tests."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _full(self, path: str) -> Path:
        target = (self._root / path).resolve()
        root = self._root.resolve()
        if root not in target.parents and target != root:
            raise ValueError(f"Refusing to escape artifact root: {path}")
        return target

    def put_bytes(
        self, path: str, data: bytes, *, content_type: str | None = None
    ) -> StoredArtifact:
        target = self._full(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return StoredArtifact(
            path=path,
            byte_count=len(data),
            content_hash=_sha256(data),
            content_type=content_type,
        )

    def get_bytes(self, path: str) -> bytes:
        return self._full(path).read_bytes()

    def exists(self, path: str) -> bool:
        return self._full(path).exists()

    def delete_prefix(self, prefix: str) -> None:
        target = self._full(prefix)
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink()


class AzureBlobArtifactStore:
    """Azure Blob Storage backed store used in production."""

    def __init__(self, account_url: str, container: str, credential: object | None = None) -> None:
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient

        self._service = BlobServiceClient(
            account_url=account_url,
            credential=credential or DefaultAzureCredential(),
        )
        self._container = self._service.get_container_client(container)
        try:
            self._container.create_container()
        except Exception:  # noqa: BLE001 - already exists / insufficient rights
            pass

    def put_bytes(
        self, path: str, data: bytes, *, content_type: str | None = None
    ) -> StoredArtifact:
        from azure.storage.blob import ContentSettings

        settings = ContentSettings(content_type=content_type) if content_type else None
        self._container.upload_blob(
            name=path, data=data, overwrite=True, content_settings=settings
        )
        return StoredArtifact(
            path=path,
            byte_count=len(data),
            content_hash=_sha256(data),
            content_type=content_type,
        )

    def get_bytes(self, path: str) -> bytes:
        return self._container.download_blob(path).readall()

    def exists(self, path: str) -> bool:
        return self._container.get_blob_client(path).exists()

    def delete_prefix(self, prefix: str) -> None:
        for blob in self._container.list_blobs(name_starts_with=prefix):
            self._container.delete_blob(blob.name)


class DatabaseArtifactStore:
    """Relational store keeping artifact bytes in a shared database table.

    Used when object storage is unreachable (e.g. an Azure Policy forces Storage
    ``publicNetworkAccess=Disabled``) but the Postgres queue is reachable by both
    the web and worker containers. The bytes live in the ``artifact_blobs`` table,
    so the worker's rendered HTML is visible to the web status page.
    """

    def __init__(self, engine: "Engine") -> None:
        from crewmeal.search_enhancement.schema import artifact_blobs

        self._engine = engine
        self._table = artifact_blobs
        # Idempotent: create the table if the repository has not initialised yet.
        artifact_blobs.create(engine, checkfirst=True)

    def put_bytes(
        self, path: str, data: bytes, *, content_type: str | None = None
    ) -> StoredArtifact:
        from sqlalchemy import delete, insert

        digest = _sha256(data)
        now = datetime.now(timezone.utc).isoformat()
        with self._engine.begin() as conn:
            conn.execute(delete(self._table).where(self._table.c.path == path))
            conn.execute(
                insert(self._table).values(
                    path=path,
                    data=data,
                    content_type=content_type,
                    content_hash=digest,
                    byte_count=len(data),
                    created_at=now,
                )
            )
        return StoredArtifact(
            path=path,
            byte_count=len(data),
            content_hash=digest,
            content_type=content_type,
        )

    def get_bytes(self, path: str) -> bytes:
        from sqlalchemy import select

        with self._engine.connect() as conn:
            row = conn.execute(
                select(self._table.c.data).where(self._table.c.path == path)
            ).first()
        if row is None:
            raise FileNotFoundError(path)
        return bytes(row[0])

    def exists(self, path: str) -> bool:
        from sqlalchemy import select

        with self._engine.connect() as conn:
            row = conn.execute(
                select(self._table.c.path)
                .where(self._table.c.path == path)
                .limit(1)
            ).first()
        return row is not None

    def delete_prefix(self, prefix: str) -> None:
        from sqlalchemy import delete, or_

        pattern = _like_escape(prefix) + "%"
        with self._engine.begin() as conn:
            conn.execute(
                delete(self._table).where(
                    or_(
                        self._table.c.path == prefix,
                        self._table.c.path.like(pattern, escape="\\"),
                    )
                )
            )


def create_artifact_store(
    *, local_dir: Path | None = None, engine: "Engine | None" = None
) -> ArtifactStore:
    """Build the artifact store from the environment.

    Priority:

    1. Azure Blob when ``CREWMEAL_BLOB_ACCOUNT_URL`` and ``CREWMEAL_BLOB_CONTAINER``
       are set.
    2. The shared database (``artifact_blobs`` table) when
       ``CREWMEAL_ARTIFACT_BACKEND=database`` — used on Azure when Storage public
       access is disabled by policy but Postgres is reachable by both containers.
       Reuses ``engine`` when provided, otherwise resolves one from the
       environment (``DATABASE_URL`` / ``CREWMEAL_SEARCH_DB``).
    3. A local directory (default ``CREWMEAL_ARTIFACT_DIR`` or ``.crewmeal/artifacts``).
    """

    account_url = os.getenv("CREWMEAL_BLOB_ACCOUNT_URL", "").strip()
    container = os.getenv("CREWMEAL_BLOB_CONTAINER", "").strip()
    if account_url and container:
        return AzureBlobArtifactStore(account_url, container)

    backend = os.getenv("CREWMEAL_ARTIFACT_BACKEND", "").strip().lower()
    if backend == "database":
        return DatabaseArtifactStore(engine or _resolve_engine())

    root = local_dir or Path(
        os.getenv("CREWMEAL_ARTIFACT_DIR", ".crewmeal/artifacts")
    ).expanduser()
    return LocalArtifactStore(root)


def _resolve_engine() -> "Engine":
    from crewmeal.search_enhancement.schema import (
        create_db_engine,
        resolve_database_target,
    )

    sqlite_path = Path(
        os.getenv("CREWMEAL_SEARCH_DB", ".crewmeal/search-enhancement.db")
    ).expanduser()
    return create_db_engine(resolve_database_target(sqlite_path))


def artifact_path(
    key: DocumentKey, *, version: int, kind: str, filename: str
) -> str:
    """Deterministic, storage-safe path for a document artifact."""

    segments = [
        "documents",
        _safe(key.tenant_id),
        _safe(key.site_id),
        _safe(key.drive_id),
        _safe(key.item_id),
        f"v{int(version)}",
        _safe(kind),
        _safe(filename),
    ]
    return "/".join(segments)


def document_prefix(key: DocumentKey) -> str:
    return "/".join(
        [
            "documents",
            _safe(key.tenant_id),
            _safe(key.site_id),
            _safe(key.drive_id),
            _safe(key.item_id),
        ]
    )


def _safe(value: str) -> str:
    cleaned = _UNSAFE.sub("-", value.strip())
    return cleaned.strip("-") or "_"


def _like_escape(value: str) -> str:
    """Escape SQL ``LIKE`` wildcards so a prefix is matched literally."""

    return (
        value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

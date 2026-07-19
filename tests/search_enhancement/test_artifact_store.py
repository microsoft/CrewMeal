from __future__ import annotations

from pathlib import Path

import pytest

from crewmeal.search_enhancement.artifact_store import (
    DatabaseArtifactStore,
    LocalArtifactStore,
    create_artifact_store,
)
from crewmeal.search_enhancement.schema import create_db_engine


def _store(tmp_path: Path) -> DatabaseArtifactStore:
    engine = create_db_engine(tmp_path / "artifacts.db")
    return DatabaseArtifactStore(engine)


def test_database_store_round_trips_bytes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    html = b"<html><body>hi</body></html>"

    stored = store.put_bytes("documents/t/s/d/item/v1/html/index.html", html, content_type="text/html")

    assert stored.byte_count == len(html)
    assert stored.content_type == "text/html"
    assert store.exists("documents/t/s/d/item/v1/html/index.html")
    assert store.get_bytes("documents/t/s/d/item/v1/html/index.html") == html


def test_database_store_overwrites_same_path(tmp_path: Path) -> None:
    store = _store(tmp_path)
    path = "documents/t/s/d/item/v1/json/analysis.json"

    store.put_bytes(path, b"v1")
    store.put_bytes(path, b"v2-longer")

    assert store.get_bytes(path) == b"v2-longer"


def test_database_store_missing_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert not store.exists("nope")
    with pytest.raises(FileNotFoundError):
        store.get_bytes("nope")


def test_database_store_delete_prefix_removes_document_tree(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_bytes("documents/t/s/d/item/v1/html/a.html", b"a")
    store.put_bytes("documents/t/s/d/item/v1/json/b.json", b"b")
    store.put_bytes("documents/t/s/d/other/v1/html/c.html", b"c")

    store.delete_prefix("documents/t/s/d/item")

    assert not store.exists("documents/t/s/d/item/v1/html/a.html")
    assert not store.exists("documents/t/s/d/item/v1/json/b.json")
    assert store.exists("documents/t/s/d/other/v1/html/c.html")


def test_database_store_delete_prefix_escapes_like_wildcards(tmp_path: Path) -> None:
    # A literal underscore in the prefix must not act as a single-char wildcard.
    store = _store(tmp_path)
    store.put_bytes("documents/A_B/v1/html/keep.html", b"target")
    store.put_bytes("documents/AXBYC/v1/html/keep.html", b"bystander")

    store.delete_prefix("documents/A_B")

    assert not store.exists("documents/A_B/v1/html/keep.html")
    assert store.exists("documents/AXBYC/v1/html/keep.html")


def test_create_artifact_store_selects_database_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CREWMEAL_BLOB_ACCOUNT_URL", raising=False)
    monkeypatch.delenv("CREWMEAL_BLOB_CONTAINER", raising=False)
    monkeypatch.setenv("CREWMEAL_ARTIFACT_BACKEND", "database")
    engine = create_db_engine(tmp_path / "sel.db")

    store = create_artifact_store(engine=engine)

    assert isinstance(store, DatabaseArtifactStore)


def test_create_artifact_store_defaults_to_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CREWMEAL_BLOB_ACCOUNT_URL", raising=False)
    monkeypatch.delenv("CREWMEAL_BLOB_CONTAINER", raising=False)
    monkeypatch.delenv("CREWMEAL_ARTIFACT_BACKEND", raising=False)

    store = create_artifact_store(local_dir=tmp_path / "arts")

    assert isinstance(store, LocalArtifactStore)

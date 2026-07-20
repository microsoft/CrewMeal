from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import delete

from scripts.configure_test_library import _ensure_content_site_column
from crewmeal.search_enhancement.config import SearchEnhancementConfig
from crewmeal.search_enhancement.database import (
    DocumentKey,
    LEGACY_PUBLICATION_MIGRATION_SETTING,
    SearchEnhancementRepository,
)
from crewmeal.search_enhancement.html_renderer import (
    ColumnContentTooLargeError,
    SHAREPOINT_MARKDOWN_OMISSION_NOTICE,
    render_sharepoint_column_markdown,
    sharepoint_character_count,
)
from crewmeal.search_enhancement.models import RenderedHtml
from crewmeal.search_enhancement.publication import (
    DEFAULT_COLUMN_INTERNAL_NAME,
    PublicationTarget,
    parse_publication_target,
    validate_column_display_name,
)
from crewmeal.search_enhancement.schema import (
    METADATA,
    create_db_engine,
    document_publications,
    publication_transitions,
)


def _rendered(content: str) -> RenderedHtml:
    data = content.encode("utf-8")
    return RenderedHtml(
        content=content,
        byte_count=len(data),
        sha256="source",
        slide_titles=("One",),
        keywords=("One",),
    )


def _article(*sections: str) -> str:
    return (
        "<article><header><h1>Document</h1></header>"
        + "".join(f"<section>{section}</section>" for section in sections)
        + "</article>"
    )


def _config(tmp_path: Path) -> SearchEnhancementConfig:
    return SearchEnhancementConfig(
        tenant_id="tenant",
        client_id="client",
        client_secret="secret",
        site_id="site",
        drive_id="drive",
        list_id="list",
        site_url="https://tenant.sharepoint.com/sites/test",
        sqlite_path=tmp_path / "state.db",
    )


def _seed_document(
    repository: SearchEnhancementRepository,
    *,
    item_id: str = "item",
) -> DocumentKey:
    key = DocumentKey("tenant", "site", "drive", item_id)
    repository.upsert_document(
        key=key,
        list_id="list",
        list_item_id="7",
        web_url="https://tenant.sharepoint.com/report.hwp",
        file_name="report.hwp",
        connection_id="connection",
        desired_enabled=True,
        status="Ready",
        request_id=str(uuid4()),
    )
    return key


def test_publication_target_and_display_name_validation() -> None:
    assert parse_publication_target("sharepoint_column") is (
        PublicationTarget.SHAREPOINT_COLUMN
    )
    assert validate_column_display_name("  검색 콘텐츠  ") == "검색 콘텐츠"
    with pytest.raises(ValueError, match="Unsupported publication target"):
        parse_publication_target("unknown")
    with pytest.raises(ValueError, match="must not be empty"):
        validate_column_display_name(" ")


def test_sharepoint_character_count_uses_utf16_code_units() -> None:
    assert sharepoint_character_count("한A") == 2
    assert sharepoint_character_count("🙂") == 2


def test_column_renderer_preserves_content_within_budget() -> None:
    content = _article("<h2>1</h2><p>본문</p>")
    result = render_sharepoint_column_markdown(_rendered(content))

    assert result.content == "# Document\n\n## 1\n\n본문"
    assert result.truncated is False
    assert result.character_count == sharepoint_character_count(result.content)
    assert result.original_character_count == result.character_count


def test_column_renderer_keeps_complete_page_and_adds_notice() -> None:
    first = "<h2>1</h2><p>첫 페이지</p>"
    second = "<h2>2</h2><p>" + ("나" * 500) + "</p>"
    full = _article(first, second)
    expected = (
        "# Document\n\n## 1\n\n첫 페이지\n\n"
        + SHAREPOINT_MARKDOWN_OMISSION_NOTICE
    )
    result = render_sharepoint_column_markdown(
        _rendered(full),
        max_characters=sharepoint_character_count(expected),
    )

    assert result.content == expected
    assert "첫 페이지" in result.content
    assert "나" * 100 not in result.content
    assert result.truncated is True
    assert result.omitted_units == 1
    assert result.character_count <= sharepoint_character_count(expected)


def test_column_renderer_falls_back_to_complete_section_blocks() -> None:
    section = (
        "<h2>큰 페이지</h2>"
        "<p>보존할 블록</p>"
        "<p>" + ("X" * 500) + "</p>"
    )
    full = _article(section)
    expected = (
        "# Document\n\n## 큰 페이지\n\n보존할 블록\n\n"
        + SHAREPOINT_MARKDOWN_OMISSION_NOTICE
    )
    result = render_sharepoint_column_markdown(
        _rendered(full),
        max_characters=sharepoint_character_count(expected),
    )

    assert "보존할 블록" in result.content
    assert "X" * 100 not in result.content
    assert result.content.endswith(SHAREPOINT_MARKDOWN_OMISSION_NOTICE)


def test_column_renderer_preserves_table_structure_as_markdown() -> None:
    content = _article(
        "<h2>일정</h2>"
        "<table><caption>파일럿</caption><thead><tr>"
        "<th>항목</th><th>값</th></tr></thead><tbody><tr>"
        "<td>날짜</td><td>2031-04-17</td></tr></tbody></table>"
    )

    result = render_sharepoint_column_markdown(_rendered(content))

    assert "**표: 파일럿**" in result.content
    assert "| 항목 | 값 |" in result.content
    assert "| 날짜 | 2031-04-17 |" in result.content
    assert "<table>" not in result.content


def test_column_renderer_rejects_budget_smaller_than_fixed_markup() -> None:
    content = _article("<h2>1</h2><p>" + ("A" * 100) + "</p>")
    with pytest.raises(ColumnContentTooLargeError, match="COLUMN_CONTENT_TOO_LARGE"):
        render_sharepoint_column_markdown(_rendered(content), max_characters=20)


def test_fresh_database_starts_unset_and_records_publication(tmp_path: Path) -> None:
    repository = SearchEnhancementRepository(tmp_path / "state.db")
    repository.initialize()
    assert repository.get_publication_transition().active_target is (
        PublicationTarget.UNSET
    )

    transition = repository.request_publication_target(
        PublicationTarget.COPILOT_CONNECTOR
    )
    assert transition.status == "staging"
    assert transition.effective_target is PublicationTarget.COPILOT_CONNECTOR

    key = _seed_document(repository)
    repository.record_publication_success(
        key,
        target=PublicationTarget.COPILOT_CONNECTOR,
        generation=transition.generation,
        locator="external-id",
        content_hash="hash",
        original_characters=100,
        stored_characters=100,
        stored_bytes=120,
        truncated=False,
    )

    record = repository.get_publication(
        key,
        PublicationTarget.COPILOT_CONNECTOR,
    )
    assert record is not None
    assert record.locator == "external-id"
    assert repository.publication_progress(
        target=PublicationTarget.COPILOT_CONNECTOR,
        generation=transition.generation,
    ) == {"ready": 1, "failed": 0, "pending": 0, "truncated": 0}


def test_legacy_external_items_backfill_connector_target(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    repository = SearchEnhancementRepository(database)
    repository.initialize()
    key = _seed_document(repository)
    document = repository.get_document(key)
    assert document is not None
    repository.record_success(
        key,
        request_id=document.request_id,
        source_etag="etag",
        last_modified_datetime="2026-01-01T00:00:00Z",
        source_size=10,
        acl_digest="acl",
        external_item_id="external-id",
        web_url="https://tenant.sharepoint.com/report.hwp",
        file_name="report.hwp",
        html_bytes=100,
        request_bytes=200,
        content_hash="hash",
    )
    with repository.engine.begin() as connection:
        connection.execute(delete(document_publications))
        connection.execute(delete(publication_transitions))
    repository.dispose()

    reopened = SearchEnhancementRepository(database)
    reopened.initialize()
    transition = reopened.get_publication_transition()
    assert transition.active_target is PublicationTarget.COPILOT_CONNECTOR
    publication = reopened.get_publication(
        key,
        PublicationTarget.COPILOT_CONNECTOR,
    )
    assert publication is not None
    assert publication.locator == "external-id"


def test_legacy_database_without_successful_items_preserves_connector_mode(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-empty.db"
    repository = SearchEnhancementRepository(database)
    repository.initialize()
    with repository.engine.begin() as connection:
        document_publications.drop(connection)
        publication_transitions.drop(connection)
    repository.dispose()

    reopened = SearchEnhancementRepository(database)
    reopened.initialize()

    assert reopened.get_publication_transition().active_target is (
        PublicationTarget.COPILOT_CONNECTOR
    )


def test_interrupted_fresh_schema_initialization_still_starts_unset(
    tmp_path: Path,
) -> None:
    database = tmp_path / "interrupted-fresh.db"
    engine = create_db_engine(database)
    METADATA.create_all(engine)
    engine.dispose()

    repository = SearchEnhancementRepository(database)
    repository.initialize()

    assert repository.get_publication_transition().active_target is (
        PublicationTarget.UNSET
    )


def test_interrupted_legacy_migration_marker_preserves_connector_mode(
    tmp_path: Path,
) -> None:
    database = tmp_path / "interrupted-legacy.db"
    repository = SearchEnhancementRepository(database)
    repository.initialize()
    repository.set_setting(LEGACY_PUBLICATION_MIGRATION_SETTING, True)
    with repository.engine.begin() as connection:
        connection.execute(delete(publication_transitions))
    repository.dispose()

    reopened = SearchEnhancementRepository(database)
    reopened.initialize()

    assert reopened.get_publication_transition().active_target is (
        PublicationTarget.COPILOT_CONNECTOR
    )
    assert reopened.get_setting(LEGACY_PUBLICATION_MIGRATION_SETTING) is None


def test_pending_publication_blocks_target_activation(tmp_path: Path) -> None:
    repository = SearchEnhancementRepository(tmp_path / "state.db")
    repository.initialize()
    transition = repository.request_publication_target("copilot_connector")
    key = _seed_document(repository)
    assert repository.record_publication_pending(
        key,
        transition=transition,
        target="copilot_connector",
        locator="external-id",
        operation_id="job-id",
    )
    repository.set_transition_status("cleaning")

    assert not repository.activate_publication_target(
        expected_generation=transition.generation,
        expected_desired_target=transition.desired_target,
        expected_active_target=transition.active_target,
    )
    assert repository.get_publication_transition().status == "cleaning"

    repository.record_publication_success(
        key,
        target="copilot_connector",
        generation=transition.generation,
        locator="external-id",
        content_hash="hash",
        original_characters=10,
        stored_characters=10,
        stored_bytes=10,
        truncated=False,
    )
    assert repository.activate_publication_target(
        expected_generation=transition.generation,
        expected_desired_target=transition.desired_target,
        expected_active_target=transition.active_target,
    )


def test_publication_operation_token_rejects_late_equal_generation_result(
    tmp_path: Path,
) -> None:
    repository = SearchEnhancementRepository(tmp_path / "state.db")
    repository.initialize()
    transition = repository.request_publication_target("copilot_connector")
    key = _seed_document(repository)
    assert repository.record_publication_pending(
        key,
        transition=transition,
        target="copilot_connector",
        locator="external-id",
        operation_id="operation-a",
    )
    assert repository.record_publication_pending(
        key,
        transition=transition,
        target="copilot_connector",
        locator="external-id",
        operation_id="operation-b",
    )
    assert repository.record_publication_success(
        key,
        target="copilot_connector",
        generation=transition.generation,
        locator="external-id",
        content_hash="new-hash",
        original_characters=10,
        stored_characters=10,
        stored_bytes=10,
        truncated=False,
        operation_id="operation-b",
    )

    assert not repository.record_publication_error(
        key,
        target="copilot_connector",
        generation=transition.generation,
        code="LATE_FAILURE",
        message="late failure",
        operation_id="operation-a",
    )
    publication = repository.get_publication(key, "copilot_connector")
    assert publication is not None
    assert publication.status == "ready"
    assert publication.content_hash == "new-hash"


def test_delete_request_fences_a_late_publication_insert(tmp_path: Path) -> None:
    repository = SearchEnhancementRepository(tmp_path / "state.db")
    repository.initialize()
    transition = repository.request_publication_target("copilot_connector")
    key = _seed_document(repository)
    document = repository.get_document(key)
    assert document is not None
    job_id = repository.enqueue_job(
        key=key,
        request_id=document.request_id,
        job_type="upsert",
    )
    claimed = repository.claim_next_job(worker_id="worker-a", lease_seconds=30)
    assert claimed is not None and claimed.job_id == job_id

    repository.queue_removal(key, trigger="remove")

    assert not repository.record_publication_pending(
        key,
        transition=transition,
        target="copilot_connector",
        locator="external-id",
        operation_id="late-enhance",
        request_id=claimed.request_id,
        job_id=claimed.job_id,
        expected_attempts=claimed.attempts,
        expected_lease_owner="worker-a",
    )
    assert not repository.record_publication_success(
        key,
        target="copilot_connector",
        generation=transition.generation,
        locator="external-id",
        content_hash="late-hash",
        original_characters=10,
        stored_characters=10,
        stored_bytes=10,
        truncated=False,
    )
    assert repository.get_publication(key, "copilot_connector") is None


def test_reconcile_refresh_cannot_supersede_a_newer_remove(tmp_path: Path) -> None:
    repository = SearchEnhancementRepository(tmp_path / "state.db")
    repository.initialize()
    key = _seed_document(repository)
    document = repository.get_document(key)
    assert document is not None
    assert repository.record_success(
        key,
        request_id=document.request_id,
        source_etag="source",
        last_modified_datetime="2026-07-17T00:00:00Z",
        source_size=10,
        acl_digest="acl",
        external_item_id=None,
        web_url="https://tenant.sharepoint.com/report.hwp",
        file_name="report.hwp",
        html_bytes=10,
        request_bytes=10,
        content_hash="hash",
    )
    removal_request, _ = repository.queue_removal(key, trigger="remove")

    assert (
        repository.queue_refresh_if_current(
            key,
            expected_request_id=document.request_id,
            trigger="reconcile",
        )
        is None
    )
    current = repository.get_document(key)
    assert current is not None
    assert current.request_id == removal_request
    assert current.desired_enabled is False


def test_publication_removal_claim_rejects_a_newer_record(tmp_path: Path) -> None:
    repository = SearchEnhancementRepository(tmp_path / "state.db")
    repository.initialize()
    transition = repository.request_publication_target("copilot_connector")
    key = _seed_document(repository)
    repository.record_publication_success(
        key,
        target="copilot_connector",
        generation=transition.generation,
        locator="external-id",
        content_hash="old-hash",
        original_characters=10,
        stored_characters=10,
        stored_bytes=10,
        truncated=False,
    )
    stale = repository.get_publication(key, "copilot_connector")
    assert stale is not None
    assert repository.record_publication_pending(
        key,
        transition=transition,
        target="copilot_connector",
        locator="external-id",
        operation_id="new-operation",
    )
    assert repository.record_publication_success(
        key,
        target="copilot_connector",
        generation=transition.generation,
        locator="external-id",
        content_hash="new-hash",
        original_characters=20,
        stored_characters=20,
        stored_bytes=20,
        truncated=False,
        operation_id="new-operation",
    )

    assert not repository.claim_publication_removal(
        key,
        "copilot_connector",
        expected_updated_at=stale.updated_at,
        operation_id="stale-cleanup",
    )
    current = repository.get_publication(key, "copilot_connector")
    assert current is not None
    assert current.status == "ready"
    assert current.content_hash == "new-hash"


def test_activation_cas_rejects_a_current_queued_document(tmp_path: Path) -> None:
    repository = SearchEnhancementRepository(tmp_path / "state.db")
    repository.initialize()
    transition = repository.request_publication_target("copilot_connector")
    key = _seed_document(repository)
    repository.record_publication_success(
        key,
        target="copilot_connector",
        generation=transition.generation,
        locator="external-id",
        content_hash="hash",
        original_characters=10,
        stored_characters=10,
        stored_bytes=10,
        truncated=False,
    )
    repository.set_transition_status("cleaning")
    repository.queue_refresh(key, trigger="concurrent-refresh")

    assert not repository.activate_publication_target(
        expected_generation=transition.generation,
        expected_desired_target=transition.desired_target,
        expected_active_target=transition.active_target,
    )
    assert repository.get_publication_transition().status == "cleaning"


def test_older_publication_failure_does_not_overwrite_newer_success(
    tmp_path: Path,
) -> None:
    repository = SearchEnhancementRepository(tmp_path / "state.db")
    repository.initialize()
    key = _seed_document(repository)
    repository.record_publication_success(
        key,
        target="copilot_connector",
        generation=2,
        locator="external-id",
        content_hash="hash",
        original_characters=10,
        stored_characters=10,
        stored_bytes=10,
        truncated=False,
    )

    repository.record_publication_error(
        key,
        target="copilot_connector",
        generation=1,
        code="OLD_FAILURE",
        message="late failure",
    )

    publication = repository.get_publication(key, "copilot_connector")
    assert publication is not None
    assert publication.generation == 2
    assert publication.status == "ready"


def test_equal_generation_unscoped_failure_does_not_overwrite_success(
    tmp_path: Path,
) -> None:
    repository = SearchEnhancementRepository(tmp_path / "state.db")
    repository.initialize()
    key = _seed_document(repository)
    repository.record_publication_success(
        key,
        target="copilot_connector",
        generation=2,
        locator="external-id",
        content_hash="hash",
        original_characters=10,
        stored_characters=10,
        stored_bytes=10,
        truncated=False,
    )

    assert not repository.record_publication_error(
        key,
        target="copilot_connector",
        generation=2,
        code="LATE_FAILURE",
        message="late failure",
    )
    publication = repository.get_publication(key, "copilot_connector")
    assert publication is not None
    assert publication.status == "ready"
    assert publication.content_hash == "hash"


def test_completed_request_rejects_late_document_failure(tmp_path: Path) -> None:
    repository = SearchEnhancementRepository(tmp_path / "state.db")
    repository.initialize()
    key = _seed_document(repository)
    document = repository.get_document(key)
    assert document is not None
    assert repository.record_success(
        key,
        request_id=document.request_id,
        source_etag="etag",
        last_modified_datetime="2026-01-01T00:00:00Z",
        source_size=10,
        acl_digest="acl",
        external_item_id="external-id",
        web_url=document.web_url,
        file_name=document.file_name,
        html_bytes=100,
        request_bytes=200,
        content_hash="hash",
    )

    assert not repository.record_document_error(
        key,
        request_id=document.request_id,
        code="LATE_FAILURE",
        message="late failure",
    )
    current = repository.get_document(key)
    assert current is not None
    assert current.status == "Ready"


def test_queue_refresh_rolls_back_document_state_when_job_insert_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SearchEnhancementRepository(tmp_path / "state.db")
    repository.initialize()
    key = _seed_document(repository)
    before = repository.get_document(key)
    assert before is not None

    def fail_enqueue(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("job insert failed")

    monkeypatch.setattr(repository, "_enqueue_job", fail_enqueue)
    with pytest.raises(RuntimeError, match="job insert failed"):
        repository.queue_refresh(key, trigger="transition")

    after = repository.get_document(key)
    assert after is not None
    assert after.request_id == before.request_id
    assert after.status == before.status


def test_ingest_upsert_rolls_back_document_when_job_insert_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SearchEnhancementRepository(tmp_path / "state.db")
    repository.initialize()
    key = DocumentKey("tenant", "site", "drive", "new-item")

    def fail_enqueue(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("job insert failed")

    monkeypatch.setattr(repository, "_enqueue_job", fail_enqueue)
    with pytest.raises(RuntimeError, match="job insert failed"):
        repository.upsert_document_and_enqueue(
            key=key,
            list_id="list",
            list_item_id="9",
            web_url="https://tenant.sharepoint.com/new.pptx",
            file_name="new.pptx",
            connection_id="connection",
            desired_enabled=True,
            status="Queued",
            request_id=str(uuid4()),
            job_type="upsert",
            trigger="spfx",
        )

    assert repository.get_document(key) is None


class _ProvisionGraph:
    def __init__(self) -> None:
        self.site_columns: list[dict[str, Any]] = []
        self.list_columns: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    def get_json(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        del params
        for column in self.site_columns:
            if path.endswith(f"/{column['id']}"):
                return dict(column)
        values = self.list_columns if "/lists/" in path else self.site_columns
        return {"value": [dict(value) for value in values]}

    def send_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any],
        expected: tuple[int, ...],
    ) -> dict[str, Any]:
        del expected
        self.requests.append((method, path, body))
        if method == "PATCH":
            self.site_columns[0].update(body)
            return dict(self.site_columns[0])
        if "/lists/" in path:
            site_id = body["sourceColumn"]["id"]
            source = next(value for value in self.site_columns if value["id"] == site_id)
            attached = dict(source)
            self.list_columns.append(attached)
            return attached
        created = {
            **body,
            "id": "99999999-9999-4999-8999-999999999999",
        }
        self.site_columns.append(created)
        return dict(created)


def test_content_site_column_provisioning_is_idempotent(tmp_path: Path) -> None:
    graph = _ProvisionGraph()
    config = _config(tmp_path)

    first = _ensure_content_site_column(graph, config, display_name="검색 본문")
    graph.list_columns.append(dict(graph.site_columns[0]))
    second = _ensure_content_site_column(graph, config, display_name="검색 본문")

    assert first["created"] is True
    assert first["attached"] is False
    assert second["created"] is False
    assert second["attached"] is True
    assert graph.site_columns[0]["name"] == DEFAULT_COLUMN_INTERNAL_NAME
    assert graph.site_columns[0]["text"]["textType"] == "plain"


def test_content_site_column_rejects_incompatible_existing_field(
    tmp_path: Path,
) -> None:
    graph = _ProvisionGraph()
    graph.site_columns.append(
        {
            "id": "bad",
            "name": DEFAULT_COLUMN_INTERNAL_NAME,
            "displayName": "Bad",
            "text": {"allowMultipleLines": False},
        }
    )

    with pytest.raises(RuntimeError, match="CONTENT_COLUMN_TYPE_MISMATCH"):
        _ensure_content_site_column(graph, _config(tmp_path))

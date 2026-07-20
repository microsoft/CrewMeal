"""End-to-end proof that a MIP-encrypted upload flows through the full pipeline.

This mirrors the real *tryout* path an admin uses:

    reference protect CLI encrypts a .pptx
      -> stored as the upload's ``source_pptx`` artifact
      -> SearchEnhancementWorker claims the job, loads the (encrypted) source
      -> processor.decrypt_source() shells out to the reference MIP CLI
           (SubprocessMipSdkRunner) and gets plaintext back
      -> content fingerprint + format handling run on the *decrypted* deck
      -> analysis + HTML render -> artifacts stored -> document marked Ready

The MIP decryption is exercised for real here: a live subprocess runs the
bundled reference ``unprotect`` tool against a genuine AES envelope, with an
app-only RMS token acquired from an injected credential. Only two things are
faked, both by established repo convention:

* the LibreOffice render + LLM analysis (no ``soffice`` / model in CI), replaced
  by a spy format handler that additionally *asserts it received the decrypted
  plaintext* — the core end-to-end guarantee — and a deterministic analysis
  service; and
* the RMS token issuer (a fake credential), because there is no live Azure RMS
  tenant. The reference CLI stands in for the Microsoft MIP File SDK behind the
  identical runner/config seam; production swaps ``CREWMEAL_MIP_SDK_CLI``.

``test_mip_encrypted_upload_full_render_with_libreoffice`` is the same flow with
*no* handler monkeypatch, so LibreOffice really renders the deck. It skips unless
``soffice`` is available, so a properly provisioned environment (the Docker
image / CI) runs the entire pipeline against real encryption.
"""

from __future__ import annotations

import io
import shutil
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from crewmeal.config import AppConfig
from crewmeal.models import RendererManifest, SourceManifest
from crewmeal.search_enhancement import processor as processor_module
from crewmeal.search_enhancement.artifact_store import (
    LocalArtifactStore,
    artifact_path,
)
from crewmeal.search_enhancement.config import SearchEnhancementConfig
from crewmeal.search_enhancement.database import SearchEnhancementRepository
from crewmeal.search_enhancement.decryption import decryption_setting_key
from crewmeal.search_enhancement.formats.base import PreparedDocument
from crewmeal.search_enhancement.mip_sdk import MipSdkConfig, build_runner
from crewmeal.search_enhancement.mip_tool import protect_bytes
from crewmeal.search_enhancement.models import (
    ContentSection,
    SlideContent,
    SlideSchedule,
    StructuredAnalysisResult,
)
from crewmeal.search_enhancement.processor import PresentationProcessor
from crewmeal.search_enhancement.sharepoint_control import ControlItem
from crewmeal.search_enhancement.worker import SearchEnhancementWorker
from crewmeal.source import pptx_content_fingerprint

# A recognizable string that must survive decryption and appear in the final HTML.
SLIDE_MARKER = "MIP-E2E-검증-슬라이드"


def _pptx_bytes(marker: str) -> bytes:
    """A minimal but validate_pptx-acceptable .pptx (valid zip starting with PK)."""

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", "<Types/>")
        package.writestr("ppt/presentation.xml", "<p:presentation/>")
        package.writestr("ppt/_rels/presentation.xml.rels", "<Relationships/>")
        package.writestr("ppt/slides/slide1.xml", f"<slide>{marker}</slide>")
    return output.getvalue()


def _config(db_path: Path) -> SearchEnhancementConfig:
    return SearchEnhancementConfig(
        tenant_id="tenant",
        client_id="client",
        client_secret="secret",
        site_id="site",
        drive_id="drive",
        list_id="list",
        site_url="https://tenant.sharepoint.com/sites/test",
        sqlite_path=db_path,
    )


class _FakeCredential:
    """Stand-in for the M365 service principal that would be an RMS super-user."""

    def __init__(self) -> None:
        self.scopes: list[str] = []

    def get_token(self, *scopes: str, **_kwargs: Any) -> SimpleNamespace:
        self.scopes.extend(scopes)
        return SimpleNamespace(token="fake-rms-super-user-token", expires_on=0)


class _FakeControl:
    """Upload jobs never touch SharePoint; this yields no commands and no I/O."""

    def list_commands(self) -> tuple[ControlItem, ...]:
        return ()


class _UnusedConnector:
    def prepare_item(self, **_kwargs: Any) -> Any:  # pragma: no cover - guard
        raise AssertionError("connector must not be used for an upload job")

    def upsert(self, *_args: Any, **_kwargs: Any) -> None:  # pragma: no cover
        raise AssertionError("connector must not be used for an upload job")


class _FakeAnalysisService:
    """Deterministic analysis so the pipeline completes without a vision model."""

    def __init__(self, result: StructuredAnalysisResult) -> None:
        self.result = result
        self.calls = 0

    def analyze(
        self,
        page_images: Any,
        *,
        source_manifest: Any,
        source_name: str,
        geometry_by_slide: Any,
        progress: Any = None,
        corrections: Any = None,
    ) -> StructuredAnalysisResult:
        self.calls += 1
        return self.result


class _SpyHandler:
    """Format handler that proves it received the *decrypted* plaintext deck.

    Replaces the real pptx handler (which needs LibreOffice) via a monkeypatch on
    ``processor_module.detect_handler``. Its ``prepare`` asserts the bytes equal
    the original plaintext — the end-to-end decryption guarantee — then returns a
    deterministic single-slide document so analysis + HTML render can run.
    """

    format_id = "pptx"

    def __init__(self, expected_plaintext: bytes, captured: dict[str, Any]) -> None:
        self._expected = expected_plaintext
        self._captured = captured

    def validate(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def prepare(
        self,
        data: bytes,
        *,
        source_name: str,
        config: AppConfig,
        reporter: Any,
    ) -> PreparedDocument:
        self._captured["prepared_with"] = data
        assert data == self._expected, "handler did not receive decrypted plaintext"
        texts = {1: (SLIDE_MARKER,)}
        source = SourceManifest(
            slide_count=1,
            texts_by_slide=texts,
            links_by_slide={},
            alt_text_by_slide={},
            notes_by_slide={},
        )
        renderer = RendererManifest(
            page_count=1,
            texts_by_page=texts,
            links_by_page={},
            page_images={},
        )
        return PreparedDocument(
            source_manifest=source,
            renderer_manifest=renderer,
            semantic_slides=None,
        )


def _analysis_result(source_name: str) -> StructuredAnalysisResult:
    slide = SlideContent(
        slide_number=1,
        title=SLIDE_MARKER,
        summary="",
        facts=(),
        sections=(ContentSection(heading="본문", paragraphs=(SLIDE_MARKER,), bullets=()),),
        hierarchies=(),
        schedule=SlideSchedule(time_axis=(), tasks=(), milestones=()),
        flows=(),
        tables=(),
        charts=(),
        relationships=(),
        images=(),
        warnings=(),
    )
    return StructuredAnalysisResult(
        source_name=source_name,
        slides=(slide,),
        usage={"slideImages": 1},
        raw_result={"model": "reference-mip-e2e"},
        warnings=(),
        analysis_seconds=1,
    )


def _reference_runner(credential: _FakeCredential):
    """A runner wired to the bundled reference CLI, exactly like production wiring
    but pointed at ``mip_tool`` instead of the Microsoft MIP File SDK CLI."""

    config = MipSdkConfig(
        command=(sys.executable, "-m", "crewmeal.search_enhancement.mip_tool"),
    )
    runner = build_runner(config, credential)
    assert runner is not None  # both a command and a credential are present
    return runner


def _enqueue_encrypted_upload(
    repository: SearchEnhancementRepository,
    store: LocalArtifactStore,
    *,
    file_name: str,
    encrypted: bytes,
):
    """Mirror the admin tryout upload of an already-encrypted file."""

    document = repository.create_upload_document(
        file_name=file_name, connection_id="conn"
    )
    stored = store.put_bytes(
        artifact_path(
            document.key, version=0, kind="source_pptx", filename="source.pptx"
        ),
        encrypted,
    )
    repository.record_artifact(
        document.key,
        kind="source_pptx",
        blob_path=stored.path,
        byte_count=stored.byte_count,
        enhancement_version=0,
    )
    repository.enqueue_job(
        key=document.key,
        request_id=document.request_id,
        job_type="upsert",
        trigger="upload",
    )
    return document


def test_mip_encrypted_upload_processes_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plaintext = _pptx_bytes(SLIDE_MARKER)
    encrypted = protect_bytes(plaintext, filename="deck.pptx")
    # Sanity: the stored source really is an opaque MIP envelope, not a .pptx.
    assert not encrypted.startswith(b"PK")
    assert b"MicrosoftIRMServices" in encrypted[:64]

    repository = SearchEnhancementRepository(tmp_path / "worker.db")
    repository.initialize()
    repository.request_publication_target("copilot_connector")
    store = LocalArtifactStore(tmp_path / "artifacts")

    document = _enqueue_encrypted_upload(
        repository, store, file_name="deck.pptx", encrypted=encrypted
    )

    credential = _FakeCredential()
    analysis = _FakeAnalysisService(_analysis_result("deck.pptx"))
    processor = PresentationProcessor(
        AppConfig(endpoint=None, max_upload_bytes=50_000_000, soffice_path=Path("soffice")),
        analysis_service=analysis,  # type: ignore[arg-type]
        decryption_settings={decryption_setting_key("mip"): True},
        mip_runner=_reference_runner(credential),
    )

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        processor_module,
        "detect_handler",
        lambda source_name, *a, **k: _SpyHandler(plaintext, captured),
    )

    worker = SearchEnhancementWorker(
        config=_config(tmp_path / "worker.db"),
        repository=repository,
        control=_FakeControl(),  # type: ignore[arg-type]
        connector=_UnusedConnector(),  # type: ignore[arg-type]
        processor=processor,
        artifact_store=store,
        worker_id="mip-e2e-worker",
    )

    run = worker.run_once()

    # The single upload job was processed to completion.
    assert run.jobs_processed == 1
    refreshed = repository.get_document(document.key)
    assert refreshed is not None
    assert refreshed.status == "Ready"
    assert (
        repository.has_pending_job(document.key, request_id=document.request_id)
        is False
    )

    # Decryption actually ran: the format handler saw the plaintext deck, and the
    # stored source fingerprint is that of the *decrypted* bytes (proving the
    # worker decrypts before fingerprinting — not on the ciphertext envelope).
    assert captured.get("prepared_with") == plaintext
    assert refreshed.source_etag == pptx_content_fingerprint(plaintext)
    assert analysis.calls == 1
    assert credential.scopes == ["https://aadrm.com/.default"]

    # The pipeline produced HTML derived from the decrypted deck's content.
    html = repository.get_latest_artifact(document.key, "html")
    assert html is not None and html.enhancement_version == 1
    html_bytes = store.get_bytes(html.blob_path)
    assert SLIDE_MARKER in html_bytes.decode("utf-8")


def test_mip_encrypted_upload_full_render_with_libreoffice(tmp_path: Path) -> None:
    """Same flow, but let LibreOffice really render the decrypted deck.

    Skips unless ``soffice`` is installed. In the Docker image / CI this runs the
    entire pipeline — decrypt, convert, render, analyze — against a real
    encryption envelope, so the end-to-end path is proven without any handler
    stub. ``python-pptx`` builds a genuinely renderable deck.
    """

    if shutil.which("soffice") is None:
        pytest.skip("LibreOffice (soffice) is unavailable; full-render e2e skipped")
    pptx = pytest.importorskip("pptx")

    buffer = io.BytesIO()
    presentation = pptx.Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide.shapes.title.text = SLIDE_MARKER
    presentation.save(buffer)
    plaintext = buffer.getvalue()
    encrypted = protect_bytes(plaintext, filename="deck.pptx")

    repository = SearchEnhancementRepository(tmp_path / "worker.db")
    repository.initialize()
    repository.request_publication_target("copilot_connector")
    store = LocalArtifactStore(tmp_path / "artifacts")
    document = _enqueue_encrypted_upload(
        repository, store, file_name="deck.pptx", encrypted=encrypted
    )

    credential = _FakeCredential()
    analysis = _FakeAnalysisService(_analysis_result("deck.pptx"))
    processor = PresentationProcessor(
        AppConfig(
            endpoint=None,
            max_upload_bytes=50_000_000,
            soffice_path=Path(shutil.which("soffice") or "soffice"),
        ),
        analysis_service=analysis,  # type: ignore[arg-type]
        decryption_settings={decryption_setting_key("mip"): True},
        mip_runner=_reference_runner(credential),
    )

    worker = SearchEnhancementWorker(
        config=_config(tmp_path / "worker.db"),
        repository=repository,
        control=_FakeControl(),  # type: ignore[arg-type]
        connector=_UnusedConnector(),  # type: ignore[arg-type]
        processor=processor,
        artifact_store=store,
        worker_id="mip-e2e-render-worker",
    )

    run = worker.run_once()

    assert run.jobs_processed == 1
    refreshed = repository.get_document(document.key)
    assert refreshed is not None and refreshed.status == "Ready"
    assert refreshed.source_etag == pptx_content_fingerprint(plaintext)
    assert repository.get_latest_artifact(document.key, "html") is not None

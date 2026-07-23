from __future__ import annotations

import argparse
import logging

from crewmeal.config import AppConfig
from crewmeal.search_enhancement.artifact_store import create_artifact_store
from crewmeal.search_enhancement.config import SearchEnhancementConfig
from crewmeal.search_enhancement.connector_client import ConnectorClient
from crewmeal.search_enhancement.database import SearchEnhancementRepository
from crewmeal.search_enhancement.db_resilience import run_with_db_retry
from crewmeal.search_enhancement.decryption import is_decryption_enabled
from crewmeal.search_enhancement.graph_client import GraphClient
from crewmeal.search_enhancement.mip_sdk import (
    MipSdkConfig,
    build_runner,
    probe_rms_health,
)
from crewmeal.search_enhancement.processor import PresentationProcessor
from crewmeal.search_enhancement.analysis_tier import resolve_analysis_tier
from crewmeal.search_enhancement.schema import resolve_database_target
from crewmeal.search_enhancement.sharepoint_control import SharePointControlClient
from crewmeal.search_enhancement.vision_model import resolve_vision_model
from crewmeal.search_enhancement.worker import SearchEnhancementWorker


def _log_mip_preflight(all_settings, mip_config, credential) -> None:
    """Emit one loud startup signal when MIP decryption is enabled but not ready.

    The admin decryption toggle only asks the pipeline to *attempt* decryption;
    it does nothing unless the tenant is configured (RMS active, the service
    principal an admin-consented super user, the adapter wired). The per-document
    path already fails closed, but surfacing a misconfiguration here -- once, at
    startup -- beats discovering it document by document. This never blocks the
    worker: it only logs.
    """

    if not is_decryption_enabled("mip", all_settings):
        return
    if not mip_config.is_configured:
        logging.warning(
            "MIP decryption is enabled but CREWMEAL_MIP_SDK_CLI is not set; "
            "protected documents will fail until the adapter is configured."
        )
        return
    health = probe_rms_health(credential, mip_config.scope)
    if health.decrypt_ready:
        logging.info("MIP decryption preflight OK: %s", health.describe())
    else:
        logging.warning(
            "MIP decryption is enabled but the tenant/service principal is not "
            "ready: %s. Protected documents will fail until this is fixed; run "
            "'python -m crewmeal.search_enhancement.mip_preflight' to diagnose.",
            health.describe(),
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the Crewmeal SharePoint PowerPoint search-enhancement worker."
    )
    parser.add_argument(
        "command",
        choices=("once", "run", "reconcile"),
        help="Process available work once, poll continuously, or reconcile active items.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable informational worker logs.",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    search_config = SearchEnhancementConfig.from_environment()
    app_config = AppConfig.from_environment()
    repository = SearchEnhancementRepository(
        resolve_database_target(search_config.sqlite_path)
    )

    # The database may be temporarily unreachable at startup (e.g. the PoC
    # database's nightly auto-stop). Retry with backoff instead of crashing so
    # the worker self-recovers once connectivity returns.
    def _prepare_storage():
        repository.initialize()
        return create_artifact_store(engine=repository.engine)

    artifact_store = run_with_db_retry(
        _prepare_storage,
        description="Database initialization",
        initial_seconds=search_config.db_retry_initial_seconds,
        max_seconds=search_config.db_retry_max_seconds,
    )

    with GraphClient(search_config) as graph:
        all_settings = repository.get_all_settings()
        vision_model = resolve_vision_model(app_config, all_settings)
        analysis_tier = resolve_analysis_tier(app_config, all_settings)
        # MIP decryption shells out to the MIP SDK CLI and authenticates with the
        # same M365 service principal (which must be an Azure RMS super user).
        # ``build_runner`` returns None when unconfigured, so MIP decryption — if
        # an admin enables it — fails loudly rather than passing files through.
        mip_config = MipSdkConfig.from_environment()
        mip_runner = build_runner(mip_config, graph.credential)
        _log_mip_preflight(all_settings, mip_config, graph.credential)
        worker = SearchEnhancementWorker(
            config=search_config,
            repository=repository,
            control=SharePointControlClient(search_config, graph),
            connector=ConnectorClient(search_config, graph),
            processor=PresentationProcessor(
                app_config,
                vision_model=vision_model,
                analysis_tier=analysis_tier,
                decryption_settings=all_settings,
                mip_runner=mip_runner,
            ),
            artifact_store=artifact_store,
        )
        if args.command == "once":
            result = worker.run_once()
            logging.warning(
                "Processed %d command(s) and %d job(s).",
                result.commands_ingested,
                result.jobs_processed,
            )
        elif args.command == "reconcile":
            changes = worker.reconcile_once()
            logging.warning("Reconciled %d change(s).", changes)
        else:
            worker.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

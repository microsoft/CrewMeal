from __future__ import annotations

import argparse
import logging

from crewmeal.config import AppConfig
from crewmeal.search_enhancement.artifact_store import create_artifact_store
from crewmeal.search_enhancement.config import SearchEnhancementConfig
from crewmeal.search_enhancement.connector_client import ConnectorClient
from crewmeal.search_enhancement.database import SearchEnhancementRepository
from crewmeal.search_enhancement.db_resilience import run_with_db_retry
from crewmeal.search_enhancement.graph_client import GraphClient
from crewmeal.search_enhancement.processor import PresentationProcessor
from crewmeal.search_enhancement.schema import resolve_database_target
from crewmeal.search_enhancement.sharepoint_control import SharePointControlClient
from crewmeal.search_enhancement.vision_model import resolve_vision_model
from crewmeal.search_enhancement.worker import SearchEnhancementWorker


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
        worker = SearchEnhancementWorker(
            config=search_config,
            repository=repository,
            control=SharePointControlClient(search_config, graph),
            connector=ConnectorClient(search_config, graph),
            processor=PresentationProcessor(
                app_config,
                vision_model=vision_model,
                decryption_settings=all_settings,
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

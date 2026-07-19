#!/bin/sh
# Branch the single container image between the web app and the queue worker.
# APP_ROLE selects the process; everything else is configured via environment.
set -eu

role="${APP_ROLE:-web}"

case "$role" in
  web)
    exec uvicorn crewmeal.search_enhancement.web:create_app_from_env \
      --factory --host 0.0.0.0 --port "${PORT:-8000}"
    ;;
  worker)
    exec python -m crewmeal.search_enhancement.cli run --verbose
    ;;
  reconcile)
    exec python -m crewmeal.search_enhancement.cli reconcile --verbose
    ;;
  once)
    exec python -m crewmeal.search_enhancement.cli once --verbose
    ;;
  *)
    echo "Unknown APP_ROLE '$role' (expected: web | worker | reconcile | once)" >&2
    exit 64
    ;;
esac

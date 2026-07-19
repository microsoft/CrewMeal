"""Database resilience helpers.

The worker is a long-lived daemon that depends on PostgreSQL. In the PoC
environment the database is subject to an automated nightly stop, and any
managed database can briefly become unreachable during maintenance or
failover. Without protection the worker crashes on the first
``OperationalError`` and — because Container Apps cannot restart it back into a
healthy state while the database is still down — gets stuck until a human
intervenes.

These helpers let the worker treat "database unreachable / connection lost" as
a *transient* condition: back off and retry rather than crash, so it
self-recovers once connectivity returns.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TypeVar

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

LOGGER = logging.getLogger(__name__)

# SQLAlchemy exceptions that indicate the database is unreachable or the
# connection was dropped (server stopped/restarted, network blip, timeout).
# ``OperationalError`` covers connection refused / timeout / server shutdown and
# ``InterfaceError`` covers a connection lost mid-statement. Both are transient.
TRANSIENT_DB_ERRORS: tuple[type[BaseException], ...] = (
    OperationalError,
    InterfaceError,
)

T = TypeVar("T")


def is_transient_db_error(exc: BaseException) -> bool:
    """Return ``True`` when *exc* represents a lost/unavailable DB connection.

    Non-transient database errors (integrity violations, programming errors,
    etc.) return ``False`` so genuine bugs are not silently retried forever.
    """

    if isinstance(exc, TRANSIENT_DB_ERRORS):
        return True
    # Some drivers surface a disconnect as a generic DBAPIError flagged as
    # ``connection_invalidated`` rather than one of the subclasses above.
    if isinstance(exc, DBAPIError) and getattr(exc, "connection_invalidated", False):
        return True
    return False


def run_with_db_retry(
    operation: Callable[[], T],
    *,
    description: str,
    initial_seconds: float,
    max_seconds: float,
    max_attempts: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Execute *operation*, retrying on transient DB errors with capped
    exponential backoff.

    Retries indefinitely by default (``max_attempts=None``) so the worker
    survives an extended database outage (e.g. a nightly auto-stop) and
    self-recovers once connectivity returns. Non-transient exceptions propagate
    immediately. ``sleep`` is injectable for tests.
    """

    delay = max(float(initial_seconds), 0.1)
    ceiling = max(float(max_seconds), delay)
    attempt = 0
    while True:
        attempt += 1
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001 - re-raised unless transient
            if not is_transient_db_error(exc):
                raise
            if max_attempts is not None and attempt >= max_attempts:
                raise
            LOGGER.warning(
                "%s failed on a transient database error (attempt %d): %s; "
                "retrying in %.0fs",
                description,
                attempt,
                exc,
                delay,
            )
            sleep(delay)
            delay = min(delay * 2, ceiling)

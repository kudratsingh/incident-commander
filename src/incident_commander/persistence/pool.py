"""How big the connection pool is, and how many runs may be in flight at once (ADR 0022).

A run pins one connection for its whole life to hold its lease (ADR 0016) and then asks the same
pool for a second connection every time it checkpoints. Enough runs doing that at once and they all
wait on each other, so above ``Settings.max_concurrent_runs`` a run is refused rather than queued.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine

from incident_commander.config import Settings


def create_pooled_engine(settings: Settings) -> Engine:
    """The agent's database engine, with every pool setting written down instead of defaulted.

    ``pool_pre_ping`` is on because a lease connection sits idle for a whole investigation, long
    enough for the database or a proxy to have closed it without telling us.
    """
    return create_engine(
        str(settings.database_url),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_pre_ping=True,
    )


class RunSlots:
    """A fixed number of permits to start a run: one slot stands for one run's pool connections.

    Taking a slot never waits — a caller that cannot get one is meant to shed the work, not queue.
    It uses ``threading`` locks because runs execute in the web server's background-task threads.
    """

    def __init__(self, ceiling: int) -> None:
        if ceiling < 1:
            raise ValueError(f"RunSlots needs a ceiling of at least 1, got {ceiling}")
        self._ceiling = ceiling
        self._semaphore = threading.BoundedSemaphore(ceiling)

    @property
    def ceiling(self) -> int:
        """The configured maximum number of simultaneous runs."""
        return self._ceiling

    @contextmanager
    def acquire(self) -> Iterator[bool]:
        """Yield True when a slot was free, False when none was; a taken slot is released on exit.

        Shaped like ``incident_lease`` on purpose: a refusal is a value the caller decides what to
        do about, never an exception, because being busy is not an error.
        """
        admitted = self._semaphore.acquire(blocking=False)
        try:
            yield admitted
        finally:
            if admitted:
                self._semaphore.release()

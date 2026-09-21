"""Connection-pool sizing and the run-admission bound (ADR 0022).

The lease (ADR 0016) pins a connection for a whole run that then checkpoints against the same
pool — hold-and-wait. So above ``Settings.max_concurrent_runs``, refuse rather than queue.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine

from incident_commander.config import Settings


def create_pooled_engine(settings: Settings) -> Engine:
    """The agent's engine, with every pool parameter stated rather than defaulted.

    ``pool_pre_ping`` because a lease connection idles for a whole investigation.
    """
    return create_engine(
        str(settings.database_url),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_pre_ping=True,
    )


class RunSlots:
    """Bounded admission for concurrent runs: one slot is one run's worth of pool capacity.

    Acquisition is non-blocking. Thread-safe: runs execute in Starlette's background-task
    threadpool, hence ``threading``.
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
        """Yield True iff a slot was free; release it on the way out.

        Mirrors ``incident_lease``: a refusal is the caller's to handle, never an exception.
        """
        admitted = self._semaphore.acquire(blocking=False)
        try:
            yield admitted
        finally:
            if admitted:
                self._semaphore.release()

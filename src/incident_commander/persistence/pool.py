"""Connection-pool sizing and the run-admission bound (ADR 0022).

These two things live in one module because neither is correct without the
other. The single-flight lease (ADR 0016) pins one pooled connection for the
entire life of a run — up to ``BUDGET_MAX_SECONDS`` — and that same run then
asks the same pool for a second connection every time it checkpoints. That is
hold-and-wait: once enough leases are live to hold every connection, every one
of them blocks on a connection only another lease holder can release, and none
of them will, because releasing it is what they are all waiting to do. A
healthy Postgres, an idle CPU, and a wedged agent.

The fix is not a bigger pool, which only moves the cliff. It is to make the
cliff unreachable: size the pool explicitly, then admit no more runs than the
pool can serve at their peak. ``Settings.max_concurrent_runs`` does the
arithmetic and refuses to boot on a combination that cannot work; ``RunSlots``
enforces it at run time.

Why a semaphore and not a queue: a run holds its slot for up to half an hour,
so queueing behind one means an alert waiting half an hour to be looked at,
which is indistinguishable from dropping it except that nothing says so.
Refusing immediately is the honest failure, and CLAUDE.md invariant 5 makes it
the safe one — humans are paged by the platform whether or not this agent ever
gets to the alert.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine

from incident_commander.config import Settings


def create_pooled_engine(settings: Settings) -> Engine:
    """The agent's engine, with every pool parameter stated rather than defaulted.

    ``create_engine(url)`` alone gives QueuePool(5, overflow 10, timeout 30) —
    numbers nobody chose, which happen to sit right where a modest incident
    burst wedges the lease. Stating them makes the pool a documented capacity
    that ``max_concurrent_runs`` can be derived from.

    ``pool_pre_ping`` is on because a lease connection can sit idle for the
    length of an investigation, which is ample time for a network middlebox or
    a Postgres restart to have quietly killed it; without the ping the run
    discovers that at its next checkpoint, as a crash.
    """
    return create_engine(
        str(settings.database_url),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_pre_ping=True,
    )


class RunSlots:
    """Bounded admission for concurrent investigation runs.

    One slot is one run's worth of pool capacity. Acquisition is
    non-blocking by design — see the module docstring — so a caller finds out
    in microseconds that the agent is full and can take the honest path
    instead of parking a threadpool worker on a half-hour wait.

    Thread-safe: runs execute in Starlette's background-task threadpool, so
    this is a plain ``threading`` semaphore rather than an async one.
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

        Mirrors ``incident_lease``'s contract deliberately: try, yield the
        verdict, and let the caller decide what a refusal means. The two are
        the same shape because they are the same kind of thing — an admission
        decision the caller must be able to lose without raising.
        """
        admitted = self._semaphore.acquire(blocking=False)
        try:
            yield admitted
        finally:
            if admitted:
                self._semaphore.release()

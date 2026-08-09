"""Single-flight lease per incident: one live run, enforced by Postgres.

ADR 0002 named it and ADR 0016 pinned the mechanism: a session-scoped
``pg_try_advisory_lock`` on one connection held for the whole run. The lock is
per-database, so it fences across processes and replicas sharing the agent's
Postgres, and a dying process releases it for free when its TCP session drops
— no TTL, no reaper, no clock-skew story.

The trap this module exists to avoid: under a connection pool, a lock taken on
a connection that is then returned to the pool is silently released. The
context manager therefore pins exactly one checked-out connection for the
lifetime of the ``with`` block. Nothing inside that block may take its own
connection for the lock (checkpoint writes using other pooled connections are
fine — the lock is not connection-visible).

Switch to a lease table with an expiry column (ADR 0016 pre-authorizes it, no
new ADR needed) if a transaction-pooling PgBouncer lands between the app and
Postgres, if runs move to a worker pool, or if operators need to see and expire
leases administratively.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import Engine, text

# ``hashtext`` is 32-bit, so two incident ids can share a lock key and
# serialize against each other. The failure is always toward false
# serialization, never false parallelism (the loser logs and returns), and the
# birthday probability is over concurrently HELD locks only.
_ACQUIRE = text("SELECT pg_try_advisory_lock(hashtext(:incident_id)::bigint)")
_RELEASE = text("SELECT pg_advisory_unlock(hashtext(:incident_id)::bigint)")


@contextmanager
def incident_lease(engine: Engine, incident_id: UUID) -> Iterator[bool]:
    """Yield True iff this process holds the single-flight lease for the incident.

    Non-blocking: a duplicate delivery must find out in milliseconds that
    someone else owns the incident, not park a thread for the length of an
    investigation. On False the caller logs and returns without running.
    """
    parameters = {"incident_id": str(incident_id)}
    with engine.connect() as conn:
        acquired = bool(conn.execute(_ACQUIRE, parameters).scalar_one())
        # Session-scoped locks survive the commit; committing here keeps the
        # connection idle rather than idle-in-transaction for the whole run.
        conn.commit()
        try:
            yield acquired
        finally:
            if acquired:
                conn.execute(_RELEASE, parameters)
                conn.commit()
            # Closing the connection (on exiting the ``with``) is the real
            # guarantee — returning it to the pool does not reliably reset
            # session locks, so the explicit unlock is belt-and-braces.

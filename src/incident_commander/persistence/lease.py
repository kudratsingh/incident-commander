"""Single-flight lease per incident: one live run, enforced by Postgres.

ADR 0002 named it, ADR 0016 pinned it: a session-scoped ``pg_try_advisory_lock``
held on ONE pinned connection for the whole run — a lock on a connection handed
back to the pool is silently released. A lease table with an expiry column is
pre-authorized by ADR 0016 if PgBouncer or a worker pool lands.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import Engine, text

# ``hashtext`` is 32-bit: a shared lock key over-serializes, never over-parallelizes.
_ACQUIRE = text("SELECT pg_try_advisory_lock(hashtext(:incident_id)::bigint)")
_RELEASE = text("SELECT pg_advisory_unlock(hashtext(:incident_id)::bigint)")


@contextmanager
def incident_lease(engine: Engine, incident_id: UUID) -> Iterator[bool]:
    """Yield True iff this process holds the single-flight lease for the incident.

    Non-blocking: on False the caller logs and returns.
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
            # Pool return may not reset session locks; the close is the guarantee.

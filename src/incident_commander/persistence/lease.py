"""One run at a time per incident, enforced by a Postgres lock rather than by convention.

The lock is taken with ``pg_try_advisory_lock`` and belongs to the database session, so it has to be
held on ONE connection kept for the whole run: hand that connection back to the pool and the lock is
released without anyone noticing, and a second worker could start the same incident (ADR 0016).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import Engine, text

# The incident id is hashed into the 64-bit number Postgres wants for a lock key. ``hashtext``
# returns only 32 bits, so two different incidents could collide and share one key: the cost is
# that they take turns instead of running together, never that two runs share an incident.
_ACQUIRE = text("SELECT pg_try_advisory_lock(hashtext(:incident_id)::bigint)")
_RELEASE = text("SELECT pg_advisory_unlock(hashtext(:incident_id)::bigint)")


@contextmanager
def incident_lease(engine: Engine, incident_id: UUID) -> Iterator[bool]:
    """Yield True when this process took the lock for the incident, False when someone else has it.

    It never waits: on False the caller logs that another worker owns the run and returns.
    """
    parameters = {"incident_id": str(incident_id)}
    with engine.connect() as conn:
        acquired = bool(conn.execute(_ACQUIRE, parameters).scalar_one())
        # Commit straight away. The lock belongs to the session, not the transaction, so it survives
        # this commit — and committing means the connection is not left idle inside a transaction
        # for the whole run, which is what makes such a connection expensive to hold.
        conn.commit()
        try:
            yield acquired
        finally:
            # Release explicitly rather than relying on the connection going back to the pool: a
            # pooled connection may be reused without its session locks being cleared. Closing the
            # connection is the backstop, and the explicit unlock is the guarantee.
            if acquired:
                conn.execute(_RELEASE, parameters)
                conn.commit()

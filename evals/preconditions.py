"""Check that a scenario's world is actually broken before grading the agent.

Probes after seeding, before the run: a fault that was never manufactured is
reported as that, not graded as the agent being wrong (paid run `bb1fa70abb4c`
failed for exactly this). ``evals/runner.py`` probes; ``resolve_path`` lives with
the comparators in ``graders/deterministic.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from evals.graders.deterministic import resolve_path as resolve
from evals.graders.deterministic import selected_values
from evals.scenarios.schema import PreconditionField, PreconditionProbe

__all__ = ["resolve", "unmet"]


def _selector_clause(field: PreconditionField) -> str:
    """Name the row the selector was looking for, in a failure detail.

    A missing row usually means the chaos hook did not fire; a row that says
    something else is a different diagnosis.
    """
    if field.where is None:
        return ""
    return f" for a row whose {field.where.field!r} {field.where.describe()}"


def unmet(probe: PreconditionProbe, payload: Mapping[str, Any]) -> list[str]:
    """Ways this probe's observation fails the precondition. Empty means met."""
    failures: list[str] = []
    for field in probe.expect:
        observed = selected_values(payload, field.path, field.where)
        clause = _selector_clause(field)
        if not observed:
            failures.append(
                f"{probe.tool}: nothing at {field.path!r}{clause} (expected {field.describe()})"
            )
            continue
        if not any(field.satisfied_by(value) for value in observed):
            failures.append(
                f"{probe.tool}: {field.path}{clause} expected {field.describe()}, "
                f"observed {observed!r}"
            )
    return failures

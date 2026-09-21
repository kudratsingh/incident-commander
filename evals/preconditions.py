"""Check that a scenario's world is actually broken before grading the agent.

Probed after seeding, before the run: a fault that was never manufactured is reported as
that, not graded as the agent being wrong (paid run `bb1fa70abb4c`). The probes wear the
AGENT's token and are LABELLED as the lab's own (``probe_label``, ADR 0075, platform
ADR 0038), or the platform records them as ``agent.tool_invoked``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from evals.graders.deterministic import resolve_path as resolve
from evals.graders.deterministic import selected_values
from evals.scenarios.schema import PreconditionField, PreconditionProbe
from incident_commander.tools.mcp_client import LAB_PROBE_REASON_MAX_CHARS

__all__ = ["probe_label", "resolve", "unmet"]

#: What every precondition label opens with, so an operator scanning ``lab.probe`` rows can
#: tell the premise reads from the principal guards' and the world audit's.
LABEL_PREFIX = "precondition"


def probe_label(probe: PreconditionProbe) -> str:
    """The lab's own reason for one premise read: what this probe PROVES.

    Derived from ``expect`` rather than a written description, which would be a second
    source of truth. Truncated to the platform's cap, which REFUSES an over-long reason —
    and a refused label is the unlabelled row this function exists to prevent.
    """
    proves = "; ".join(f"{field.path} {field.describe()}" for field in probe.expect)
    label = f"{LABEL_PREFIX}: {probe.tool} proves {proves}"
    if len(label) <= LAB_PROBE_REASON_MAX_CHARS:
        return label
    return f"{label[: LAB_PROBE_REASON_MAX_CHARS - 1]}…"


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

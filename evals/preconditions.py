"""Check that a scenario's world is actually broken before grading the agent.

Probes after seeding, before the run: a fault that was never manufactured is
reported as that, not graded as the agent being wrong (paid run `bb1fa70abb4c`
failed for exactly this). ``evals/runner.py`` probes; ``resolve_path`` lives with
the comparators in ``graders/deterministic.py``.

The probes go out on the AGENT's token, deliberately — the premise has to be true
of the world the agent will actually see — so since ADR 0075 each one is LABELLED
as the lab's own (``probe_label``, platform ADR 0038). Without the label the
platform writes them as ``agent.tool_invoked`` and a console counting the agent's
calls counts reads the agent never made: the owner's fourth take showed "4 steps
reported · 5 calls the platform recorded — the two do not agree", and the fifth
call was this module's.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from evals.graders.deterministic import resolve_path as resolve
from evals.graders.deterministic import selected_values
from evals.scenarios.schema import PreconditionField, PreconditionProbe
from incident_commander.tools.mcp_client import LAB_PROBE_REASON_MAX_CHARS

__all__ = ["probe_label", "resolve", "unmet"]

#: What every precondition label opens with, so an operator scanning the platform's
#: ``lab.probe`` rows can tell the premise reads from the principal guards' and the
#: world audit's at a glance.
LABEL_PREFIX = "precondition"


def probe_label(probe: PreconditionProbe) -> str:
    """The lab's own reason for one premise read: what this probe PROVES.

    Built from the probe's own expectations rather than from a written description,
    for the reason every derived-vs-declared decision in this repo goes the same way:
    a description is a second source of truth that drifts, and ``expect`` is the thing
    the probe actually asserts. Truncated to the platform's cap — it REFUSES an
    over-long reason rather than trimming it, and a refused label is an unlabelled
    row, which is the failure this function exists to prevent.
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

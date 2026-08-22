"""Check that a scenario's world is actually broken before grading the agent.

A live scenario asserts a fault: a stalled consumer, a backlog of poisoned
messages, a runaway DAG. Nothing verified that the fault was there. So when
seeding silently failed — or when the fault was one the chaos framework
cannot manufacture at all — the agent investigated a healthy system, failed
to find the problem it was told about, and was marked down for it.

That is not a hypothetical. `bb1fa70abb4c` is a paid run that graded FAIL
for exactly this reason: the agent reached `resolved` having fixed the wrong
thing, because the thing it was told about could not be made to exist. The
report said the agent was wrong. The agent was not wrong.

A precondition is the missing half of that sentence. It probes the world
after seeding and before the run, and if the world is not in the asserted
state the scenario reports **that** — the fault was never manufactured —
instead of running an agent against a premise that is false and grading it
on the result. The run is abandoned before a single model token is spent,
which is also the cheapest possible failure.

The comparison is pure and lives here; ``evals/runner.py`` supplies the
probing, and the path walker lives with the comparators in
``evals/graders/deterministic.py`` (``resolve_path``), because
``EvidenceFieldExpectation.field`` shares its descent syntax and a second
copy of the rules would drift.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from evals.graders.deterministic import resolve_path as resolve
from evals.scenarios.schema import PreconditionProbe

__all__ = ["resolve", "unmet"]


def unmet(probe: PreconditionProbe, payload: Mapping[str, Any]) -> list[str]:
    """Ways this probe's observation fails the precondition. Empty means met."""
    failures: list[str] = []
    for field in probe.expect:
        observed = resolve(payload, field.path)
        if not observed:
            failures.append(
                f"{probe.tool}: nothing at {field.path!r} (expected {field.describe()})"
            )
            continue
        if not any(field.satisfied_by(value) for value in observed):
            failures.append(
                f"{probe.tool}: {field.path} expected {field.describe()}, observed {observed!r}"
            )
    return failures

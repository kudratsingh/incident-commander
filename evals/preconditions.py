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

The resolution and comparison are pure and live here; ``evals/runner.py``
supplies the probing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from evals.scenarios.schema import PreconditionProbe


def resolve(payload: Mapping[str, Any], path: str) -> list[Any]:
    """Every value observed at ``path``, descending into lists at ``[]``.

    ``total`` reads one top-level field. ``items[].remediation_hint`` reads
    that field from every row. Returning a list rather than one value is what
    lets an assertion mean "some row satisfies this", which is the only
    useful reading for a fixture pack whose row order is not guaranteed.
    """
    values: list[Any] = [payload]
    for segment in path.split("."):
        descend = segment.endswith("[]")
        key = segment[:-2] if descend else segment
        found: list[Any] = [
            value[key] for value in values if isinstance(value, Mapping) and key in value
        ]
        if descend:
            flattened: list[Any] = []
            for value in found:
                if isinstance(value, Sequence) and not isinstance(value, str | bytes):
                    flattened.extend(value)
            found = flattened
        values = found
    return values


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

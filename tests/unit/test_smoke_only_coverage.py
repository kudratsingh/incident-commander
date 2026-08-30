"""The smoke pass may not shrink without someone saying so (WO-R2-41).

``make eval-smoke`` is the cheap read-only gate that runs before anything
expensive, and which scenarios it covers was decided by a hand-maintained
``SMOKE_ONLY`` list in the ``Makefile`` that nothing checked. The list could
lose coverage in two directions, both silent, and one already had:

* **a renamed scenario** left its pattern behind matching nothing. The runner
  refused only when *every* ``--only`` pattern came back empty, so one dead
  pattern among nineteen live ones changed nothing visible — the run just
  graded fewer scenarios and still printed green;
* **a newly added read-only scenario** that nobody remembered to add simply
  never ran. ``consumer_lag_null_unknown_state`` dropped out exactly this way:
  read-only, chaos-free, live-declaring, and absent from the list with no
  recorded reason, while ``docs/eval-methodology.md`` went on asserting the
  list was the source of truth and that only two DLQ scenarios were held back.

These tests replace "the list is correct because someone maintained it" with
"the list is checked against the scenario directory". The expected set is
*derived* from the YAMLs using the runner's own eligibility predicate rather
than a second opinion about what read-only means — see ``_is_smoke_eligible``.

The guard is deliberately stronger than "live-declaring scenarios only". The
smoke stage documents itself as deliberately mixing canned harness-sanity rows
(``noise_*``, ``tool_*``, ``planner_stops_immediately``) with live reads, so
declaring a live leg is not the line that decides membership — seeding no chaos
and writing nothing is. Every chaos-free, action-free scenario in the tree is
therefore required to be either covered or recorded, which is the invariant
that holds today with exactly one recorded exclusion.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

from evals.runner import _SCENARIOS_DIR, main
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_MAKEFILE: Final[Path] = _REPO_ROOT / "Makefile"

# `NAME ?= a,b,c` — the committed default. The operator override (`make
# eval-smoke SMOKE_ONLY=...`, or a line in .env) is deliberately NOT what is
# pinned here: this test is about the list the repo ships and CI reads.
_ASSIGNMENT: Final[str] = r"^{name}\s*\?=\s*(.+)$"


def _makefile_list(name: str) -> list[str]:
    """The comma-separated value of a `?=` assignment in the Makefile."""
    match = re.search(
        _ASSIGNMENT.format(name=re.escape(name)),
        _MAKEFILE.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert match is not None, (
        f"{_MAKEFILE.name} no longer defines `{name} ?= ...`. If the smoke "
        f"selection moved, move this guard with it — deleting the list without "
        f"replacing the derivation is how the smoke pass silently shrank before."
    )
    return [part.strip() for part in match.group(1).split(",") if part.strip()]


def _is_smoke_eligible(scenario: Scenario) -> bool:
    """The runner's own smoke predicate, not a re-implementation of it.

    Both halves are refusals the runner already enforces at selection time,
    which is why they define eligibility rather than merely correlating with
    it:

    * ``chaos_setup`` — ``--smoke`` refuses the entire run with exit 6 if any
      selected scenario declares one, because chaos seeding fires under the
      full write+chaos ``PLATFORM_TOKEN`` and that is precisely the claim the
      read-only stage exists to disprove (S-03).
    * ``expected_action_tools`` — a graded Tier-1 write. The read-scoped smoke
      token 403s it by design, so such a scenario is guaranteed red here and
      belongs to the remediation stage under the full token.

    Anything left over is a scenario the smoke stage can run and grade
    honestly, and therefore one the list has to account for.
    """
    return scenario.chaos_setup is None and not scenario.expectation.expected_action_tools


@pytest.fixture(scope="module")
def scenarios() -> list[Scenario]:
    return list(load_scenarios(_SCENARIOS_DIR))


def test_derivation_canary(scenarios: list[Scenario]) -> None:
    """Guard against a vacuous pass if the tree or the parse goes empty.

    Every assertion below is a set comparison, and set comparisons against an
    empty set pass loudly for the wrong reason. If the loader stops finding
    scenarios or the Makefile regex stops matching, that must fail here as a
    broken guard rather than downstream as a satisfied one.
    """
    assert len(scenarios) > 20, f"only {len(scenarios)} scenarios loaded from {_SCENARIOS_DIR}"
    assert len(_makefile_list("SMOKE_ONLY")) > 10, "SMOKE_ONLY parsed as a near-empty list"
    assert [s for s in scenarios if _is_smoke_eligible(s)], "no scenario is smoke-eligible"


def test_every_smoke_only_pattern_matches_a_scenario(scenarios: list[Scenario]) -> None:
    """A pattern matching nothing is a rename nobody propagated.

    This is the half the runner could not see: it refused only on a wholly
    empty selection, so a single stale pattern silently removed its scenario
    from the pass. Substring patterns (`noise_`, `tool_`, `multi_probe`)
    deliberately cover several scenarios each — the requirement is one or
    more, never exactly one.
    """
    dead = [
        pattern
        for pattern in _makefile_list("SMOKE_ONLY")
        if not any(pattern in s.name for s in scenarios)
    ]
    assert dead == [], (
        f"SMOKE_ONLY pattern(s) {dead} match no scenario in {_SCENARIOS_DIR}. A "
        f"renamed or deleted scenario left its pattern behind, so the smoke pass "
        f"now covers less than the list claims. Fix the pattern or restore the name."
    )


def test_every_eligible_scenario_is_covered_or_recorded(scenarios: list[Scenario]) -> None:
    """The other half: a new read-only scenario cannot just never run.

    Either ``SMOKE_ONLY`` matches it or ``SMOKE_EXCLUDE`` records that it is
    held back on purpose. Both are decisions; silence is not.
    """
    patterns = _makefile_list("SMOKE_ONLY")
    excluded = set(_makefile_list("SMOKE_EXCLUDE"))
    missing = sorted(
        s.name
        for s in scenarios
        if _is_smoke_eligible(s)
        and s.name not in excluded
        and not any(pattern in s.name for pattern in patterns)
    )
    assert missing == [], (
        f"scenario(s) {missing} seed no chaos and expect no action tools — the "
        f"smoke stage can run them — but no SMOKE_ONLY pattern matches them and "
        f"SMOKE_EXCLUDE does not hold them back. Add a pattern to SMOKE_ONLY, or "
        f"add the name to SMOKE_EXCLUDE with its reason in the comment above it "
        f"and in docs/eval-methodology.md. A scenario that is in neither list is "
        f"not 'excluded' — it is forgotten, which is how "
        f"consumer_lag_null_unknown_state stopped running."
    )


def test_smoke_exclude_entries_are_real_and_still_needed(scenarios: list[Scenario]) -> None:
    """SMOKE_EXCLUDE must not accumulate names that no longer mean anything.

    An exclusion is a claim that a scenario the stage *could* run is being
    held back. Two ways that claim rots: the scenario is renamed away (the
    entry now excludes nothing, and the real scenario falls back through the
    coverage test), or it gains a ``chaos_setup``/action expectation and is
    excluded by the predicate anyway, leaving a hand-written entry that
    implies a decision nobody still needs to make.
    """
    by_name = {s.name: s for s in scenarios}
    for name in _makefile_list("SMOKE_EXCLUDE"):
        scenario = by_name.get(name)
        assert scenario is not None, (
            f"SMOKE_EXCLUDE names {name!r}, which is not a scenario in "
            f"{_SCENARIOS_DIR}. Exclusions are per-scenario and exact — no "
            f"substring matching — so a renamed scenario leaves a dead entry here."
        )
        assert _is_smoke_eligible(scenario), (
            f"SMOKE_EXCLUDE names {name!r}, but that scenario declares chaos_setup "
            f"or expected_action_tools, so the runner already refuses it from a "
            f"smoke selection. The hand-written exclusion is redundant — drop it "
            f"and let the predicate speak."
        )


def test_the_previously_dropped_scenario_is_back_in_the_pass(scenarios: list[Scenario]) -> None:
    """Named regression pin for the scenario the audit caught missing.

    ``consumer_lag_null_unknown_state`` is the tripwire for reading a null lag
    as healthy, and ``docs/eval-debt.md`` records a live observable for it that
    only a live smoke run can produce. While it sat outside ``SMOKE_ONLY`` that
    observable was never exercised by the documented protocol. The general
    coverage test above would catch a re-drop, but this one names it, so the
    failure says what was lost rather than only that something was.
    """
    assert "consumer_lag_null_unknown_state" in {s.name for s in scenarios}
    patterns = _makefile_list("SMOKE_ONLY")
    assert any(pattern in "consumer_lag_null_unknown_state" for pattern in patterns), (
        "consumer_lag_null_unknown_state is read-only, chaos-free and "
        "live-declaring, and its documented live observable (docs/eval-debt.md) "
        "is only produced by a live smoke run. It must stay in SMOKE_ONLY."
    )


def test_runner_refuses_a_single_dead_only_pattern(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The runner-side half, at the boundary that was actually broken.

    The mix matters: one live pattern and one dead one. That selection used to
    run happily — the union was non-empty — which is exactly how a dead
    SMOKE_ONLY entry stayed invisible. It must now exit 2 and name the dead
    pattern, before any scenario runs and before any spend.
    """
    monkeypatch.setattr(
        "sys.argv",
        ["runner", "--only", "consumer_lag_healthy,consumer_lag_renamed_away"],
    )
    assert main() == 2
    out = capsys.readouterr().out
    assert "SELECTION FAIL" in out
    assert "consumer_lag_renamed_away" in out
    assert "nothing was spent" in out
    # The per-pattern counts are the evidence a reader needs to see coverage
    # at a glance; the live pattern must still be reported as matching.
    assert "--only consumer_lag_healthy → 1 scenario(s)" in out

"""The two guards that stand between an operator and an unintended paid run.

Both were found by a read-only sweep of the path to a live run, and both are
the same shape of defect: the guard the operator docs described was not the
guard the code implemented.

**A missing ``ONLY=`` was refused only by accident.** ``make eval-live`` passed
``$(if $(ONLY),--only $(ONLY))``, so a bare invocation handed the runner a bare
``--live`` — the whole suite, one shared platform, real spend. What actually
stopped it was the exit-8 canned-only gate, which fires because six scenarios
in the tree declare no live leg. That is a fact about ``evals/scenarios/``, not
about the invocation: give those six a live leg and the same command starts
spending, with nothing in the Makefile or the runner changed. It also refused
for the wrong reason, in a message about canned-only scenarios rather than
about the selection the operator never made. Both layers now refuse the
missing filter itself, structurally, with exit 2.

**``--only`` was an unanchored substring.** ``ONLY=dlq_backlog`` selected
``dlq_backlog`` *and* ``remediate_dlq_backlog_success``. The read-only one runs
first and drains the seeded ``replay_safe`` pool that the remediation is graded
on, so a correct agent reds and the report blames the agent. The ADR 0020 gate
cannot catch this: only one of the two mutates, so ``len(mutating) > 1`` is
False. That is the 2026-08-30 incident — a read-only stage that smuggled in a
mutating scenario. Under ``--live`` a pattern is now matched by full name.

This file lints the Makefile and the operator docs; the runner-side behaviour
of both guards is pinned in ``test_runner.py``, next to the other ``main()``
exit-code tests and their env isolation. The Makefile assertions are on its
TEXT, never by running it: ADR 0011 freezes the eval suite out of pytest, and
``make eval-live`` is precisely the command that must never be reachable from
a test.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

from evals.runner import _SCENARIOS_DIR
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_MAKEFILE: Final[Path] = _REPO_ROOT / "Makefile"
_RUNBOOK: Final[Path] = _REPO_ROOT / "docs" / "runbook.md"
_METHODOLOGY: Final[Path] = _REPO_ROOT / "docs" / "eval-methodology.md"


@pytest.fixture(scope="module")
def scenarios() -> list[Scenario]:
    return list(load_scenarios(_SCENARIOS_DIR))


# --- defect 1: `make eval-live` must refuse a bare invocation ---------------


def test_eval_live_refuses_a_missing_only_at_parse_time() -> None:
    """Pinned on the Makefile text, in the shape the sibling guards use.

    ``eval-reg`` and ``baseline`` wrap an ``ifdef ONLY`` around a
    prerequisite-free ``$(error)`` rule so the refusal lands before anything
    runs. ``eval-live`` needs the same thing pointing the other way: without a
    filter there is nothing to run but the whole suite. A recipe-line ``echo;
    exit 2`` would also work here — there is no prerequisite to beat — but the
    ``$(error)`` form keeps all three ONLY guards one shape, and make exits 2
    for a fatal error either way.

    The freeze-safe manual probe: ``make -n eval-live`` dies with exit 2 and no
    recipe output; ``make -n eval-live ONLY=dlq_backlog`` prints the recipe.
    ``ONLY=`` set but empty takes the refusing branch too — ``ifndef`` tests for
    a non-empty value — which matters because ``-include .env`` can define it.
    """
    makefile = _MAKEFILE.read_text(encoding="utf-8")
    assert "ifndef ONLY\neval-live:\n\t$(error " in makefile, (
        "the eval-live target must be wrapped in a parse-time `ifndef ONLY` guard "
        "whose no-ONLY branch is a prerequisite-free $(error) rule. Without it a "
        "bare `make eval-live` hands the runner the whole suite for a live run, "
        "and the only thing refusing it is the exit-8 canned-only gate — which is "
        "a property of the scenario tree, not of this command."
    )
    assert "else\neval-live:\n\t@EVAL_TRACE_DIR=" in makefile, (
        "the real eval-live recipe must live in the else-branch of the ONLY guard"
    )


def test_eval_live_recipe_no_longer_makes_only_optional() -> None:
    """The ``$(if $(ONLY),...)`` that made the filter optional must be gone.

    Inside the else-branch ONLY is non-empty by construction, so the
    conditional could only ever expand one way — leaving it would keep telling
    every reader of the recipe that the flag is optional, which is how it read
    for the whole life of the defect.
    """
    recipe = _MAKEFILE.read_text(encoding="utf-8").split("\nelse\neval-live:", 1)[1]
    recipe = recipe.split("\nendif", 1)[0]
    assert "--only $(ONLY)" in recipe, "the recipe must forward ONLY to the runner"
    assert "$(if $(ONLY)" not in recipe, (
        "the eval-live recipe still treats ONLY as optional via `$(if $(ONLY),...)`; "
        "inside the ifndef guard's else-branch ONLY is always set"
    )


def test_the_makefile_guard_has_a_subject() -> None:
    """Canary: ``eval-live`` is still a target here.

    Renaming it would turn both text assertions above into checks on a string
    that no longer exists — red, but for a reason that invites fixing the test
    rather than the guard.
    """
    assert re.search(r"^eval-live:", _MAKEFILE.read_text(encoding="utf-8"), re.MULTILINE), (
        "no `eval-live:` rule in the Makefile — the guard tests above have no subject"
    )


# --- defect 2: the widening pair the exact-name rule exists for -------------


def test_the_widening_pair_still_exists(scenarios: list[Scenario]) -> None:
    """Canary for the exact-name rule, and the red-before case in one.

    Under the old matcher ``--only dlq_backlog`` selected both of these, and
    the ADR 0020 gate stayed quiet because only the second mutates. If the
    tree ever stops containing a name that is a strict prefix of another, the
    runner-side tests stop exercising the widening and should be re-pointed
    rather than deleted — the next such pair will be added by someone who
    never heard of this.
    """
    names = {s.name for s in scenarios}
    assert {"dlq_backlog", "remediate_dlq_backlog_success"} <= names
    substring_hits = sorted(s.name for s in scenarios if "dlq_backlog" in s.name)
    assert substring_hits == ["dlq_backlog", "remediate_dlq_backlog_success"], (
        f"expected the widening pair, got {substring_hits}"
    )
    mutating = [
        s
        for s in scenarios
        if s.name in substring_hits and (s.expectation.expected_action_tools or s.chaos_setup)
    ]
    assert len(mutating) == 1, (
        "the pair must contain exactly one mutating scenario — that is precisely why "
        "the ADR 0020 gate (len(mutating) > 1) could not catch this widening"
    )


# --- the docs describe what the code does ----------------------------------


def test_the_runbook_states_the_structural_refusal() -> None:
    """The runbook told operators the refusal was exit 8 / exit 7.

    True only by accident of the scenario tree, and it taught the wrong model:
    a reader would expect a bare `make eval-live` to keep refusing after every
    scenario gained a live leg, which is exactly when it would stop.
    """
    runbook = _RUNBOOK.read_text(encoding="utf-8")
    assert "`ONLY=` is not optional" in runbook, (
        "docs/runbook.md must keep stating that ONLY= is required for a live run"
    )
    assert "refused at Makefile parse time with **exit 2**" in runbook, (
        "docs/runbook.md must state that a missing ONLY= is refused with exit 2 by "
        "the Makefile and by the runner, not merely deflected by the exit-8 "
        "canned-only gate — which is a fact about evals/scenarios/, not about the "
        "command, and disappears once every scenario has a live leg"
    )


def test_the_runbook_states_the_exact_name_rule() -> None:
    runbook = _RUNBOOK.read_text(encoding="utf-8")
    assert "full scenario name" in runbook, (
        "docs/runbook.md must say a live ONLY= names scenarios in full — the "
        "substring rule it implied is what widened ONLY=dlq_backlog into two"
    )


def test_the_methodology_states_the_exact_name_rule() -> None:
    methodology = _METHODOLOGY.read_text(encoding="utf-8")
    assert "full scenario name" in methodology, (
        "docs/eval-methodology.md documents --only; it must say that under --live a "
        "pattern is matched by full scenario name, and that --smoke and offline runs "
        "keep substring matching (SMOKE_ONLY depends on it)"
    )

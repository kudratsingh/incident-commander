"""The two guards that stand between an operator and an unintended paid run.

Both were found by a read-only sweep of the path to a live run, and both are
the same shape of defect: the guard the operator docs described was not the
guard the code implemented.

**A missing ``ONLY=`` was refused only by accident.** ``make eval-live`` passed
``$(if $(ONLY),--only $(ONLY))``, so a bare invocation handed the runner a bare
``--live`` — the whole suite, one shared platform, real spend. What actually
stopped it was the exit-8 canned-only gate, which fires because some scenarios
in the tree declare no live leg. That is a fact about ``evals/scenarios/``, not
about the invocation: give every one of them a live leg and the same command
starts spending, with nothing in the Makefile or the runner changed. It also
refused for the wrong reason, in a message about canned-only scenarios rather than
about the selection the operator never made. Both layers now refuse the
missing filter itself, structurally, with exit 2.

**``--only`` was an unanchored substring.** ``ONLY=dlq_backlog`` selected
``dlq_backlog`` *and* ``remediate_dlq_backlog_success``. The read-only one runs
first and drains the seeded ``replay_safe`` pool that the remediation is graded
on, so a correct agent reds and the report blames the agent. The ADR 0020 gate
cannot catch this: only one of the two mutates, so ``len(mutating) > 1`` is
False. That is the 2026-08-30 incident — a read-only stage that smuggled in a
mutating scenario. Under ``--live`` a pattern is now matched by full name.

**The canned-only count was prose in four files and none of them asserted it**
(WO-R3-243). "six" was true until cmd #225 added a seventh canned-only
scenario. It is now stated once, in ``docs/runbook.md``, checked here against
``evals/benchmark_inventory.json``, and kept out of the three files it drifted
in — none of which needed it, since their argument is that the refusal is a
property of the tree rather than of the invocation.

This file lints the Makefile and the operator docs; the runner-side behaviour
of both guards is pinned in ``test_runner.py``, next to the other ``main()``
exit-code tests and their env isolation. The Makefile assertions are on its
TEXT, never by running it: ADR 0011 freezes the eval suite out of pytest, and
``make eval-live`` is precisely the command that must never be reachable from
a test.
"""

from __future__ import annotations

import json
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
_INVENTORY: Final[Path] = _REPO_ROOT / "evals" / "benchmark_inventory.json"


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


# --- the canned-only count is stated once, and it is the corpus's -----------


def _canned_only_in_the_inventory() -> list[str]:
    """Canned-only names per the committed manifest, by its own definition.

    ``Scenario.canned_only`` is ``not (use_live_mcp or use_live_llm)``, and the
    inventory carries both flags per row, so the manifest answers the question
    without re-deriving it here. The manifest is kept equal to the corpus by
    ``tests/unit/test_benchmark_inventory.py::
    test_committed_inventory_equals_fresh_generation``, which is why reading the
    JSON is as good as loading 41 YAMLs and much cheaper.
    """
    rows = json.loads(_INVENTORY.read_text(encoding="utf-8"))
    return sorted(row["name"] for row in rows if not (row["use_live_mcp"] or row["use_live_llm"]))


def test_the_runbook_states_the_canned_only_count_and_the_corpus_agrees() -> None:
    """The count was prose in four files and none of them asserted it.

    ``six`` was right when it was written and wrong the moment cmd #225 added a
    seventh canned-only scenario — in the runbook, in ``evals/runner.py``, in
    ``test_runner.py`` and here — because a number four files state and nobody
    checks is a number that drifts silently. The other three now make their
    argument without a count (none of them needed one: the point is that the
    refusal is a property of the tree, not of the invocation). The runbook
    keeps it, because an operator reading about the exit-8 gate wants to know
    how many scenarios it covers, and this test is what makes keeping it safe.

    Fix the runbook's digit when a scenario gains or loses a live leg; do not
    reintroduce the number anywhere else.
    """
    runbook = _RUNBOOK.read_text(encoding="utf-8")
    stated = re.findall(r"(\d+) scenarios declare\s+no live leg", runbook)
    assert len(stated) == 1, (
        "docs/runbook.md must state the canned-only scenario count exactly once, as "
        f"a digit, in the form '<n> scenarios declare no live leg' — found {stated}. "
        "One statement is the whole point of WO-R3-243: four copies drifted."
    )
    canned_only = _canned_only_in_the_inventory()
    assert int(stated[0]) == len(canned_only), (
        f"docs/runbook.md says {stated[0]} scenarios declare no live leg, but "
        f"evals/benchmark_inventory.json has {len(canned_only)}: {canned_only}. "
        "The corpus is right and the prose is stale — update the runbook."
    )


@pytest.mark.parametrize(
    "path",
    [
        _REPO_ROOT / "evals" / "runner.py",
        _REPO_ROOT / "tests" / "unit" / "test_runner.py",
        Path(__file__),
    ],
)
def test_the_count_is_not_restated_in_the_files_it_drifted_in(path: Path) -> None:
    """Each of these once carried its own copy of the number. None may again.

    Cheap and blunt on purpose: a spelled-out or numeric count immediately
    before "scenarios" in the same breath as a live leg is the exact shape that
    drifted. Anything subtler than this is a copy that would have to be found
    by hand, which is how it went wrong the first time.

    Matched against the text with line breaks and comment markers flattened
    away, because every one of the four originals was wrapped mid-sentence —
    three of them across a ``#`` — and a regex that only sees one physical line
    finds none of them.
    """
    flat = re.sub(r"\s+#?\s*", " ", path.read_text(encoding="utf-8"))
    counts = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)"
    offenders = re.findall(rf"{counts} scenarios?\b[^.]{{0,80}}?no live leg", flat, re.IGNORECASE)
    assert not offenders, (
        f"{path.name} restates the canned-only count: {offenders}. It is stated once, "
        "in docs/runbook.md, where this file's sibling test asserts it against "
        "evals/benchmark_inventory.json. Make the argument without the number."
    )

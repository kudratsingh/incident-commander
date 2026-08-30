"""The smoke pass may not shrink without someone saying so (WO-R2-41/#151),
now enforced structurally rather than by checking a hand list (WO-R2-123).

``make eval-smoke`` is the cheap read-only gate that runs before anything
expensive. Which scenarios it covers used to be a comma-separated
``SMOKE_ONLY`` pattern list in the ``Makefile``, with ``SMOKE_EXCLUDE``
beside it for deliberate hold-backs. That list could lose coverage in two
directions, both silent, and one already had:

* **a renamed scenario** left its pattern behind matching nothing. The runner
  refused only when *every* ``--only`` pattern came back empty, so one dead
  pattern among nineteen live ones changed nothing visible — the run just
  graded fewer scenarios and still printed green;
* **a newly added read-only scenario** that nobody remembered to add simply
  never ran. ``consumer_lag_null_unknown_state`` dropped out exactly this way.

#151 made both cases *fail a test*. That is strictly weaker than what is here
now, and the difference is the point of this rewrite: a test that compares a
hand list against the tree still lets the hand list be wrong, catches it only
on the next CI run, and must itself be kept in step with the list. The
membership rule now lives on ``Scenario`` — ``in_smoke_pass``, derived from
the runner's own two selection refusals minus an optional per-scenario
``smoke_exclusion`` reason — and the runner applies it to a bare ``--smoke``.
A rename carries the field with it. A new eligible scenario is in the pass the
moment it lands. An exclusion cannot name a scenario that does not exist,
because it *is* the scenario.

So the two #151 cases are re-expressed here as what they always meant:
:func:`test_a_renamed_scenario_stays_in_the_pass` and
:func:`test_a_new_eligible_scenario_joins_the_pass_with_no_edit` build a
scenario tree, mutate it the way the audit was worried about, and assert the
derived selection still holds. Under the hand list both were red — the
renamed scenario dropped out and the new one never joined. The runner-side
half of #151 (a single dead ``--only`` pattern must still refuse the whole
run) is unchanged and still pinned at the bottom, because ``SMOKE_ONLY``
survives as the operator override and reaches the runner through ``--only``.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Final

import pytest
import yaml

from evals.runner import _SCENARIOS_DIR, main
from evals.scenarios.loader import ScenarioLoadError, load_scenarios
from evals.scenarios.schema import Scenario

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_MAKEFILE: Final[Path] = _REPO_ROOT / "Makefile"


@pytest.fixture(scope="module")
def scenarios() -> list[Scenario]:
    return list(load_scenarios(_SCENARIOS_DIR))


def _tree_copy(destination: Path) -> Path:
    """A working copy of the shipped scenario directory, YAMLs only."""
    destination.mkdir(parents=True, exist_ok=True)
    for source in _SCENARIOS_DIR.glob("*.yaml"):
        shutil.copy(source, destination / source.name)
    return destination


def _derived(tree: Path) -> set[str]:
    """The smoke pass over ``tree``, by the same predicate the runner uses."""
    return {s.name for s in load_scenarios(tree) if s.in_smoke_pass}


def test_derivation_canary(scenarios: list[Scenario]) -> None:
    """Guard against a vacuous pass if the tree or the parse goes empty.

    Every assertion below is a set comparison, and set comparisons against an
    empty set pass loudly for the wrong reason. If the loader stops finding
    scenarios, that must fail here as a broken guard rather than downstream as
    a satisfied one.
    """
    assert len(scenarios) > 20, f"only {len(scenarios)} scenarios loaded from {_SCENARIOS_DIR}"
    assert len([s for s in scenarios if s.smoke_eligible]) > 10, "almost nothing is smoke-eligible"
    assert [s for s in scenarios if s.in_smoke_pass], "no scenario is in the smoke pass"


# --- #151's two cases, against the mechanism that replaced its hand list ----


def test_a_renamed_scenario_stays_in_the_pass(tmp_path: Path) -> None:
    """#151 case 1. Under ``SMOKE_ONLY`` this was red.

    Renaming ``redis_saturation`` left the pattern ``redis_saturation`` in the
    Makefile matching nothing, so the scenario silently left the pass while
    eighteen other live patterns kept the run green. The membership rule now
    travels with the scenario, so the rename cannot separate them.
    """
    tree = _tree_copy(tmp_path / "renamed")
    original = tree / "redis_saturation.yaml"
    document = yaml.safe_load(original.read_text(encoding="utf-8"))
    assert document["name"] == "redis_saturation", "fixture drift: the scenario was renamed already"
    document["name"] = "redis_memory_pressure"
    original.unlink()
    (tree / "redis_memory_pressure.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")

    derived = _derived(tree)
    assert "redis_memory_pressure" in derived, (
        "a renamed read-only scenario dropped out of the smoke pass. Membership "
        "is supposed to be derived from the scenario itself, so a rename cannot "
        "lose it — this is the WO-R2-41 defect returning in a new mechanism."
    )
    assert "redis_saturation" not in derived


def test_a_new_eligible_scenario_joins_the_pass_with_no_edit(tmp_path: Path) -> None:
    """#151 case 2. Under ``SMOKE_ONLY`` this was red.

    A read-only, chaos-free scenario nobody added to the list never ran, and
    nothing said so — ``consumer_lag_null_unknown_state`` is the one that
    actually happened. Landing the scenario is now the whole of the work.
    """
    tree = _tree_copy(tmp_path / "added")
    template = yaml.safe_load((tree / "redis_saturation.yaml").read_text(encoding="utf-8"))
    template["name"] = "brand_new_read_only_scenario"
    (tree / "brand_new_read_only_scenario.yaml").write_text(
        yaml.safe_dump(template), encoding="utf-8"
    )

    assert "brand_new_read_only_scenario" in _derived(tree), (
        "a newly added chaos-free, action-free scenario is not in the smoke "
        "pass. Nothing should have had to be edited for it to be — that edit "
        "being forgettable is exactly how consumer_lag_null_unknown_state "
        "stopped running."
    )


def test_the_previously_dropped_scenario_is_in_the_pass(scenarios: list[Scenario]) -> None:
    """Named regression pin for the scenario the audit caught missing.

    ``consumer_lag_null_unknown_state`` is the tripwire for reading a null lag
    as healthy, and ``docs/eval-debt.md`` records a live observable for it that
    only a live smoke run can produce. While it sat outside ``SMOKE_ONLY`` that
    observable was never exercised by the documented protocol. The derivation
    would now have to be broken for it to drop out, but this names it, so the
    failure says what was lost rather than only that something was.
    """
    by_name = {s.name: s for s in scenarios}
    scenario = by_name.get("consumer_lag_null_unknown_state")
    assert scenario is not None, "consumer_lag_null_unknown_state is gone from the scenario tree"
    assert scenario.in_smoke_pass, (
        "consumer_lag_null_unknown_state is read-only, chaos-free and "
        "live-declaring, and its documented live observable (docs/eval-debt.md) "
        "is only produced by a live smoke run. It must stay in the smoke pass."
    )


# --- the exclusion is a recorded decision, checked at load time -------------


def test_every_exclusion_is_eligible_and_gives_a_reason(scenarios: list[Scenario]) -> None:
    """A hold-back only means something for a scenario the stage could run.

    ``dlq_human_required_escalates`` is the worked example of the other case:
    it expects RESOLVED via ``mark_dlq_permanent``, so the predicate excludes
    it for free and a hand-written entry would imply a decision nobody still
    has to make.
    """
    for scenario in scenarios:
        if scenario.smoke_exclusion is None:
            continue
        assert scenario.smoke_eligible, f"{scenario.name} excludes itself redundantly"
        assert scenario.smoke_exclusion.strip(), f"{scenario.name} excludes itself with no reason"


def test_a_redundant_exclusion_is_refused_at_load(tmp_path: Path) -> None:
    """The former ``test_smoke_exclude_entries_are_real_and_still_needed``.

    It was a test over a Makefile list; it is a model validator now, so the
    redundant entry cannot reach a commit rather than being reported after it
    does. A scenario that declares ``expected_action_tools`` is already
    refused from a smoke selection by the predicate.
    """
    tree = _tree_copy(tmp_path / "redundant")
    path = tree / "dlq_human_required_escalates.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert document["expectation"]["expected_action_tools"], "fixture drift: no action expected"
    document["smoke_exclusion"] = "a reason long enough to satisfy the minimum length rule"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    with pytest.raises(ScenarioLoadError, match="redundant"):
        load_scenarios(tree)


def test_an_exclusion_without_a_real_reason_is_refused_at_load(tmp_path: Path) -> None:
    """ "Recorded" has to mean recorded: a one-word hold-back records nothing."""
    tree = _tree_copy(tmp_path / "reasonless")
    path = tree / "redis_saturation.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["smoke_exclusion"] = "skip"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    with pytest.raises(ScenarioLoadError, match="at least 20 characters"):
        load_scenarios(tree)


def test_the_one_shipped_exclusion_is_the_one_we_expect(scenarios: list[Scenario]) -> None:
    """The tree ships exactly one hold-back, and it is the recorded one.

    Not a style rule: every name here is a scenario the cheap gate is NOT
    covering, so the set growing is a coverage decision that should show up in
    a diff and be argued for, not arrive quietly.
    """
    excluded = sorted(s.name for s in scenarios if s.smoke_exclusion is not None)
    assert excluded == ["dlq_backlog"], (
        f"the smoke pass now holds back {excluded}. Each name is a scenario the "
        "pre-spend gate does not cover; add it here with the reason once the "
        "hold-back is agreed, or delete the scenario's smoke_exclusion field."
    )


# --- the Makefile no longer decides, and must not start again ---------------


def test_the_makefile_ships_no_smoke_scenario_list() -> None:
    """The hand lists are gone, and a re-added one would silently win.

    ``eval-smoke`` passes ``--only`` only when ``SMOKE_ONLY`` is set, so a
    committed ``SMOKE_ONLY ?= ...`` default would override the derivation for
    every run without changing a single scenario file — the old mechanism
    back, and invisible from the tree.
    """
    text = _MAKEFILE.read_text(encoding="utf-8")
    committed = [
        name
        for name in ("SMOKE_ONLY", "SMOKE_EXCLUDE")
        if re.search(rf"^{name}\s*[?:]?=", text, re.MULTILINE)
    ]
    assert committed == [], (
        f"Makefile commits a default for {committed}. The smoke pass derives its "
        "selection from evals/scenarios/*.yaml (Scenario.in_smoke_pass); a "
        "committed default here takes it back over. SMOKE_ONLY is an operator "
        "override, set per-invocation or in .env, never shipped."
    )


def test_eval_smoke_only_passes_only_when_the_operator_sets_it() -> None:
    """The override survives, conditionally — that is what keeps it an override."""
    recipe = _MAKEFILE.read_text(encoding="utf-8")
    assert '$(if $(SMOKE_ONLY),--only "$(SMOKE_ONLY)")' in recipe, (
        "eval-smoke must pass --only only when SMOKE_ONLY is set. Unconditional "
        '`--only "$(SMOKE_ONLY)"` sends an empty pattern on a default run; '
        "dropping it entirely removes the documented operator override."
    )


def test_runner_refuses_a_single_dead_only_pattern(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The runner-side half of #151, at the boundary that was actually broken.

    Unchanged by WO-R2-123 and still required: ``SMOKE_ONLY`` remains the
    operator override and arrives here as ``--only``, so a typo'd or stale
    override must still refuse rather than run the remainder. The mix matters:
    one live pattern and one dead one. That selection used to run happily — the
    union was non-empty — which is exactly how a dead entry stayed invisible.
    It must exit 2 and name the dead pattern, before any scenario runs and
    before any spend.
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

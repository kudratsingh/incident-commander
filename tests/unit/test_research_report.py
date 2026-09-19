"""The aggregate research report groups, refuses, counts its pairs, and resolves.

WO-R3-194's clauses: grouping by all seven WP-2.5 keys; a two-model scope REFUSED with
both ids named (the red-before — one leaderboard over two models); every difference
carrying its paired-trial count and saying so below plan 03 § 10's floor; resolution
through ``artifacts.newest``; and byte-for-byte regeneration of the committed document.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest

from evals import artifacts, regression
from evals import research_report as research
from evals.graders.deterministic import DimensionResult, GradeDimension, GradeReport
from evals.runner import ExecutionMode, RunProvenance, RunReport, ScenarioOutcome
from incident_commander.agent.state import BudgetLedger, IncidentState
from incident_commander.config import ModelRole

_WHEN = datetime(2026, 9, 17, 12, tzinfo=UTC)


def _outcome(
    scenario: str,
    *,
    model: str = "claude-sonnet-4-6",
    strategy: str = "baseline",
    role: ModelRole = ModelRole.BENCHMARK,
    mode: ExecutionMode = ExecutionMode.CANNED,
    family: str | None = "consumer_lag",
    difficulty: str | None = "single",
    split: str | None = "dev",
    template_id: str | None = None,
    seed: int | None = 0,
    passed: bool = True,
    tool_calls: int = 3,
    tokens: int = 1000,
    usd: str = "0.100000",
    safety_detail: str = "all 1 action argument assertion(s) satisfied",
    safety_passed: bool = True,
) -> ScenarioOutcome:
    return ScenarioOutcome(
        scenario=scenario,
        template_id=template_id if template_id is not None else scenario,
        seed=seed,
        family=family,
        difficulty=difficulty,
        benchmark_split=split,
        final_state=IncidentState.RESOLVED if passed else IncidentState.ESCALATED,
        tool_calls_used=tool_calls,
        report=GradeReport(
            scenario=scenario,
            passed=passed,
            dimensions=(
                DimensionResult(
                    dimension=GradeDimension.OUTCOME,
                    passed=passed,
                    detail="terminal state resolved matched expectation",
                ),
                DimensionResult(
                    dimension=GradeDimension.SAFETY,
                    passed=safety_passed,
                    detail=safety_detail,
                ),
            ),
        ),
        provenance=RunProvenance(
            commander_revision="0" * 40,
            platform_image_digest="sha256:" + "1" * 64,
            agent_model=model,
            model_role=role,
            judge_model="claude-haiku-4-5",
            strategy=strategy,
            scenario=scenario,
            invocation_id="a" * 12,
            recorded_at=_WHEN,
            execution_mode=mode,
            budget=BudgetLedger(
                max_tool_calls=13,
                tool_calls_used=tool_calls,
                max_tokens=200_000,
                tokens_used=tokens,
                max_wall_seconds=600,
                wall_seconds_used=12.5,
                max_usd=Decimal("1.00"),
                usd_used=Decimal(usd),
            ),
        ),
    )


def _archive(root: Path, archive_id: str, outcomes: tuple[ScenarioOutcome, ...]) -> None:
    """Write one synthetic archive where ``assemble`` will look for it."""
    directory = root / "evals/runs" / archive_id
    directory.mkdir(parents=True, exist_ok=True)
    report = RunReport(
        generated_at=_WHEN,
        total=len(outcomes),
        passed=sum(o.report.passed for o in outcomes),
        failed=sum(not o.report.passed for o in outcomes),
        invocation_id=archive_id,
        closing=all(
            o.provenance is not None and o.provenance.model_role is ModelRole.BENCHMARK
            for o in outcomes
        ),
        outcomes=outcomes,
    )
    (directory / "report.json").write_text(report.model_dump_json(indent=2))


def _differences(document: dict[str, Any]) -> list[dict[str, Any]]:
    section = document["sections"]["paired_differences"]
    return list(section["differences"])


def _synthetic_root(tmp_path: Path) -> Path:
    """A root ``assemble`` can read end to end: it loads the corpus for the pass@k section."""
    (tmp_path / "evals" / "scenarios").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _rung(rung: str, *, entered: tuple[str, ...] = (), tokens: int = 0) -> dict[str, Any]:
    return {
        "rung": rung,
        "index": 0,
        "entered_because": list(entered),
        "fired": [],
        "unmeasured": [],
        "reasons": [],
        "llm_calls": 1,
        "tokens_used": tokens,
        "usd_used": "0",
        "tool_calls_used": 0,
        "climbed": False,
        "emitted": True,
    }


def _ladder_block(
    *,
    terminated_on: str = "baseline",
    extra: int = 0,
    rungs: tuple[dict[str, Any], ...] = (),
    search_available: bool = False,
    unclearable: tuple[str, ...] = (),
) -> dict[str, Any]:
    """One ``LadderRecord`` as a trace line carries it (WP-13.2)."""
    return {
        "ladder": ["baseline", "best_of_n_enumerated", "candidate_selector", "search_or_escalate"],
        "terminated_on": terminated_on,
        "rungs_used": len(rungs) or 1,
        "extra_llm_calls": extra,
        "search_available": search_available,
        "unclearable": list(unclearable),
        "thresholds": {"top1_confidence_floor": 0.75},
        "rungs": list(rungs or (_rung(terminated_on),)),
    }


def _step_line(scenario: str, ladder: dict[str, Any], *, strategy: str = "adaptive") -> str:
    return json.dumps(
        {
            "kind": "step",
            "step_id": "a" * 12,
            "run_id": scenario,
            "iteration": 0,
            "strategy": strategy,
            "model": "claude-sonnet-4-6",
            "candidate_set": [],
            "ladder": ladder,
            "emitted_step": {"hypotheses": [], "next_action": {"kind": "stop", "reason": "done"}},
            "llm_calls": [],
        }
    )


def _trace(root: Path, archive: str, scenario: str, lines: Sequence[str]) -> None:
    """Write one scenario's trace file inside a synthetic archive."""
    directory = root / "evals/runs" / archive / "traces"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{scenario}.jsonl").write_text("\n".join(lines) + "\n")


# --------------------------------------------------------------------------
# The seven grouping keys
# --------------------------------------------------------------------------


def test_the_report_groups_by_all_seven_keys_with_more_than_one_value_of_each(
    tmp_path: Path,
) -> None:
    """WP-2.5 verbatim: strategy, scenario, template, family, difficulty, split, mode."""
    outcomes = (
        _outcome("lag_a", template_id="lag", family="consumer_lag", difficulty="single"),
        _outcome("lag_b", template_id="lag", family="consumer_lag", difficulty="ambiguous"),
        _outcome(
            "dlq_a",
            strategy="best_of_n_enumerated",
            template_id="dlq",
            family="dlq",
            difficulty="control",
            split="holdout",
            mode=ExecutionMode.LIVE,
        ),
        _outcome(
            "dlq_b",
            strategy="best_of_n_enumerated",
            template_id="dlq",
            family="dlq",
            difficulty="control",
            split="validation",
            mode=ExecutionMode.LIVE,
        ),
    )
    _archive(tmp_path, "aaaaaaaaaaaa", outcomes)
    grouping = research.assemble(tmp_path, ("aaaaaaaaaaaa",))["sections"]["grouping"]

    assert grouping["keys"] == list(regression.GROUPING_KEYS) and len(grouping["keys"]) == 7
    for key in regression.GROUPING_KEYS:
        assert len(grouping["distinct_values"][key]) >= 2, key
        assert sum(grouping["distinct_values"][key].values()) == len(outcomes), key
    assert grouping["group_count"] == len(outcomes)
    assert all(set(regression.GROUPING_KEYS) <= set(group) for group in grouping["groups"])


def test_a_row_missing_a_key_is_bucketed_as_unknown_not_dropped(tmp_path: Path) -> None:
    """Archived rows predate WP-1.4; dropping them would drop their grades too."""
    _archive(
        tmp_path,
        "bbbbbbbbbbbb",
        (
            _outcome("lag_a", family=None, difficulty=None, split=None, template_id=""),
            _outcome("lag_b"),
        ),
    )
    document = research.assemble(tmp_path, ("bbbbbbbbbbbb",))
    grouping = document["sections"]["grouping"]
    assert grouping["unknown_counts"]["family"] == 1
    assert grouping["distinct_values"]["family"][regression.UNKNOWN_GROUP] == 1
    assert document["scope"]["rows"] == 2
    assert sum(group["runs"] for group in grouping["groups"]) == 2


# --------------------------------------------------------------------------
# One model per table — the red-before
# --------------------------------------------------------------------------


def test_a_two_model_scope_is_refused_and_both_ids_are_named(tmp_path: Path) -> None:
    """Refused, not footnoted.

    Red before: it took the first model id it saw and wrote a leaderboard over both.
    """
    _archive(tmp_path, "cccccccccccc", (_outcome("lag_a", model="claude-sonnet-4-6"),))
    _archive(tmp_path, "dddddddddddd", (_outcome("lag_a", model="claude-haiku-4-5"),))

    with pytest.raises(research.TwoModelsRefused) as refused:
        research.assemble(tmp_path, ("cccccccccccc", "dddddddddddd"))

    message = str(refused.value)
    assert "claude-sonnet-4-6" in message and "claude-haiku-4-5" in message
    assert "cccccccccccc" in message and "dddddddddddd" in message


def test_two_models_inside_one_archive_are_refused_too(tmp_path: Path) -> None:
    _archive(
        tmp_path,
        "eeeeeeeeeeee",
        (_outcome("lag_a", model="claude-sonnet-4-6"), _outcome("lag_b", model="some-other-model")),
    )
    with pytest.raises(research.TwoModelsRefused, match="some-other-model"):
        research.assemble(tmp_path, ("eeeeeeeeeeee",))


def test_the_cli_refuses_with_exit_2_and_prints_no_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 2 is the gate's "not a comparable input"; the refusal is the whole output."""
    _archive(tmp_path, "cccccccccccc", (_outcome("lag_a", model="claude-sonnet-4-6"),))
    _archive(tmp_path, "dddddddddddd", (_outcome("lag_a", model="claude-haiku-4-5"),))
    monkey = pytest.MonkeyPatch()
    monkey.setattr(research, "SCOPE", ("cccccccccccc", "dddddddddddd"))
    try:
        assert research.main(["--root", str(tmp_path)]) == 2
    finally:
        monkey.undo()
    printed = capsys.readouterr().out
    assert printed.startswith("RESEARCH REPORT REFUSED:")
    assert "leaderboard" not in printed.lower() or "|" not in printed


def test_one_model_under_two_roles_is_not_two_models(tmp_path: Path) -> None:
    """The committed scope's shape: one model, two roles. Comparable."""
    _archive(tmp_path, "ffffffffffff", (_outcome("lag_a", role=ModelRole.DEVELOPMENT),))
    _archive(tmp_path, "999999999999", (_outcome("lag_a", role=ModelRole.BENCHMARK),))
    document = research.assemble(tmp_path, ("ffffffffffff", "999999999999"))
    assert document["model"] == "claude-sonnet-4-6"
    assert len(document["sections"]["strategy_leaderboard"]["arms"]) == 2


# --------------------------------------------------------------------------
# Paired-trial counts, and the sample-size floor
# --------------------------------------------------------------------------


def test_every_difference_carries_its_paired_trial_count(tmp_path: Path) -> None:
    _archive(tmp_path, "111111111111", (_outcome("lag_a"), _outcome("lag_b")))
    _archive(
        tmp_path,
        "222222222222",
        (
            _outcome("lag_a", role=ModelRole.DEVELOPMENT, passed=False),
            _outcome("lag_b", role=ModelRole.DEVELOPMENT),
        ),
    )
    document = research.assemble(tmp_path, ("111111111111", "222222222222"))
    differences = _differences(document)
    assert differences, "two arms over the same scenarios produce differences"
    for difference in differences:
        assert difference["paired_trials"] == 2
        assert difference["paired_on"] == "scenario"
        assert "paired trials" in research.render_difference(difference)
    rendered = research.render_markdown(document)
    for difference in differences:
        assert research.render_difference(difference) in rendered


def test_a_difference_below_the_sample_floor_is_labelled_as_such(tmp_path: Path) -> None:
    _archive(tmp_path, "111111111111", (_outcome("lag_a"),))
    _archive(tmp_path, "222222222222", (_outcome("lag_a", role=ModelRole.DEVELOPMENT),))
    document = research.assemble(tmp_path, ("111111111111", "222222222222"))
    assert document["sections"]["paired_differences"]["all_below_floor"] is True
    for difference in _differences(document):
        assert difference["below_sample_floor"] is True
        assert difference["sample_floor"] == research.PAIRED_TRIAL_FLOOR == 100
        assert "BELOW the 100-trial floor" in research.render_difference(difference)


def test_a_difference_at_the_floor_is_not_labelled_below_it(tmp_path: Path) -> None:
    """The label has to be conditional, or it says nothing about the sample."""
    scenarios = [f"scenario_{index:03d}" for index in range(research.PAIRED_TRIAL_FLOOR)]
    _archive(tmp_path, "111111111111", tuple(_outcome(name) for name in scenarios))
    _archive(
        tmp_path,
        "222222222222",
        tuple(_outcome(name, role=ModelRole.DEVELOPMENT) for name in scenarios),
    )
    document = research.assemble(tmp_path, ("111111111111", "222222222222"))
    assert document["sections"]["paired_differences"]["all_below_floor"] is False
    for difference in _differences(document):
        assert difference["paired_trials"] == research.PAIRED_TRIAL_FLOOR
        assert difference["below_sample_floor"] is False
        assert "at or above the 100-trial floor" in research.render_difference(difference)


def test_reps_of_one_scenario_are_averaged_before_they_are_paired(tmp_path: Path) -> None:
    """One arm running a scenario twice must not weigh it twice against an arm that ran it once."""
    _archive(
        tmp_path,
        "111111111111",
        (_outcome("lag_a", passed=False), _outcome("lag_a", passed=True)),
    )
    _archive(tmp_path, "222222222222", (_outcome("lag_a", role=ModelRole.DEVELOPMENT),))
    document = research.assemble(tmp_path, ("111111111111", "222222222222"))
    pass_rate = next(d for d in _differences(document) if d["metric"] == "pass_rate")
    assert pass_rate["paired_trials"] == 1
    assert pass_rate["reps_left"] == 2 and pass_rate["reps_right"] == 1
    assert pass_rate["value_left"] == 0.5
    assert pass_rate["bootstrap_ci"] is None  # one pair: nothing to resample


def test_arms_two_keys_apart_are_not_compared(tmp_path: Path) -> None:
    _archive(tmp_path, "111111111111", (_outcome("lag_a"),))
    _archive(
        tmp_path,
        "222222222222",
        (_outcome("lag_a", role=ModelRole.DEVELOPMENT, mode=ExecutionMode.LIVE),),
    )
    section = research.assemble(tmp_path, ("111111111111", "222222222222"))["sections"][
        "paired_differences"
    ]
    assert section["differences"] == []
    assert len(section["skipped_arm_pairs"]) == 1
    assert set(section["skipped_arm_pairs"][0]["differs_in"]) == {"model_role", "execution_mode"}


def test_the_bootstrap_is_deterministic(tmp_path: Path) -> None:
    """A CI that moved between two runs of the assembler would not be a fact about the runs."""
    _archive(
        tmp_path, "111111111111", tuple(_outcome(f"s{i}", passed=i % 2 == 0) for i in range(8))
    )
    _archive(
        tmp_path,
        "222222222222",
        tuple(_outcome(f"s{i}", role=ModelRole.DEVELOPMENT) for i in range(8)),
    )
    first = research.assemble(tmp_path, ("111111111111", "222222222222"))
    second = research.assemble(tmp_path, ("111111111111", "222222222222"))
    assert research.render_json(first) == research.render_json(second)
    ci = next(d for d in _differences(first) if d["metric"] == "pass_rate")["bootstrap_ci"]
    assert ci is not None and ci[0] <= ci[1]


# --------------------------------------------------------------------------
# Safety: which half of the dimension failed
# --------------------------------------------------------------------------


def test_a_missing_action_is_not_counted_as_a_forbidden_action(tmp_path: Path) -> None:
    """A run that escalated without acting cannot have touched a forbidden tool."""
    _archive(
        tmp_path,
        "111111111111",
        (
            _outcome(
                "lag_a",
                passed=False,
                safety_passed=False,
                safety_detail="no call to ['restart_consumer_group'] to check 'consumer_group'",
            ),
            _outcome(
                "lag_b",
                passed=False,
                safety_passed=False,
                safety_detail="forbidden tool(s) called or attempted: ['pause_job']",
            ),
        ),
    )
    row = research.assemble(tmp_path, ("111111111111",))["sections"]["safety_by_strategy"]["rows"][
        0
    ]
    assert row["forbidden_action_runs"] == 1
    assert row["action_argument_assertion_failures"] == 1
    assert row["forbidden_action_rate"] == 0.5
    assert {failure["kind"] for failure in row["failures"]} == {
        "forbidden_action",
        "action_argument_assertion",
    }


# --------------------------------------------------------------------------
# Sections, and the ones that honestly have no number
# --------------------------------------------------------------------------


def test_the_committed_report_carries_every_section_and_none_is_empty() -> None:
    # Every section of the list as it stood when that version was written: the two WP-13.2
    # sections came later and are named in ``_DELIBERATE_NEW_SECTIONS`` below.
    document = json.loads(artifacts.newest("research_report").read_text())
    expected = tuple(key for key in research.SECTION_KEYS if key not in _DELIBERATE_NEW_SECTIONS)
    assert tuple(document["sections"]) == expected
    for key in expected:
        assert document["sections"][key], f"section {key} is empty"


def test_a_fresh_report_carries_every_section_in_the_closed_list() -> None:
    document = research.assemble(research.REPO_ROOT)
    assert tuple(document["sections"]) == research.SECTION_KEYS
    for key in research.SECTION_KEYS:
        assert document["sections"][key], f"section {key} is empty"


def test_an_empty_section_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(research, "_grouping", lambda rows: {})
    with pytest.raises(ValueError, match="empty section"):
        research.assemble(research.REPO_ROOT)


def test_the_unmeasurable_sections_say_so_and_say_what_they_need() -> None:
    """Missing would read as "nothing to report"; present reads as "not yet"."""
    document = json.loads(artifacts.newest("research_report").read_text())
    for key in ("pass_at_k_vs_selected_at_k", "oracle_gap", "calibration"):
        section = document["sections"][key]
        assert section["measurable"] is False
        assert section["value"] is None
        assert section["why"].strip() and section["requires"]


def test_the_report_states_what_it_cannot_say_yet() -> None:
    document = json.loads(artifacts.newest("research_report").read_text())
    rendered = artifacts.newest("research_report_md").read_text()
    limits = " ".join(document["limits"])
    for claim in ("ONE STRATEGY", "ONE MODEL", "NO RECORDED-WORLD MODE"):
        assert claim in limits
    assert "What this report cannot say yet" in rendered
    assert document["no_runs_were_made_to_produce_this"].startswith("Zero live invocations")


def test_the_root_cause_limit_counts_every_ungraded_row_and_says_which_reason() -> None:
    """The version before the Phase 2 archives said "NO ROOT-CAUSE NUMBER".

    Graded plus the three ungraded reasons must add up to every row in scope.
    """
    document = json.loads(artifacts.newest("research_report").read_text())
    limit = next(entry for entry in document["limits"] if "ROOT-CAUSE NUMBER" in entry)

    sources = research.read_scope(research.REPO_ROOT)
    rows = research.build_rows(research.REPO_ROOT, sources)
    graded = [row for row in rows if row.root_cause_graded]
    dimensionless = sum(
        1
        for source in sources
        for outcome in source.report.outcomes
        if not any(d.dimension is GradeDimension.ROOT_CAUSE for d in outcome.report.dimensions)
    )
    world_mismatch = sum(
        len(
            [
                e
                for e in research.regraded_verdicts(research.REPO_ROOT, source.archive).values()
                if not e.root_cause_graded and e.dimensions
            ]
        )
        for source in sources
        if source.archive in research.SUPERSEDED_ROOT_CAUSE
    )
    assert 0 < len(graded) < len(rows)  # a denominator, not an accuracy over the corpus
    unlabelled = len(rows) - len(graded) - dimensionless - world_mismatch
    assert unlabelled > 0

    # Every one of the four buckets is named in the sentence, and they add up.
    for number in (len(graded), len(rows), len(rows) - len(graded), dimensionless):
        assert str(number) in limit, number
    assert "ADR 0040" in limit and "INC-003" in limit
    assert f"{len(graded)} correct" in limit  # every graded row is correct today


def test_the_committed_report_is_non_closing_because_a_development_run_is_in_scope() -> None:
    """Plan 03 § 14, reused from ``RunReport.non_closing_reason`` rather than restated."""
    document = json.loads(artifacts.newest("research_report").read_text())
    assert document["closing"] is False
    assert [entry["archive"] for entry in document["non_closing"]] == ["2408b07ef532"]
    assert "development" in document["non_closing"][0]["reason"]
    assert "NON-CLOSING" in artifacts.newest("research_report_md").read_text()


def test_scenario_level_regressions_exclude_the_filtered_runs() -> None:
    """The gate's own rule: a filtered report is not a suite-level input."""
    section = json.loads(artifacts.newest("research_report").read_text())["sections"][
        "scenario_level_regressions"
    ]
    assert section["computed_by"] == "evals/regression.py::compare"
    assert {entry["archive"] for entry in section["excluded_filtered_runs"]} == {
        "42000dfda188",
        "845bdae22195",
        "ee183c85429c",
        "47abb70a2b9e",
        "759e198cdd27",
        "648a32f2339d",
        "fc896b25a09c",
        "d16aa18dce08",
        "42c675d9c145",
    }
    assert [(c["baseline_side"], c["latest_side"]) for c in section["comparisons"]] == [
        ("2408b07ef532", "32ae38f6b38b"),
        ("2408b07ef532", "b75527784077"),
        ("32ae38f6b38b", "b75527784077"),
    ]
    assert all(not c["gate_would_fail"] for c in section["comparisons"])


def test_a_partial_suite_is_excluded_from_the_diff_too() -> None:
    """The red-before, and it cost $2.15 to learn.

    ``0db6fe722f7c`` is not filtered, so the filtered-run rule let 27 unseeded live rows
    into a diff against a 41-row canned sweep and printed "7 regressions" (INC-003).
    """
    section = json.loads(artifacts.newest("research_report").read_text())["sections"][
        "scenario_level_regressions"
    ]
    partial = section["excluded_partial_runs"]
    assert [entry["archive"] for entry in partial] == ["0db6fe722f7c"]
    assert partial[0]["scenarios"] == 27
    assert partial[0]["full_suite_is"] == 41
    compared = {
        side for c in section["comparisons"] for side in (c["baseline_side"], c["latest_side"])
    }
    assert "0db6fe722f7c" not in compared
    assert all(not c["dropped_scenarios"] for c in section["comparisons"])


def test_the_withdrawn_root_cause_verdicts_are_read_from_the_regrade_not_the_archive() -> None:
    """INC-003: the archive still carries seven grades the project withdrew.

    Red before ``SUPERSEDED_ROOT_CAUSE``: the table read the archive and reported 46 of 53.
    """
    archive = "0db6fe722f7c"
    assert archive in research.SUPERSEDED_ROOT_CAUSE
    regraded = research.regraded_verdicts(research.REPO_ROOT, archive)
    source = research.read_source(research.REPO_ROOT, archive)
    archived = {
        outcome.scenario: research._root_cause_verdict(outcome)
        for outcome in source.report.outcomes
    }

    # The archive's own verdicts: 18 graded, 7 of them failing.
    assert sum(1 for graded, _ in archived.values() if graded) == 18
    assert sum(1 for graded, ok in archived.values() if graded and not ok) == 7

    # The re-grade's: 2 graded, none failing, 16 held back by ADR 0040.
    assert sum(1 for entry in regraded.values() if entry.root_cause_graded) == 2
    assert all(entry.root_cause_correct for entry in regraded.values() if entry.root_cause_graded)

    rows = {
        row.scenario: row
        for row in research.build_rows(
            research.REPO_ROOT, research.read_scope(research.REPO_ROOT, (archive,))
        )
    }
    assert sum(1 for row in rows.values() if row.root_cause_graded) == 2
    assert sum(1 for row in rows.values() if row.passed) == 26  # was 20 as archived
    assert source.report.passed == 20  # and the archive is untouched


# --------------------------------------------------------------------------
# The artifact
# --------------------------------------------------------------------------


def test_the_research_report_resolves_through_the_artifact_resolver() -> None:
    """Divergence D2: without a KINDS entry the report cannot be resolved at all."""
    for kind in ("research_report", "research_report_md"):
        assert kind in artifacts.KINDS
        path = artifacts.newest(kind)
        assert path.is_file()
        assert path.parent == research.REPO_ROOT / "evals/reports/research"


def test_the_new_kind_does_not_adopt_or_get_adopted_by_its_neighbours(tmp_path: Path) -> None:
    """Four families share ``evals/reports/``; each resolves only its own stem."""
    reports = tmp_path / "evals" / "reports"
    reports.mkdir(parents=True)
    names = {
        "report": "report.20260917T000000Z.aaaaaaaaaaaa.json",
        "baseline_report": "baseline_report.20260917T000000Z.bbbbbbbbbbbb.json",
        "phase_close_report": "phase_close_report.20260917T000000Z.cccccccccccc.json",
        "research_report": "research_report.20260917T000000Z.dddddddddddd.json",
    }
    for name in names.values():
        (reports / name).write_text("{}")
    for kind, expected in names.items():
        assert artifacts.newest(kind, root=tmp_path).name == expected
    assert len(artifacts.versions("research_report", root=tmp_path)) == 1


def test_writing_twice_refuses_rather_than_replacing(tmp_path: Path) -> None:
    """Invariant 9 at the filesystem: a second write raises, never overwrites."""
    document = json.loads(artifacts.newest("research_report").read_text())
    assert all(path.is_file() for path in research.write(document, root=tmp_path))
    with pytest.raises(FileExistsError):
        research.write(document, root=tmp_path)


def test_the_artifact_is_named_after_its_scope_not_after_the_clock() -> None:
    """Re-running the assembler must aim at the same path, so the write can refuse."""
    document = research.assemble(research.REPO_ROOT)
    timestamp, invocation_id = research.scope_stamp(document)
    assert artifacts.newest("research_report").name == artifacts.version_name(
        "research_report", timestamp=timestamp, invocation_id=invocation_id
    )


#: Keys a later packet deliberately changed in a leaderboard row — the only reason the
#: committed JSON and today's render may differ. One so far: WP-6.3's ADR 0052 gate.
_DELIBERATE_ROW_CHANGES: Final[tuple[str, ...]] = (
    "judge_mean_overall",
    "judge",
    "judge_calibration_report_id",
    "judge_gate",
)

#: Sections a later packet ADDED, which the committed document therefore cannot carry: the
#: artifact is named after its scope (see the test above), so a section added without a new
#: archive aims at a path invariant 9 refuses to rewrite. Both of these report themselves
#: unmeasurable today and neither renders into the Markdown half, which is why that half is
#: still compared with nothing excused. WP-13.2's two.
_DELIBERATE_NEW_SECTIONS: Final[tuple[str, ...]] = (
    "adaptive_cost_frontier",
    "adaptive_rung_distribution",
)


def _without_the_deliberate_changes(payload: Any) -> Any:
    """The document with the enumerated keys dropped from every row that has them.

    Identified by ``judge_mean_overall``, which both sides have.
    """
    if isinstance(payload, dict):
        gated = "judge_mean_overall" in payload
        return {
            key: _without_the_deliberate_changes(value)
            for key, value in payload.items()
            if not (gated and key in _DELIBERATE_ROW_CHANGES)
        }
    if isinstance(payload, list):
        return [_without_the_deliberate_changes(item) for item in payload]
    return payload


def _without_the_new_sections(document: dict[str, Any]) -> dict[str, Any]:
    """The document with the sections a later packet added removed, by name."""
    return {
        **document,
        "sections": {
            key: value
            for key, value in document["sections"].items()
            if key not in _DELIBERATE_NEW_SECTIONS
        },
    }


def test_the_committed_report_regenerates_byte_for_byte() -> None:
    """Every input is a locked archive, so the document is a function of the repo.

    A failure outside ``_DELIBERATE_ROW_CHANGES`` and ``_DELIBERATE_NEW_SECTIONS`` means
    something the report READ changed. The Markdown half is compared byte for byte with nothing
    excused, which is what keeps the two allowances from growing quietly.
    """
    document = research.assemble(research.REPO_ROOT)
    committed = json.loads(artifacts.newest("research_report").read_text())
    assert _without_the_deliberate_changes(
        _without_the_new_sections(document)
    ) == _without_the_deliberate_changes(committed)
    assert research.render_markdown(document) == artifacts.newest("research_report_md").read_text()


def test_the_committed_report_predates_the_adaptive_sections() -> None:
    """The other half of that allowance: it is used, and only for these two sections.

    Asserting both sides means the excuse cannot start covering a third section, and the
    Markdown comparison above proves the new ones are silent while they have no number.
    """
    committed = json.loads(artifacts.newest("research_report").read_text())
    assert not set(committed["sections"]) & set(_DELIBERATE_NEW_SECTIONS)
    fresh = research.assemble(research.REPO_ROOT)
    assert tuple(fresh["sections"]) == research.SECTION_KEYS
    for key in _DELIBERATE_NEW_SECTIONS:
        section = fresh["sections"][key]
        assert section["measurable"] is False
        assert section["value"] is None
        assert section["why"].strip() and section["requires"]


def test_the_committed_report_predates_the_judge_gate() -> None:
    """The other half of the allowance above: it is used, and only for this.

    Asserting both sides means the excuse cannot start covering another field.
    """
    committed = json.loads(artifacts.newest("research_report").read_text())
    rows = committed["sections"]["strategy_leaderboard"]["arms"]
    assert rows
    assert all(isinstance(row["judge_mean_overall"], float) for row in rows)
    assert all("judge_gate" not in row for row in rows)
    today = research.assemble(research.REPO_ROOT)
    fresh = today["sections"]["strategy_leaderboard"]["arms"]
    assert all(row["judge_mean_overall"] == research.WITHHELD_JUDGE for row in fresh)
    assert all(row["judge_calibration_report_id"] is None for row in fresh)


def test_every_archive_in_scope_is_committed_and_carries_provenance() -> None:
    for archive in research.SCOPE:
        source = research.read_source(research.REPO_ROOT, archive)
        assert source.report.outcomes
        assert all(outcome.provenance is not None for outcome in source.report.outcomes)


# --------------------------------------------------------------------------
# WP-13.2: the adaptive frontier, and where the ladder terminated
# --------------------------------------------------------------------------


def _frontier(document: dict[str, Any]) -> dict[str, Any]:
    section = document["sections"]["adaptive_cost_frontier"]
    assert section["measurable"], section["why"]
    return dict(section["value"])


def _comparison(document: dict[str, Any], right: str) -> dict[str, Any]:
    return next(
        c
        for c in _frontier(document)["adaptive_against_each_fixed_arm"]
        if c["right"].startswith(right)
    )


def _two_arm_root(
    tmp_path: Path,
    *,
    adaptive: dict[str, Any],
    baseline: dict[str, Any],
    scenarios: tuple[str, ...] = ("alpha", "beta"),
) -> Path:
    """One archive per arm over the same scenarios — the paired shape the frontier needs."""
    root = _synthetic_root(tmp_path)
    _archive(
        root,
        "ad" + "0" * 10,
        tuple(_outcome(name, strategy="adaptive", **adaptive) for name in scenarios),
    )
    _archive(
        root,
        "ba" + "0" * 10,
        tuple(_outcome(name, strategy="baseline", **baseline) for name in scenarios),
    )
    return root


def test_the_frontier_is_not_measurable_without_an_adaptive_arm() -> None:
    document = research.assemble(research.REPO_ROOT)
    section = document["sections"]["adaptive_cost_frontier"]
    assert section["measurable"] is False
    assert "no archive in scope carries an `adaptive` arm" in section["why"]
    assert any("WP-13.2" in requirement for requirement in section["requires"])


def test_the_frontier_pairs_the_arms_on_the_instances_they_share(tmp_path: Path) -> None:
    root = _synthetic_root(tmp_path)
    _archive(
        root,
        "ad" + "0" * 10,
        tuple(_outcome(name, strategy="adaptive") for name in ("alpha", "beta", "adaptive_only")),
    )
    _archive(
        root,
        "ba" + "0" * 10,
        tuple(_outcome(name, strategy="baseline") for name in ("alpha", "beta")),
    )
    value = _frontier(research.assemble(root, ("ad" + "0" * 10, "ba" + "0" * 10)))
    assert value["instances"] == ["alpha", "beta"]
    assert all(row["instances"] == 2 for row in value["rows"])
    assert all(row["runs"] == 2 for row in value["rows"])


def test_an_arm_better_on_every_axis_dominates_and_the_other_is_off_the_frontier(
    tmp_path: Path,
) -> None:
    root = _two_arm_root(
        tmp_path,
        adaptive={"passed": True, "tokens": 500, "tool_calls": 2, "usd": "0.050000"},
        baseline={"passed": False, "tokens": 900, "tool_calls": 5, "usd": "0.090000"},
    )
    document = research.assemble(root, ("ad" + "0" * 10, "ba" + "0" * 10))
    comparison = _comparison(document, "baseline")
    assert comparison["verdict"] == research.DOMINATES
    assert comparison["worse_on"] == []
    rows = {row["arm"]: row for row in _frontier(document)["rows"]}
    adaptive_arm = next(arm for arm in rows if arm.startswith("adaptive"))
    baseline_arm = next(arm for arm in rows if arm.startswith("baseline"))
    assert rows[adaptive_arm]["on_frontier"] is True
    assert rows[baseline_arm]["on_frontier"] is False
    assert rows[baseline_arm]["dominated_by"] == [adaptive_arm]


def test_a_trade_off_names_the_axis_the_ladder_is_worse_on(tmp_path: Path) -> None:
    """The claim that matters: no summary may hide a regression (plan 03 § 173)."""
    root = _two_arm_root(
        tmp_path,
        adaptive={"passed": True, "tokens": 2_000, "tool_calls": 4, "usd": "0.200000"},
        baseline={"passed": False, "tokens": 900, "tool_calls": 4, "usd": "0.090000"},
    )
    document = research.assemble(root, ("ad" + "0" * 10, "ba" + "0" * 10))
    comparison = _comparison(document, "baseline")
    assert comparison["verdict"] == research.TRADE_OFF
    assert comparison["better_on"] == ["pass_rate"]
    assert comparison["worse_on"] == ["mean_tokens", "mean_usd"]
    assert comparison["equal_on"] == ["safety_rate", "mean_tool_calls", "mean_wall_seconds"]
    # Both arms stay on the frontier: neither dominates the other.
    assert all(row["on_frontier"] for row in _frontier(document)["rows"])
    rendered = research.render_markdown(document)
    assert f"**{research.TRADE_OFF}**" in rendered
    assert "worse on mean_tokens, mean_usd" in rendered


def test_an_arm_worse_on_every_axis_is_reported_as_dominated(tmp_path: Path) -> None:
    root = _two_arm_root(
        tmp_path,
        adaptive={"passed": False, "tokens": 3_000, "tool_calls": 9, "usd": "0.300000"},
        baseline={"passed": True, "tokens": 900, "tool_calls": 4, "usd": "0.090000"},
    )
    document = research.assemble(root, ("ad" + "0" * 10, "ba" + "0" * 10))
    comparison = _comparison(document, "baseline")
    assert comparison["verdict"] == research.DOMINATED
    assert comparison["better_on"] == []
    rows = {row["arm"]: row for row in _frontier(document)["rows"]}
    adaptive_arm = next(arm for arm in rows if arm.startswith("adaptive"))
    assert rows[adaptive_arm]["on_frontier"] is False


def test_a_safety_violation_moves_the_safety_axis_and_not_the_pass_axis(tmp_path: Path) -> None:
    root = _two_arm_root(
        tmp_path,
        adaptive={
            "passed": False,
            "safety_passed": False,
            "safety_detail": "forbidden tool(s) called or attempted: restart_consumer_group",
        },
        baseline={"passed": False},
    )
    document = research.assemble(root, ("ad" + "0" * 10, "ba" + "0" * 10))
    rows = {row["arm"]: row for row in _frontier(document)["rows"]}
    adaptive_arm = next(arm for arm in rows if arm.startswith("adaptive"))
    baseline_arm = next(arm for arm in rows if arm.startswith("baseline"))
    assert rows[adaptive_arm]["safety_rate"] == 0.0
    assert rows[baseline_arm]["safety_rate"] == 1.0
    assert _comparison(document, "baseline")["worse_on"] == ["safety_rate"]


def test_equal_arms_both_stay_on_the_frontier(tmp_path: Path) -> None:
    root = _two_arm_root(
        tmp_path,
        adaptive={"passed": True},
        baseline={"passed": True},
    )
    document = research.assemble(root, ("ad" + "0" * 10, "ba" + "0" * 10))
    assert _comparison(document, "baseline")["verdict"] == research.EQUAL
    assert all(row["on_frontier"] for row in _frontier(document)["rows"])


def test_the_frontier_refuses_arms_that_share_no_instance(tmp_path: Path) -> None:
    root = _synthetic_root(tmp_path)
    _archive(root, "ad" + "0" * 10, (_outcome("alpha", strategy="adaptive"),))
    _archive(root, "ba" + "0" * 10, (_outcome("beta", strategy="baseline"),))
    section = research.assemble(root, ("ad" + "0" * 10, "ba" + "0" * 10))["sections"][
        "adaptive_cost_frontier"
    ]
    assert section["measurable"] is False
    assert "share no instance" in section["why"]


def test_the_frontier_pairs_on_the_recorded_world_id_where_the_rows_carry_one() -> None:
    """ADR 0049's unit: two arms over the same recording are one instance, not two names."""
    left = _outcome("alpha", strategy="adaptive")
    right = _outcome("alpha_renamed", strategy="baseline")
    left = left.model_copy(update={"replay": {"world_fingerprint": "w0"}})
    right = right.model_copy(update={"replay": {"world_fingerprint": "w0"}})
    rows = [
        research.build_row(
            research.Source(
                archive="a" * 12,
                path=Path("x"),
                sha256="",
                report=RunReport(
                    generated_at=_WHEN,
                    total=1,
                    passed=1,
                    failed=0,
                    invocation_id="a" * 12,
                    outcomes=(left,),
                ),
            ),
            left,
        ),
        research.build_row(
            research.Source(
                archive="b" * 12,
                path=Path("y"),
                sha256="",
                report=RunReport(
                    generated_at=_WHEN,
                    total=1,
                    passed=1,
                    failed=0,
                    invocation_id="b" * 12,
                    outcomes=(right,),
                ),
            ),
            right,
        ),
    ]
    assert [row.instance for row in rows] == ["w0", "w0"]
    section = research._adaptive_cost_frontier(rows)
    assert section["measurable"] is True
    assert section["value"]["instances"] == ["w0"]


def test_the_rung_distribution_is_not_measurable_without_a_ladder_block() -> None:
    section = research.assemble(research.REPO_ROOT)["sections"]["adaptive_rung_distribution"]
    assert section["measurable"] is False
    assert "`ladder` block" in section["why"]


def test_the_rung_distribution_counts_the_terminating_rung_by_difficulty(tmp_path: Path) -> None:
    root = _synthetic_root(tmp_path)
    archive = "ad" + "0" * 10
    _archive(
        root,
        archive,
        (
            _outcome("easy_one", strategy="adaptive", difficulty="single"),
            _outcome("hard_one", strategy="adaptive", difficulty="ambiguous"),
        ),
    )
    _trace(
        root,
        archive,
        "easy_one",
        [
            _step_line("easy_one", _ladder_block()),
            _step_line("easy_one", _ladder_block()),
        ],
    )
    _trace(
        root,
        archive,
        "hard_one",
        [
            _step_line(
                "hard_one",
                _ladder_block(
                    terminated_on="candidate_selector",
                    extra=2,
                    rungs=(
                        _rung("baseline", tokens=100),
                        _rung("best_of_n_enumerated", entered=("top1_confidence_low",), tokens=400),
                        _rung(
                            "candidate_selector",
                            entered=("top1_top2_margin_narrow",),
                            tokens=200,
                        ),
                    ),
                ),
            ),
            _step_line("hard_one", _ladder_block()),
        ],
    )
    value = research.assemble(root, (archive,))["sections"]["adaptive_rung_distribution"]["value"]
    by_difficulty = {row["difficulty"]: row for row in value["by_difficulty"]}
    assert by_difficulty["single"]["baseline_rung_share"] == 1.0
    assert by_difficulty["single"]["mean_extra_llm_calls"] == 0.0
    assert by_difficulty["ambiguous"]["baseline_rung_share"] == 0.5
    assert by_difficulty["ambiguous"]["terminated_on"] == {
        "baseline": 1,
        "candidate_selector": 1,
    }
    assert by_difficulty["ambiguous"]["mean_extra_llm_calls"] == 1.0
    hard = next(row for row in value["rows"] if row["scenario"] == "hard_one")
    assert hard["opened_by_signal"] == {
        "top1_confidence_low": 1,
        "top1_top2_margin_narrow": 1,
    }
    assert hard["tokens_by_rung"] == {
        "baseline": 100,
        "best_of_n_enumerated": 400,
        "candidate_selector": 200,
    }


def test_the_rung_distribution_renders_and_names_the_two_sided_claim(tmp_path: Path) -> None:
    root = _synthetic_root(tmp_path)
    archive = "ad" + "0" * 10
    _archive(root, archive, (_outcome("easy_one", strategy="adaptive", difficulty="single"),))
    _trace(root, archive, "easy_one", [_step_line("easy_one", _ladder_block())])
    document = research.assemble(root, (archive,))
    rendered = research.render_markdown(document)
    assert research.SECTION_TITLES["adaptive_rung_distribution"] in rendered
    assert "baseline-rung share" in rendered
    assert "easy cases" in document["sections"]["adaptive_rung_distribution"]["value"][
        "two_sided_claim"
    ].replace("easy-cases", "easy cases")


def test_a_ladderless_step_record_is_not_counted_as_a_ladder(tmp_path: Path) -> None:
    """Anti-vacuity: `baseline`'s own step records carry no ladder, so they must not appear."""
    root = _synthetic_root(tmp_path)
    archive = "ba" + "0" * 10
    _archive(root, archive, (_outcome("alpha", strategy="baseline"),))
    line = json.loads(_step_line("alpha", _ladder_block(), strategy="baseline"))
    del line["ladder"]
    _trace(root, archive, "alpha", [json.dumps(line)])
    section = research.assemble(root, (archive,))["sections"]["adaptive_rung_distribution"]
    assert section["measurable"] is False
    assert research.ladder_records_in(root, archive) == {}


def test_the_adaptive_strategy_name_matches_the_arm_that_writes_it() -> None:
    from incident_commander.agent.strategies.names import StrategyName

    assert StrategyName.ADAPTIVE.value == research.ADAPTIVE_STRATEGY
    assert StrategyName.BASELINE.value == research.BASELINE_RUNG

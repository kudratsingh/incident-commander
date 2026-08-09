from datetime import UTC, datetime
from pathlib import Path

import pytest

from evals import regression
from evals.graders.deterministic import (
    DimensionResult,
    GradeDimension,
    GradeReport,
)
from evals.regression import compare
from evals.runner import RunReport, ScenarioOutcome
from incident_commander.agent.state import IncidentState


def _outcome(name: str, passed: bool) -> ScenarioOutcome:
    return ScenarioOutcome(
        scenario=name,
        final_state=IncidentState.ESCALATED,
        tool_calls_used=0,
        report=GradeReport(
            scenario=name,
            passed=passed,
            dimensions=(
                DimensionResult(
                    dimension=GradeDimension.OUTCOME,
                    passed=passed,
                    detail="",
                ),
            ),
        ),
    )


def _report(
    outcomes: tuple[ScenarioOutcome, ...],
    *,
    degraded_count: int | None = None,
    only_patterns: tuple[str, ...] = (),
) -> RunReport:
    passed = sum(1 for o in outcomes if o.report.passed)
    return RunReport(
        generated_at=datetime(2026, 7, 16, tzinfo=UTC),
        total=len(outcomes),
        passed=passed,
        failed=len(outcomes) - passed,
        degraded_count=degraded_count,
        only_patterns=only_patterns,
        outcomes=outcomes,
    )


def _point_gate_at(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    baseline: RunReport,
    latest: RunReport | None,
) -> None:
    """Write synthetic reports under tmp_path and aim main() at them.

    main() reads the module-level _BASELINE/_LATEST paths; evals/reports/
    is append-only evidence, so tests must never point at the real files.
    ``latest=None`` leaves the latest path nonexistent.
    """
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(baseline.model_dump_json())
    latest_path = tmp_path / "latest.json"
    if latest is not None:
        latest_path.write_text(latest.model_dump_json())
    monkeypatch.setattr(regression, "_BASELINE", baseline_path)
    monkeypatch.setattr(regression, "_LATEST", latest_path)


class TestCompare:
    def test_no_changes_no_regressions(self) -> None:
        baseline = _report((_outcome("a", True), _outcome("b", True)))
        latest = _report((_outcome("a", True), _outcome("b", True)))
        result = compare(baseline, latest)
        assert result.regressions == ()
        assert result.improvements == ()
        assert result.new_scenarios == ()
        assert result.dropped_scenarios == ()
        assert result.has_regressions is False

    def test_pass_to_fail_is_regression(self) -> None:
        baseline = _report((_outcome("a", True), _outcome("b", True)))
        latest = _report((_outcome("a", True), _outcome("b", False)))
        result = compare(baseline, latest)
        assert result.regressions == ("b",)
        assert result.has_regressions is True

    def test_fail_to_pass_is_improvement_not_regression(self) -> None:
        baseline = _report((_outcome("a", False),))
        latest = _report((_outcome("a", True),))
        result = compare(baseline, latest)
        assert result.regressions == ()
        assert result.improvements == ("a",)
        assert result.has_regressions is False

    def test_new_scenario_not_regression(self) -> None:
        baseline = _report((_outcome("a", True),))
        latest = _report((_outcome("a", True), _outcome("b", False)))
        result = compare(baseline, latest)
        assert result.regressions == ()
        assert result.new_scenarios == ("b",)
        assert result.has_regressions is False

    def test_dropped_is_not_a_regression(self) -> None:
        # Classification is deliberately unchanged by the A-03 fix: a
        # dropped scenario is reported distinctly, not misfiled as a
        # regression, so _print_comparison output keeps its meaning. The
        # GATE decision moved to main() — TestMainGate pins that dropped
        # now exits 1. (Renamed from test_dropped_scenario_reported,
        # which pinned the old dropped-never-fails intent.)
        baseline = _report((_outcome("a", True), _outcome("b", True)))
        latest = _report((_outcome("a", True),))
        result = compare(baseline, latest)
        assert result.dropped_scenarios == ("b",)
        assert result.regressions == ()
        assert result.has_regressions is False

    def test_regressions_sorted(self) -> None:
        baseline = _report((_outcome("z", True), _outcome("a", True), _outcome("m", True)))
        latest = _report((_outcome("z", False), _outcome("a", False), _outcome("m", True)))
        result = compare(baseline, latest)
        assert result.regressions == ("a", "z")

    def test_baseline_failing_scenario_still_failing_not_regression(self) -> None:
        baseline = _report((_outcome("a", False),))
        latest = _report((_outcome("a", False),))
        result = compare(baseline, latest)
        assert result.regressions == ()
        assert result.improvements == ()


class TestMainGate:
    """Exit-code policy of regression.main() (A-03, S-14; ADR 0013).

    compare() classification is pinned by TestCompare; these tests pin what
    the gate DOES with it: 0 = comparable full-suite input, no regressions,
    no coverage loss; 1 = gate failed (regression or dropped scenario);
    2 = not a comparable input (missing file, filtered report). Provenance
    mismatch warns on stdout and never gates (S-14 — deliberate; gating is
    deferred until after the next baseline bless, per ADR 0013).
    """

    def test_dropped_scenarios_fail_the_gate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The exact repro_a03.py shape: baseline {a,b,c all pass} vs a
        # latest that silently lost b and c. At pre-fix HEAD this exited 0
        # ("dropped_scenarios: ('b','c'); exit code: 0") — the assertion
        # that would have caught A-03.
        baseline = _report((_outcome("a", True), _outcome("b", True), _outcome("c", True)))
        latest = _report((_outcome("a", True),))
        _point_gate_at(monkeypatch, tmp_path, baseline, latest)
        assert regression.main() == 1
        err = capsys.readouterr().err
        assert "GATE FAIL" in err
        assert "2 baseline scenario(s) missing" in err

    def test_dropped_is_not_a_regression_but_fails_the_gate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The two halves of the old test_dropped_scenario_reported, made
        # explicit: classification unchanged (intent), exit 0 was the bug.
        baseline = _report((_outcome("a", True), _outcome("b", True)))
        latest = _report((_outcome("a", True),))
        result = compare(baseline, latest)
        assert result.regressions == ()
        assert result.dropped_scenarios == ("b",)
        _point_gate_at(monkeypatch, tmp_path, baseline, latest)
        assert regression.main() == 1

    def test_regressions_still_fail_the_gate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        baseline = _report((_outcome("a", True), _outcome("b", True)))
        latest = _report((_outcome("a", True), _outcome("b", False)))
        _point_gate_at(monkeypatch, tmp_path, baseline, latest)
        assert regression.main() == 1

    def test_filtered_latest_is_refused_even_when_green(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Every scenario present passes and none dropped — the report is
        # still refused as gate input (exit 2, "not a comparable input"),
        # not diffed: a filtered run proves nothing about the full suite.
        baseline = _report((_outcome("a", True), _outcome("b", True)))
        latest = _report(
            (_outcome("a", True), _outcome("b", True)),
            only_patterns=("remediate_",),
        )
        _point_gate_at(monkeypatch, tmp_path, baseline, latest)
        assert regression.main() == 2
        err = capsys.readouterr().err
        assert "filtered run" in err
        assert "remediate_" in err

    def test_missing_latest_is_not_comparable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        baseline = _report((_outcome("a", True),))
        _point_gate_at(monkeypatch, tmp_path, baseline, latest=None)
        assert regression.main() == 2
        assert "latest report not found" in capsys.readouterr().err

    def test_provenance_unknown_warns_without_gating(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The committed baseline predates provenance stamping
        # (degraded_count None) while a fresh offline latest records 32 —
        # the gate warns and still exits 0 given no regressions/drops.
        # Warn-only is the S-14 contract; pinning it here makes any future
        # move to hard gating a deliberate test change.
        baseline = _report(
            (_outcome("a", True), _outcome("b", True)),
            degraded_count=None,
        )
        latest = _report(
            (_outcome("a", True), _outcome("b", True)),
            degraded_count=32,
        )
        _point_gate_at(monkeypatch, tmp_path, baseline, latest)
        assert regression.main() == 0
        assert "PROVENANCE" in capsys.readouterr().out

    def test_provenance_mismatch_warns_without_gating(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        baseline = _report(
            (_outcome("a", True), _outcome("b", True)),
            degraded_count=0,
        )
        latest = _report(
            (_outcome("a", True), _outcome("b", True)),
            degraded_count=32,
        )
        _point_gate_at(monkeypatch, tmp_path, baseline, latest)
        assert regression.main() == 0
        out = capsys.readouterr().out
        assert "PROVENANCE WARNING" in out
        assert "baseline ran 0 degraded, latest 32" in out

    def test_full_suite_matching_provenance_is_quiet_and_green(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # New scenarios still never fail the gate, and equal known
        # degraded_counts emit no provenance line at all.
        baseline = _report(
            (_outcome("a", True), _outcome("b", True)),
            degraded_count=0,
        )
        latest = _report(
            (_outcome("a", True), _outcome("b", True), _outcome("c", False)),
            degraded_count=0,
        )
        _point_gate_at(monkeypatch, tmp_path, baseline, latest)
        assert regression.main() == 0
        assert "PROVENANCE" not in capsys.readouterr().out

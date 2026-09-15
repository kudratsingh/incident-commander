"""The per-run provenance record: what produced a number (WP-0.3).

ADR 0013 made provenance part of the eval result and answered one question
with it — live or canned. It answered no others, and the gap was total: a
saved run carried no model id, no commander revision, no platform digest and
none of the four budget meters ``BudgetLedger`` had been tracking all along
(divergence D3). A Phase 0 baseline written on that report would have been
un-attributable the day it was written, and every later phase report is built
on it.

So these tests are about attribution, not about behaviour:

* the record survives a write and a read with every field populated;
* the platform digest is READ FROM ``demo/compose.yml``, so it cannot drift
  from the stack that ran;
* the budgets are the ones the run was SEEDED with, not the ones the
  configuration documents (they are different numbers — ADR 0019's
  per-scenario cap, and the paid-run protocol's .env-only ceilings, D7);
* a report containing a development-role run is marked non-closing, in the
  artifact and not only on the console (plan 03 § 14);
* a comparison spanning two ``agent_model`` ids is REFUSED, because a
  leaderboard row across two models is a model change and a behaviour change
  added together and the table cannot say which (plan 02 § 9);
* every archived report still parses. They are locked, append-only evidence
  that predates all of this, and the reader tolerates its absence.
"""

from __future__ import annotations

import re
import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from pydantic import SecretStr, ValidationError

from evals import regression
from evals.graders.deterministic import (
    DimensionResult,
    GradeDimension,
    GradeReport,
    ScenarioExpectation,
)
from evals.runner import (
    ExecutionMode,
    RunProvenance,
    RunReport,
    ScenarioOutcome,
    _parse_model_role,
    build_provenance,
    commander_revision,
    main,
    platform_image_digest,
    run_all,
    write_report,
)
from evals.scenarios.schema import Scenario
from incident_commander.agent.state import BudgetLedger, IncidentState
from incident_commander.api.schemas import AlertPayload
from incident_commander.config import ModelRole, Settings

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_COMPOSE: Final[Path] = _REPO_ROOT / "demo" / "compose.yml"
_BASELINE: Final[Path] = _REPO_ROOT / "evals" / "reports" / "baseline.json"
_SHA1: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_DIGEST: Final[re.Pattern[str]] = re.compile(r"^sha256:[0-9a-f]{64}$")


def _settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "anthropic_api_key": SecretStr("eval"),
        "judge_model": "claude-haiku-4-5",
        "platform_mcp_url": "https://eval.local",
        "platform_rest_url": "https://eval.local",
        "platform_token": SecretStr("eval"),
        "platform_webhook_secret": SecretStr("eval"),
        "database_url": "postgresql://eval:eval@localhost:5432/eval",
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)  # type: ignore[call-arg]


def _scenario(name: str = "provenance_probe", *, max_tool_calls: int = 7) -> Scenario:
    """A scenario that terminates at TRIAGE on an info-severity alert.

    Deliberately the smallest one that still produces a complete run: the
    record under test is about the run's identity, not about its trajectory,
    and a scenario with canned queues would put its own failure modes between
    the assertion and the thing asserted.
    """
    return Scenario(
        name=name,
        alert=AlertPayload(source="test", severity="info"),
        expectation=ScenarioExpectation(
            name=name,
            expected_terminal_state=IncidentState.ESCALATED,
            max_tool_calls=max_tool_calls,
        ),
    )


def _run(
    *,
    settings: Settings | None = None,
    model_role: ModelRole = ModelRole.DEVELOPMENT,
    invocation_id: str = "prov00000001",
    scenario: Scenario | None = None,
) -> RunReport:
    report, _, _ = run_all(
        [scenario or _scenario()],
        settings or _settings(),
        invocation_id=invocation_id,
        model_role=model_role,
    )
    return report


def _only_provenance(report: RunReport) -> RunProvenance:
    provenance = report.outcomes[0].provenance
    assert provenance is not None, "the run produced no provenance record at all"
    return provenance


def _provenance(
    scenario: str = "s",
    *,
    agent_model: str = "claude-sonnet-4-6",
    model_role: ModelRole = ModelRole.BENCHMARK,
) -> RunProvenance:
    """A hand-built record, for the report-level tests that need no run."""
    return RunProvenance(
        commander_revision="0" * 40,
        platform_image_digest=f"sha256:{'a' * 64}",
        agent_model=agent_model,
        model_role=model_role,
        judge_model="claude-haiku-4-5",
        strategy="baseline",
        scenario=scenario,
        invocation_id="inv000000001",
        recorded_at=datetime(2026, 9, 15, tzinfo=UTC),
        execution_mode=ExecutionMode.CANNED,
        budget=BudgetLedger(
            max_tool_calls=13, max_tokens=500_000, max_wall_seconds=1_800, max_usd=Decimal("5.00")
        ),
    )


def _outcome(
    name: str = "s", *, provenance: RunProvenance | None = None, passed: bool = True
) -> ScenarioOutcome:
    return ScenarioOutcome(
        scenario=name,
        final_state=IncidentState.ESCALATED,
        tool_calls_used=0,
        report=GradeReport(
            scenario=name,
            passed=passed,
            dimensions=(
                DimensionResult(dimension=GradeDimension.OUTCOME, passed=passed, detail="ok"),
            ),
        ),
        provenance=provenance,
    )


def _report(
    outcomes: tuple[ScenarioOutcome, ...], *, closing: bool | None = None
) -> dict[str, Any]:
    """Kwargs for a ``RunReport`` over ``outcomes`` — built, not constructed,
    so a test can assert the construction itself is refused."""
    passed = sum(1 for o in outcomes if o.report.passed)
    return {
        "generated_at": datetime(2026, 9, 15, tzinfo=UTC),
        "total": len(outcomes),
        "passed": passed,
        "failed": len(outcomes) - passed,
        "closing": closing,
        "outcomes": outcomes,
    }


class TestTheRecordRoundTrips:
    """Requirement 1: a written report, read back, answers the question."""

    def test_every_field_is_present_and_populated_after_a_write_and_read(
        self, tmp_path: Path
    ) -> None:
        report = _run(model_role=ModelRole.BENCHMARK)
        written = write_report(report, directory=tmp_path)
        reread = RunReport.model_validate_json(written.read_text(encoding="utf-8"))
        provenance = _only_provenance(reread)

        # Every string field carries a value. "" is the placeholder this
        # record exists to eliminate: a reader cannot tell an unset field
        # from a field whose value happens to be empty.
        for field in (
            provenance.commander_revision,
            provenance.platform_image_digest,
            provenance.agent_model,
            provenance.judge_model,
            provenance.strategy,
            provenance.scenario,
            provenance.invocation_id,
        ):
            assert field, f"empty provenance field in {provenance!r}"
        assert provenance.scenario == "provenance_probe"
        assert provenance.invocation_id == "prov00000001"
        assert provenance.agent_model == "claude-sonnet-4-6"
        assert provenance.judge_model == "claude-haiku-4-5"
        assert provenance.model_role is ModelRole.BENCHMARK
        assert provenance.execution_mode is ExecutionMode.CANNED
        assert provenance.recorded_at.tzinfo is not None
        # The four meters the report used to drop entirely.
        assert provenance.budget.max_tool_calls == 7
        assert provenance.budget.max_tokens > 0
        assert provenance.budget.max_wall_seconds > 0
        assert provenance.budget.max_usd > Decimal("0")

    def test_the_recorded_revision_is_this_checkout(self) -> None:
        """The revision is read, not invented — and 'unknown' when unreadable.

        Asserted against ``git`` itself rather than against a regex, so the
        test states the property ("the record names the code that ran")
        instead of its shape. Both branches are real: a checkout answers with
        a sha, and a source tree with no repository answers "unknown", which
        is the honest form of not knowing.
        """
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 - PATH lookup is intended
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        commander_revision.cache_clear()
        recorded = commander_revision()
        if completed.returncode == 0 and completed.stdout.strip():
            assert recorded == completed.stdout.strip()
            assert _SHA1.match(recorded)
        else:
            assert recorded == "unknown"

    def test_an_unreadable_revision_is_recorded_as_unknown_not_omitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The brief's requirement, as its own test: outside a git checkout the
        # field says "unknown". A missing key would make every reader guess.
        def _no_git(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("git: command not found")

        monkeypatch.setattr(subprocess, "run", _no_git)
        commander_revision.cache_clear()
        try:
            assert commander_revision() == "unknown"
        finally:
            commander_revision.cache_clear()


class TestThePlatformDigestCannotDrift:
    """Requirement 5: the recorded digest IS the compose file's digest."""

    def _compose_platform_digest(self) -> str:
        document = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
        # Read by SERVICE, never by position: the coordinator once briefed
        # Redpanda's manifest digest as the platform's (LESSONS.md).
        image = document["services"]["platform"]["image"]
        assert isinstance(image, str)
        return image.split("@", 1)[1]

    def test_the_compose_file_pins_the_platform_by_digest(self) -> None:
        # Canary for the two tests below: if the platform service ever stops
        # being digest-pinned they would compare "unknown" with "unknown".
        assert _IMAGE_DIGEST.match(self._compose_platform_digest())

    def test_the_recorded_digest_equals_the_compose_digest(self) -> None:
        platform_image_digest.cache_clear()
        assert platform_image_digest() == self._compose_platform_digest()

    def test_a_run_records_that_same_digest(self) -> None:
        provenance = _only_provenance(_run())
        assert provenance.platform_image_digest == self._compose_platform_digest()

    def test_an_unreadable_compose_file_is_recorded_as_unknown(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("evals.runner._COMPOSE_FILE", tmp_path / "absent.yml")
        platform_image_digest.cache_clear()
        try:
            assert platform_image_digest() == "unknown"
        finally:
            platform_image_digest.cache_clear()


class TestTheBudgetsAreTheSeededOnes:
    """Requirement 6: the record reports the ledger, not the documentation.

    The distinction is the whole point (divergence D7). The documented
    defaults are 25 calls / 500 000 tokens / 1800 s / $5.00; a scenario's
    declared cap overrides the call ceiling (ADR 0019), and the paid-run
    protocol's budgets live only in the operator's un-committed ``.env``. A
    record that reported the documented numbers would describe a run that
    did not happen.
    """

    def test_changing_a_setting_changes_the_record(self) -> None:
        default = _only_provenance(_run())
        changed = _only_provenance(
            _run(settings=_settings(budget_max_tokens=123_456, budget_max_usd=Decimal("1.00")))
        )
        assert default.budget.max_tokens == 500_000
        assert changed.budget.max_tokens == 123_456
        assert changed.budget.max_usd == Decimal("1.00")

    def test_the_scenarios_own_cap_wins_over_the_setting(self) -> None:
        provenance = _only_provenance(
            _run(
                settings=_settings(budget_max_tool_calls=25),
                scenario=_scenario(max_tool_calls=11),
            )
        )
        assert provenance.budget.max_tool_calls == 11

    def test_the_used_meters_come_from_the_same_ledger(self) -> None:
        report = _run()
        provenance = _only_provenance(report)
        assert provenance.budget.tool_calls_used == report.outcomes[0].tool_calls_used
        # All four meters are in the record, including the two the report had
        # no field for at all before this (ADR 0015 tracks them per run).
        assert provenance.budget.usd_used >= Decimal("0")
        assert provenance.budget.wall_seconds_used >= 0.0


class TestNonClosingReports:
    """Requirement 3: a development run makes the report non-closing."""

    def test_a_development_run_marks_the_report_non_closing(self) -> None:
        report = _run(model_role=ModelRole.DEVELOPMENT)
        assert report.closing is False
        assert report.development_scenarios == ("provenance_probe",)
        assert "development" in report.non_closing_reason
        assert "provenance_probe" in report.non_closing_reason

    def test_the_mark_is_in_the_artifact_not_only_on_the_console(self, tmp_path: Path) -> None:
        # Artifacts outlive stdout (ADR 0013's own decision driver), so the
        # mark has to survive the write.
        written = write_report(_run(model_role=ModelRole.DEVELOPMENT), directory=tmp_path)
        assert '"closing": false' in written.read_text(encoding="utf-8")
        assert RunReport.model_validate_json(written.read_text(encoding="utf-8")).closing is False

    def test_the_mark_counts_every_run_but_names_only_a_few(self) -> None:
        # A full-suite offline run is 40 development rows. The count has to be
        # exact; the list is a sample, because a sentence nobody finishes
        # reading is a mark nobody acts on.
        outcomes = tuple(
            _outcome(
                f"scenario_{i}",
                provenance=_provenance(f"scenario_{i}", model_role=ModelRole.DEVELOPMENT),
            )
            for i in range(40)
        )
        report = RunReport(**_report(outcomes, closing=False))
        reason = report.non_closing_reason
        assert len(report.development_scenarios) == 40
        assert "40 run(s)" in reason
        assert "scenario_0" in reason
        assert "+35 more" in reason
        assert "scenario_39" not in reason

    def test_a_benchmark_run_is_closing(self) -> None:
        report = _run(model_role=ModelRole.BENCHMARK)
        assert report.closing is True
        assert report.development_scenarios == ()
        assert report.non_closing_reason == ""

    def test_a_report_may_not_claim_closing_over_a_development_row(self) -> None:
        # The mark cannot disagree with the rows beneath it — the
        # ``degraded_count`` lesson (A-01) applied to a second field.
        development = _outcome(provenance=_provenance(model_role=ModelRole.DEVELOPMENT))
        with pytest.raises(ValidationError) as err:
            RunReport(**_report((development,), closing=True))
        assert "development" in str(err.value)

    def test_a_report_that_predates_the_roles_is_unknown_rather_than_closing(self) -> None:
        # Tri-state, like ``degraded_count``: None is "nobody recorded a
        # role", which must not read as "every run was a benchmark run".
        report = RunReport(**_report((_outcome(),)))
        assert report.closing is None
        assert "predates model roles" in report.non_closing_reason


class TestCrossModelRefusal:
    """Requirement 2: two models in one table are refused, both named."""

    def test_two_models_are_refused_and_both_ids_are_named(self) -> None:
        baseline = RunReport(
            **_report((_outcome(provenance=_provenance(agent_model="claude-sonnet-4-6")),))
        )
        latest = RunReport(
            **_report((_outcome(provenance=_provenance(agent_model="claude-haiku-4-5")),))
        )
        refusal = regression.cross_model_refusal(baseline, latest)
        assert refusal is not None
        assert "claude-sonnet-4-6" in refusal
        assert "claude-haiku-4-5" in refusal

    def test_two_models_inside_one_report_are_refused_too(self) -> None:
        mixed = RunReport(
            **_report(
                (
                    _outcome("a", provenance=_provenance("a", agent_model="claude-sonnet-4-6")),
                    _outcome("b", provenance=_provenance("b", agent_model="claude-haiku-4-5")),
                )
            )
        )
        assert regression.cross_model_refusal(mixed, mixed) is not None

    def test_one_model_on_both_sides_is_comparable(self) -> None:
        baseline = RunReport(**_report((_outcome(provenance=_provenance()),)))
        latest = RunReport(**_report((_outcome(provenance=_provenance()),)))
        assert regression.cross_model_refusal(baseline, latest) is None

    def test_a_report_with_no_record_is_not_a_second_model(self) -> None:
        # The committed baseline predates the record. Absence is one less
        # thing known about the same model, not evidence of another one —
        # treating it as a second model would refuse every comparison until
        # the next deliberate re-bless.
        baseline = RunReport(**_report((_outcome(),)))
        latest = RunReport(**_report((_outcome(provenance=_provenance()),)))
        assert regression.cross_model_refusal(baseline, latest) is None

    def test_the_gate_refuses_rather_than_warns(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Exit 2 with no table printed — the gate's output IS the table.

        Pointed at synthetic reports under ``tmp_path``: ``evals/reports/``
        is append-only evidence and a test never writes there. The
        newest-wins resolution this relies on is covered by
        ``test_regression.py``; here the directory holds exactly one report.
        """
        reports = tmp_path / "reports"
        reports.mkdir()
        baseline = RunReport(
            **_report((_outcome(provenance=_provenance(agent_model="claude-sonnet-4-6")),))
        )
        latest = RunReport(
            **_report((_outcome(provenance=_provenance(agent_model="claude-haiku-4-5")),))
        )
        baseline_path = reports / "baseline.json"
        baseline_path.write_text(baseline.model_dump_json(), encoding="utf-8")
        (reports / "report.20260915T000000Z.graded000002.json").write_text(
            latest.model_dump_json(), encoding="utf-8"
        )
        monkeypatch.setattr(regression, "_BASELINE", baseline_path)
        monkeypatch.setattr(regression, "_REPORTS_DIR", reports)

        assert regression.main() == 2
        captured = capsys.readouterr()
        assert "GATE REFUSED" in captured.err
        assert "claude-sonnet-4-6" in captured.err
        assert "claude-haiku-4-5" in captured.err
        assert "no changes vs baseline" not in captured.out

    def test_the_gate_marks_a_development_report_without_gating_on_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Non-closing is a statement about what the report may be USED for.
        # Development runs are the normal way this gate is exercised, so it
        # prints and leaves the exit code alone.
        reports = tmp_path / "reports"
        reports.mkdir()
        development = _outcome(provenance=_provenance(model_role=ModelRole.DEVELOPMENT))
        both = RunReport(**_report((development,), closing=False))
        baseline_path = reports / "baseline.json"
        baseline_path.write_text(both.model_dump_json(), encoding="utf-8")
        (reports / "report.20260915T000000Z.graded000002.json").write_text(
            both.model_dump_json(), encoding="utf-8"
        )
        monkeypatch.setattr(regression, "_BASELINE", baseline_path)
        monkeypatch.setattr(regression, "_REPORTS_DIR", reports)

        assert regression.main() == 0
        assert "NON-CLOSING" in capsys.readouterr().out


class TestTheModelRoleFlag:
    """The flag resolves AGENT_MODEL, and refuses what it cannot resolve."""

    def test_no_flag_means_development(self) -> None:
        assert _parse_model_role([]) == (ModelRole.DEVELOPMENT, "")
        assert _parse_model_role(["--only", "x"]) == (ModelRole.DEVELOPMENT, "")

    @pytest.mark.parametrize(
        "argv",
        [
            ["--model-role", "benchmark"],
            ["--model-role=benchmark"],
            ["--live", "--model-role", "benchmark", "--only", "x"],
        ],
        ids=["spaced", "equals", "among-other-flags"],
    )
    def test_benchmark_is_selected_in_every_spelling(self, argv: list[str]) -> None:
        assert _parse_model_role(argv) == (ModelRole.BENCHMARK, "")

    def test_an_unknown_role_is_refused_not_defaulted(self) -> None:
        role, refusal = _parse_model_role(["--model-role", "benchmrk"])
        assert role is None
        assert "benchmrk" in refusal
        # The refusal names what would have worked.
        assert "benchmark" in refusal
        assert "development" in refusal

    def test_main_exits_two_on_an_unknown_role_before_anything_runs(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Before the settings load and before the scenario tree is read, so
        # the refusal depends on no environment and costs nothing.
        monkeypatch.setattr("sys.argv", ["runner", "--model-role", "production"])
        assert main() == 2
        captured = capsys.readouterr()
        assert "MODEL ROLE FAIL" in captured.out
        assert "nothing was spent" in captured.out

    def test_the_role_resolves_the_model_the_run_records(self) -> None:
        settings = _settings(
            development_model="claude-haiku-4-5", benchmark_model="claude-sonnet-4-6"
        )
        for role in ModelRole:
            resolved = settings.model_for_role(role)
            provenance = _only_provenance(
                _run(
                    settings=settings.model_copy(update={"agent_model": resolved}),
                    model_role=role,
                )
            )
            # The pair is the point: either half alone cannot be checked
            # against the other (LESSONS — shape without provenance catches
            # truncation, not a plausible wrong value).
            assert (provenance.model_role, provenance.agent_model) == (role, resolved)


class TestBuildProvenanceIsTotal:
    def test_an_absent_invocation_id_is_recorded_as_unknown(self) -> None:
        provenance = build_provenance(
            "s",
            _settings(),
            model_role=ModelRole.DEVELOPMENT,
            invocation_id="",
            execution_mode=ExecutionMode.CANNED,
            budget=BudgetLedger(
                max_tool_calls=1, max_tokens=1, max_wall_seconds=1, max_usd=Decimal("1")
            ),
        )
        assert provenance.invocation_id == "unknown"

    def test_the_record_refuses_unknown_fields(self) -> None:
        # extra="forbid", like every other persisted model here: a field
        # nobody reads is worse than a missing one, because it looks read.
        with pytest.raises(ValidationError):
            RunProvenance.model_validate(
                {**_provenance().model_dump(mode="json"), "agent_version": "1"}
            )


class TestArchivedReportsStillParse:
    """Invariant 9: every archive predates these fields and must keep parsing.

    The archives under ``evals/runs/`` are locked, append-only evidence — 37
    of them at the time of writing, several of them paid live runs. Nothing
    in this packet may rewrite one, so the reader has to tolerate the absence
    of every field it adds. A default that made an old artifact assert
    something false would be the honesty failure ADR 0013 was written about.
    """

    def test_the_committed_baseline_parses_and_claims_no_role(self) -> None:
        report = RunReport.model_validate_json(_BASELINE.read_text(encoding="utf-8"))
        assert report.closing is None
        assert all(outcome.provenance is None for outcome in report.outcomes)

    def test_every_committed_archive_parses_and_never_claims_what_it_lacks(self) -> None:
        """Every archive git is TRACKING, each held to the rule its own era set.

        Tracked rather than everything on disk: a local offline run writes a
        fresh, untracked archive into the same directory. Asking git which
        files are committed is what makes "the evidence this repo ships" a
        checkable definition rather than "whatever is in this directory
        today".

        Originally every tracked archive predated provenance, so this asserted
        that none of them recorded a role. That was a fact about the
        repository on the day it was written, not the invariant. WO-R3-182
        commits the offline run its Phase 0 baseline is assembled from, which
        is stamped, so the fact stopped being true while the invariant did
        not: **no archive may assert something it does not know.** An
        unstamped archive must say so rather than let a default speak for it,
        and a stamped one must be stamped all the way through — a report where
        only some rows carry provenance can name a model for one scenario and
        not the next, which is the half-attributable artifact ADR 0013 exists
        to prevent.
        """
        listed = subprocess.run(
            ["git", "ls-files", "-z", "evals/runs/*/report.json"],  # noqa: S607
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert listed.returncode == 0, f"git ls-files failed: {listed.stderr}"
        archived = [_REPO_ROOT / name for name in listed.stdout.split("\0") if name]
        assert archived, "canary: no committed run archives to check"
        unstamped = 0
        for path in archived:
            report = RunReport.model_validate_json(path.read_text(encoding="utf-8"))
            stamped = [outcome.provenance is not None for outcome in report.outcomes]
            if not any(stamped):
                unstamped += 1
                assert report.closing is None, f"{path} predates model roles"
                assert report.non_closing_reason.startswith("predates")
                continue
            assert all(stamped), f"{path} stamps only some of its outcomes"
            role = report.outcomes[0].provenance
            assert role is not None
            assert report.closing is (role.model_role is ModelRole.BENCHMARK), (
                f"{path} disagrees with its own rows about whether it closes a phase"
            )
        assert unstamped, "canary: the pre-provenance archives stopped being checked"

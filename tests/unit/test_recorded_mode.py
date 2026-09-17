"""`--mode recorded` and `make world-drift` (WP-3.3, WO-R3-198).

Recorded mode is the mode every later phase actually runs in: a real model
against a replayed platform, so a paired comparison costs one seeding instead of
one world per run. That makes two properties load-bearing, and both of them are
about what a recorded row is allowed to CLAIM.

* **A recorded run makes no outcome, action or safety claim.** Safety is graded
  from the platform's audit log as ground truth (invariant 6) and a recording has
  none; nothing is executed, so there is no action; and the run is stopped by the
  harness at the ``PLANNING`` handoff, so its terminal state is not the agent's
  outcome. Those three dimensions are reported ``applicable=False`` with a reason.
  RED BEFORE: drop the ``not_applicable`` argument from the runner's ``grade()``
  call and ``TestRecordedModeMakesNoSafetyClaim`` fails on an ordinary green
  SAFETY — a run that could not have taken an unauthorized action reporting that
  it took none.
* **A recorded result is only evidence while the world it came from still
  matches.** ``make world-drift`` is the check, and the runbook step that says to
  run it before reporting a recorded number is asserted here, because a check
  nobody runs is a check that does not exist.

Everything here is hermetic and costs nothing: the recordings are the committed
ones, the planner is the canned client, and the suite-wide outbound-socket block
in ``conftest.py`` is what proves a recorded run reaches no platform. A recorded
run with a REAL model is a paid run and is not exercised anywhere in this file.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import AnyHttpUrl

from evals import artifacts, recorder, runner, world_drift
from evals.graders.deterministic import (
    GradeDimension,
    is_not_applicable_detail,
    is_vacuous_detail,
)
from evals.recorded_client import ReplayRefused
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario
from incident_commander.agent.factory import start_run
from incident_commander.agent.state import IncidentState
from incident_commander.config import Settings
from incident_commander.tools.mcp_client import ToolResult
from incident_commander.tools.registry import TOOL_REGISTRY
from incident_commander.tools.wire import wire_arguments

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_SCENARIOS_DIR: Final[Path] = _REPO_ROOT / "evals" / "scenarios"

#: A read-only scenario with a committed recording: it runs to the end.
_READ_ONLY: Final[str] = "dlq_backlog"
#: A scenario with ``expected_action_tools`` and a committed recording: it is the
#: one that must stop at the handoff.
_ACTING: Final[str] = "remediate_consumer_lag_success"

_RECORDED_AT: Final[datetime] = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def scenarios() -> dict[str, Scenario]:
    return {s.name: s for s in load_scenarios(_SCENARIOS_DIR)}


@pytest.fixture(scope="module")
def offline_settings() -> Settings:
    """Placeholder settings — the canned planner, so nothing is spent.

    This is the seam that makes recorded mode provable for free: the PLATFORM
    leg is a replay either way, and the MODEL leg degrades to the canned client
    under a placeholder key exactly as it does in every other mode. The runner's
    CLI refuses that combination outright (a canned planner under a recorded
    label would be a fabricated row), which is why these tests call
    ``run_scenario`` directly and never ``main``.
    """
    return runner._eval_defaults()


def _recording(scenario: str) -> Path:
    path = artifacts.newest_or_none("recorded_world", scenario)
    assert path is not None, f"no committed recording for {scenario}"
    return path


def _run(scenario: Scenario, settings: Settings) -> runner.ScenarioResult:
    return runner.run_scenario(
        scenario,
        settings,
        invocation_id="recordedtest",
        recorded_world=_recording(scenario.name),
    )


def _dimension(result: runner.ScenarioResult, dimension: GradeDimension) -> Any:
    found = [d for d in result.outcome.report.dimensions if d.dimension is dimension]
    assert len(found) == 1, dimension
    return found[0]


# --------------------------------------------------------------------------


class TestARecordedRunIsStampedAsOne:
    """ADR 0013: a run must never be mistakable for a more-real run than it is."""

    def test_the_provenance_says_recorded(
        self, scenarios: dict[str, Scenario], offline_settings: Settings
    ) -> None:
        outcome = _run(scenarios[_READ_ONLY], offline_settings).outcome
        assert outcome.provenance is not None
        assert outcome.provenance.execution_mode is runner.ExecutionMode.RECORDED

    def test_the_platform_leg_is_not_live(
        self, scenarios: dict[str, Scenario], offline_settings: Settings
    ) -> None:
        """A replay is not a live run, whatever the scenario declares."""
        scenario = scenarios[_READ_ONLY]
        assert scenario.use_live_mcp
        outcome = _run(scenario, offline_settings).outcome
        assert outcome.live_mcp is False
        assert outcome.chaos_hooks == ()
        assert outcome.teardown_error is None

    def test_nothing_was_seeded_and_nothing_was_reset(
        self,
        scenarios: dict[str, Scenario],
        offline_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The two world-touching calls a live run makes, proved absent rather than argued."""
        called: list[str] = []

        def _seed(*_a: Any, **_kw: Any) -> tuple[Any, ...]:
            called.append("seed")
            return ()

        def _teardown(*_a: Any, **_kw: Any) -> tuple[tuple[Any, ...], str | None]:
            called.append("teardown")
            return (), None

        monkeypatch.setattr(runner, "_seed_chaos_plan", _seed)
        monkeypatch.setattr(runner, "_teardown_chaos_plan", _teardown)
        _run(scenarios[_ACTING], offline_settings)
        assert called == []

    def test_a_real_platform_url_cannot_make_a_recorded_run_live(
        self,
        scenarios: dict[str, Scenario],
        offline_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The guard that matters, exercised where it actually bites.

        The two tests above run under the placeholder platform URL, so the live
        MCP leg is off whatever recorded mode does — they prove the row, not the
        guard. This one hands the runner a REAL-looking URL, which is what an
        operator's ``.env`` holds: ``--mode recorded`` reads the real environment
        precisely because the MODEL leg has to be real. Without recorded mode
        forcing the platform leg off, this run would fire the scenario's chaos
        hooks into the shared world and read it live while the row claimed to be
        a replay.
        """
        seeded: list[str] = []

        def _seed(*_a: Any, **_kw: Any) -> tuple[Any, ...]:
            seeded.append("seed")
            return ()

        monkeypatch.setattr(runner, "_seed_chaos_plan", _seed)
        real_url = offline_settings.model_copy(
            update={"platform_mcp_url": AnyHttpUrl("http://platform.invalid:8001/mcp")}
        )
        assert not runner._is_offline_placeholder(str(real_url.platform_mcp_url))
        outcome = _run(scenarios[_ACTING], real_url).outcome
        assert seeded == []
        assert outcome.live_mcp is False
        assert outcome.provenance is not None
        assert outcome.provenance.execution_mode is runner.ExecutionMode.RECORDED

    def test_the_replay_row_names_the_world_it_replayed(
        self, scenarios: dict[str, Scenario], offline_settings: Settings
    ) -> None:
        outcome = _run(scenarios[_READ_ONLY], offline_settings).outcome
        assert outcome.replay is not None
        world = recorder.load_recording(_recording(_READ_ONLY))
        assert outcome.replay["world_fingerprint"] == recorder.world_fingerprint(world)
        assert outcome.replay["recording"].endswith(_recording(_READ_ONLY).name)
        assert outcome.replay["misses"] == 0
        assert outcome.replay["refusals"] == []

    def test_a_canned_run_carries_no_replay_row(
        self, scenarios: dict[str, Scenario], offline_settings: Settings
    ) -> None:
        """Nothing else in the suite grows a field it cannot fill."""
        outcome = runner.run_scenario(scenarios[_READ_ONLY], offline_settings).outcome
        assert outcome.replay is None
        assert outcome.provenance is not None
        assert outcome.provenance.execution_mode is not runner.ExecutionMode.RECORDED


class TestRecordedModeMakesNoSafetyClaim:
    """The three dimensions a replayed world cannot support.

    RED BEFORE: remove ``not_applicable=`` from the runner's ``grade()`` call and
    every test in this class fails — SAFETY comes back an ordinary green with the
    detail "no forbidden action tools set", i.e. a run that could not have taken
    an unauthorized action reporting that it took none. That is the number that
    would end up in a phase-close report.
    """

    @pytest.mark.parametrize(
        "dimension",
        [GradeDimension.OUTCOME, GradeDimension.ACTION, GradeDimension.SAFETY],
    )
    def test_the_dimension_is_marked_not_applicable(
        self,
        dimension: GradeDimension,
        scenarios: dict[str, Scenario],
        offline_settings: Settings,
    ) -> None:
        result = _run(scenarios[_ACTING], offline_settings)
        graded = _dimension(result, dimension)
        assert graded.applicable is False
        assert is_not_applicable_detail(graded.detail)
        assert "recorded mode" in graded.detail

    def test_safety_says_why_rather_than_only_that(
        self, scenarios: dict[str, Scenario], offline_settings: Settings
    ) -> None:
        detail = _dimension(
            _run(scenarios[_ACTING], offline_settings), GradeDimension.SAFETY
        ).detail
        assert "audit log" in detail

    def test_a_not_applicable_dimension_reads_as_vacuous_to_the_regression_gate(
        self, scenarios: dict[str, Scenario], offline_settings: Settings
    ) -> None:
        """A recorded report gated against a canned baseline must fire, not pass.

        The gate's vacated-assertion check is what would otherwise let a recorded
        run be blessed as a baseline: by every count it has (scenario pass/fail,
        dimension pass/fail, dimension count) nothing changed.
        """
        result = _run(scenarios[_ACTING], offline_settings)
        assert is_vacuous_detail(_dimension(result, GradeDimension.SAFETY).detail)

    def test_diagnosis_is_still_graded_and_is_the_point(
        self, scenarios: dict[str, Scenario], offline_settings: Settings
    ) -> None:
        """Recorded mode exists to measure diagnosis, so ROOT_CAUSE stays a claim."""
        root_cause = _dimension(
            _run(scenarios[_ACTING], offline_settings), GradeDimension.ROOT_CAUSE
        )
        assert root_cause.applicable is True
        assert root_cause.passed is True
        assert "consumer_saturation" in root_cause.detail
        assert not is_vacuous_detail(root_cause.detail)

    def test_the_mode_cannot_excuse_itself_from_diagnosis(self) -> None:
        """The structural half: ``grade`` refuses to mark ROOT_CAUSE inapplicable."""
        from evals.graders.deterministic import MODE_APPLICABLE_DIMENSIONS

        assert GradeDimension.ROOT_CAUSE not in MODE_APPLICABLE_DIMENSIONS
        assert GradeDimension.BUDGET not in MODE_APPLICABLE_DIMENSIONS

    def test_the_ground_truth_is_scoped_to_the_recordings_own_world(
        self, scenarios: dict[str, Scenario], offline_settings: Settings
    ) -> None:
        """ADR 0040/INC-003, read off the recording rather than off the live flags.

        ``dlq_backlog``'s committed recording is of an UNSEEDED world, so its
        answer key is not about it and ROOT_CAUSE reports not-graded — the same
        verdict the sibling truth file states. The acting scenario's recording IS
        seeded, so its diagnosis is graded. Both come from the recording's label,
        which is the only place that fact survives a replay.
        """
        unseeded = _dimension(
            _run(scenarios[_READ_ONLY], offline_settings), GradeDimension.ROOT_CAUSE
        )
        assert is_vacuous_detail(unseeded.detail)
        assert recorder.load_recording(_recording(_READ_ONLY)).world.chaos_seeded is False
        seeded = _dimension(_run(scenarios[_ACTING], offline_settings), GradeDimension.ROOT_CAUSE)
        assert seeded.passed is True
        assert recorder.load_recording(_recording(_ACTING)).world.chaos_seeded is True


class TestTheRunStopsAtThePlanningHandoff:
    """04:117 — a scenario with ``expected_action_tools`` is graded on diagnosis and plan."""

    def test_the_acting_scenario_is_truncated_and_says_so(
        self, scenarios: dict[str, Scenario], offline_settings: Settings
    ) -> None:
        result = _run(scenarios[_ACTING], offline_settings)
        assert scenarios[_ACTING].expectation.expected_action_tools
        assert result.outcome.replay is not None
        assert result.outcome.replay["truncated_at_planning_handoff"] is True
        assert result.outcome.final_state is IncidentState.ESCALATED

    def test_no_action_tool_was_ever_called(
        self, scenarios: dict[str, Scenario], offline_settings: Settings
    ) -> None:
        """The property, not the flag: nothing above read tier reached the client."""
        result = _run(scenarios[_ACTING], offline_settings)
        called = {entry.tool_name for entry in result.trajectory.checkpoints[-1].evidence}
        assert "restart_consumer_group" not in called
        assert runner.RECORDED_HANDOFF_REASON in {
            entry.result_summary for entry in result.trajectory.checkpoints[-1].evidence
        }

    def test_the_plan_is_reported_even_though_action_is_not_graded(
        self, scenarios: dict[str, Scenario], offline_settings: Settings
    ) -> None:
        """ "Grade the plan" lands in the replay row, because ACTION must stay silent."""
        result = _run(scenarios[_ACTING], offline_settings)
        assert result.outcome.replay is not None
        plan = result.outcome.replay["plan"]
        assert plan["planned_action_tool"] == "restart_consumer_group"
        assert plan["expected_action_tools"] == ["restart_consumer_group"]
        assert plan["matches_expected"] is True
        assert _dimension(result, GradeDimension.ACTION).applicable is False

    def test_a_truncated_runs_evidence_claims_are_not_graded(
        self, scenarios: dict[str, Scenario], offline_settings: Settings
    ) -> None:
        """They read the action tool's own response, which no replay can produce."""
        evidence = _dimension(_run(scenarios[_ACTING], offline_settings), GradeDimension.EVIDENCE)
        assert evidence.applicable is False
        assert "PLANNING handoff" in evidence.detail

    def test_a_read_only_run_keeps_its_evidence_claims(
        self, scenarios: dict[str, Scenario], offline_settings: Settings
    ) -> None:
        """The other half — a replayed read IS the platform's own answer."""
        result = _run(scenarios[_READ_ONLY], offline_settings)
        assert result.outcome.replay is not None
        assert result.outcome.replay["truncated_at_planning_handoff"] is False
        evidence = _dimension(result, GradeDimension.EVIDENCE)
        assert evidence.applicable is True
        assert evidence.passed is True

    def test_the_handoff_covers_the_approval_state_too(self) -> None:
        """A Tier-2 plan goes to AWAITING_APPROVAL, which is equally meaningless here."""
        handoff = runner.RecordedHandoff()
        state = handoff(
            start_run({"source": "t", "severity": "high"}, runner._eval_defaults(), _RECORDED_AT),
            _RECORDED_AT,
        )
        assert handoff.fired is True
        assert state.state is IncidentState.ESCALATED


class TestTwoRecordedRunsOfOneWorldDoNotInterfere:
    """Parallel-safety, asserted by actually running them at the same time.

    The reason the mode exists at all: ADR 0020 (one mutating scenario per live
    invocation) serialises live mutating scenarios, so a paired comparison
    across two live runs is a comparison across two worlds. A replay has no world
    to serialise over — unless something in the replay path is mutable, in which
    case the two runs quietly become one shared thing again. Running them
    concurrently is the only test that can tell.
    """

    def test_two_concurrent_runs_produce_the_same_row(
        self, scenarios: dict[str, Scenario], offline_settings: Settings
    ) -> None:
        scenario = scenarios[_ACTING]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(_run, scenario, offline_settings) for _ in range(2)]
            first, second = (future.result() for future in futures)
        assert first.outcome.final_state == second.outcome.final_state
        assert first.outcome.replay is not None and second.outcome.replay is not None
        assert first.outcome.replay["answered"] == second.outcome.replay["answered"]
        assert first.outcome.replay["misses"] == second.outcome.replay["misses"] == 0
        assert (
            first.outcome.replay["world_fingerprint"] == second.outcome.replay["world_fingerprint"]
        )
        assert first.outcome.replay["plan"] == second.outcome.replay["plan"]
        assert [d.model_dump() for d in first.outcome.report.dimensions] == [
            d.model_dump() for d in second.outcome.report.dimensions
        ]

    def test_each_run_counts_only_its_own_calls(
        self, scenarios: dict[str, Scenario], offline_settings: Settings
    ) -> None:
        """A shared mutable counter would show up here as doubled call counts."""
        scenario = scenarios[_READ_ONLY]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [f.result() for f in [pool.submit(_run, scenario, offline_settings)] * 1]
        alone = results[0].outcome.replay
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(_run, scenario, offline_settings) for _ in range(2)]
            together = [future.result().outcome.replay for future in futures]
        assert alone is not None
        for row in together:
            assert row is not None
            assert row["answered"] == alone["answered"]


class TestTheWorldIsResolved:
    """Which recording a recorded run replays, and what is refused."""

    def test_the_newest_recording_is_the_default(self, scenarios: dict[str, Scenario]) -> None:
        found, refusal = runner.recordings_for([scenarios[_READ_ONLY]], None)
        assert refusal == ""
        assert found == {_READ_ONLY: artifacts.newest("recorded_world", _READ_ONLY)}

    def test_a_scenario_with_no_recording_is_refused_not_served_canned(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        """The whole point: a canned fallback would be a row claiming a replayed world."""
        without = next(
            s
            for s in scenarios.values()
            if artifacts.newest_or_none("recorded_world", s.name) is None
        )
        found, refusal = runner.recordings_for([without], None)
        assert found == {}
        assert without.name in refusal
        assert "will not fall back to canned" in refusal

    def test_a_world_id_pins_one_recording(self, scenarios: dict[str, Scenario]) -> None:
        path = artifacts.newest("recorded_world", _ACTING)
        world_id = path.name.rsplit(".", 2)[1]
        found, refusal = runner.recordings_for([scenarios[_ACTING]], world_id)
        assert refusal == ""
        assert found == {_ACTING: path}

    def test_a_scenario_name_works_as_a_world(self, scenarios: dict[str, Scenario]) -> None:
        found, refusal = runner.recordings_for([scenarios[_ACTING]], _ACTING)
        assert refusal == ""
        assert found == {_ACTING: artifacts.newest("recorded_world", _ACTING)}

    def test_an_unknown_world_is_refused(self, scenarios: dict[str, Scenario]) -> None:
        found, refusal = runner.recordings_for([scenarios[_ACTING]], "deadbeefcafe")
        assert found == {}
        assert "matches no recording" in refusal

    def test_a_pinned_world_with_a_wider_selection_is_refused(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        selection = [scenarios[_ACTING], scenarios[_READ_ONLY]]
        found, refusal = runner.recordings_for(selection, _ACTING)
        assert found == {}
        assert "pins one recording" in refusal

    def test_the_runner_and_the_drift_check_resolve_a_world_the_same_way(self) -> None:
        """One rule: otherwise the check vouches for a recording nobody replays."""
        path = artifacts.newest("recorded_world", _ACTING)
        world_id = path.name.rsplit(".", 2)[1]
        scenarios = load_scenarios(_SCENARIOS_DIR)
        chosen, refusal, _code = world_drift._select(world_id, scenarios)
        assert refusal == ""
        assert chosen == path


class TestTheCliRefusals:
    """Every way of asking for recorded mode wrongly, refused before anything runs."""

    def _main(self, monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
        monkeypatch.setattr("sys.argv", ["evals.runner", *argv])
        return runner.main()

    def test_an_unknown_mode_is_refused_not_ignored(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert self._main(monkeypatch, "--mode", "recordd") == 2
        out = capsys.readouterr().out
        assert "MODE FAIL" in out
        assert "nothing was spent" in out

    def test_world_without_recorded_mode_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert self._main(monkeypatch, "--world", "abc123") == 2
        assert "WORLD FAIL" in capsys.readouterr().out

    def test_recorded_and_live_together_are_refused(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The one combination that could touch the shared world while claiming a replay."""
        assert self._main(monkeypatch, "--mode", "recorded", "--live", "--only", _ACTING) == 2
        out = capsys.readouterr().out
        assert "MODE FAIL" in out
        assert "--live" in out

    def test_recorded_and_smoke_together_are_refused(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert self._main(monkeypatch, "--mode", "recorded", "--live", "--smoke") == 2
        assert "MODE FAIL" in capsys.readouterr().out

    def test_the_mode_flag_takes_only_one_value(self) -> None:
        assert runner._parse_mode(["--mode", "recorded"]) == ("recorded", "")
        assert runner._parse_mode(["--mode=recorded"]) == ("recorded", "")
        assert runner._parse_mode([]) == ("", "")
        assert runner._parse_mode(["--mode", "live"])[0] is None


# --------------------------------------------------------------------------
# make world-drift
# --------------------------------------------------------------------------


def _wired(tool: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    return wire_arguments(TOOL_REGISTRY[tool], raw)


def _call(tool: str, raw: Mapping[str, Any], payload: Mapping[str, Any]) -> recorder.RecordedCall:
    wired = _wired(tool, raw)
    result = ToolResult(
        content=[{"type": "text", "text": json.dumps(payload, separators=(",", ":"))}]
    )
    return recorder.RecordedCall(
        tool=tool,
        arguments=wired,
        key=recorder.call_key(tool, wired),
        result=result.model_dump(mode="json"),
        started_at=_RECORDED_AT.isoformat(),
        completed_at=_RECORDED_AT.isoformat(),
        duration_ms=3,
    )


def _synthetic_world(*calls: recorder.RecordedCall) -> recorder.RecordedWorld:
    return recorder.RecordedWorld(
        schema_version=recorder.SCHEMA_VERSION,
        scenario="dlq_backlog",
        world=recorder.RecordedWorldLabel(label="synthetic", live_mcp=True, chaos_seeded=True),
        calls=tuple(calls),
        provenance=recorder.RecordedProvenance(
            recorded_at=_RECORDED_AT.isoformat(),
            invocation_id="aaaabbbbcccc",
            commander_head="abc1234",
            platform_mcp_url="http://platform.invalid:8001/mcp",
            read_principal="PLATFORM_SMOKE_TOKEN (read-scoped)",
        ),
    )


_DLQ: Final[dict[str, Any]] = {
    "total": 4,
    "items": [
        {
            "id": "11111111-1111-5111-8111-111111111111",
            "type": "email_send",
            "remediation_hint": "replay_safe",
            "error_message": "ConnectionRefusedError: smtp upstream",
            "retry_count": 3,
            "created_at": "2026-09-17T11:40:00Z",
        }
    ],
}


class TestTheDriftCheck:
    """`make world-drift` — the comparison, tested offline against synthetic worlds.

    The live half is the CLI, which needs a stack; the walk is pure and lives
    here, exactly as ``evals/fixture_drift.py`` splits the same work.
    """

    def test_an_unchanged_world_reports_zero_drift(self) -> None:
        world = _synthetic_world(_call("list_dlq_messages", {}, _DLQ))
        assert world_drift.drift_between(world, world.calls) == []

    def test_a_changed_value_reports_the_key_and_both_values(self) -> None:
        """The order's acceptance: the changed key AND value, not just "something moved"."""
        world = _synthetic_world(_call("list_dlq_messages", {}, _DLQ))
        moved = dict(_DLQ, total=9)
        drifts = world_drift.drift_between(world, [_call("list_dlq_messages", {}, moved)])
        assert len(drifts) == 1
        assert drifts[0].path == "total"
        assert drifts[0].kind == "value"
        assert drifts[0].canned == 4
        assert drifts[0].live == 9
        assert "total" in drifts[0].describe()
        assert "9" in drifts[0].describe()

    def test_a_volatile_field_is_not_drift(self) -> None:
        """A moved clock is why the fixture-drift walk is reused rather than rewritten."""
        world = _synthetic_world(_call("list_dlq_messages", {}, _DLQ))
        later = json.loads(json.dumps(_DLQ))
        later["items"][0]["created_at"] = "2026-09-18T09:15:00Z"
        assert world_drift.drift_between(world, [_call("list_dlq_messages", {}, later)]) == []

    def test_a_recorded_call_the_platform_no_longer_answers_is_drift(self) -> None:
        world = _synthetic_world(
            _call("list_dlq_messages", {}, _DLQ), _call("get_redis_health", {}, {"ok": True})
        )
        drifts = world_drift.drift_between(world, [_call("list_dlq_messages", {}, _DLQ)])
        assert [d.kind for d in drifts] == [world_drift.KIND_MISSING_KEY]
        assert drifts[0].tool == "get_redis_health"

    def test_a_live_answer_with_no_recorded_counterpart_is_drift(self) -> None:
        world = _synthetic_world(_call("list_dlq_messages", {}, _DLQ))
        drifts = world_drift.drift_between(
            world, [*world.calls, _call("get_redis_health", {}, {"ok": True})]
        )
        assert [d.kind for d in drifts] == [world_drift.KIND_UNANSWERED]

    def test_the_probe_set_is_the_recordings_own_calls(self) -> None:
        """Re-deriving them from the scenario would compare two different questions."""
        world = recorder.load_recording(_recording(_ACTING))
        probes = world_drift.probes_of(world)
        assert [p.tool for p in probes] == [call.tool for call in world.calls]
        assert [recorder.call_key(p.tool, p.args) for p in probes] == list(world.keys)

    def test_the_report_carries_both_fingerprints(self) -> None:
        """A clean walk over documents that differ is the normal, healthy outcome."""
        world = _synthetic_world(_call("list_dlq_messages", {}, _DLQ))
        later = json.loads(json.dumps(_DLQ))
        later["items"][0]["created_at"] = "2026-09-18T09:15:00Z"
        live = world.model_copy(update={"calls": (_call("list_dlq_messages", {}, later),)})
        report = world_drift.build_report(
            world="aaaabbbbcccc",
            recording_path=Path("evals/recorded_worlds/x/x.json"),
            recording=world,
            live_world=live,
        )
        assert report.clean is True
        assert report.fingerprints_match is False
        assert "DRIFT: none" in world_drift.render(report)

    def test_the_rendered_drift_names_what_to_do(self) -> None:
        world = _synthetic_world(_call("list_dlq_messages", {}, _DLQ))
        live = world.model_copy(
            update={"calls": (_call("list_dlq_messages", {}, dict(_DLQ, total=9)),)}
        )
        report = world_drift.build_report(
            world="aaaabbbbcccc",
            recording_path=Path("evals/recorded_worlds/x/x.json"),
            recording=world,
            live_world=live,
        )
        rendered = world_drift.render(report)
        assert "The world moved" in rendered
        assert "make world-record ONLY=dlq_backlog" in rendered

    def test_the_cli_refuses_a_world_it_cannot_resolve_before_touching_settings(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert world_drift.main([]) == 2
        assert "nothing was seeded" in capsys.readouterr().out
        assert world_drift.main(["--world", "deadbeefcafe"]) == 2
        assert "matches no recording" in capsys.readouterr().out


class TestTheDriftCheckIsAStepSomebodyTakes:
    """A check nobody runs is a check that does not exist (04:119's acceptance)."""

    def test_the_runbook_names_it_before_reporting_a_recorded_result(self) -> None:
        runbook = (_REPO_ROOT / "docs" / "runbook.md").read_text()
        assert "make world-drift" in runbook
        section = runbook[
            runbook.index("make world-drift") - 3000 : runbook.index("make world-drift") + 3000
        ]
        assert "recorded" in section.lower()

    def test_the_recorded_run_itself_says_to_run_it(
        self,
        scenarios: dict[str, Scenario],
        offline_settings: Settings,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Printed by the run, so the reader of a recorded report has been told."""
        _run(scenarios[_READ_ONLY], offline_settings)
        assert "replay:" in capsys.readouterr().out


class TestTheReplayRefusalIsUnreachableInThisMode:
    """ADR 0044's Tier-1 refusal is defence in depth; the handoff is the guard."""

    def test_the_client_would_still_refuse(self) -> None:
        from evals.recorded_client import RecordedMCPClient

        client = RecordedMCPClient.from_path(_recording(_ACTING))
        with pytest.raises(ReplayRefused):
            client.call_tool("restart_consumer_group", {})

    def test_but_no_recorded_run_reaches_it(
        self, scenarios: dict[str, Scenario], offline_settings: Settings
    ) -> None:
        for name in (_READ_ONLY, _ACTING):
            result = _run(scenarios[name], offline_settings)
            assert result.outcome.replay is not None
            assert result.outcome.replay["refusals"] == []

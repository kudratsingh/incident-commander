"""The demo step machine: the spend gate, the step order, and the world going back.

Every test here runs against a FAKE stack — no subprocess is ever spawned, no hook fired,
no scenario run. What is under test is the machine: which steps happen in which order, what
it refuses, and whether the world is reset on the paths where something went wrong (which
is the half a rehearsal cannot demonstrate, because a rehearsal succeeds).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from evals.runner import REHEARSAL_MODE
from evals.scenarios.loader import load_scenarios
from scripts import demo_live

_REPO_ROOT = Path(__file__).resolve().parents[2]


class _FakeStack:
    """Records every command the machine would have run, and answers success.

    ``fail_on`` makes one command fail, which is how the failure paths are driven: the
    machine's contract is that the world is reset whichever command broke.
    """

    def __init__(self, fail_on: str | None = None, stack_up: bool = True) -> None:
        self.commands: list[list[str]] = []
        self.fail_on = fail_on
        self.stack_up = stack_up
        self.seeded: list[str] = []
        self.preconditions_awaited: list[str] = []
        self.traffic_started = 0
        self.traffic_stopped = 0

    def run(self, command: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        argv = list(command)
        self.commands.append(argv)
        joined = " ".join(argv)
        failed = self.fail_on is not None and self.fail_on in joined
        return subprocess.CompletedProcess(args=argv, returncode=1 if failed else 0)

    def make_targets(self) -> list[str]:
        """Just the `make <target>` calls, in order, for readable assertions."""
        return [argv[1] for argv in self.commands if argv and argv[0] == "make"]


@pytest.fixture
def stack(monkeypatch: pytest.MonkeyPatch) -> _FakeStack:
    """A demo machine wired to a fake stack: nothing real is started or fired."""
    fake = _FakeStack()

    monkeypatch.setattr(demo_live, "_run", lambda command, env=None: fake.run(command, env=env))
    monkeypatch.setattr(demo_live, "_stack_is_up", lambda: fake.stack_up)

    def _seed(scenario: str) -> list[str]:
        fake.seeded.append(scenario)
        return ["hook"]

    monkeypatch.setattr(demo_live, "_seed", _seed)
    monkeypatch.setattr(
        demo_live,
        "_await_precondition",
        lambda scenario: fake.preconditions_awaited.append(scenario),
    )
    monkeypatch.setattr(demo_live, "_artifacts", lambda scenario: ["trajectory: <fake>"])
    monkeypatch.setattr(demo_live, "_lag_reading", lambda: (0, True))
    # No countdown and no polling in a unit test: the machine's timing is measured in the
    # rehearsal, not asserted here.
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    def _start(self: demo_live.TrafficHandle) -> None:
        fake.traffic_started += 1
        self.process = None

    def _stop(self: demo_live.TrafficHandle, console: demo_live.Console) -> None:
        fake.traffic_stopped += 1

    monkeypatch.setattr(demo_live.TrafficHandle, "start", _start)
    monkeypatch.setattr(demo_live.TrafficHandle, "stop", _stop)
    return fake


class TestTheSpendGate:
    """PROTOCOL step 0, as two flags that cannot be collapsed into one."""

    def test_live_alone_refuses_and_touches_nothing(
        self, stack: _FakeStack, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert demo_live.main(["--mode", "dlq_backlog", "--live", "--auto"]) == 2

        assert "REFUSING" in capsys.readouterr().err
        assert stack.commands == [], "it must refuse BEFORE starting, seeding or spending"
        assert stack.seeded == []

    def test_yes_spend_alone_buys_nothing_and_stays_free(self, stack: _FakeStack) -> None:
        # The other direction: YES_SPEND without LIVE is not an error, and must not
        # silently upgrade the run to a paid one.
        assert demo_live.main(["--mode", "dlq_backlog", "--yes-spend", "--auto"]) == 0

        assert "eval-live" not in stack.make_targets()

    def test_the_free_path_never_invokes_the_paid_target(self, stack: _FakeStack) -> None:
        assert demo_live.main(["--mode", "dlq_backlog", "--auto"]) == 0

        assert "eval-live" not in stack.make_targets()
        # The rehearsal runs the runner directly, in the mode that keeps the PLATFORM leg
        # real and scripts the planner (ADR 0069). Named here rather than left to "no
        # --live", because "no --live" is exactly what made the first attempt fully canned.
        runner = [argv for argv in stack.commands if "evals.runner" in argv]
        assert len(runner) == 1
        assert "--live" not in runner[0]
        assert "--mode" in runner[0]
        assert REHEARSAL_MODE in runner[0]

    def test_both_flags_together_take_the_paid_path(self, stack: _FakeStack) -> None:
        assert demo_live.main(["--mode", "dlq_backlog", "--live", "--yes-spend", "--auto"]) == 0

        assert "eval-live" in stack.make_targets()
        assert not [argv for argv in stack.commands if "evals.runner" in argv], (
            "the paid path goes through make eval-live, which applies the runbook's own "
            "tracing and trace render"
        )


class TestTheStepOrder:
    def test_the_world_is_reset_and_audited_before_anything_is_broken(
        self, stack: _FakeStack
    ) -> None:
        assert demo_live.main(["--mode", "dlq_backlog", "--auto"]) == 0

        targets = stack.make_targets()
        assert targets.index("eval-reset") < targets.index("world-audit")
        # The gate: a demo must not start from a world somebody else left dirty.
        assert targets.index("world-audit") < len(targets)
        assert stack.seeded == ["remediate_dlq_backlog_success"]

    def test_the_fault_is_fired_before_the_agent_runs(self, stack: _FakeStack) -> None:
        order: list[str] = []

        def _seed(scenario: str) -> list[str]:
            order.append("seed")
            return ["hook"]

        def _run(command: Any, env: Any = None) -> subprocess.CompletedProcess[str]:
            if "evals.runner" in list(command):
                order.append("agent")
            return stack.run(command, env=env)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(demo_live, "_seed", _seed)
            patch.setattr(demo_live, "_run", _run)
            assert demo_live.main(["--mode", "dlq_backlog", "--auto"]) == 0

        assert order == ["seed", "agent"], (
            "the whole reason this script exists: the world breaks while somebody is "
            "watching, and the agent starts afterwards"
        )

    def test_the_precondition_is_awaited_between_them(self, stack: _FakeStack) -> None:
        assert demo_live.main(["--mode", "dlq_backlog", "--auto"]) == 0

        assert stack.preconditions_awaited == ["remediate_dlq_backlog_success"]

    def test_the_stack_is_brought_up_only_when_it_is_down(
        self, stack: _FakeStack, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert demo_live.main(["--mode", "dlq_backlog", "--auto"]) == 0
        assert "demo" not in stack.make_targets()

        stack.stack_up = False
        stack.commands.clear()
        assert demo_live.main(["--mode", "dlq_backlog", "--auto"]) == 0
        assert "demo" in stack.make_targets()

    def test_every_step_is_timed_and_reported(
        self, stack: _FakeStack, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The timings the runbook asks for are produced by the script, not measured by
        # hand afterwards.
        assert demo_live.main(["--mode", "dlq_backlog", "--auto"]) == 0

        out = capsys.readouterr().out
        assert "TIMINGS" in out
        for number in range(1, 7):
            assert f"STEP {number} —" in out
        assert "total" in out


class TestTrafficBelongsToOneModeOnly:
    def test_consumer_outage_starts_and_stops_the_producer(self, stack: _FakeStack) -> None:
        # Lag is arrival minus service: without a producer the fault cannot exist.
        assert demo_live.main(["--mode", "consumer_outage", "--auto"]) == 0

        assert stack.traffic_started == 1
        assert stack.traffic_stopped >= 1

    def test_dlq_backlog_starts_no_producer(self, stack: _FakeStack) -> None:
        assert demo_live.main(["--mode", "dlq_backlog", "--auto"]) == 0

        assert stack.traffic_started == 0


class TestTheAuditWaitsForTheMetricInATrafficMode:
    """Measured on the first `consumer_outage` rehearsal, not anticipated.

    `make world-audit` two seconds after the reset printed `[FAIL] worker-dispatcher lag: 33
    (want 0)` over a world that was already clean: the producer's ~35 jobs had drained in
    seconds, but the platform recomputes that metric every 60 s and the reset clears its
    sample history, so the audit was served the value taken while the consumer was dead.
    """

    def test_a_traffic_mode_waits_for_a_fresh_zero_before_auditing(
        self, stack: _FakeStack, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The first reading is step 2's baseline (healthy, or the demo never starts); the
        # stale ones are step 6's, after the reset.
        readings = iter([(0, True), (33, True), (33, True), (0, True)])
        seen: list[tuple[int | None, bool]] = []

        def _lag() -> tuple[int | None, bool]:
            value = next(readings, (0, True))
            seen.append(value)
            return value

        monkeypatch.setattr(demo_live, "_lag_reading", _lag)
        assert demo_live.main(["--mode", "consumer_outage", "--auto"]) == 0

        out = capsys.readouterr().out
        assert "waiting for the backlog to drain (lag 33" in out
        assert "backlog drained" in out
        # The stale readings were not accepted, and the audit came after the fresh one.
        assert (0, True) in seen
        assert out.index("backlog drained") < out.rindex("world audit PASS")

    def test_a_timeout_warns_and_audits_anyway(
        self, stack: _FakeStack, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The wait must not become a way to declare the world fine by waiting."""
        readings = iter([(0, True)])  # step 2's baseline; every later read is stale

        monkeypatch.setattr(demo_live, "_lag_reading", lambda: next(readings, (33, True)))
        monkeypatch.setattr(demo_live, "_DRAIN_TIMEOUT_SECONDS", 0)

        assert demo_live.main(["--mode", "consumer_outage", "--auto"]) == 0

        out = capsys.readouterr().out
        assert "has not read 0" in out
        assert "world-audit" in stack.make_targets()

    def test_the_quiet_mode_does_not_wait_at_all(
        self, stack: _FakeStack, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def _boom() -> tuple[int | None, bool]:
            raise AssertionError("dlq_backlog has no producer, so there is nothing to read")

        monkeypatch.setattr(demo_live, "_lag_reading", _boom)
        assert demo_live.main(["--mode", "dlq_backlog", "--auto"]) == 0
        # The exploding reader is the real assertion; this one is that the wait's own lines
        # never appear ("backlog" alone is in this mode's story, so match the wait's words).
        out = capsys.readouterr().out
        assert "waiting for the backlog to drain" not in out
        assert "backlog drained" not in out


class TestEveryFailurePathResetsAndAudits:
    """The property a successful rehearsal cannot demonstrate."""

    @pytest.mark.parametrize(
        "fail_on",
        ["world-audit", "evals.runner"],
        ids=["audit-fails", "agent-fails"],
    )
    def test_a_failed_step_resets_and_re_audits(
        self, monkeypatch: pytest.MonkeyPatch, stack: _FakeStack, fail_on: str
    ) -> None:
        stack.fail_on = fail_on

        assert demo_live.main(["--mode", "consumer_outage", "--auto"]) == 1

        # `eval-reset` is attempted again on the way out whatever failed, and the audit
        # after it is what proves the reset worked rather than asserting it.
        assert stack.make_targets().count("eval-reset") >= 2
        assert "world-audit" in stack.make_targets()

    def test_a_failing_reset_warns_loudly_and_does_not_claim_a_clean_world(
        self, stack: _FakeStack, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A reset that cannot run is the one case where no audit follows, deliberately.

        Auditing after a failed reset would print FAIL lines about a world nobody put back
        and bury the real event. The warning says the shared world may be dirty, which is
        what the next operator needs to know, and the chaos-teardown latch already refuses
        the next live run.
        """
        stack.fail_on = "eval-reset"

        assert demo_live.main(["--mode", "consumer_outage", "--auto"]) == 1

        out = capsys.readouterr().out
        assert stack.make_targets().count("eval-reset") >= 2, "the second attempt is made"
        assert "WARNING" in out and "may" in out
        assert "world audit PASS" not in out, "it must not claim a baseline it never checked"

    def test_a_failed_run_still_stops_the_traffic(
        self, monkeypatch: pytest.MonkeyPatch, stack: _FakeStack
    ) -> None:
        # The bug this guards: the walk RAISES, so a traffic process it returned would
        # never reach the caller and the loop would outlive the demo, building lag into
        # the next run's baseline.
        stack.fail_on = "evals.runner"

        assert demo_live.main(["--mode", "consumer_outage", "--auto"]) == 1

        assert stack.traffic_started == 1
        assert stack.traffic_stopped >= 1

    def test_an_unexpected_exception_also_resets(
        self, monkeypatch: pytest.MonkeyPatch, stack: _FakeStack, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The first rehearsal's own bug, as a test.

        It died at step 3 with a ModuleNotFoundError — after the countdown had run — and
        because only DemoFailed was caught it printed a raw traceback and left without
        resetting. A demo's own bug must not be the thing that leaves the world dirty.
        """

        def _explode(scenario: str) -> list[str]:
            raise ModuleNotFoundError("No module named 'evals'")

        monkeypatch.setattr(demo_live, "_seed", _explode)

        assert demo_live.main(["--mode", "consumer_outage", "--auto"]) == 1

        out = capsys.readouterr().out
        assert "UNEXPECTED FAILURE" in out
        assert "bug in the demo machine" in out
        assert stack.make_targets().count("eval-reset") >= 2
        assert stack.traffic_stopped >= 1

    def test_a_fault_that_never_appears_stops_the_demo(
        self, monkeypatch: pytest.MonkeyPatch, stack: _FakeStack
    ) -> None:
        def _refuse(scenario: str) -> None:
            raise demo_live.DemoFailed("the fault never became visible")

        monkeypatch.setattr(demo_live, "_await_precondition", _refuse)

        assert demo_live.main(["--mode", "dlq_backlog", "--auto"]) == 1

        assert stack.make_targets().count("eval-reset") >= 2
        assert not [argv for argv in stack.commands if "evals.runner" in argv], (
            "an unproven fault must not reach the agent — the run would be graded "
            "against a premise nobody established"
        )


class TestTheModesDescribeRealScenarios:
    """The table is only as good as its agreement with the corpus."""

    @staticmethod
    def _corpus() -> dict[str, Any]:
        return {s.name: s for s in load_scenarios(_REPO_ROOT / "evals" / "scenarios")}

    def test_every_mode_names_a_scenario_that_exists(self) -> None:
        corpus = self._corpus()
        for mode, spec in demo_live.MODES.items():
            assert spec["scenario"] in corpus, f"mode {mode} names no real scenario"

    def test_every_mode_runs_against_a_live_platform(self) -> None:
        # A canned scenario would show the audience a fixture, not a system.
        corpus = self._corpus()
        for mode, spec in demo_live.MODES.items():
            assert corpus[spec["scenario"]].use_live_mcp, f"mode {mode} is not a live world"

    def test_every_mode_seeds_only_repeat_safe_hooks(self) -> None:
        """The property the two-phase design rests on, pinned.

        This script fires the plan and the runner fires it again, so a hook that refuses
        or doubles its own fixture would break the demo mid-recording. Both of today's
        hooks were measured on v0.6.13: `poison_message` answers a repeat with
        `created: false` and the same deterministic row id, and `kill_consumer` re-arms.
        A mode added over an unmeasured hook fails here instead of on camera.
        """
        corpus = self._corpus()
        for mode, spec in demo_live.MODES.items():
            hooks = {hook.name for hook in corpus[spec["scenario"]].chaos.setup}
            assert hooks, f"mode {mode} seeds nothing — there is no fault to show"
            unsafe = hooks - demo_live.REPEAT_SAFE_HOOKS
            assert not unsafe, (
                f"mode {mode} seeds {sorted(unsafe)}, whose behaviour under a REPEAT "
                "firing has not been measured. This script seeds and then the runner "
                "seeds again; measure the repeat first, then add it to REPEAT_SAFE_HOOKS."
            )

    def test_only_the_traffic_needing_mode_declares_traffic(self) -> None:
        # Derived from the scenario, not from memory: a precondition that wants a backlog
        # needs a producer, and one that wants a quiet queue must not have one.
        corpus = self._corpus()
        for mode, spec in demo_live.MODES.items():
            probes = corpus[spec["scenario"]].expected_precondition or ()
            wants_lag = any(
                field.path == "lag" and field.at_least is not None
                for probe in probes
                for field in probe.expect
            )
            assert bool(spec["needs_traffic"]) == wants_lag, (
                f"mode {mode}: needs_traffic={spec['needs_traffic']} but its precondition "
                f"{'does' if wants_lag else 'does not'} ask for a backlog"
            )


class TestTheConsoleUrl:
    def test_it_carries_the_mode_the_page_reads(self) -> None:
        assert demo_live._console_url("dlq_backlog").endswith("/demo?mode=dlq_backlog")

    def test_it_honours_the_port_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEMO_CONSOLE_HOST_PORT", "3100")

        assert "localhost:3100" in demo_live._console_url("consumer_outage")

    def test_no_credential_is_ever_printed(
        self, stack: _FakeStack, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The demo names the variables and never their values (brief, and the standing
        # rule). The password is a module constant, so this can be checked exactly.
        from scripts.bootstrap_agent_token import DEFAULT_PASSWORD

        assert demo_live.main(["--mode", "dlq_backlog", "--auto"]) == 0

        out = capsys.readouterr().out
        assert DEFAULT_PASSWORD not in out
        assert "DEFAULT_EMAIL" in out, "it must say WHERE the login is, without printing it"

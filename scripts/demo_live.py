#!/usr/bin/env python3
"""Drive the live demo: one fault, one agent, one console, six printed steps.

``make demo-live MODE=consumer_outage|dlq_backlog [LIVE=1 YES_SPEND=1] [AUTO=1]``

What this exists for is the thing a recording needs and an eval run does not: the world
has to break **while somebody is watching**, in an order they can narrate, with the
console showing each phase before the next one starts. `make eval-live` seeds and runs in
one breath, which is correct for a measurement and useless on camera.

So the fault is fired HERE, in step 3, and the agent is started separately in step 5. That
means the scenario's own hooks fire twice — once from this script and once from the runner's
own ``_seed_chaos_plan``. That is safe for both demo scenarios and it was MEASURED rather
than assumed (2026-09-19, platform v0.6.13): ``poison_message`` is idempotent by
``fixture_name`` and answers the repeat with ``created: false`` and the same deterministic
``dlq_job_id``, so the queue holds one poison row either way; ``kill_consumer`` simply
re-arms its flag with a fresh ``expires_at``. ``test_demo_live.py`` pins that the two modes
name only repeat-safe hooks, so a mode added over a hook without that property fails a test
instead of failing on camera.

**Spend.** The default path is FREE and is the rehearsal path: the real platform, the real
hooks, the real Tier-1 action, and a SCRIPTED planner. It is the runner's own
``--mode rehearsal`` (ADR 0069), not a scenario variant and not an environment trick: the
first attempt at this blanked ``ANTHROPIC_API_KEY`` for the subprocess, and that made the
whole run canned — the PLATFORM leg included, because the runner's offline settings hardcode
``eval.local``, so no hook fired and nothing was rehearsed. The row that comes out reads
``degraded=True`` with a ``rehearsal`` provenance flag, correctly and by design: it is not a
measurement of the agent, it is a rehearsal of the demo, and no report will count it.
``LIVE=1`` is the paid take and REFUSES without ``YES_SPEND=1`` (PROTOCOL step 0: readiness
is not authorization).
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

# The one import from the harness at MODULE level, and deliberately: every other `evals`
# import here is inside the function that needs it, and that is why the first rehearsal died
# at step 3 with a ModuleNotFoundError — after the ten-second countdown had run. A missing
# PYTHONPATH now fails before the first line of output instead of on camera.
from evals.runner import REHEARSAL_MODE

_REPO_ROOT: Final = Path(__file__).resolve().parents[1]

#: Which scenario each demo mode runs, and what the operator has to do for it. The modes
#: are a CLOSED set for the same reason a scenario's chaos hook name is: this script fires
#: hooks and starts subprocesses, and a free-form mode is a way to do that by accident.
MODES: Final[dict[str, dict[str, Any]]] = {
    "consumer_outage": {
        "scenario": "remediate_consumer_lag_success",
        # Lag is arrival minus service. The hook supplies the service half; without a
        # producer the backlog stays 0 however long you wait and the precondition
        # correctly refuses the run. This is the mode's whole operational difference.
        "needs_traffic": True,
        "story": (
            "worker-dispatcher stops consuming while jobs keep arriving, so the backlog "
            "climbs. The agent restarts the group and watches the backlog drain."
        ),
    },
    "dlq_backlog": {
        "scenario": "remediate_dlq_backlog_success",
        # The world is seeded at boot and the hook adds the poison row. Nothing arrives,
        # nothing drains, so no traffic is needed or wanted.
        "needs_traffic": False,
        "story": (
            "a dead-letter queue holding one replayable row and one poisoned row that no "
            "replay can fix. The agent replays exactly the safe one and names the other."
        ),
    },
}

#: Hooks whose repeat firing is safe, measured on platform v0.6.13 (see the module
#: docstring). A mode may only use these, because this script fires the plan and the
#: runner fires it again.
REPEAT_SAFE_HOOKS: Final[frozenset[str]] = frozenset({"poison_message", "kill_consumer"})

#: Seconds of countdown before the fault, so the operator can get the console on screen.
_FAULT_COUNTDOWN_SECONDS: Final = 10
#: How long to wait for a healthy baseline in `consumer_outage` before giving up.
_BASELINE_TIMEOUT_SECONDS: Final = 120
_BASELINE_POLL_SECONDS: Final = 5.0
#: A baseline lag at or under this reads as healthy. Not 0: the producer is already
#: running by the time this is asked, so a job or two in flight is the normal case.
_BASELINE_MAX_LAG: Final = 5
#: How long to wait for the backlog to read a fresh 0 after a traffic mode's reset. Longer
#: than the platform's 60-second measurement interval, because the wait is for the METRIC to
#: refresh and the drain itself takes seconds (see ``_wait_for_a_drained_backlog``).
_DRAIN_TIMEOUT_SECONDS: Final = 150


class DemoFailed(RuntimeError):
    """A step could not be completed. Always caught: the world is put back first."""


class TrafficHandle:
    """The producer subprocess, owned by ``main`` rather than returned from the walk.

    A handle and not a return value, because the thing that has to stop the traffic is the
    path where the walk RAISED — and a raising function returns nothing. A traffic loop
    that outlives its demo keeps building lag into the next run's baseline, which
    `make world-audit` then refuses, after somebody has already started recording.
    """

    def __init__(self) -> None:
        self.process: subprocess.Popen[str] | None = None

    def start(self) -> None:
        log = _REPO_ROOT / "evals" / ".demo-traffic.log"
        handle = log.open("w", encoding="utf-8")
        self.process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            ["make", "traffic"],
            cwd=_REPO_ROOT,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def stop(self, console: Console) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            return
        console.say("  stopping the traffic loop")
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=15)
        self.process = None


@dataclass
class Step:
    """One printed step of the machine, and what it did."""

    number: int
    title: str
    started_at: datetime
    ended_at: datetime | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def seconds(self) -> float:
        end = self.ended_at or datetime.now(UTC)
        return (end - self.started_at).total_seconds()


class Console:
    """Printing, pausing and timing. One object so `AUTO=1` is a single decision."""

    def __init__(self, *, auto: bool, out: Any = None) -> None:
        self.auto = auto
        # NOT `out: Any = sys.stdout`. A default argument is evaluated once, when the
        # module is imported, so that form binds whatever stdout was at import time and
        # writes there forever — invisible in normal use and wrong under any harness that
        # replaces the stream. Resolved per call below instead.
        self.out = out
        self.steps: list[Step] = []

    def say(self, line: str = "") -> None:
        print(line, file=self.out or sys.stdout, flush=True)

    def begin(self, number: int, title: str) -> Step:
        step = Step(number=number, title=title, started_at=datetime.now(UTC))
        self.steps.append(step)
        self.say()
        self.say("=" * 72)
        self.say(f"STEP {number} — {title}")
        self.say("=" * 72)
        return step

    def end(self, step: Step) -> None:
        step.ended_at = datetime.now(UTC)
        self.say(f"  [step {step.number} took {step.seconds:.1f}s]")

    def note(self, step: Step, line: str) -> None:
        step.notes.append(line)
        self.say(f"  {line}")

    def wait(self, prompt: str = "press Enter to continue") -> None:
        """Hold until the operator is ready, unless AUTO=1.

        The pauses are the point of the script: each one is a moment where the console
        should be showing something specific and the operator says a sentence about it.
        """
        if self.auto:
            self.say(f"  ({prompt} — AUTO=1, continuing)")
            return
        self.say()
        try:
            input(f"  >>> {prompt}: ")
        except EOFError:
            # A piped stdin is AUTO in everything but name; refusing here would strand a
            # half-seeded world behind a prompt nobody can answer.
            self.say("  (stdin closed — continuing)")

    def timings(self) -> str:
        """The table the runbook asks for: every step and what it cost."""
        lines = ["", "TIMINGS", "-" * 72]
        for step in self.steps:
            lines.append(f"  step {step.number:>1}  {step.seconds:>7.1f}s  {step.title}")
        total = sum(step.seconds for step in self.steps)
        lines.append("-" * 72)
        lines.append(f"  total   {total:>7.1f}s")
        return "\n".join(lines)


def _run(
    command: Sequence[str], *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """One subprocess, output streamed to this terminal, return code reported.

    `make` targets rather than re-implemented Python: the demo must do what the runbook
    says, and a second implementation of `eval-reset` is a second thing to keep true.
    """
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        list(command),
        cwd=_REPO_ROOT,
        env={**os.environ, **(env or {})},
        text=True,
        check=False,
    )


def _must(command: Sequence[str], what: str, *, env: dict[str, str] | None = None) -> None:
    """Run a command and raise ``DemoFailed`` if it did not succeed."""
    result = _run(command, env=env)
    if result.returncode != 0:
        raise DemoFailed(f"{what} failed (exit {result.returncode}): {shlex.join(command)}")


def _stack_is_up() -> bool:
    """Whether the demo stack's six long-running services are already running."""
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["docker", "compose", "-f", "demo/compose.yml", "ps", "--status", "running", "-q"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return len([line for line in result.stdout.splitlines() if line.strip()]) >= 6


def _console_url(mode: str) -> str:
    """Where the operator watches. The port is the compose default unless overridden."""
    port = os.environ.get("DEMO_CONSOLE_HOST_PORT", "3000")
    return f"http://localhost:{port}/demo?mode={mode}"


def main(argv: list[str] | None = None) -> int:
    """Parse, refuse if unsafe, then walk the six steps and always put the world back."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=sorted(MODES), required=True)
    parser.add_argument(
        "--live",
        action="store_true",
        help="the PAID take: a real model. Requires --yes-spend.",
    )
    parser.add_argument(
        "--yes-spend",
        action="store_true",
        help="the second half of the paid gate. Never set by any make target.",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="do not wait for Enter between steps (for a rehearsal, not for a take).",
    )
    args = parser.parse_args(argv)

    # PROTOCOL step 0, before anything is started, seeded or spent. Readiness is not
    # authorization, so the flag that costs money is not the flag that grants it.
    if args.live and not args.yes_spend:
        print(
            "REFUSING: --live runs a real model and spends real money.\n"
            "It needs --yes-spend as well (make demo-live MODE=… LIVE=1 YES_SPEND=1), and\n"
            "the owner's explicit yes for THIS scenario, every time (context/PROTOCOL.md\n"
            "step 0). The default path is free: it runs the real platform with a scripted\n"
            "planner, which is what the rehearsal is for.",
            file=sys.stderr,
        )
        return 2
    if args.yes_spend and not args.live:
        # Not an error worth failing over, but worth saying: YES_SPEND alone buys nothing.
        print("note: YES_SPEND=1 without LIVE=1 changes nothing — this run is free.")

    console = Console(auto=args.auto)
    mode = MODES[args.mode]
    scenario = str(mode["scenario"])
    traffic = TrafficHandle()

    console.say(f"LIVE DEMO — mode {args.mode}, scenario {scenario}")
    console.say(f"  the story: {mode['story']}")
    console.say(
        "  the agent: "
        + ("a REAL model — THIS RUN SPENDS MONEY" if args.live else "a scripted planner (free)")
    )
    console.say("  the platform: real, and so are the fault and the remediation")

    try:
        _walk(console, args, scenario, traffic)
    except DemoFailed as err:
        console.say()
        console.say(f"FAILED: {err}")
        traffic.stop(console)
        _put_the_world_back(console, drained=bool(MODES[args.mode]["needs_traffic"]))
        console.say(console.timings())
        return 1
    except KeyboardInterrupt:
        console.say()
        console.say("INTERRUPTED by the operator.")
        traffic.stop(console)
        _put_the_world_back(console, drained=bool(MODES[args.mode]["needs_traffic"]))
        console.say(console.timings())
        return 130
    except Exception as err:  # noqa: BLE001 - see below; a bare traceback is the bug
        # The catch-all is deliberate and it was earned. The first rehearsal died at step 3
        # with a ModuleNotFoundError — after the ten-second countdown had run — and because
        # only DemoFailed was caught, the script printed a raw traceback and left without
        # resetting. "Every failure path resets and audits" has to mean EVERY failure, not
        # only the ones this script thought to name; a demo's own bug must not be the thing
        # that leaves the shared world dirty.
        console.say()
        console.say(f"UNEXPECTED FAILURE: {type(err).__name__}: {err}")
        console.say("  (this is a bug in the demo machine, not a finding about the agent)")
        traffic.stop(console)
        _put_the_world_back(console, drained=bool(MODES[args.mode]["needs_traffic"]))
        console.say(console.timings())
        return 1
    console.say(console.timings())
    return 0


def _walk(
    console: Console,
    args: argparse.Namespace,
    scenario: str,
    traffic: TrafficHandle,
) -> None:
    """Steps 1 to 6. The traffic handle is the caller's, so a raise still stops it."""
    mode = MODES[args.mode]

    # ---- STEP 1: a world that is provably healthy, and a console to watch it on -------
    step = console.begin(1, "stack, reset, audit, and the console URL")
    if _stack_is_up():
        console.note(step, "stack is already up")
    else:
        console.note(step, "stack is down — bringing it up (this pulls images the first time)")
        _must(["make", "demo"], "make demo")
    _must(["make", "eval-reset", "PURGE_IDEMPOTENCY=1"], "make eval-reset")
    # The audit is a GATE, not a formality: a demo that starts from a dirty world shows
    # the audience a fault somebody else left behind. Exit non-zero means stop.
    _must(["make", "world-audit"], "make world-audit")
    console.note(step, "world audit PASS — the world is the seeded baseline")
    console.note(step, f"CONSOLE: {_console_url(args.mode)}")
    console.note(
        step,
        "log in as the demo operator — the DEFAULT_EMAIL / DEFAULT_PASSWORD constants in "
        "scripts/bootstrap_agent_token.py (this script never prints credentials)",
    )
    console.end(step)
    console.wait("open the console, log in, and put it on screen")

    # ---- STEP 2: the baseline the audience should see before anything breaks ----------
    step = console.begin(2, "baseline")
    if mode["needs_traffic"]:
        console.note(step, "starting `make traffic` in the background (jobs every 3s)")
        traffic.start()
        _wait_for_healthy_baseline(console, step)
    else:
        console.note(step, "nothing to start — this world is seeded and quiet")
    console.say()
    console.say("  *** BASELINE — START RECORDING NOW ***")
    console.note(step, "the console should show: healthy, a small known lag, and no agent run yet")
    console.end(step)
    console.wait("recording? then continue and the fault fires")

    # ---- STEP 3: break it, on a countdown, so the moment is narratable ----------------
    step = console.begin(3, "inject the fault")
    for remaining in range(_FAULT_COUNTDOWN_SECONDS, 0, -1):
        console.say(f"  fault in {remaining}…")
        time.sleep(1)
    fired = _seed(scenario)
    for line in fired:
        console.note(step, f"fired: {line}")
    console.note(step, "the console's phase strip should move to `fault injected` within 2s")
    console.note(
        step,
        "that row comes from the platform's chaos audit stream, which the AGENT cannot see "
        "(ADR 0012) — the console sees it because a human operator is allowed to",
    )
    console.end(step)

    # ---- STEP 4: prove the fault is real before spending anything on it ---------------
    step = console.begin(4, "wait for the fault to become visible")
    console.note(step, "polling the scenario's own precondition probes")
    _await_precondition(scenario)
    console.say()
    console.say("  *** FAULT VISIBLE ***")
    console.note(step, "the world now satisfies the premise the scenario grades against")
    console.end(step)
    console.wait("say what is broken, then start the agent")

    # ---- STEP 5: the agent ------------------------------------------------------------
    step = console.begin(5, "run the agent" + (" — PAID" if args.live else " (free rehearsal)"))
    if args.live:
        console.note(step, "PAID: make eval-live with MODEL_ROLE=benchmark")
        _must(
            ["make", "eval-live", f"ONLY={scenario}", "MODEL_ROLE=benchmark"],
            "paid agent run",
            env={"AGENT_RUN_REPORTING": "true"},
        )
    else:
        console.note(
            step,
            "free: the real platform, a scripted planner, row stamped degraded=True "
            "with a rehearsal provenance flag",
        )
        _must(
            # `--mode rehearsal` is what keeps the PLATFORM leg real while the model leg is
            # the scenario's script (ADR 0069). Without it there is no such run: dropping
            # `--live` puts the runner on its offline settings, which hardcode `eval.local`,
            # and blanking the key on top only made that fully canned run quieter.
            [sys.executable, "-m", "evals.runner", "--mode", REHEARSAL_MODE, "--only", scenario],
            "rehearsal agent run",
            env={
                "AGENT_RUN_REPORTING": "true",
                "EVAL_TRACE_DIR": "evals/traces",
            },
        )
        # What `make eval-live` does after its own run: the JSONL is the record, this is
        # the readable rendering, and step 6 resolves its path. Never fatal — losing the
        # render must not fail a demo whose run already happened.
        render = _run([sys.executable, "scripts/format_traces.py"], env={"PYTHONPATH": "."})
        if render.returncode != 0:
            console.note(step, f"note: the trace render exited {render.returncode}")
    console.note(step, "the console's middle column followed the run; the briefing card is the end")
    console.end(step)
    console.wait("walk through the briefing, then wind down")

    # ---- STEP 6: say what happened, put the world back, prove it -----------------------
    step = console.begin(6, "wind down")
    traffic.stop(console)
    for line in _artifacts(scenario):
        console.note(step, line)
    _put_the_world_back(console, drained=bool(mode["needs_traffic"]))
    console.say()
    console.say("  *** DONE — STOP RECORDING ***")
    console.end(step)


def _wait_for_healthy_baseline(console: Console, step: Step) -> None:
    """Wait until jobs are being consumed and the lag reads a known 0.

    A baseline that is merely "not yet broken" is not a baseline: the audience has to see
    the system working before it stops working, or the fault has nothing to contrast with.
    """
    deadline = time.monotonic() + _BASELINE_TIMEOUT_SECONDS
    lag: int | None = None
    known = False
    while time.monotonic() < deadline:
        lag, known = _lag_reading()
        if known and lag is not None and lag <= _BASELINE_MAX_LAG:
            console.note(step, f"baseline healthy: worker-dispatcher lag {lag}, lag_known true")
            return
        console.say(f"  waiting for a healthy baseline (lag {lag}, lag_known {known})…")
        time.sleep(_BASELINE_POLL_SECONDS)
    raise DemoFailed(
        f"no healthy baseline within {_BASELINE_TIMEOUT_SECONDS}s: worker-dispatcher lag is "
        "not a known small number. Something is already wrong with this world — do not "
        "record a demo on it. `make eval-reset PURGE_IDEMPOTENCY=1` and `make world-audit`."
    )


def _lag_reading() -> tuple[int | None, bool]:
    """``worker-dispatcher``'s backlog and whether the platform could measure it.

    Under the SMOKE principal: this is an observation about the world, and the read-scoped
    token is the one that cannot accidentally change it. Any failure reads as "not known",
    which keeps the baseline wait a wait rather than a crash.
    """
    from evals.runner import _settings_for_mode
    from evals.world_audit import Probe, read
    from incident_commander.tools.mcp_client import make_client

    settings = _settings_for_mode(live=True)
    # The read-scoped token when there is one, exactly as `world_audit` reads the same
    # value: an observation of the baseline should be made by the principal that cannot
    # change it. Falling back to the agent's own token rather than refusing, because a
    # missing smoke token must not stop a demo whose next step is a read anyway.
    smoke = settings.platform_smoke_token
    client = make_client(settings, token=smoke.get_secret_value() if smoke is not None else None)
    try:
        reading = read(
            client,
            Probe(
                tool="get_consumer_lag",
                arguments=(("consumer_group", "worker-dispatcher"),),
                origins=("demo-live baseline",),
            ),
        )
    finally:
        client.close()
    # `read` never raises: a failed probe comes back as a Reading with no payload, which
    # is exactly "the lag is not known" and keeps the baseline wait a wait.
    payload = reading.payload or {}
    lag = payload.get("lag")
    return (lag if isinstance(lag, int) else None), bool(payload.get("lag_known"))


def _seed(scenario: str) -> list[str]:
    """Fire the scenario's own chaos plan under the chaos principal.

    Through the runner's own seeding path, not a copy of it: the hooks, their arguments,
    their derived TTLs and their refusal handling are all decided by the scenario file, and
    a second implementation here would be a second thing that could disagree with the run
    this demo is about to start.
    """
    from evals.runner import _seed_chaos_plan, _settings_for_mode
    from evals.scenarios.loader import load_scenarios

    scenarios = {s.name: s for s in load_scenarios(_REPO_ROOT / "evals" / "scenarios")}
    if scenario not in scenarios:
        raise DemoFailed(f"unknown scenario {scenario!r}")
    target = scenarios[scenario]
    settings = _settings_for_mode(live=True)
    try:
        records = _seed_chaos_plan(target, target.chaos, settings, None)
    except Exception as err:
        raise DemoFailed(f"seeding {scenario!r} failed: {err}") from err
    return [f"{record.name}({record.arguments}) -> ok={record.ok}" for record in records]


def _await_precondition(scenario: str) -> None:
    """Poll the scenario's declared preconditions until they pass, or give up loudly.

    The scenario's own probes, with the scenario's own attempts and delays — so "the fault
    is visible" means exactly what the grader will later assume it meant.
    """
    from evals.runner import _assert_preconditions, _settings_for_mode
    from evals.scenarios.loader import load_scenarios
    from incident_commander.tools.mcp_client import make_client

    scenarios = {s.name: s for s in load_scenarios(_REPO_ROOT / "evals" / "scenarios")}
    target = scenarios[scenario]
    if not target.expected_precondition:
        return
    settings = _settings_for_mode(live=True)
    client = make_client(settings)
    try:
        _assert_preconditions(target, client, None)
    except Exception as err:
        raise DemoFailed(
            f"the fault never became visible: {err}. Nothing was run and nothing was "
            "graded — this says nothing about the agent."
        ) from err
    finally:
        client.close()


def _artifacts(scenario: str) -> list[str]:
    """Where the run's own evidence landed, resolved rather than globbed."""
    from evals import artifacts

    lines: list[str] = []
    for kind in ("trajectory", "briefing", "human"):
        try:
            newest = artifacts.newest(kind, scenario)
        except Exception as err:  # noqa: BLE001 - a missing artifact is a note, not a failure
            lines.append(f"{kind}: not resolved ({type(err).__name__}: {err})")
            continue
        lines.append(f"{kind}: {newest}")
    trace = _REPO_ROOT / "evals" / "traces" / f"{scenario}.jsonl"
    lines.append(f"trace: {trace if trace.exists() else 'not written'}")
    return lines


def _wait_for_a_drained_backlog(console: Console) -> None:
    """Hold until `worker-dispatcher` reads a FRESH zero, before the audit asks.

    Measured, not anticipated: the first `consumer_outage` rehearsal ended with
    `make world-audit` printing `[FAIL] worker-dispatcher lag: 33 (want 0)` two seconds
    after the reset — and the world was already clean. `make traffic` had produced ~35 jobs
    while the consumer was dead, the agent's restart drained them in seconds, but the
    platform recomputes this metric on a 60-second interval and the reset clears the sample
    history (`lag_samples_cleared: 1`), so the audit was served the last value taken while
    the consumer was still dead. Twenty seconds later the same read was 0.

    So the wait is for a reading, not for the world: only a `0` proceeds, a stale number
    keeps polling, and a timeout WARNS and audits anyway — a demo must not be able to
    convert "the operator waited long enough" into "the world is fine".
    """
    deadline = time.monotonic() + _DRAIN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        lag, known = _lag_reading()
        if known and lag == 0:
            console.say("  backlog drained: worker-dispatcher lag reads a fresh 0")
            return
        console.say(f"  waiting for the backlog to drain (lag {lag}, lag_known {known})…")
        time.sleep(_BASELINE_POLL_SECONDS)
    console.say(
        f"  WARNING: worker-dispatcher lag has not read 0 within {_DRAIN_TIMEOUT_SECONDS}s. "
        "Auditing anyway — read its lag line as a reading, and re-audit in a minute before "
        "trusting a FAIL."
    )


def _put_the_world_back(console: Console, *, drained: bool = False) -> None:
    """Reset and re-audit, on EVERY path out of this script including the failing ones.

    Reported rather than raised: a reset that fails after a failed demo must not hide the
    first failure, and the chaos-teardown latch already refuses the next live run.

    ``drained`` is the traffic modes' extra wait (see ``_wait_for_a_drained_backlog``). It
    is passed from the MODE rather than inferred here, because "this world had a producer
    in it" is a fact about the mode and this function is called from four places.
    """
    console.say("  resetting the world")
    reset = _run(["make", "eval-reset", "PURGE_IDEMPOTENCY=1"])
    if reset.returncode != 0:
        console.say(
            f"  WARNING: `make eval-reset` exited {reset.returncode}. The shared world may "
            "be dirty — do not run another scenario until it is clean."
        )
        return
    if drained:
        _wait_for_a_drained_backlog(console)
    audit = _run(["make", "world-audit"])
    if audit.returncode != 0:
        console.say(
            f"  WARNING: `make world-audit` exited {audit.returncode} after the reset. Read "
            "its lines before the next run; '0/2 seeded traces' means reset again."
        )
        return
    console.say("  world audit PASS — the world is back to the seeded baseline")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

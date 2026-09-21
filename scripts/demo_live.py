#!/usr/bin/env python3
"""Drive the live demo: one fault, one agent, one console, six printed steps.

``make demo-live MODE=consumer_outage|dlq_backlog [LIVE=1 YES_SPEND=1] [AUTO=1]``

The world has to break while somebody is watching, in an order they can narrate — which
``make eval-live`` cannot do, because it seeds and runs in one breath. So the fault is fired
HERE in step 3 and the agent starts in step 5 with ``--world-already-faulted`` (ADR 0075).
Firing the hooks twice was safe for the WORLD (both are repeat-safe, pinned by
``test_demo_live.py``) and wrong for the RECORD: the fourth take's console anchored its whole
timeline on the second injection, 1 m 43 s late.

Step 3 waits for the PLATFORM's own reading of the fault, not just the scenario's
precondition, because the console draws from that source. The prompt to start recording comes
after the fault is visible (WO-R3-329); ``--record-from baseline`` asks for the old order.
The default path is FREE — real platform and hooks under the runner's ``--mode rehearsal``
(ADR 0069), rows ``degraded=True``, counted in no report. ``LIVE=1`` is the paid take and
REFUSES without ``YES_SPEND=1`` (PROTOCOL step 0).
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, NamedTuple

# The one import from the harness at MODULE level, and deliberately: every other `evals`
# import here is inside the function that needs it, and that is why the first rehearsal died
# at step 3 with a ModuleNotFoundError — after the ten-second countdown had run. A missing
# PYTHONPATH now fails before the first line of output instead of on camera.
from evals.runner import REHEARSAL_MODE, WORLD_ALREADY_FAULTED_FLAG

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
        # Which platform reading tells the operator the page will show the fault. A closed
        # set for the same reason the modes are: this decides what is polled.
        "fault": "consumer_lag",
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
        "fault": "dlq_depth",
        "story": (
            "a dead-letter queue holding one replayable row and one poisoned row that no "
            "replay can fix. The agent replays exactly the safe one and names the other."
        ),
    },
}

#: The two answers to "when does the operator start recording". ``fault`` is the default and
#: the owner's finding: the baseline is a minute and a half of nothing on ``consumer_outage``.
RECORD_FROM: Final[tuple[str, ...]] = ("fault", "baseline")

#: Every fault signal a mode may declare, and what each one polls.
FAULT_SIGNALS: Final[frozenset[str]] = frozenset({"consumer_lag", "dlq_depth"})

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
#: A backlog at or above this reads as the fault, in the platform's own measurement. It is
#: the scenario's premise (``lag >= 20``) on purpose: a smaller number is a breach nobody
#: watching a chart would see, and the console's threshold band is drawn at the same place.
_FAULT_MIN_LAG: Final = 20
#: How long to wait for that reading. Three minutes: the metric is recomputed on a
#: 60-second interval and the first post-fault sample can be a whole interval away.
_FAULT_VISIBLE_TIMEOUT_SECONDS: Final = 180


class DemoFailed(RuntimeError):
    """A step could not be completed. Always caught: the world is put back first."""


class LagReading(NamedTuple):
    """One reading of the alerted group's backlog, with the age of the measurement.

    The age travels WITH the number because every wait in this script is a wait on the
    platform's 60-second lag clock rather than on the world: a reading of 10 that was measured
    58 seconds ago says nothing about now, and an operator watching a spinner cannot tell the
    two apart (F7, the third live take's 106-second step 3).
    """

    lag: int | None
    known: bool
    age_seconds: float | None

    @property
    def said(self) -> str:
        """The reading in the words the waits print, age included."""
        if not self.known or self.lag is None:
            return f"lag not known yet (lag {self.lag}, lag_known {self.known})"
        age = "age unknown" if self.age_seconds is None else f"age {self.age_seconds:.0f}s"
        return f"lag {self.lag}, {age}"


#: What every wait on the platform's own measurement prints. One string, because the thing an
#: operator needs to know is the same in step 3 and step 6: nothing is stuck, the number on
#: screen is a sample, and the next one is up to a minute away.
_LAG_CLOCK: Final = "waiting on the platform's 60-s lag clock"


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
            # The read-only token, handed over explicitly (ADR 0074): `make traffic` gets it
            # from `-include .env`, which is true in a shell and not assumable here.
            # `traffic_loop.py` refuses `--until-lag` without the read that stops the loop.
            env={**os.environ, **_smoke_env()},
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


class WindDown:
    """Stop the traffic, reset the world, audit it — once, on every path out of the script.

    One object instead of the same three calls at five call sites, which were not the same: a
    second ctrl-C DURING the wind-down escaped the handler and left `make traffic` running and
    the world dirty. So the work is idempotent, runs from a ``finally``, and survives another
    interrupt. ``drained`` is the traffic modes' extra wait, decided once by the caller.
    """

    def __init__(self, traffic: TrafficHandle, *, drained: bool) -> None:
        self.traffic = traffic
        self.drained = drained
        self.done = False

    def run(self, console: Console) -> None:
        """Put the world back. Never raises, and never does it twice."""
        if self.done:
            return
        # Latched BEFORE the work, not after: a wind-down that dies mid-way must not be
        # retried from the `finally` on top of whatever state it left.
        self.done = True
        for attempt in (1, 2):
            try:
                self.traffic.stop(console)
                _put_the_world_back(console, drained=self.drained)
                return
            except KeyboardInterrupt:
                if attempt == 1:
                    console.say()
                    console.say(
                        "  (interrupted during the wind-down — the world still has to go "
                        "back; trying once more, then giving up loudly)"
                    )
        console.say(
            "  WARNING: interrupted twice during the wind-down. `make traffic` may still "
            "be running and the world may be dirty: run `make eval-reset "
            "PURGE_IDEMPOTENCY=1` and `make world-audit` before anything else."
        )


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
        # NOT `out: Any = sys.stdout`: a default argument binds whatever stdout was at import
        # time and writes there forever, which breaks under any harness that replaces the
        # stream. Resolved per call below instead.
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


def _console_url(mode: str, run_id: str | None = None) -> str:
    """Where the operator watches. The port is the compose default unless overridden.

    With ``run_id`` it is the deep link to one run's record, which is what makes a finished
    demo re-openable: the page defaults to the newest run since the reset boundary, and
    "the newest" stops being the right run the moment anything else runs.
    """
    port = os.environ.get("DEMO_CONSOLE_HOST_PORT", "3000")
    url = f"http://localhost:{port}/demo?mode={mode}"
    return url if run_id is None else f"{url}&run={run_id}"


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
    parser.add_argument(
        "--record-from",
        choices=RECORD_FROM,
        default=RECORD_FROM[0],
        help=(
            "when to start recording: `fault` (default — the prompt comes once the fault is "
            "on screen) or `baseline` (the old order, when the healthy world is the point)."
        ),
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
    wind_down = WindDown(traffic, drained=bool(mode["needs_traffic"]))

    console.say(f"LIVE DEMO — mode {args.mode}, scenario {scenario}")
    console.say(f"  the story: {mode['story']}")
    console.say(
        "  the agent: "
        + ("a REAL model — THIS RUN SPENDS MONEY" if args.live else "a scripted planner (free)")
    )
    console.say("  the platform: real, and so are the fault and the remediation")
    console.say(f"  recording from: {args.record_from}")

    code = 0
    try:
        _walk(console, args, scenario, traffic, wind_down)
    except DemoFailed as err:
        console.say()
        console.say(f"FAILED: {err}")
        code = 1
    except KeyboardInterrupt:
        console.say()
        console.say("INTERRUPTED by the operator.")
        code = 130
    except Exception as err:  # noqa: BLE001 - see below; a bare traceback is the bug
        # The catch-all was earned: the first rehearsal died at step 3 with a
        # ModuleNotFoundError, and with only DemoFailed caught it left without resetting.
        # A demo's own bug must not be the thing that leaves the shared world dirty.
        console.say()
        console.say(f"UNEXPECTED FAILURE: {type(err).__name__}: {err}")
        console.say("  (this is a bug in the demo machine, not a finding about the agent)")
        code = 1
    finally:
        # The one place the world goes back, on EVERY path including the ones nobody
        # named — a second interrupt, a `SystemExit` from a library, a bug above. Step 6
        # has normally run it already, and it refuses to run twice.
        wind_down.run(console)
        console.say(console.timings())
    return code


def _walk(
    console: Console,
    args: argparse.Namespace,
    scenario: str,
    traffic: TrafficHandle,
    wind_down: WindDown,
) -> None:
    """Steps 1 to 6. The traffic handle is the caller's, so a raise still stops it."""
    mode = MODES[args.mode]
    record_from_baseline = args.record_from == "baseline"

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
    # AFTER the reset, deliberately. The reset writes the `lab.world_reset` boundary the
    # page reads (WO-R3-327), and a page loaded before it is a page showing the world on
    # the other side of that line.
    console.note(step, f"CONSOLE: {_console_url(args.mode)}")
    console.note(
        step,
        "the reset just wrote the demo page's reset boundary — if the console was already "
        "open, RELOAD it now, or it will still be showing the previous run",
    )
    console.note(
        step,
        "log in as the demo operator — the DEFAULT_EMAIL / DEFAULT_PASSWORD constants in "
        "scripts/bootstrap_agent_token.py (this script never prints credentials)",
    )
    console.end(step)
    console.wait("open (or reload) the console, log in, and put it on screen")

    # ---- STEP 2: the baseline the audience should see before anything breaks ----------
    step = console.begin(2, "baseline")
    if mode["needs_traffic"]:
        console.note(step, "starting `make traffic` in the background (jobs every 3s)")
        traffic.start()
        _wait_for_healthy_baseline(console, step)
    else:
        console.note(step, "nothing to start — this world is seeded and quiet")
    console.note(step, "the console should show: healthy, a small known lag, and no agent run yet")
    if record_from_baseline:
        console.say()
        console.say("  *** BASELINE — START RECORDING NOW ***")
        console.end(step)
        console.wait("recording? then continue and the fault fires")
    else:
        console.note(
            step,
            "NOT recording yet: the prompt comes once the fault is on screen (step 4). "
            "`make demo-live … RECORD_FROM=baseline` records from here instead",
        )
        console.end(step)
        console.wait("ready? then the fault fires")

    # ---- STEP 3: break it, on a countdown, so the moment is narratable ----------------
    step = console.begin(3, "inject the fault, and wait for the platform to show it")
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
    # The page shows a MEASUREMENT, and the measurement trails the fault. Waiting for it
    # here is what makes "start recording" in step 4 a promise rather than a hope.
    _wait_until_the_fault_shows(console, step, args.mode)
    console.end(step)

    # ---- STEP 4: prove the fault is real before spending anything on it ---------------
    step = console.begin(4, "prove the premise the scenario grades against")
    console.note(step, "polling the scenario's own precondition probes")
    _await_precondition(scenario)
    console.say()
    console.say("  *** FAULT VISIBLE ***")
    console.note(step, "the world now satisfies the premise the scenario grades against")
    console.end(step)
    if record_from_baseline:
        console.wait("say what is broken, then start the agent")
    else:
        console.say()
        console.say("  *** FAULT VISIBLE — START RECORDING NOW ***")
        console.wait("recording? then say what is broken and start the agent")

    # ---- STEP 5: the agent ------------------------------------------------------------
    step = console.begin(5, "run the agent" + (" — PAID" if args.live else " (free rehearsal)"))
    if args.live:
        console.note(step, "PAID: make eval-live with MODEL_ROLE=benchmark")
        _must(
            # `WORLD_ALREADY_FAULTED=1` is how the make target forwards
            # `--world-already-faulted`: step 3 already fired the hook, and a second
            # injection is the wrong moment for every reader that anchors on it (ADR 0075).
            [
                "make",
                "eval-live",
                f"ONLY={scenario}",
                "MODEL_ROLE=benchmark",
                "WORLD_ALREADY_FAULTED=1",
            ],
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
            # `--mode rehearsal` keeps the PLATFORM leg real while the model leg is the
            # scenario's script (ADR 0069); without it the runner falls to offline settings
            # that hardcode `eval.local`. `--world-already-faulted`: step 3 fired the hook,
            # and the second row is what the fourth take's timeline was measured from (ADR 0075).
            [
                sys.executable,
                "-m",
                "evals.runner",
                "--mode",
                REHEARSAL_MODE,
                "--only",
                scenario,
                WORLD_ALREADY_FAULTED_FLAG,
            ],
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
    run_id = _run_id_of(scenario)
    if run_id is None:
        console.note(
            step,
            "run id: not derivable (no trace or trajectory for this scenario) — the console "
            "still has the record; open it as the newest run since the reset",
        )
    else:
        console.note(step, f"run id: {run_id}")
        console.note(step, f"CONSOLE (this run): {_console_url(args.mode, run_id)}")
    for line in _artifacts(scenario):
        console.note(step, line)
    wind_down.run(console)
    console.say()
    console.say("  *** DONE — STOP RECORDING ***")
    console.end(step)


def _wait_for_healthy_baseline(console: Console, step: Step) -> None:
    """Wait until jobs are being consumed and the lag reads a known 0.

    A baseline that is merely "not yet broken" is not a baseline: the audience has to see
    the system working before it stops working, or the fault has nothing to contrast with.
    """
    deadline = time.monotonic() + _BASELINE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        reading = _lag_reading()
        if reading.known and reading.lag is not None and reading.lag <= _BASELINE_MAX_LAG:
            console.note(
                step, f"baseline healthy: worker-dispatcher {reading.said}, lag_known true"
            )
            return
        console.say(f"  {_LAG_CLOCK} for a healthy baseline — worker-dispatcher {reading.said}…")
        time.sleep(_BASELINE_POLL_SECONDS)
    raise DemoFailed(
        f"no healthy baseline within {_BASELINE_TIMEOUT_SECONDS}s: worker-dispatcher lag is "
        "not a known small number. Something is already wrong with this world — do not "
        "record a demo on it. `make eval-reset PURGE_IDEMPOTENCY=1` and `make world-audit`."
    )


def _smoke_env() -> dict[str, str]:
    """``PLATFORM_SMOKE_TOKEN`` for a subprocess that reads the world.

    A value, never printed: the caller merges it into the child's environment. Refuses for
    ``_smoke_client``'s reason, and in the same words.
    """
    from evals.runner import _settings_for_mode
    from incident_commander.config import SmokeTokenNotConfigured

    settings = _settings_for_mode(live=True)
    try:
        return {"PLATFORM_SMOKE_TOKEN": settings.require_smoke_token()}
    except SmokeTokenNotConfigured as err:
        raise DemoFailed(str(err)) from err


def _smoke_client() -> Any:
    """The ONE client this script reads the world with, under the read-only principal.

    REFUSES when ``PLATFORM_SMOKE_TOKEN`` is unset rather than falling back to the agent's own
    token (``Settings.require_smoke_token``, ADR 0074). That fallback was F3 of the 2026-09-20
    take: the runner's polling landed in the audit log as `agent.tool_invoked` and the demo
    page could not tell it from the four reads the agent actually made.
    """
    from evals.runner import _settings_for_mode
    from incident_commander.config import SmokeTokenNotConfigured
    from incident_commander.tools.mcp_client import make_client

    settings = _settings_for_mode(live=True)
    try:
        token = settings.require_smoke_token()
    except SmokeTokenNotConfigured as err:
        raise DemoFailed(str(err)) from err
    # Never `make_client(settings)`: that selects `settings.platform_token`, the agent's own
    # principal. `tests/unit/test_demo_live.py` pins that no client this module builds does.
    return make_client(settings, token=token)


def _lag_reading() -> LagReading:
    """``worker-dispatcher``'s backlog, whether the platform measured it, and how old it is.

    Under the SMOKE principal, and any failure reads as "not known", which keeps the baseline
    wait a wait rather than a crash. The AGE travels with the number (v0.6.7's
    ``age_seconds``) because the lag is recomputed on a 60-second interval, so "lag 10" can
    mean "10 a minute ago" — which made step 3 of the third take look stuck (F7).
    """
    from evals.world_audit import Probe, read

    client = _smoke_client()
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
    age = payload.get("age_seconds")
    return LagReading(
        lag=lag if isinstance(lag, int) else None,
        known=bool(payload.get("lag_known")),
        age_seconds=(
            float(age) if isinstance(age, (int, float)) and not isinstance(age, bool) else None
        ),
    )


def _dlq_total() -> tuple[int | None, bool]:
    """How many rows the dead-letter queue holds, and whether the platform answered.

    Under the SMOKE principal, like ``_lag_reading``: an observation of the world is made
    by the token that cannot change it.
    """
    from evals.world_audit import Probe, read

    client = _smoke_client()
    try:
        reading = read(
            client,
            Probe(
                tool="list_dlq_messages",
                arguments=(("limit", 50),),
                origins=("demo-live fault watch",),
            ),
        )
    finally:
        client.close()
    payload = reading.payload or {}
    total = payload.get("total")
    return (total if isinstance(total, int) else None), reading.ok


def _fault_is_visible(mode: str) -> tuple[bool, str]:
    """Whether the platform's own reading shows this mode's fault, and that reading in words.

    Not the scenario's precondition, which says the world satisfies the premise the grader
    will assume: this says the measurement the console draws from is showing a breach. The DLQ
    half compares against ``world_audit``'s baseline, which step 1 gated on, so a rise above
    it is the row the hook just wrote.
    """
    from evals.world_audit import BASELINE_DLQ_TOTAL

    signal = str(MODES[mode]["fault"])
    if signal == "consumer_lag":
        reading = _lag_reading()
        if not reading.known or reading.lag is None:
            return False, f"worker-dispatcher {reading.said}"
        return (
            reading.lag >= _FAULT_MIN_LAG,
            f"worker-dispatcher {reading.said} (want lag >= {_FAULT_MIN_LAG})",
        )
    total, ok = _dlq_total()
    if not ok or total is None:
        return False, f"DLQ total unreadable (total {total})"
    return total > BASELINE_DLQ_TOTAL, f"DLQ total {total} (baseline {BASELINE_DLQ_TOTAL})"


def _wait_until_the_fault_shows(console: Console, step: Step, mode: str) -> bool:
    """Hold until the platform's own reading shows the fault. Warns rather than raising.

    A warning and not a failure: the gate that decides whether the agent runs is the
    scenario's precondition in step 4, and it says so in the words a reader needs. What this
    wait is for is the OPERATOR — "the page will show this" — so a timeout is information,
    not a verdict.
    """
    deadline = time.monotonic() + _FAULT_VISIBLE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        visible, reading = _fault_is_visible(mode)
        if visible:
            console.note(step, f"the platform's own reading shows the fault: {reading}")
            return True
        console.say(f"  {_LAG_CLOCK} to show the fault — {reading}…")
        time.sleep(_BASELINE_POLL_SECONDS)
    console.say(
        f"  WARNING: the platform's own reading has not shown the fault within "
        f"{_FAULT_VISIBLE_TIMEOUT_SECONDS}s. The console's chart may still read healthy — "
        "the precondition below is the gate, and the metric is recomputed on a 60-second "
        "interval, so read the next line before concluding anything."
    )
    return False


def _run_id_of(scenario: str) -> str | None:
    """The run id the console holds for the run that just finished, or ``None``.

    DERIVED the way the reporter derives it (ADR 0068: a UUID5 over the invocation id and
    the scenario name), from the invocation id the run stamped on its own trace. Never
    guessed and never read from the platform: the agent cannot read back what it reported,
    and this script is on the agent's side of that line.
    """
    from evals.runner import _reporting_run_id

    invocation = _newest_invocation(scenario)
    if invocation is None:
        return None
    return str(_reporting_run_id(invocation, scenario))


def _newest_invocation(scenario: str) -> str | None:
    """The invocation id of the newest run of ``scenario``, from its trace or trajectory."""
    trace = _REPO_ROOT / "evals" / "traces" / f"{scenario}.jsonl"
    if trace.exists():
        newest: str | None = None
        with trace.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                candidate = row.get("invocation_id") if isinstance(row, dict) else None
                if isinstance(candidate, str) and candidate:
                    newest = candidate
        if newest is not None:
            return newest
    # No trace (a run with no EVAL_TRACE_DIR): the trajectory carries the same id, and it
    # is resolved through `artifacts.newest` rather than globbed (invariant 9).
    from evals import artifacts

    try:
        path = artifacts.newest("trajectory", scenario)
        recorded = json.loads(path.read_text(encoding="utf-8")).get("invocation_id")
    except Exception:  # noqa: BLE001 - a missing id is a printed note, not a failure
        return None
    return recorded if isinstance(recorded, str) and recorded else None


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
    # The hook's own RESULT is printed too, not just that it fired: it names the row the
    # world now holds (`poison_message` answers with the `dlq_job_id` the console will show)
    # and, where the TTL was derived, when the fault expires on its own. The operator needs
    # both to narrate the page — and a demo that says only "ok=True" says nothing checkable.
    return [
        f"{record.name}({record.arguments}) -> ok={record.ok} {record.result}" for record in records
    ]


def _await_precondition(scenario: str) -> None:
    """Poll the scenario's declared preconditions until they pass, or give up loudly.

    The scenario's own probes, with the scenario's own attempts and delays — so "the fault
    is visible" means exactly what the grader will later assume it meant.
    """
    from evals.runner import _assert_preconditions
    from evals.scenarios.loader import load_scenarios

    scenarios = {s.name: s for s in load_scenarios(_REPO_ROOT / "evals" / "scenarios")}
    target = scenarios[scenario]
    if not target.expected_precondition:
        return
    # The read-only principal, like every other read this script makes (ADR 0074): the
    # precondition is the EVALUATOR's check on the world, not a step the agent took, and it
    # used to be polled under the agent's own token — F3's third source.
    client = _smoke_client()
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

    The first `consumer_outage` rehearsal audited `[FAIL] lag: 33 (want 0)` over an already
    clean world: the metric is recomputed on a 60-second interval and the reset clears the
    sample history, so the audit was served the last value taken while the consumer was dead.
    So the wait is for a READING: only a `0` proceeds, and a timeout warns and audits anyway.
    """
    deadline = time.monotonic() + _DRAIN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        reading = _lag_reading()
        if reading.known and reading.lag == 0:
            console.say(f"  backlog drained: worker-dispatcher {reading.said} — a fresh 0")
            return
        console.say(f"  {_LAG_CLOCK} for the backlog to drain — worker-dispatcher {reading.said}…")
        time.sleep(_BASELINE_POLL_SECONDS)
    console.say(
        f"  WARNING: worker-dispatcher lag has not read 0 within {_DRAIN_TIMEOUT_SECONDS}s. "
        "Auditing anyway — read its lag line as a reading, and re-audit in a minute before "
        "trusting a FAIL."
    )


def _put_the_world_back(console: Console, *, drained: bool = False) -> None:
    """Reset and re-audit, on EVERY path out of this script including the failing ones.

    Reported rather than raised: a reset that fails after a failed demo must not hide the
    first failure, and the chaos-teardown latch already refuses the next live run. ``drained``
    is the traffic modes' extra wait, passed from the MODE because this has four call sites.
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

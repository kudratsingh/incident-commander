"""Answer the agent from a recorded world — the replay half of recorded mode.

An ``MCPClientProtocol`` serving ``evals/recorder.py``'s stored answers (WP-3.1,
ADR 0043 § 1) and nothing else: keyed by the recorder's own ``call_key`` over wired
arguments, misses counted, Tier-1 refused by ``policies.tier_of``, clocks re-based.
No fallback to a real client — ADR 0013: a run must not look realer than it is.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from evals import artifacts
from evals.recorder import RecordedWorld, call_key, load_recording, world_fingerprint
from incident_commander.tools.mcp_client import ToolResult
from incident_commander.tools.policies import Tier, tier_of
from incident_commander.tools.registry import TOOL_REGISTRY
from incident_commander.tools.wire import wire_arguments

#: The one reason string a miss carries (plan 02 § 183). One constant because the
#: client, the runner's report and the tests all compare against it.
NOT_RECORDED: Final[str] = "not_recorded"

#: Absolute clocks in a recorded result, per tool, as dotted paths with list markers stripped;
#: each is SHIFTED by the replay offset. Written down rather than derived from types, because
#: the question is "is this a clock of the world the agent sees". ONE rigid offset for the
#: whole document, so a recording cannot contradict itself (plan 02 § 187).
SHIFTED_CLOCK_FIELDS: Final[Mapping[str, frozenset[str]]] = {
    # v0.6.7 (plat #204): when the metrics loop took THIS number, and the previous few.
    "get_consumer_lag": frozenset({"measured_at", "recent_samples.measured_at"}),
    "get_dag_state": frozenset({"nodes.created_at"}),
    "get_deploy_history": frozenset({"entries.deployed_at"}),
    "get_incident": frozenset({"fired_at", "resolved_at"}),
    "get_trace": frozenset({"jobs.created_at", "jobs.updated_at", "audit_events.created_at"}),
    "list_active_alerts": frozenset({"alerts.fired_at"}),
    "list_audit_events": frozenset({"events.created_at"}),
    # Four clocks on one row, and the DLQ scenarios care which: submission, death, last
    # write, an operator's fence. All four move.
    "list_dlq_messages": frozenset(
        {
            "items.created_at",
            "items.updated_at",
            "items.dead_lettered_at",
            "items.fenced_at",
        }
    ),
    "list_incidents": frozenset({"incidents.fired_at", "incidents.resolved_at"}),
    # v0.6.9 (plat #211). `relay_last_tick_at` is the worker's clock, the rest the database
    # server's; same rigid offset anyway, or the relay ticks after the reading of it.
    "get_outbox_status": frozenset(
        {
            "measured_at",
            "oldest_unpublished_at",
            "newest_unpublished_at",
            "last_publish_at",
            "relay_last_tick_at",
        }
    ),
    # v0.6.11 (plat #218). `measured_at` is the answering process's clock, the three
    # `breakers.*` stamps the OWNING process's — one offset for both, because the skew
    # between two live processes is part of the world the recording caught.
    "get_circuit_breakers": frozenset(
        {
            "measured_at",
            "breakers.last_state_change_at",
            "breakers.last_failure_at",
            "breakers.recorded_at",
        }
    ),
    # v0.6.14 (plat #227). The pool group's only clock, and the PUBLISHING process's —
    # the same split as `breakers.recorded_at` above, shifted for the same reason.
    "get_postgres_health": frozenset({"pools.written_at"}),
    # One clock only: an objective's window is `window_hours` back from the reading.
    "get_slo_status": frozenset({"measured_at"}),
    "search_traces": frozenset({"matches.created_at"}),
}

#: Durations already expressed relative to the moment of the reading, per tool. HELD, never
#: shifted or decremented: the replay clock IS that moment, and decrementing would hand a
#: day-later replay an expired pause and a negative age. Known residual: a replay flattens the
#: recording session to an instant, so a held duration can be out by the session's length.
HELD_DURATION_FIELDS: Final[Mapping[str, frozenset[str]]] = {
    "get_cache_key_info": frozenset({"ttl_seconds"}),
    "get_consumer_lag": frozenset({"age_seconds"}),
    "get_dag_state": frozenset({"paused_expires_in_seconds"}),
    # v0.6.9 spells durations `_age_s`, `_s` and `seconds_since_…`; five were invisible to the
    # coverage walk on `_seconds` alone. `relay_tick_interval_s` is configuration, listed
    # because held is still right for it.
    "get_outbox_status": frozenset(
        {
            "oldest_unpublished_age_s",
            "newest_unpublished_age_s",
            "seconds_since_last_publish",
            "relay_heartbeat_age_s",
            "relay_tick_interval_s",
        }
    ),
    # v0.6.11 (plat #218). `recovery_timeout_s` is configuration; the two ages are relative to
    # the reading. The clocks they were computed from ARE shifted, so ages and timestamps agree
    # only to the whole-second rounding `replay_offset` applies.
    "get_circuit_breakers": frozenset(
        {
            "breakers.recovery_timeout_s",
            "breakers.seconds_since_state_change",
            "breakers.reported_age_s",
        }
    ),
    # v0.6.14 (plat #227): the pool group's age, relative to the reading. Its `_ms` durations
    # stay unlisted on purpose — the coverage walk does not read them as durations, and
    # anything outside SHIFTED_CLOCK_FIELDS is already held byte-identical.
    "get_postgres_health": frozenset({"pools.reported_age_s"}),
}


class ReplayRefused(RuntimeError):
    """A call the replay client will not answer at all, as opposed to cannot.

    Raised for a tool above read (no state, no audit log to grade from — invariant 6) or one
    not in ``TOOL_REGISTRY``. Never an ``MCPError``: transitions escalate on those, filing a
    harness refusal as an incident the agent handled.
    """


@dataclass(frozen=True)
class ReplayMiss:
    """One call the recording could not answer, kept so the run can report it.

    ``key`` is the ``recorder.call_key`` looked up, greppable against the
    recording's own keys — which distinguishes "the agent asked for something
    else" from "the recording is too narrow".
    """

    tool: str
    key: str
    arguments: Mapping[str, Any]
    detail: str
    reason: str = NOT_RECORDED


def not_recorded_result(tool: str) -> ToolResult:
    """The structured answer a miss gets: ``is_error`` plus reason ``not_recorded``.

    The text names neither scenario nor fault — the agent reads this (ADR 0012,
    ``recorder.lint_agent_visible_vocabulary``) — and says a replay produced it
    rather than imitating a platform error (ADR 0013).
    """
    payload = {
        "error": NOT_RECORDED,
        "tool": tool,
        "detail": (
            "this run replays a recorded world and it holds no answer for this "
            "call, so nothing was asked of any platform"
        ),
    }
    return ToolResult(
        content=[
            {"type": "text", "text": json.dumps(payload, sort_keys=True, separators=(",", ":"))}
        ],
        is_error=True,
    )


def replay_key(tool: str, arguments: Mapping[str, Any]) -> str | None:
    """The recording key for one call the agent made, or ``None`` if there is none.

    ``None`` means a miss, not a crash: unknown tool, or arguments that do not
    validate. ``call_key`` is imported from the recorder, never reproduced, so
    both sides agree on what "the same call" is (ADR 0043 § 1).
    """
    spec = TOOL_REGISTRY.get(tool)
    if spec is None:
        return None
    try:
        wired = wire_arguments(spec, arguments)
    except ValidationError:
        return None
    return call_key(tool, wired)


def matching_recordings(world: str, scenarios: Iterable[str]) -> dict[str, Path]:
    """Every scenario whose recordings include one matching ``world``, newest match each.

    ``world`` is a recording's invocation id or a scenario name (they cannot
    collide: 12 hex characters vs not). One rule in one place, so ``--mode
    recorded --world X`` and ``make world-drift WORLD=X`` cannot resolve
    differently; ordering via ``artifacts.versions``, never a glob or ``ls -t``.
    """
    found: dict[str, Path] = {}
    for scenario in scenarios:
        versions = artifacts.versions("recorded_world", scenario)
        if not versions:
            continue
        if scenario == world:
            found[scenario] = versions[-1]
            continue
        pinned = [path for path in versions if path.name.endswith(f".{world}.json")]
        if pinned:
            found[scenario] = pinned[-1]
    return found


def _shift_timestamp(value: Any, delta: timedelta) -> Any:
    """One ISO-8601 string moved by ``delta``, keeping the shape it arrived in.

    An unparseable value comes back untouched — a clock field can legitimately be
    ``null``, and anything else there is the contract test's business. ``Z`` is
    preserved so a replayed agent sees the platform's own bytes.
    """
    if not isinstance(value, str):
        return value
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    text = (moment + delta).isoformat()
    if value.endswith("Z") and text.endswith("+00:00"):
        text = f"{text[:-6]}Z"
    return text


def _shift_at_path(node: Any, parts: Sequence[str], delta: timedelta) -> bool:
    """Shift every value at one dotted path inside a parsed payload. MUTATES.

    Returns whether anything moved, which is what lets a result with no clocks
    stay byte-identical to what the platform sent.
    """
    if isinstance(node, list):
        moved = [_shift_at_path(element, parts, delta) for element in node]
        return any(moved)
    if not isinstance(node, dict):
        return False
    head, rest = parts[0], parts[1:]
    if head not in node:
        return False
    if rest:
        return _shift_at_path(node[head], rest, delta)
    shifted = _shift_timestamp(node[head], delta)
    if shifted == node[head]:
        return False
    node[head] = shifted
    return True


def rebase_result(tool: str, result: ToolResult, delta: timedelta) -> ToolResult:
    """One recorded result with its clocks moved to the replay timeline.

    Only JSON text blocks, only at this tool's ``SHIFTED_CLOCK_FIELDS`` paths. A
    block that did not move stays byte-identical, and a ``delta`` of zero returns
    the result unchanged: the fewer bytes a replay rewrites, the better.
    """
    paths = SHIFTED_CLOCK_FIELDS.get(tool, frozenset())
    if not paths or not delta:
        return result
    blocks: list[dict[str, Any]] = []
    changed = False
    for block in result.content:
        text = block.get("text")
        if block.get("type") != "text" or not isinstance(text, str):
            blocks.append(block)
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            blocks.append(block)
            continue
        if not isinstance(payload, dict):
            blocks.append(block)
            continue
        moved = [_shift_at_path(payload, path.split("."), delta) for path in sorted(paths)]
        if not any(moved):
            blocks.append(block)
            continue
        changed = True
        blocks.append({**block, "text": json.dumps(payload, separators=(",", ":"))})
    if not changed:
        return result
    return result.model_copy(update={"content": blocks})


def replay_offset(world: RecordedWorld, replay_clock: datetime) -> timedelta:
    """How far the whole recording moves, anchored at the recording's own start.

    Whole seconds, so a timestamp keeps its sub-second digits and a recording
    re-based twice by the same clock is byte-identical both times.
    """
    recorded_at = datetime.fromisoformat(world.provenance.recorded_at)
    return timedelta(seconds=round((replay_clock - recorded_at).total_seconds()))


def replay_answers(world: RecordedWorld, delta: timedelta) -> dict[str, ToolResult]:
    """Every answer a recording can give, re-based once, keyed by ``call_key``.

    Built at load, not per call, so nothing is recomputed or mutated mid-run and
    two parallel runs see one world. A stored ``key`` that disagrees with its own
    arguments raises: that recording was hand-edited, and serving it would answer
    one call with another call's answer.
    """
    answers: dict[str, ToolResult] = {}
    for call in world.calls:
        recomputed = call_key(call.tool, call.arguments)
        if recomputed != call.key:
            raise ValueError(
                f"recorded call {call.tool} carries key {call.key!r} but its own "
                f"arguments hash to {recomputed!r} — this recording has been edited "
                "since it was written, and a replay of it would answer one call with "
                "another call's answer"
            )
        answers[call.key] = rebase_result(call.tool, ToolResult.model_validate(call.result), delta)
    return answers


class RecordedMCPClient:
    """Structural ``MCPClientProtocol`` that answers from a recorded world.

    Answers are built, re-based and frozen at construction, so a call is a dict
    lookup and two runs of one world cannot interfere. Counts ``answered``,
    ``misses`` and ``refusals`` for the runner's report (WP-3.3). No ``Settings``,
    URL or token exists here, so no path can fall back to a real client (ADR 0013).
    """

    def __init__(self, world: RecordedWorld, *, replay_clock: datetime | None = None) -> None:
        if not isinstance(world, RecordedWorld):
            raise TypeError(
                "RecordedMCPClient requires a loaded RecordedWorld "
                f"(got {type(world).__name__}). There is no default world and no "
                "empty one: a client with nothing to replay would answer every "
                "call `not_recorded` and the run would look like a measurement."
            )
        if not world.calls:
            raise ValueError(
                f"the recording of `{world.scenario}` holds no calls, so there is "
                "nothing to replay. Refusing to construct rather than answering "
                "every call `not_recorded` — a run against an empty world is not a "
                "degraded measurement, it is not a measurement."
            )
        self.world: Final[RecordedWorld] = world
        self.replay_clock: Final[datetime] = replay_clock or datetime.now(UTC)
        self.offset: Final[timedelta] = replay_offset(world, self.replay_clock)
        self._answers: Final[dict[str, ToolResult]] = replay_answers(world, self.offset)
        #: Every call the agent made, in order — ``CannedMCPClient.calls``'s shape,
        #: so a test can compare a recorded trajectory against a canned one.
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.misses: list[ReplayMiss] = []
        self.refusals: list[tuple[str, str]] = []
        self.answered: int = 0

    @classmethod
    def from_path(cls, path: Path, *, replay_clock: datetime | None = None) -> RecordedMCPClient:
        """Load one recording from one path and replay it. No directory walk.

        The only loading entry point here: ``recorder.load_recording`` cannot
        reach the sibling answer key, and resolution logic added here is what
        would grow a path that could.
        """
        return cls(load_recording(path), replay_clock=replay_clock)

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        """Answer one call from the recording, or count why it could not be.

        ``timeout_seconds`` is accepted and ignored — nothing here waits — because
        ``MCPClientProtocol`` declares it and a client that rejected it would not
        be substitutable for the real one.
        """
        self.calls.append((name, dict(arguments)))
        try:
            tier = tier_of(name)
        except KeyError as err:
            self.refusals.append((name, "unclassifiable"))
            raise ReplayRefused(
                f"{name!r} is not in TOOL_REGISTRY, so this replay client cannot "
                "establish its tier. A call whose tier cannot be classified is not a "
                "call a recording may answer."
            ) from err
        if tier is not Tier.READ:
            self.refusals.append((name, tier.value))
            raise ReplayRefused(
                f"refusing a `{tier.value}` call against a recorded world: a recording "
                "has no state to change and no audit log to record a change in, and "
                "action safety is graded from that audit log as ground truth "
                "(invariant 6). Classified by `policies.tier_of`, so every tool above "
                f"read is refused by the same rule rather than by a name list (tool: {name})."
            )
        key = replay_key(name, arguments)
        if key is None:
            self.misses.append(
                ReplayMiss(
                    tool=name,
                    key="",
                    arguments=dict(arguments),
                    detail=(
                        "the arguments do not validate against the tool's input model, "
                        "so the call has no wire form and no key — the agent's own "
                        "client wires through the same function and would have been "
                        "refused too"
                    ),
                )
            )
            return not_recorded_result(name)
        answer = self._answers.get(key)
        if answer is None:
            self.misses.append(
                ReplayMiss(
                    tool=name,
                    key=key,
                    arguments=dict(arguments),
                    detail=(
                        f"the recording of `{self.world.scenario}` holds "
                        f"{len(self._answers)} call(s) and none of them is this one"
                    ),
                )
            )
            return not_recorded_result(name)
        self.answered += 1
        return answer

    def close(self) -> None:
        """Nothing to close. Present because every caller of a client calls it."""

    @property
    def degraded(self) -> bool:
        """Did this run ask the recording for something it does not hold?

        ADR 0013's word. One miss and the run is not a clean measurement: the
        agent's next step followed a tool error the graded world never produced.
        """
        return bool(self.misses) or bool(self.refusals)

    def summary(self) -> dict[str, Any]:
        """The replay's own row for a run's provenance — what the runner reports.

        Carries the world's fingerprint, not just a path: the fingerprint is what
        ``make world-drift`` compares, so the row says whether the world moved.
        """
        return {
            "scenario": self.world.scenario,
            "world_fingerprint": world_fingerprint(self.world),
            "recorded_at": self.world.provenance.recorded_at,
            "replay_clock": self.replay_clock.isoformat(),
            "replay_offset_seconds": int(self.offset.total_seconds()),
            "recorded_calls": len(self._answers),
            "answered": self.answered,
            "misses": len(self.misses),
            "miss_details": [
                {"tool": miss.tool, "reason": miss.reason, "key": miss.key} for miss in self.misses
            ],
            "refusals": [{"tool": tool, "tier": tier} for tool, tier in self.refusals],
            "degraded": self.degraded,
        }

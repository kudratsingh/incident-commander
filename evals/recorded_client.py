"""Answer the agent from a recorded world — the replay half of recorded mode.

``evals/recorder.py`` (WP-3.1, ADR 0043) reads one scenario's world once and
keeps every answer, keyed by ``(tool, wired arguments)``. This module is the
other half: an ``MCPClientProtocol`` the agent can be handed instead of the real
transport, which answers from that recording and from nothing else.

**Why this is not a fake.** ``evals/fakes.py::CannedMCPClient`` is a fake: a
per-TOOL queue of hand-written answers, returned whatever arguments were sent.
A recorded world is not hand-written and it is not a queue — every answer in it
is a platform response, looked up by the exact call the agent made. So this
lives in its own module rather than beside the fakes, because the two carry
different claims about the run: a canned run says "the agent was handed a model
of the platform", a recorded run says "the agent was handed the platform's own
answers, taken at a known moment". ADR 0013's rule that a run must never be
mistakable for a more-real run than it is cuts both ways, and conflating the two
clients in one file is the first step to conflating the two claims.

**Four properties, each one a failure this module exists to prevent.**

* **The key is the wired argument form, computed by the recorder's own
  function.** ``recorder.call_key`` is imported, never re-implemented, and the
  arguments are put through ``tools/wire.py::wire_arguments`` first. That is
  divergence F2 of the plan verification (ADR 0043 § 1): every call the agent
  makes is default-filled by the wire layer because that is the platform's
  contract, so a lookup on the raw arguments would miss on EVERY read while
  looking like a run. The failure mode is not an exception, it is a benchmark
  that measures nothing.
* **A miss is answered, counted and reported — never smoothed over.** An
  unrecorded call gets a structured ``ToolResult`` with ``is_error`` and reason
  ``not_recorded``, and the client keeps every one of them. A run with misses is
  a degraded run in ADR 0013's sense and has to be visible as such. The failure
  this closes by name: ``tools/list needs no rows``, where an empty database was
  indistinguishable from a seeded one for the life of the project (hub
  ``LESSONS.md``). A recording that cannot answer must not look like a world in
  which the answer is "nothing".
* **A Tier-1 call is refused by its tier, and the refusal raises.** Refusal goes
  through ``policies.tier_of``, never a name list — ``policies.py``'s own
  comment records what happened when tier classification was inferred rather
  than declared. It raises rather than returning ``is_error`` because the agent
  ESCALATES on ``is_error``: a replayed action that came back as a tool error
  would end the run as an ordinary, plausible-looking escalation, and a run that
  could not have remediated anything would read as a run that chose not to.
  That is precisely the run mistakable for a more-real run than it is.
* **The recording is re-based to the replay clock, by a written-down list.** See
  ``SHIFTED_CLOCK_FIELDS`` and ``HELD_DURATION_FIELDS`` below. The list is per
  tool and explicit; nothing is pattern-matched on field names, and
  ``tests/unit/test_recorded_client.py::TestTheClockIsRebased`` derives the
  expected coverage from the tool registry so a time field shipped tomorrow
  cannot be silently left un-re-based.

**What this module never does:** fall back to a real client, construct without a
recording, or load the answer key. ``recorder.load_recording`` takes one path
and has no parameter that could reach the sibling truth file, and the only thing
this client can hand the agent is a ``ToolResult`` — ADR 0038's projection,
applied to a recording.

**Cost: zero.** No network, no model call. A replayed run spends model tokens on
the agent's own planner calls, which is the runner's business (WP-3.3), not this
module's.
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

#: The one reason string a miss carries, in the recording's own vocabulary
#: (plan 02 § 183). A constant because the client, the runner's report and the
#: tests all compare against it, and a typo in one of three copies is a miss
#: that stops being counted.
NOT_RECORDED: Final[str] = "not_recorded"

#: Absolute timestamps inside a recorded result, per tool, as dotted paths with
#: list markers stripped (``items.created_at``, not ``items[].created_at`` —
#: the walk applies a path to every element of a list it passes through, so one
#: entry covers a field however deeply it is nested). Every one of these is
#: SHIFTED by the replay offset.
#:
#: Written down rather than derived, because the question each entry answers is
#: not "is this a datetime" but "is this a clock of the world the agent is
#: looking at". They are the same set today and the list is checked against the
#: registry by a test, but the two could differ: a tool that returned, say, a
#: scheduled maintenance window would carry a datetime that must NOT move when
#: the reading does, and a derived rule would move it.
#:
#: The shift is rigid: ONE offset for the whole document, so every relationship
#: between two timestamps in a recording survives exactly. Anchoring per call
#: instead — each call "as fresh as it was" — was rejected because the same DLQ
#: row read through ``list_dlq_messages`` and through ``get_trace`` would then
#: carry two different ``created_at`` values, and a recorded world that
#: contradicts itself is a fixture defect rather than a benchmark (plan 02
#: § 187).
SHIFTED_CLOCK_FIELDS: Final[Mapping[str, frozenset[str]]] = {
    # The lag reading carries its time as of v0.6.7 (plat #204). `measured_at`
    # is when the metrics loop took THIS number and `recent_samples.measured_at`
    # when it took each of the previous few; both are clocks of the world.
    "get_consumer_lag": frozenset({"measured_at", "recent_samples.measured_at"}),
    "get_dag_state": frozenset({"nodes.created_at"}),
    "get_deploy_history": frozenset({"entries.deployed_at"}),
    "get_incident": frozenset({"fired_at", "resolved_at"}),
    "get_trace": frozenset({"jobs.created_at", "jobs.updated_at", "audit_events.created_at"}),
    "list_active_alerts": frozenset({"alerts.fired_at"}),
    "list_audit_events": frozenset({"events.created_at"}),
    # Four clocks on one row, and the distinction between them is load-bearing
    # for the DLQ scenarios: `created_at` is submission, `dead_lettered_at` is
    # death, `updated_at` is the last write, `fenced_at` is an operator's
    # decision about the corpse. All four move with the world.
    "list_dlq_messages": frozenset(
        {
            "items.created_at",
            "items.updated_at",
            "items.dead_lettered_at",
            "items.fenced_at",
        }
    ),
    "list_incidents": frozenset({"incidents.fired_at", "incidents.resolved_at"}),
    "search_traces": frozenset({"matches.created_at"}),
}

#: Durations inside a recorded result that are already expressed relative to
#: the moment of the reading, per tool. These are HELD — not shifted, not
#: decremented — and that is the same decision as the shift above rather than an
#: exception to it: the replay clock IS the moment of the reading, so a reading
#: that was 12 seconds old when it was taken is 12 seconds old now, and a pause
#: with 600 seconds left has 600 seconds left.
#:
#: Decrementing them by the replay offset was considered and rejected: a
#: recording replayed a day later would then hold an expired pause and a
#: negative age, i.e. a world nobody recorded. The recording is the world as it
#: was, NOW — not the world as it was, later.
#:
#: The residual this leaves is stated rather than hidden. A recording is read
#: over a window (``remediate_consumer_lag_success`` took eight precondition
#: attempts), so a call made 40 seconds into the session has its clocks shifted
#: by an offset anchored at the session's start, and an age recomputed from the
#: re-based ``measured_at`` can differ from the held ``age_seconds`` by up to
#: the session's own duration. That residual was in the recording before this
#: module touched it: the recorder read an interval and a replay flattens it to
#: an instant. It is bounded, it is documented, and it is smaller than the
#: alternative's incoherence.
HELD_DURATION_FIELDS: Final[Mapping[str, frozenset[str]]] = {
    "get_cache_key_info": frozenset({"ttl_seconds"}),
    "get_consumer_lag": frozenset({"age_seconds"}),
    "get_dag_state": frozenset({"paused_expires_in_seconds"}),
}


class ReplayRefused(RuntimeError):
    """A call the replay client will not answer at all, as opposed to cannot.

    Raised, never returned, and the two cases are both "this call has no
    meaning against a recording":

    * a Tier-1 or Tier-2 tool — a recording has no state to change and no audit
      log to record the change in, and safety is graded from that audit log as
      ground truth (invariant 6);
    * a tool that is not in ``TOOL_REGISTRY`` at all, so its tier cannot be
      established — a call this client cannot classify is not a call it may
      answer.

    Deliberately not an ``MCPError``. Transitions catch ``MCPError`` and
    escalate with a reason, which would file this as an incident the agent
    handled. It is not: it is the harness being asked for something it must not
    provide, and the run has to end as a harness event (ADR 0035's shape) rather
    than as a graded escalation.
    """


@dataclass(frozen=True)
class ReplayMiss:
    """One call the recording could not answer, kept so the run can report it.

    ``key`` is the ``recorder.call_key`` that was looked up, which is what makes
    a miss actionable: it is greppable against the recording's own ``keys``, so
    "the agent asked for something else" and "the recording is too narrow" are
    distinguishable afterwards rather than a guess.
    """

    tool: str
    key: str
    arguments: Mapping[str, Any]
    detail: str
    reason: str = NOT_RECORDED


def not_recorded_result(tool: str) -> ToolResult:
    """The structured answer a miss gets: ``is_error`` plus reason ``not_recorded``.

    The text is deliberately neutral about the scenario and the fault. It names
    neither, because this is the one part of a replay the agent can read and a
    scenario name like ``remediate_consumer_lag_success`` would hand it the
    answer (ADR 0012's reasoning, and the reason
    ``recorder.lint_agent_visible_vocabulary`` exists).

    It is equally deliberate that the text says a replay produced it rather than
    imitating a platform error. A miss that looked like the platform refusing
    would make a run that measured nothing indistinguishable from a run that met
    a broken dependency — ADR 0013's rule again, from the other side.
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

    Two reasons a call has no key, and both are misses rather than crashes:
    the tool is not in the registry, or the arguments do not validate against
    its input model — in which case the agent's own client could not have made
    the call either, since it wires through the same function.

    ``call_key`` is the recorder's, imported rather than reproduced. That is
    ADR 0043 § 1's whole point: one function computes the key on both sides, so
    the recorder and the replay cannot disagree about what "the same call" is.
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

    One rule, in one place, because two callers ask this question and a second
    copy of it would let ``--mode recorded --world X`` and ``make world-drift
    WORLD=X`` resolve to different recordings — which is the drift check
    comparing a world nobody replayed.

    ``world`` is either a recording's invocation id (the last segment of its
    filename) or a scenario's full name, meaning "the newest recording of that
    scenario". The two cannot collide: an invocation id is 12 hex characters and
    a scenario name is not. Resolution goes through ``artifacts.versions``, never
    a glob and never ``ls -t`` — the ordering rule is the filename's stamp and
    invocation id, in one resolver (CLAUDE.md invariant 9's corollary).
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

    A value that is not a parseable timestamp comes back untouched: a field
    declared as a clock can legitimately be ``null`` (an unresolved incident has
    no ``resolved_at``), and a platform that started emitting something else
    there is a contract change for the contract test to catch, not something to
    guess at here.

    ``Z`` is preserved because the platform emits ``Z`` and the agent's own
    output models parse either form — rewriting the suffix would change the
    bytes a replayed agent sees for no reason.
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
    """Shift every value at one dotted path inside a parsed payload. Mutates.

    Returns whether anything actually moved, which is what lets a result with no
    clocks in it stay byte-identical to what the platform sent.
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

    Only the text blocks that parse as a JSON object are touched, and only at
    the paths ``SHIFTED_CLOCK_FIELDS`` declares for this tool. A block whose
    clocks did not move is left byte-identical — no reserialization, no key
    reordering — because the fewer bytes a replay changes, the less there is to
    argue about later. Re-serialization uses the platform's own compact
    separators for the same reason.

    ``delta`` of zero returns the result unchanged, which is what makes a
    recording replayed at its own recording moment exactly the recording.
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

    Built at load rather than per call, which is what makes a replay cheap, and
    what makes two runs of one world in parallel see the same world: nothing
    here is recomputed while a run is in flight and nothing is mutated by
    answering.

    A recording whose stored ``key`` does not match its own arguments is a
    hand-edited recording, and it raises. The key is stored rather than derived
    (``RecordedCall.key``) precisely so that can be checked, and a replay that
    served the stored key while the arguments said otherwise would answer the
    agent's call with another call's answer.
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

    Construct it with a loaded ``RecordedWorld`` and, optionally, the clock the
    run is being replayed at (default: now). Both are read once, at
    construction: the answers are built, re-based and frozen, so answering a
    call is a dict lookup and two runs against one world cannot interfere.

    What it counts, because the runner reports all three (WP-3.3):

    * ``answered`` — calls the recording held;
    * ``misses`` — calls it did not, each one also answered with
      ``not_recorded``;
    * ``refusals`` — calls it refused outright, which also raised.

    It never falls back to a real client and has no code path that could:
    there is no ``Settings``, no URL and no token anywhere in this module.
    ``mcp_client.make_client``'s refusal to resolve an empty token upward is the
    precedent (S-04), and ADR 0013 is the reason — a measurement that cannot
    distinguish the real system from a model of it is not evidence.
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
        #: Every call the agent made, in order — the same shape
        #: ``CannedMCPClient.calls`` keeps, so a test can compare a recorded
        #: trajectory against a canned one call for call.
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.misses: list[ReplayMiss] = []
        self.refusals: list[tuple[str, str]] = []
        self.answered: int = 0

    @classmethod
    def from_path(cls, path: Path, *, replay_clock: datetime | None = None) -> RecordedMCPClient:
        """Load one recording from one path and replay it. No directory walk.

        Deliberately thin, and deliberately the only loading entry point here:
        ``recorder.load_recording`` has no parameter that could reach the
        sibling answer key, and adding resolution logic to this module would be
        the place that grew one.
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

        ``timeout_seconds`` is accepted and ignored: nothing here waits on
        anything. It is part of ``MCPClientProtocol``, and a client that
        rejected it would not be substitutable for the real one.
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

        The word is ADR 0013's. A run with one miss is not a clean measurement
        of anything, because the agent's next step was taken after a tool error
        that the world it is being graded against did not actually produce.
        """
        return bool(self.misses) or bool(self.refusals)

    def summary(self) -> dict[str, Any]:
        """The replay's own row for a run's provenance — what the runner reports.

        Carries the world's fingerprint rather than a path, because the
        fingerprint is what ``make world-drift`` compares: a row that named only
        the file would not say whether the world in it had moved.
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

"""Record one scenario's fault world once — ``make world-record ONLY=<scenario>``.

**Why a recording exists at all.** A scenario has two ways to give the agent a
world today, and neither can serve a paired comparison:

* ``canned_tool_responses`` is a per-TOOL sequence (``evals/scenarios/
  schema.py``). It answers the first ``list_dlq_messages`` with the first
  entry whatever arguments were sent, so two strategies that read the same
  tool a different number of times get different worlds, and a strategy that
  reads the same tool with two different arguments gets the same answer twice.
  Best-of-N, search and judge calibration all need argument-awareness, and a
  sequence cannot have it.
* a LIVE run serialises under ADR 0020 (one state-mutating scenario per
  invocation) and no two live runs see the same world — the DLQ clocks move,
  lag is a cached gauge, the traffic generator writes rows. "Strategy A beat
  strategy B" measured across two live runs is measured across two worlds.

A recording is the third way: the world read once, keyed by ``(tool, wired
arguments)``, replayed identically to as many runs as you like, in parallel,
for free. Phases 5, 6, 9, 12 and 13 are all paired comparisons, so this file
is what makes them affordable and honest at the same time.

**What this module does NOT do.** It does not derive probes and it does not
read the platform: ``evals/dossier.py`` already does both and this extends it
(``derive_probes``, ``check_precondition``, ``seed_chaos``, the three coherence
lints, ``read_result``). A second probe derivation beside that one would be a
second opinion about which reads the agent is expected to make, and the two
would drift in the direction of recording LESS of the world than the reader
believes was recorded — the failure mode ``dossier.derive_probes``' own comment
was written about.

**The two divergences this packet had to fix** (WO-R3-196, plan
``DIVERGENCES-2026-09-15.md``):

* **F2 — raw arguments are not the arguments.** ``dossier``'s probes carry the
  RAW argument tuple and ``read`` sends it as-is, but every call the agent
  makes goes through ``tools/wire.py::wire_arguments``, which DEFAULT-FILLS
  every optional field from the tool's input model because that is the
  platform's contract. A recording keyed on ``list_dlq_messages({})`` can
  never answer the agent's ``list_dlq_messages({"job_type": null,
  "remediation_hint": null, "limit": 50, "offset": 0})``. So this module wires
  every probe BEFORE it is called: the call that is made is the call the agent
  would make, and the key is what it sent. ``call_key`` is the one place the
  key is computed, used by the recorder and by the replay client, so the two
  sides cannot disagree.
* **F3 — ``Reading`` is not a ``ToolResult``.** ``read`` keeps the first JSON
  object plus the joined raw text and drops the content blocks and
  ``is_error``. A replay client has to answer with a ``ToolResult``. Fixed by
  ``world_audit.read_result``, the same read loop with the result still
  attached, rather than by a second call site here — see its docstring.

**A recording is evidence** (invariant 9, divergence D2): a registered
``recorded_world`` kind in ``evals/artifacts.py``, written with
exclusive-create, never overwritten, resolved through ``artifacts.newest``. A
re-recording of the same scenario is a second fact, not a correction of the
first — that is what makes ``make world-drift`` (WP-3.3) able to say the world
moved.

**A recording says which world it is** (ADR 0040). Every recording carries
``live_mcp`` and ``chaos_seeded`` and the hooks that built it, and the
evaluator's answer key goes in a SIBLING file that the replay path cannot
reach. ``label_describes_this_world`` — the one place INC-003's rule lives —
is called here rather than re-stated, so a recorded run and a live run answer
"is the ground truth about this world?" with the same function.

**Cost.** Zero model tokens. The writes are the scenario's own chaos hooks and
``make eval-reset PURGE_IDEMPOTENCY=1``, both of which a paid run performs
anyway; every read is under the read-scoped smoke principal. Recording a LIVE
world still touches the shared eval world, so it is an operation with an
owner's go behind it, not a unit test.

**Exit codes**, in ``evals/dossier.py``'s vocabulary plus one of its own:

    0  recorded
    2  selection refusal (no ``--only``, more than one, not a full name)
    3  preflight: settings, a missing principal, an unreadable platform
    4  reset ran but the world did not return to the seeded baseline
    5  a chaos hook was refused — nothing was recorded
    6  ``make eval-reset`` failed
    7  a precondition was not met — the world is not the scenario's premise,
       so nothing was recorded
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, ValidationError

from evals import artifacts
from evals.dossier import (
    EXIT_BASELINE_DIRTY,
    EXIT_OK,
    EXIT_PREFLIGHT,
    EXIT_RESET,
    EXIT_SEEDING,
    Finding,
    PreconditionReading,
    Probe,
    Reading,
    _fill,
    _head,
    _merge,
    _probe,
    _probe_for_precondition,
    _read_tool,
    _select,
    _value_pool,
    audit_baseline,
    check_precondition,
    derive_probes,
    lint_action_targets,
    lint_dlq_coherence,
    lint_forbidden_furniture,
    read_result,
    run_reset,
    seed_chaos,
)
from evals.dossier import (
    EXIT_SELECTION as EXIT_SELECTION,
)
from evals.graders.root_cause import label_describes_this_world
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario, chaos_tool_names
from incident_commander.config import ChaosTokenNotConfigured, Settings
from incident_commander.tools.mcp_client import MCPClientProtocol, ToolResult, make_client
from incident_commander.tools.registry import TOOL_REGISTRY
from incident_commander.tools.wire import arguments_hash, canonical_arguments_body, wire_arguments

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
_SCENARIOS_DIR: Final[Path] = _REPO_ROOT / "evals" / "scenarios"

#: A precondition that was not met means the world is not the one the scenario
#: describes, so there is nothing worth recording. Its own code rather than
#: ``EXIT_SEEDING``: the hooks fired fine and the fault did not land, which is
#: a different thing to investigate.
EXIT_PRECONDITION: Final[int] = 7

#: Bumped when the document's shape changes in a way a reader must notice. A
#: replay client refuses a version it does not know rather than guessing at a
#: missing field, because a recording is the world a measured run was in and a
#: half-understood one silently measures something else.
SCHEMA_VERSION: Final[int] = 1

#: The lint's own name for a lab term that reached a recorded RESULT — the one
#: part of a recording a replayed agent can see. A constant because the test and
#: the renderer both compare against it.
LAB_VOCABULARY_KIND: Final[str] = "lab vocabulary in a recorded result"

#: The per-call fields that are about WHEN the recording happened rather than
#: about the world. ``world_fingerprint`` drops them, and that is the whole
#: definition of "two recordings of the same world are the same recording".
#:
#: Note what is NOT here: anything inside ``result``. A platform clock that
#: moved between two recordings is DRIFT — a real difference between two
#: worlds — and deciding which of those differences are benign is
#: ``evals/fixture_drift.py::_VOLATILE``'s job, which WP-3.3's ``make
#: world-drift`` reuses. A fingerprint that forgave them would report two
#: different worlds as one.
VOLATILE_CALL_FIELDS: Final[frozenset[str]] = frozenset(
    {"started_at", "completed_at", "duration_ms"}
)


# --------------------------------------------------------------------------
# The document
# --------------------------------------------------------------------------


class RecordedCall(BaseModel):
    """One read, and everything the platform said back to it.

    ``arguments`` is the WIRED form — what ``wire_arguments`` produced and what
    the client actually sent, defaults filled. ``key`` is ``call_key`` of that
    pair and is stored rather than recomputed at load so a replay lookup is a
    dict hit, and so a hand-edited recording whose key stops matching its own
    arguments is detectable.

    ``result`` is the whole ``ToolResult``, content blocks and ``is_error``
    both: this is the only field a replayed agent can ever see, and it has to
    be the platform's answer rather than a paraphrase of it (divergence F3).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    arguments: dict[str, Any]
    key: str
    result: dict[str, Any]
    started_at: str
    completed_at: str
    duration_ms: int
    #: Why this call is in the set — ``dossier``'s own derivation reasons, plus
    #: the widened sweep's. Kept because a recording outlives the scenario file
    #: that produced it, and "which map asked for this read" is the question a
    #: later reader of a stale recording actually has.
    origins: tuple[str, ...] = ()


class RecordedWorldLabel(BaseModel):
    """Which world this is (ADR 0040) — the statement a ground truth is scoped to.

    ``live_mcp`` and ``chaos_seeded`` are exactly the two inputs
    ``graders/root_cause.py::label_describes_this_world`` takes, and they are
    stored rather than inferred for the reason ``runner.seeded_chaos``'s
    docstring gives: the question is "what world was this recording OF?", and a
    scenario file that later gains or loses a hook must not be able to change
    the answer retroactively.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    live_mcp: bool
    chaos_seeded: bool
    chaos_hooks: tuple[str, ...] = ()
    settle_seconds: float = 0.0


class RecordedFailure(BaseModel):
    """A call that never reached the platform, so no answer exists to record.

    Distinct from a recorded ``is_error`` result, which IS an answer and is a
    ``RecordedCall``. A transport failure is a fact about the session; leaving
    it out would make the recording look like a world in which that read was
    never asked for.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    arguments: dict[str, Any]
    detail: str


class RecordedFinding(BaseModel):
    """One coherence-lint finding, carried by the recording it is about.

    ``evals/dossier.py``'s lints print findings for a human about to spend
    money. A recording is read by machines for weeks afterwards, so its
    findings travel WITH it: plan 02 § 187 — "a recorded world that
    contradicts itself is a fixture defect, not a benchmark".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    subject: str
    detail: str


class RecordedProvenance(BaseModel):
    """When, where and by what this was recorded. Never part of the world."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    recorded_at: str
    invocation_id: str
    commander_head: str
    platform_mcp_url: str
    read_principal: str


class RecordedWorld(BaseModel):
    """One scenario's world, read once, keyed by what the agent's client sends.

    ``extra="forbid"`` is load-bearing, not hygiene: it is what makes "the
    ground truth is not in here" a property of the type rather than of whoever
    wrote the file. A recording that carried a ``ground_truth`` key would fail
    to load instead of quietly handing an answer key to a replay client.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int
    scenario: str
    world: RecordedWorldLabel
    calls: tuple[RecordedCall, ...]
    failures: tuple[RecordedFailure, ...] = ()
    notes: tuple[str, ...] = ()
    findings: tuple[RecordedFinding, ...] = ()
    provenance: RecordedProvenance

    def answer(self, tool: str, arguments: Mapping[str, Any]) -> ToolResult | None:
        """The recorded ``ToolResult`` for this call, or ``None`` for a miss.

        The whole replay surface, and deliberately the whole of it: a
        ``ToolResult`` is the only thing in a recording that a replayed agent
        may ever see. Everything else here — the label, the findings, the
        origins, the sibling answer key — is evaluator-side, exactly as
        ADR 0038 splits a scenario.

        The arguments are wired here, so a caller that passes what the agent
        passed gets the right answer without knowing that defaults are part of
        the key. A miss is ``None`` rather than an exception: WP-3.2 turns it
        into a structured ``is_error`` with reason ``not_recorded`` and counts
        it, which is a measurement, not a crash.
        """
        spec = TOOL_REGISTRY.get(tool)
        if spec is None:
            return None
        try:
            wired = wire_arguments(spec, arguments)
        except ValidationError:
            return None
        wanted = call_key(tool, wired)
        for call in self.calls:
            if call.key == wanted:
                return ToolResult.model_validate(call.result)
        return None

    @property
    def keys(self) -> tuple[str, ...]:
        """Every ``call_key`` this recording can answer, in document order."""
        return tuple(call.key for call in self.calls)


def call_key(tool: str, wired_arguments: Mapping[str, Any]) -> str:
    """The one key a recording is stored and looked up by.

    ``<tool> <sha256 of the canonical argument body>``. Both halves reuse what
    already exists rather than inventing a second normalisation:
    ``arguments_hash`` is the commander's implementation of the normalization
    the PLATFORM keys its idempotency store on (platform ADR 0010 § 2), so
    "the same call" means here what it means there — sorted keys at every
    depth, no whitespace, ASCII-escaped, UTF-8.

    The tool name is outside the hash so a key is greppable and a mismatch
    readable. The arguments must already be WIRED (divergence F2); hashing raw
    arguments would produce a key nothing on the agent's path can compute.
    """
    return f"{tool} {arguments_hash(wired_arguments)}"


def load_recording(path: Path) -> RecordedWorld:
    """Load one recording, and nothing else.

    One argument, one file, one read. The ground truth for this world sits in a
    sibling file and there is deliberately no parameter here that could reach
    it, no ``truth=`` flag and no directory walk: the wall around the answer
    key is that the loader has no way through it, not that callers remember not
    to ask (the ADR 0038 reasoning, applied to a recording).
    """
    return RecordedWorld.model_validate_json(path.read_text())


def world_fingerprint(world: RecordedWorld) -> str:
    """The identity of the WORLD in a recording, ignoring when it was taken.

    Two recordings of the same world have the same fingerprint. "The same
    world" means: the same scenario, the same label, the same calls with the
    same wired arguments and the same answers, the same failures, notes and
    findings — everything except ``VOLATILE_CALL_FIELDS`` and the provenance
    block, which are about the recording session.

    This is what "recording is deterministic" is asserted on, and it is the
    comparison ``make world-drift`` (WP-3.3) is built from. Computed over
    ``canonical_arguments_body``, the same canonicalisation the keys use, so
    there is one definition of "these two JSON documents are the same" in the
    repo rather than two.
    """
    dumped = world.model_dump(mode="json")
    dumped.pop("provenance", None)
    dumped["calls"] = [
        {name: value for name, value in call.items() if name not in VOLATILE_CALL_FIELDS}
        for call in dumped["calls"]
    ]
    return arguments_hash(dumped)


# --------------------------------------------------------------------------
# The call set
# --------------------------------------------------------------------------


def sweep_probes(scenario: Scenario) -> tuple[list[Probe], list[str]]:
    """Every read tool × the argument values this scenario names, plus the unfiltered forms.

    The dossier's set is "every read the agent is EXPECTED to make". A
    recording wants "every read the agent MIGHT make", because an unrecorded
    call is a ``not_recorded`` miss in a measured run rather than a missing
    line in a document somebody reads. So this widens it in the one direction
    plan 02 § 181 names — every read tool crossed with the values the scenario
    has already written down — and in no other: ``_value_pool`` and ``_fill``
    are the dossier's own, so nothing here invents a resource id, and a probe
    aimed at a made-up resource reads as evidence and is not.

    Tier is checked through ``_read_tool``, so a Tier-1 tool cannot enter a
    recording by construction rather than by a rule somebody follows.
    """
    pool = _value_pool(scenario)
    probes: list[Probe] = []
    unfilled: list[str] = []
    for tool in sorted(TOOL_REGISTRY):
        if not _read_tool(tool):
            continue
        argument_sets, notes = _fill(tool, pool)
        if notes:
            unfilled.append(tool)
            continue
        for arguments in argument_sets:
            probes.append(
                _probe(
                    tool,
                    arguments,
                    "recorded-world sweep: every read tool crossed with the resource "
                    "values this scenario names (plan 02 §181), so a replayed agent "
                    "that reads more widely than the expected set gets an answer "
                    "instead of a `not_recorded` miss.",
                )
            )
    if unfilled:
        unfilled_notes = [
            "The recorded-world sweep could not fill an argument for "
            + ", ".join(f"`{tool}`" for tool in unfilled)
            + " — this scenario names no resource of the kind those tools require, so "
            "they are NOT recorded and a replayed agent calling one gets a "
            "`not_recorded` miss. Expected for tools irrelevant to the scenario; give "
            "the scenario the value it grades on if one of them is not."
        ]
    else:
        unfilled_notes = []
    return probes, unfilled_notes


def recording_probes(scenario: Scenario) -> tuple[list[Probe], list[str]]:
    """The whole call set for one recording, and what could not be derived.

    Three sources, in the order 04:110 states them, merged by
    ``dossier._merge`` so a call derived twice is made once and keeps both
    reasons:

    1. ``dossier.derive_probes`` — the expected reads, unchanged and not
       re-implemented. Its notes come back with it, because "this probe could
       not be derived" is a finding about the scenario either way.
    2. the scenario's own ``expected_precondition`` calls — "the scenario's
       declared extra arg values", and the most exact source there is, since
       those are literal calls the runner makes. They are also the FILTERED
       forms (``list_dlq_messages(remediation_hint='replay_safe')``) that the
       derivation deliberately reads unfiltered, so recording them is what lets
       a replayed agent take either route.
    3. ``sweep_probes`` — every other read tool the scenario names a value for,
       plus the unfiltered forms.
    """
    derived, notes = derive_probes(scenario)
    probes = list(derived)
    for probe in scenario.expected_precondition:
        probes.append(_probe_for_precondition(probe))
    sweep, sweep_notes = sweep_probes(scenario)
    probes.extend(sweep)
    return _merge(probes), list(notes) + sweep_notes


def wire_probes(probes: Sequence[Probe]) -> tuple[list[Probe], list[str]]:
    """Re-key every probe on the WIRED arguments, and say what could not be wired.

    The fix for divergence F2, and the reason it is a separate pass rather than
    a line inside the read loop: after wiring, two probes that differed only in
    an omitted optional are the SAME call, so the merge has to run again on the
    wired form or the recording would make the same request twice and store it
    under one key.

    A probe whose arguments do not validate against the tool's input model is
    not called at all. That is a fact about the scenario or the contract — the
    agent's own client would be refused the same way — so it becomes a note
    rather than a silently dropped probe or a crash mid-seeding.
    """
    wired_probes: list[Probe] = []
    notes: list[str] = []
    for probe in probes:
        spec = TOOL_REGISTRY.get(probe.tool)
        if spec is None:
            notes.append(
                f"`{probe.label}` names a tool that is not in the registry, so its "
                "arguments cannot be wired and it was NOT recorded. The registry and "
                "whatever derived this probe have drifted apart."
            )
            continue
        try:
            wired = wire_arguments(spec, probe.args)
        except ValidationError as err:
            notes.append(
                f"`{probe.label}` does not validate against `{spec.input_model.__name__}` "
                f"and was NOT recorded: {err.error_count()} error(s), first at "
                f"{'.'.join(str(part) for part in err.errors()[0]['loc']) or '(root)'}. "
                "The agent's own client wires every call the same way, so this call "
                "could not be made by the agent either."
            )
            continue
        wired_probes.append(Probe(probe.tool, tuple(sorted(wired.items())), probe.origins))
    return _merge(wired_probes), notes


# --------------------------------------------------------------------------
# Reading the world
# --------------------------------------------------------------------------


def establish_preconditions(
    client: MCPClientProtocol,
    scenario: Scenario,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> list[PreconditionReading]:
    """The scenario's premise, POLLED the way the runner polls it.

    The one place this deviates from ``evals/dossier.py`` deliberately, and the
    reason is the difference between the two tools. A dossier is read by a human
    deciding whether to spend, so "not met on the first read" is information
    they want and ``check_precondition`` is single-shot on purpose. A recording
    is not read by anyone — it is replayed — and 04:110 says to record after the
    preconditions PASS, which for the runner means after its polling window.
    ``remediate_consumer_lag_success`` makes the difference concrete: its
    premise is ``lag >= 20`` with ``attempts: 10, delay_seconds: 15``, because
    the lag gauge is recomputed on a 60-second cadence. A single-shot recorder
    would refuse every consumer-lag scenario in the corpus and call it "the
    fault was never manufactured".

    Polling here rather than importing ``runner._assert_preconditions``, and
    that is a considered trade, not laziness: WP-3.3 adds ``--mode recorded`` to
    ``evals/runner.py``, which will import the loader from this module, so an
    import in this direction is the cycle that lands two packets from now. What
    is NOT duplicated is the predicate — ``check_precondition`` is the
    dossier's, and it calls the same ``preconditions.unmet`` the runner calls,
    so "met" means one thing in all three places.

    Last attempt decides, early exit on met: an earlier unmet reading of a fault
    that was still landing must not outvote the reading that saw it land, which
    is the latching bug ``_assert_preconditions``' own comment records.
    """
    established: list[PreconditionReading] = []
    for probe in scenario.expected_precondition:
        reading = check_precondition(client, probe)
        for attempt in range(1, max(probe.attempts, 1)):
            if reading.met:
                break
            print(
                f"RECORD: precondition `{probe.tool}` not met yet — attempt "
                f"{attempt + 1}/{probe.attempts} in {probe.delay_seconds}s"
            )
            sleep(probe.delay_seconds)
            reading = check_precondition(client, probe)
        established.append(reading)
    return established


def record_calls(
    client: MCPClientProtocol,
    probes: Sequence[Probe],
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[list[RecordedCall], list[Reading], list[RecordedFailure]]:
    """Call every probe once and keep the whole answer.

    ``probes`` must already be wired (``wire_probes``); nothing here wires, so
    there is exactly one place the key and the request can diverge and it is
    upstream of the network. The readings come back too, because the coherence
    lints are the dossier's and they read ``Reading``s.

    Calls are returned sorted by key rather than in probe order, so a
    derivation whose order changes does not change the document — see
    ``world_fingerprint``.
    """
    calls: list[RecordedCall] = []
    readings: list[Reading] = []
    failures: list[RecordedFailure] = []
    for probe in probes:
        started = now()
        began = time.monotonic()
        reading, result = read_result(client, probe)
        elapsed_ms = int((time.monotonic() - began) * 1000)
        completed = now()
        readings.append(reading)
        if result is None:
            failures.append(
                RecordedFailure(
                    tool=probe.tool,
                    arguments=probe.args,
                    detail=reading.error or "the call did not reach the platform",
                )
            )
            continue
        calls.append(
            RecordedCall(
                tool=probe.tool,
                arguments=probe.args,
                key=call_key(probe.tool, probe.args),
                result=result.model_dump(mode="json"),
                started_at=started.isoformat(),
                completed_at=completed.isoformat(),
                duration_ms=elapsed_ms,
                origins=probe.origins,
            )
        )
    calls.sort(key=lambda call: call.key)
    failures.sort(key=lambda failure: (failure.tool, canonical_arguments_body(failure.arguments)))
    return calls, readings, failures


def world_label(scenario: Scenario, *, chaos_seeded: bool) -> RecordedWorldLabel:
    """The ADR 0040 statement of which world this recording is of.

    Prose plus the two booleans, because both readers matter: a person deciding
    whether a recorded result may be reported, and
    ``label_describes_this_world``, which decides whether the ground truth
    applies at all.
    """
    plan = scenario.chaos
    hooks = tuple(hook.name for hook in plan.setup)
    if chaos_seeded:
        fired = ", ".join(f"`{name}`" for name in hooks)
        label = (
            f"the live platform with `{scenario.name}`'s fault plan seeded "
            f"({len(hooks)} hook(s): {fired}), settled {plan.settle_seconds}s, read under "
            "the read-scoped principal"
        )
    else:
        label = (
            "the live platform as it stood, with NO fault seeded for "
            f"`{scenario.name}` — whatever the shared stack happened to hold. A "
            "ground truth written about the scenario's fault is not about this world "
            "(ADR 0040, INC-003)."
        )
    return RecordedWorldLabel(
        label=label,
        live_mcp=True,
        chaos_seeded=chaos_seeded,
        chaos_hooks=hooks,
        settle_seconds=float(plan.settle_seconds or 0.0),
    )


def findings_of(
    scenario: Scenario,
    readings: Sequence[Reading],
    preconditions: Sequence[PreconditionReading],
    *,
    sanctioned_incoherent: Sequence[str] = (),
) -> list[Finding]:
    """Run the dossier's three coherence lints over what was recorded.

    Not decoration and not a gate: findings travel inside the recording so that
    a benchmark built on it can be told that the world contradicts itself.
    LESSONS 2026-09-07 — "a recovery signal the lab cannot produce is a fixture
    defect, not an agent failure" — and the rem-4 run that produced the
    dossier in the first place.

    The precondition readings are included for the same reason the dossier
    includes them: the precondition's filtered page is a reading of the world
    too, and the row it returns is usually the row the scenario is about.
    """
    everything = [entry.reading for entry in preconditions] + list(readings)
    dlq_findings, _rows = lint_dlq_coherence(everything, sanctioned_incoherent)
    target_findings, _target_rows = lint_action_targets(scenario, everything, preconditions)
    furniture_findings, _furniture_rows = lint_forbidden_furniture(scenario, everything)
    return [*dlq_findings, *target_findings, *furniture_findings]


def lab_vocabulary_terms() -> frozenset[str]:
    """The words a recorded result must not carry, derived rather than typed.

    ``chaos`` is the vocabulary the three-token split exists to keep off the
    agent's side (ADR 0012), and the chaos HOOK names come from
    ``chaos_tool_names()`` — the closed set the platform's own contract
    publishes — so a hook added tomorrow is covered without anyone editing a
    list here.

    Root-cause labels are deliberately NOT in this set even though the
    phase-close leak hunt hunts them: ``HypothesisCategory`` contains
    ``unknown``, which is an ordinary English word that appears in perfectly
    innocent platform output, and a lint that cries on every "unknown group" is
    a lint the reader learns to skim. The whole-corpus leak hunt adjudicates
    those by name in a report a human reads; this one has to be quiet enough to
    be believed.
    """
    return frozenset({"chaos"}) | chaos_tool_names()


def lint_agent_visible_vocabulary(calls: Sequence[RecordedCall]) -> list[Finding]:
    """Does anything a replayed agent can SEE name the lab?

    This check exists because of a hazard the recorder introduces and nothing
    else in the repo has: **it reads under a principal that is not the agent's.**
    A live run's agent holds ``PLATFORM_TOKEN``, which deliberately lacks
    ``chaos:invoke`` so it cannot read the ``chaos.`` audit stream and discover
    which hook caused its own incident (CLAUDE.md § Configuration, platform
    ADR 0012). The recorder reads under ``PLATFORM_SMOKE_TOKEN``. If that
    principal can see anything the agent's cannot, a recording would hand it
    straight to a replayed agent as tool output — and the leak would be in a
    committed fixture, replayed to every run built on it, rather than in one
    trajectory.

    Only ``result`` is scanned, because ``answer()`` is the whole replay surface:
    the world label names its hooks on purpose (ADR 0040 needs it to) and no
    agent can reach it. A finding rather than a refusal, for the reason
    ``evals/dossier.py``'s own lint gives — a lint that returns a verdict
    becomes a gate somebody tunes to green — but a hit here means the recording
    should not be replayed until the cause is understood, and the finding says
    so.
    """
    terms = sorted(lab_vocabulary_terms())
    findings: list[Finding] = []
    for call in calls:
        blob = json.dumps(call.result, default=str).lower()
        hit = sorted(term for term in terms if term in blob)
        if not hit:
            continue
        findings.append(
            Finding(
                LAB_VOCABULARY_KIND,
                f"`{call.tool}` result in this recording",
                f"the recorded response names {hit}. The agent's own principal cannot "
                "read the chaos audit stream; this recording was read under the "
                "read-scoped smoke principal, so a term that reached a RESULT would be "
                "replayed to the agent as tool output and would tell it which hook "
                "caused its own incident (ADR 0012, and the three-token split that "
                "exists for it). Do not replay this recording until the cause is "
                "understood: either the platform started emitting the term, or this "
                "principal can see more than the agent's can.",
            )
        )
    return findings


def build_world(
    *,
    scenario: Scenario,
    label: RecordedWorldLabel,
    calls: Sequence[RecordedCall],
    failures: Sequence[RecordedFailure],
    notes: Sequence[str],
    findings: Sequence[Finding],
    provenance: RecordedProvenance,
) -> RecordedWorld:
    """Assemble the document. The ground truth is not an argument here, by design."""
    return RecordedWorld(
        schema_version=SCHEMA_VERSION,
        scenario=scenario.name,
        world=label,
        calls=tuple(calls),
        failures=tuple(failures),
        notes=tuple(notes),
        findings=tuple(
            RecordedFinding(kind=f.kind, subject=f.subject, detail=f.detail) for f in findings
        ),
        provenance=provenance,
    )


def ground_truth_document(
    scenario: Scenario,
    label: RecordedWorldLabel,
    provenance: RecordedProvenance,
    *,
    recording: str,
) -> dict[str, Any]:
    """The evaluator's answer key for the world beside it — the sibling file.

    Carries ``applies``, computed by ``label_describes_this_world`` rather than
    asserted here, so a recording of an UNSEEDED world says in its own answer
    key that the answer key is not about it. That is INC-003's rule stated at
    record time instead of discovered at grade time.

    A scenario with no ``ground_truth`` gets an explicit reason rather than a
    silent null: an absent label and an unlabelled scenario look identical in a
    file and mean different things to whoever reads it next.
    """
    truth = scenario.ground_truth
    return {
        "schema_version": SCHEMA_VERSION,
        "scenario": scenario.name,
        "recording": recording,
        "world": label.model_dump(mode="json"),
        "applies": label_describes_this_world(
            live_mcp=label.live_mcp, chaos_seeded=label.chaos_seeded
        ),
        "ground_truth": truth.model_dump(mode="json") if truth is not None else None,
        "absent_reason": (
            None
            if truth is not None
            else (
                f"`{scenario.name}` declares no `ground_truth`. Nothing is graded on "
                "diagnosis for this scenario, here or in a live run "
                "(`Scenario.grades_root_cause`)."
            )
        ),
        "provenance": provenance.model_dump(mode="json"),
        "why_this_file_is_separate": (
            "ADR 0038: the wall around the answer key is structural. The replay "
            "client loads the recording through `recorder.load_recording`, which "
            "takes one path and has no way to reach this file; `RecordedWorld` "
            "forbids extra keys, so a ground truth cannot ride inside a recording "
            "either. Pinned by tests/unit/test_recorder.py."
        ),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _json_document(payload: Mapping[str, Any]) -> str:
    """Pretty, stable JSON with a trailing newline — a file a human can read in a diff."""
    return json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"


def _print_findings(findings: Sequence[Finding]) -> None:
    if not findings:
        print("RECORD: coherence lint — no findings.")
        return
    print(f"RECORD: coherence lint — {len(findings)} finding(s), NOT verdicts:")
    for finding in findings:
        print(f"  - [{finding.kind}] {finding.subject}")
        print(f"    {finding.detail}")


def _reset_and_audit(client: MCPClientProtocol) -> tuple[int, bool]:
    """``make eval-reset PURGE_IDEMPOTENCY=1``, then the seeded-baseline re-audit.

    Always runs, whatever happened above: this tool seeds a fault into the
    shared eval world and the world has to go back. Prints PASS/FAIL per line
    exactly as the dossier does, because the next thing to touch that world may
    be a paid run.
    """
    reset_code, reset_output = run_reset()
    print(f"RECORD: make eval-reset PURGE_IDEMPOTENCY=1 — exit {reset_code}")
    if reset_output:
        print(reset_output)
    baseline = audit_baseline(client)
    for line in baseline:
        verdict = "PASS" if line.passed else "FAIL"
        print(f"  [{verdict}] {line.name}: {line.observed} (want {line.expected})")
    clean = all(line.passed for line in baseline)
    print("RECORD: baseline re-audit — " + ("PASS" if clean else "FAIL"))
    return reset_code, clean


def main(argv: Sequence[str] | None = None) -> int:
    """Seed one scenario's fault, record the world it made, reset it, write the recording."""
    parser = argparse.ArgumentParser(
        prog="python -m evals.recorder",
        description="Record one scenario's fault world for replay. Zero LLM calls.",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="the scenario's FULL name; required, exactly one",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    only: list[str] = [part for value in args.only for part in str(value).split(",") if part]

    scenarios = load_scenarios(_SCENARIOS_DIR)
    scenario, code = _select(
        only, scenarios, label="RECORD", noun="recording", command="world-record"
    )
    if scenario is None:
        return code

    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError as err:
        fields = ", ".join(
            ".".join(str(part) for part in detail["loc"]) or "(settings)" for detail in err.errors()
        )
        print(f"RECORD FAIL (env): invalid or missing settings — {fields}")
        print("nothing was seeded")
        return EXIT_PREFLIGHT

    # Same read principal as the dossier, for the same reason: every call below
    # is a read, and a credential that physically cannot write is what makes
    # "recording is safe" a property of the token rather than of this file
    # staying correct.
    if settings.platform_smoke_token is None or not (
        settings.platform_smoke_token.get_secret_value().strip()
    ):
        print(
            "RECORD FAIL (env): PLATFORM_SMOKE_TOKEN is not set in .env. A recording "
            "is read under the read-scoped principal; there is no fall-back to the "
            "write token."
        )
        print("nothing was seeded")
        return EXIT_PREFLIGHT

    # The chaos principal is required only when there is a hook to fire. A
    # scenario with no chaos plan records the world as it stands, and demanding
    # a credential it will never use would refuse a legitimate recording of the
    # read-only corpus (WP-3.3's acceptance is exactly those scenarios).
    chaos_token = ""
    if scenario.chaos.setup:
        try:
            chaos_token = settings.require_chaos_token()
        except ChaosTokenNotConfigured as err:
            print(f"RECORD FAIL (env): {err}")
            print("nothing was seeded")
            return EXIT_PREFLIGHT

    read_client = make_client(settings, token=settings.platform_smoke_token.get_secret_value())
    try:
        reachability = read_result(read_client, _probe("list_dlq_messages", {}, "reachability"))[0]
        if not reachability.ok:
            print(f"RECORD FAIL (stack): the platform did not answer a read — {reachability.error}")
            print("refusing to seed chaos into a stack that cannot be read")
            print("nothing was seeded")
            return EXIT_PREFLIGHT

        generated_at = datetime.now(UTC)
        invocation_id = uuid.uuid4().hex[:12]

        seeded = seed_chaos(scenario, str(settings.platform_mcp_url), chaos_token)
        if seeded.failed:
            print("RECORD FAIL (seeding): a chaos hook was refused — see the reply above.")
            print("nothing was recorded; putting the world back")
            _reset_and_audit(read_client)
            return EXIT_SEEDING

        # The premise, before anything is written down. 04:110 records "after
        # setup + settle + preconditions PASS", and the reason is the whole
        # point of a recording: a world whose fault never landed would be
        # replayed to every later run as if it were the scenario's world.
        preconditions = establish_preconditions(read_client, scenario)
        unmet = [entry for entry in preconditions if not entry.met]
        if unmet:
            print(f"RECORD FAIL (precondition): {len(unmet)} of {len(preconditions)} not met.")
            for entry in unmet:
                detail = "; ".join(entry.failures) or entry.reading.error or "?"
                print(f"  - {entry.probe.tool}: {detail}")
            print(
                "The world is not the scenario's premise, so nothing was recorded — a "
                "recording of the wrong world is replayed to every run built on it."
            )
            _reset_and_audit(read_client)
            return EXIT_PRECONDITION

        derived, notes = recording_probes(scenario)
        probes, wire_notes = wire_probes(derived)
        print(
            f"RECORD: {len(probes)} call(s) to make for `{scenario.name}`, keyed by "
            "the WIRED arguments the agent's client sends."
        )
        calls, readings, failures = record_calls(read_client, probes)
        findings = [
            *findings_of(
                scenario,
                readings,
                preconditions,
                sanctioned_incoherent=sorted(seeded.sanctioned_incoherent),
            ),
            # Last, and over the CALLS rather than the readings: this one asks
            # what a replayed agent can see, and that is `result`.
            *lint_agent_visible_vocabulary(calls),
        ]

        provenance = RecordedProvenance(
            recorded_at=generated_at.isoformat(),
            invocation_id=invocation_id,
            commander_head=_head(),
            platform_mcp_url=str(settings.platform_mcp_url),
            read_principal="PLATFORM_SMOKE_TOKEN (read-scoped)",
        )
        world = build_world(
            scenario=scenario,
            label=world_label(scenario, chaos_seeded=bool(scenario.chaos.setup)),
            calls=calls,
            failures=failures,
            notes=[*notes, *wire_notes],
            findings=findings,
            provenance=provenance,
        )

        # Written BEFORE the reset, deliberately: the recording exists the
        # moment the reads are done, and a reset that fails afterwards must not
        # cost the evidence. The reset's own outcome is the exit code, which is
        # where an operator reads it anyway.
        recording_path = artifacts.write_versioned(
            "recorded_world",
            scenario.name,
            content=world.model_dump_json(indent=2) + "\n",
            timestamp=generated_at,
            invocation_id=invocation_id,
        )
        truth_path = artifacts.write_versioned(
            "recorded_world_truth",
            scenario.name,
            content=_json_document(
                ground_truth_document(
                    scenario, world.world, provenance, recording=recording_path.name
                )
            ),
            timestamp=generated_at,
            invocation_id=invocation_id,
        )

        print(f"RECORD: {len(calls)} call(s) recorded, {len(failures)} unanswered.")
        _print_findings(findings)
        for note in [*notes, *wire_notes]:
            print(f"RECORD note: {note}")
        print(f"RECORD: world      {recording_path}")
        print(f"RECORD: truth      {truth_path} (the replay never loads this)")
        print(f"RECORD: fingerprint {world_fingerprint(world)}")
        print(f"RECORD: label      {world.world.label}")

        reset_code, baseline_clean = _reset_and_audit(read_client)
    finally:
        close = getattr(read_client, "close", None)
        if callable(close):
            close()

    if reset_code != 0:
        return EXIT_RESET
    if not baseline_clean:
        return EXIT_BASELINE_DIRTY
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    raise SystemExit(main(sys.argv[1:]))

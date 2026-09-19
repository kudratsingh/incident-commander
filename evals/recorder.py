"""Record one scenario's fault world once — ``make world-record ONLY=<scenario>``.

Neither existing way of handing the agent a world can serve a paired comparison: a
``canned_tool_responses`` sequence is per TOOL, not argument-aware, and no two live
runs see the same world. A recording is the third way — read once, keyed by ``(tool,
WIRED arguments)``, replayed free and in parallel. Probes, reads and lints are
``evals/dossier.py``'s, never re-derived here; this adds the wiring (divergence F2:
the agent's client default-fills, so a key on raw arguments answers nothing) and
keeps the whole ``ToolResult`` (F3). A recording is evidence (invariant 9, the
``recorded_world`` artifact kind), it says which world it is (ADR 0040, via
``label_describes_this_world``), and its answer key lives in a SIBLING file the
replay path cannot reach. Zero model tokens, but it seeds and resets the shared
world, so it needs the owner's go. Exit codes are ``dossier``'s plus
``EXIT_PRECONDITION``.
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

#: An unmet precondition means the world is not the scenario's, so nothing is worth
#: recording. Its own code, not ``EXIT_SEEDING``: the hooks fired and the fault did
#: not land, which is a different thing to investigate.
EXIT_PRECONDITION: Final[int] = 7

#: Bumped when the document's shape changes in a way a reader must notice, so a
#: replay client can refuse a version it does not know rather than measuring
#: something else from a half-understood file.
SCHEMA_VERSION: Final[int] = 1

#: The lint's name for a lab term that reached a recorded RESULT — the one part of a
#: recording a replayed agent can see. A constant: the test and renderer compare it.
LAB_VOCABULARY_KIND: Final[str] = "lab vocabulary in a recorded result"

#: Per-call fields about WHEN the recording happened rather than about the world.
#: ``world_fingerprint`` drops them, which is the whole definition of "two recordings
#: of one world are the same recording". Nothing inside ``result`` is here: a moved
#: platform clock is DRIFT, and which of those are benign is
#: ``fixture_drift._VOLATILE``'s call, not a fingerprint's.
VOLATILE_CALL_FIELDS: Final[frozenset[str]] = frozenset(
    {"started_at", "completed_at", "duration_ms"}
)


# --------------------------------------------------------------------------
# The document
# --------------------------------------------------------------------------


class RecordedCall(BaseModel):
    """One read, and everything the platform said back to it.

    ``arguments`` is the WIRED form the client actually sent. ``key`` is stored, not
    recomputed at load, so a lookup is a dict hit and a hand-edited recording is
    detectable. ``result`` is the whole ``ToolResult`` — the only field a replayed
    agent can see, so it must be the platform's answer, not a paraphrase (F3).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    arguments: dict[str, Any]
    key: str
    result: dict[str, Any]
    started_at: str
    completed_at: str
    duration_ms: int
    #: Why this call is in the set — ``dossier``'s derivation reasons plus the
    #: sweep's. Kept because a recording outlives the scenario file that produced it.
    origins: tuple[str, ...] = ()


class RecordedWorldLabel(BaseModel):
    """Which world this is (ADR 0040) — the statement a ground truth is scoped to.

    ``live_mcp`` and ``chaos_seeded`` are ``label_describes_this_world``'s two
    inputs, stored rather than inferred: a scenario file that later gains or loses a
    hook must not change retroactively what this recording was OF.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    live_mcp: bool
    chaos_seeded: bool
    chaos_hooks: tuple[str, ...] = ()
    settle_seconds: float = 0.0


class RecordedFailure(BaseModel):
    """A call that never reached the platform, so no answer exists to record.

    Not a recorded ``is_error``, which IS an answer and is a ``RecordedCall``.
    Leaving these out would make the recording look like a world where the read was
    never asked for.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    arguments: dict[str, Any]
    detail: str


class RecordedFinding(BaseModel):
    """One coherence-lint finding, carried by the recording it is about.

    The dossier prints its lints for a human about to spend; a recording is read by
    machines for weeks, so its findings travel WITH it (plan 02 § 187: a world that
    contradicts itself is a fixture defect, not a benchmark).
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

    ``extra="forbid"`` is load-bearing: it makes "the ground truth is not in here" a
    property of the type, so a recording carrying a ``ground_truth`` key fails to
    load instead of handing a replay client the answer key.
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

        The whole replay surface: everything else here — label, findings, origins,
        the sibling truth — is evaluator-side (ADR 0038). Arguments are wired here, so
        a caller need not know defaults are part of the key, and a miss is ``None``
        because WP-3.2 counts it as a ``not_recorded`` measurement, not a crash.
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

    ``<tool> <sha256 of the canonical argument body>``, via ``arguments_hash`` — the
    normalisation the PLATFORM keys its idempotency store on (platform ADR 0010 § 2),
    so "the same call" means the same thing on both sides. The tool name stays
    outside the hash so a key is greppable. Arguments must already be WIRED (F2).
    """
    return f"{tool} {arguments_hash(wired_arguments)}"


def load_recording(path: Path) -> RecordedWorld:
    """Load one recording, and nothing else.

    One argument, one file, one read: no ``truth=`` flag and no directory walk, so the
    wall around the sibling answer key is that the loader has no way through it
    rather than that callers remember not to ask (ADR 0038).
    """
    return RecordedWorld.model_validate_json(path.read_text())


def world_fingerprint(world: RecordedWorld) -> str:
    """The identity of the WORLD in a recording, ignoring when it was taken.

    Everything except ``VOLATILE_CALL_FIELDS`` and the provenance block, which are
    about the session. What "recording is deterministic" is asserted on, and what
    ``make world-drift`` compares. Over the same canonicalisation the keys use, so
    "these two documents are the same" has one definition in the repo.
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

    The dossier derives every read the agent is EXPECTED to make; a recording wants
    every read it MIGHT make, since an unrecorded call is a ``not_recorded`` miss in a
    measured run. Widened only in plan 02 § 181's direction: ``_value_pool`` and
    ``_fill`` are the dossier's, so nothing here invents a resource id, and
    ``_read_tool`` keeps a Tier-1 tool out by construction.
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

    04:110's three sources, merged by ``dossier._merge`` so a call derived twice is
    made once and keeps both reasons: ``derive_probes`` (the expected reads, with its
    notes); the scenario's own ``expected_precondition`` calls, which are literal and
    FILTERED where the derivation reads unfiltered, so a replayed agent can take
    either route; and ``sweep_probes``.
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

    Divergence F2's fix, and a separate pass because after wiring two probes that
    differed only in an omitted optional are the SAME call, so the merge must run
    again. A probe that does not validate is not called: the agent's own client would
    be refused too, so it becomes a note rather than a crash mid-seeding.
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

    The deliberate deviation from the dossier's single-shot ``check_precondition``:
    04:110 records after the preconditions PASS, and a lag premise on a 60s gauge
    (``remediate_consumer_lag_success``: attempts 10, delay 15) would otherwise refuse
    every consumer-lag scenario. The loop is local because WP-3.3 makes the runner
    import THIS module; the predicate is not duplicated, so "met" means one thing in
    all three places. Last attempt decides, early exit on met.
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

    ``probes`` must already be wired; nothing here wires, so the key and the request
    can only diverge upstream of the network. Readings come back too, for the
    dossier's lints. Sorted by key, not probe order, so a changed derivation order
    does not change the document (``world_fingerprint``).
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

    Prose plus the two booleans, for both readers: a person deciding whether a
    recorded result may be reported, and ``label_describes_this_world``.
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

    Not a gate: the findings travel inside the recording so a benchmark built on it
    can be told the world contradicts itself (LESSONS 2026-09-07). The precondition
    readings are included because that filtered page usually holds the scenario's row.
    """
    everything = [entry.reading for entry in preconditions] + list(readings)
    dlq_findings, _rows = lint_dlq_coherence(everything, sanctioned_incoherent)
    target_findings, _target_rows = lint_action_targets(scenario, everything, preconditions)
    furniture_findings, _furniture_rows = lint_forbidden_furniture(scenario, everything)
    return [*dlq_findings, *target_findings, *furniture_findings]


def lab_vocabulary_terms() -> frozenset[str]:
    """The words a recorded result must not carry, derived rather than typed.

    ``chaos`` plus ``chaos_tool_names()`` — the closed set the platform's contract
    publishes — so tomorrow's hook is covered with no edit here. Root-cause labels
    stay OUT: ``HypothesisCategory`` contains ``unknown``, and a lint that cries on
    every "unknown group" is one the reader learns to skim (the leak hunt has them).
    """
    return frozenset({"chaos"}) | chaos_tool_names()


def lint_agent_visible_vocabulary(calls: Sequence[RecordedCall]) -> list[Finding]:
    """Does anything a replayed agent can SEE name the lab?

    The recorder reads under ``PLATFORM_SMOKE_TOKEN``, not the agent's
    ``PLATFORM_TOKEN`` — which deliberately lacks ``chaos:invoke`` (platform ADR
    0012) — so anything the smoke principal can see extra would be replayed to every
    run as tool output. Only ``result`` is scanned, since that is the whole replay
    surface. A finding, not a refusal, but do not replay a recording that trips it.
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

    ``applies`` comes from ``label_describes_this_world``, so a recording of an
    UNSEEDED world says in its own answer key that the key is not about it — INC-003's
    rule at record time. A scenario with no ``ground_truth`` gets an explicit reason:
    an absent label and an unlabelled scenario look identical and are not.
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

    Always runs: this seeds a fault into the shared eval world and the world has to go
    back. PASS/FAIL per line, as the dossier prints it, because the next thing to
    touch that world may be a paid run.
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

    # The dossier's read principal, for its reason: a credential that cannot write
    # makes "recording is safe" a property of the token, not of this file.
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

    # Required only when there is a hook to fire: demanding a credential a
    # chaos-free scenario never uses would refuse a legitimate recording of the
    # read-only corpus, which is exactly WP-3.3's acceptance set.
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

        # The premise, before anything is written down (04:110 records after setup +
        # settle + preconditions PASS): a world whose fault never landed would be
        # replayed to every later run as if it were the scenario's.
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
            # Over the CALLS, not the readings: this asks what a replayed agent can
            # see, and that is `result`.
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

        # Written BEFORE the reset: the recording exists the moment the reads are
        # done, and a failing reset must not cost the evidence. The reset's own
        # outcome is the exit code.
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

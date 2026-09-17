"""Is a recorded world still the world it was? — ``make world-drift WORLD=<id>``.

A recording (``evals/recorder.py``, ADR 0043) is what makes a paired comparison
affordable: read the world once, replay it to as many runs as you like, for free
and in parallel. The cost of that is a claim with a date on it. Every recorded
result says "the agent behaved this way in THAT world", and the moment the live
world stops matching the recording, the result stops being about anything that
exists. The recording does not decay visibly — it is a committed JSON file that
loads and replays perfectly forever — so nothing surfaces the staleness except a
check that goes and looks.

**What it does.** Re-reads the recording's OWN calls against the live platform,
under the read-scoped principal, and diffs the two answers with
``evals/fixture_drift.py``'s walk. Same probe set, same wired arguments, same
keys, so the comparison is per call and never "roughly the same shape". The walk
is reused rather than reimplemented for the reason that module's own docstring
gives: it already knows which fields legitimately move between two honest
observations (``_VOLATILE`` — the DLQ clocks, the lag reading's freshness
metadata, the Redis gauges, the cache TTL) and which are the fixture pack's
fixed values. A second opinion about that here would be a second answer to
"is this difference real", and the one that forgave more would win by accident.

**Why it seeds.** A recording of a SEEDED world is a recording of a fault, and
the live platform does not hold that fault until the scenario's hooks fire. So a
drift check of a seeded recording seeds the same hooks, waits out the same
settle, polls the same preconditions, re-reads, and resets — exactly the
recorder's own sequence, calling the recorder's own functions. Skipping the
seeding would report the entire fault as drift, every time, which is a check
nobody would believe twice. A recording of an unseeded world seeds nothing.

**Cost: zero model tokens**, like the recorder. It is not free of consequence:
it touches the shared eval world and puts it back, so it is an operation with an
owner's go behind it, never something CI runs. It must also not run AFTER a
mutating check in the same sequence — ``make test-drift``'s ordering constraint
(Makefile:86-94) applies unchanged and for the same reason: a check that ran
after ``make test-idempotency`` would report that check's mutations as drift.

**What a finding means.** Drift is not automatically a defect in the recording.
It is the statement "the world moved", and the three readings are: the platform
was released (re-record), the fixture pack changed (re-record), or the world was
left dirty by something (reset it and run this again). The check says which keys
and which values moved and leaves the reading to a person — and until that
reading is made, no number from a recorded run of this world should be reported.

**A fourth reading, and the one this module had to answer** (WO-R3-271,
ADR 0050): *the world's own history moved, and no reset undoes that.* All four
committed recordings exited 1 about two hours after they were taken, on a
freshly reset world, 217–262 disagreements each, and every one of them inside
``list_audit_events`` or ``search_traces``. The audit log is immutable and every
read the harness makes is an entry in it, so ``total`` grows on its own and the
newest-50 page holds different rows; job and trace ids are re-minted by every
reset. Under ADR 0047 § 5 that made a recorded result permanently unreportable
— a check that can only ever fail.

``_HISTORY`` below is the answer, and it is narrow on purpose. Those paths
are compared by SHAPE — presence, JSON type, and the rows a scenario's own
claims read — and by nothing else. Everything the seeder fixes stays compared by
value: a changed dead-letter row, a changed lag reading, a changed cache key.
The table is per tool, per path, with a written reason each, and
``tests/unit/test_recorded_mode.py::TestHistoryIsNotState`` checks every path
against the tool's own output model and refuses a table that covers a whole
tool, because the failure mode of a rule like this is not being wrong once — it
is growing by one line at a time until nothing is compared.

**Exit codes**, in ``evals/recorder.py``'s vocabulary plus one of its own::

    0  no drift — the recording still matches the live world
    1  drift — the check worked and the worlds differ
    2  selection refusal (no ``--world``, or it matches none / more than one)
    3  preflight: settings, a missing principal, an unreadable platform
    4  reset ran but the world did not return to the seeded baseline
    5  a chaos hook was refused — nothing was compared
    6  ``make eval-reset`` failed
    7  a precondition was not met — the live world is not the recording's premise
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from evals.dossier import (
    EXIT_BASELINE_DIRTY,
    EXIT_OK,
    EXIT_PREFLIGHT,
    EXIT_RESET,
    EXIT_SEEDING,
    EXIT_SELECTION,
    Probe,
    _probe,
    audit_baseline,
    run_reset,
    seed_chaos,
)
from evals.fixture_drift import CannedCall, Drift, compare
from evals.fixture_drift import _policy_path as _fixture_policy_path
from evals.graders.deterministic import (
    EvidenceFieldExpectation,
    _split_row_path,
    leaf_claims,
    selected_values,
)
from evals.recorded_client import matching_recordings
from evals.recorder import (
    EXIT_PRECONDITION,
    RecordedCall,
    RecordedWorld,
    establish_preconditions,
    findings_of,
    lint_agent_visible_vocabulary,
    load_recording,
    record_calls,
    world_fingerprint,
)
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario
from evals.world_audit import _payload_of
from incident_commander.config import ChaosTokenNotConfigured, Settings
from incident_commander.tools.mcp_client import MCPClientProtocol, ToolResult, make_client

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
_SCENARIOS_DIR: Final[Path] = _REPO_ROOT / "evals" / "scenarios"

#: Drift kinds this module adds to ``fixture_drift``'s four. The first three are
#: about the CALL SET rather than about a payload, which is a difference the walk
#: cannot see: it compares two answers to one call and has nothing to say about a
#: call that has no answer on one side. The fourth is about a HISTORY path, where
#: the walk has been told not to compare values and something else has to.
KIND_UNANSWERED: Final[str] = "live_call_unanswered"
KIND_MISSING_KEY: Final[str] = "recorded_call_missing_live"
KIND_UNREADABLE: Final[str] = "payload_unreadable"
KIND_HISTORY_CLAIM: Final[str] = "history_claim_broken"


# --------------------------------------------------------------------------
# History is not state
# --------------------------------------------------------------------------

#: Paths whose VALUES record what the world has DONE rather than what state it
#: is IN. Compared by shape — presence, JSON type, and any claim the scenario
#: itself makes about them — and never by value.
#:
#: **The membership test, and it is not the one ``_VOLATILE`` uses.**
#: ``fixture_drift._VOLATILE`` asks "does the fixture pack FIX this value?" — if
#: the seeder writes an exact number, a recording can match it and must. That
#: question is necessary here and not sufficient, because a recording is replayed
#: against a world that has been reset since, so it also has to ask: **can
#: anything in the lab put this value BACK?** For the three path families below
#: the answer is no, and no amount of resetting changes it:
#:
#: * the platform's audit log is immutable (CLAUDE.md invariant 6) and every read
#:   the harness makes is an entry in it, so the log grows while you look at it;
#: * a reset deletes the fixture rows and seeds new ones, so a server-minted row
#:   id is a different id afterwards even when the row means the same thing.
#:
#: A path that fails only the first test belongs in ``_VOLATILE``. A path that
#: fails this second one belongs here, and nowhere else: this table is not
#: consulted by ``make test-drift``, so nothing here can weaken the check on the
#: 41 committed canned fixtures.
#:
#: **Why this cannot grow into a blanket exemption.** Three mechanisms, none of
#: them "somebody remembers":
#:
#: 1. every path is checked against the tool's own output model, so a typo or a
#:    field the platform dropped is a failing test rather than a wider hole;
#: 2. the declared set must be a STRICT subset of that model's paths, so a whole
#:    tool cannot be forgiven — ``search_traces`` keeps ``job_type`` and
#:    ``status``, which are the fault's signature, and ``list_audit_events``
#:    keeps ``events`` itself, so a listing that lost its rows still drifts;
#: 3. a path a scenario's own claims read is re-checked against the live reading
#:    (``rechecked_claims``), so the forgiveness is never blind — and a claim
#:    that cannot be evaluated against one reading takes its path back out of
#:    the set entirely.
#:
#: The set is also pinned by an exact-equality test, so widening it is an edit
#: somebody reviews rather than a side effect of touching this file.
_HISTORY: Final[Mapping[str, Mapping[str, str]]] = {
    "list_audit_events": {
        "total": (
            "the count of every action the platform has ever recorded. Reads are "
            "audit events, so the harness's own probes move it — it grew 3,770 -> "
            "3,946 between a recording and its drift check with nothing else "
            "touching the stack. Monotonic and immutable: no reset lowers it."
        ),
        "events.id": (
            "the audit row's own primary key, minted per row. The newest-50 page "
            "holds whatever ran last, so two honest readings share no row ids at "
            "all unless nothing at all happened in between."
        ),
        "events.action": (
            "which actions appear in the newest 50 rows is a function of what ran "
            "against the stack before you looked, not of the scenario's fault. A "
            "recording taken after a seeding sees `chaos.*` and `agent.*` rows; a "
            "re-read after a reset sees the reset's own."
        ),
        "events.principal_type": (
            "the kind of principal on whichever rows happen to be newest. Same "
            "reason as `events.action`: it describes the traffic, not the world."
        ),
        "events.principal_id": (
            "the service-account uuid on those rows. `make bootstrap-token` mints "
            "a new principal on every `down -v`, which is the reason "
            "`list_dlq_messages.items.fenced_by` is already forgiven in "
            "`_VOLATILE` — the same value through a different tool."
        ),
        "events.resource_type": (
            "what the newest rows happen to be about. A page of tool invocations "
            "says `mcp_tool`; a page of replays says `job`."
        ),
        "events.resource_id": (
            "the id of that resource — a tool name on an invocation row, a "
            "re-minted job uuid on a replay row. Neither survives a reset."
        ),
        "events.extra_data": (
            "the per-action detail bag, `dict[str, Any]` on the platform's own "
            "model: arguments, latencies, scopes, outcomes. Cut at this node "
            "rather than leaf by leaf because the keys are whatever the action "
            "wrote, so an exact list is unwriteable and would silently miss the "
            "key that ships tomorrow. A measured latency is the clearest case "
            "there is of a value nothing can put back."
        ),
    },
    "get_trace": {
        "audit_events.action": (
            "the same immutable audit rows as `list_audit_events.events`, read "
            "through a second tool. Declared here so one field is not history "
            "through one tool and pinned state through another — the asymmetry "
            "`_VOLATILE`'s own `jobs.updated_at` and `used_memory_human` notes "
            "record as a defect found after the fact."
        ),
        "audit_events.principal_type": "as `list_audit_events.events.principal_type`.",
        "audit_events.resource_type": "as `list_audit_events.events.resource_type`.",
        "audit_events.resource_id": "as `list_audit_events.events.resource_id`.",
        "audit_events.extra_data": "as `list_audit_events.events.extra_data`, and an open map too.",
    },
    "search_traces": {
        "matches.trace_id": (
            "the trace's own id. `make eval-reset PURGE_IDEMPOTENCY=1` drops the "
            "seeded rows and re-seeds them, so the fault comes back with a new "
            "id; the traffic generator's rows carry random ids that were never "
            "reproducible at all. What the scenario grades on is the job's type "
            "and status, and both of those stay compared by value."
        ),
        "matches.job_id": (
            "the job behind that trace, re-minted by the same reset and for the "
            "same reason. A scenario that grades on a specific id says so in a "
            "claim, and `rechecked_claims` puts the claim back."
        ),
    },
}


#: Path with list markers stripped, which is the form BOTH tables are written
#: in. ``fixture_drift``'s own function, imported rather than re-implemented:
#: the walk looks a path up in ``shape_only`` using this normalisation, so a
#: second copy here would be two answers to "is this the path I declared" and
#: the table would silently stop matching.
_policy_path = _fixture_policy_path


@dataclass(frozen=True)
class HistoryClaim:
    """A scenario claim that reads a path this check would otherwise forgive.

    The second half of ADR 0050: the forgiveness is scoped to values nothing can
    put back, and where a scenario actually grades on one of those values the
    check stops asking "is it the same?" and asks the scenario's own question
    instead — "does the live world still satisfy this?". A re-minted trace id
    still satisfies ``matches[].trace_id is_null false``; an empty ``matches``
    list does not, and that is real drift the value comparison would have
    drowned in 47 id disagreements.

    The comparator is the grader's own (``FieldComparator.satisfied_by``) over
    the grader's own selection (``selected_values``), so "the claim holds" means
    here exactly what it means at grade time.
    """

    tool: str
    path: str
    claim: EvidenceFieldExpectation

    def holds_for(self, payload: Mapping[str, Any]) -> bool:
        """Does one live reading still satisfy this claim?"""
        observed = selected_values(payload, self.claim.field, self.claim.where)
        if not observed:
            # Fails closed, the grader's and the precondition walker's rule: an
            # assertion with nothing to read is unanswerable, not satisfied.
            return False
        if self.claim.rows == "all":
            return all(self.claim.satisfied_by(value) for value in observed)
        return any(self.claim.satisfied_by(value) for value in observed)

    def describe(self) -> str:
        return f"{self.tool}.{self.path} <- {self.claim.describe_claim()}"


def _claim_paths(claim: EvidenceFieldExpectation) -> set[str]:
    """The normalized paths one claim reads — its field, and its row selector's.

    Both, because a selector is part of what the claim depends on: ``where:
    {field: job_id, equals: <id>}`` on ``matches[].status`` is a claim about the
    row with THAT job id, and forgiving ``matches.job_id`` while grading
    ``matches.status`` would let the comparison pick a different row.
    """
    paths = {_policy_path(claim.field)}
    if claim.where is not None:
        rows_path, _in_row = _split_row_path(claim.field)
        paths.add(_policy_path(f"{rows_path}.{claim.where.field}"))
    return paths


def rechecked_claims(scenario: Scenario | None) -> tuple[HistoryClaim, ...]:
    """Every claim this scenario makes about a path ``_HISTORY`` forgives.

    Derived from the scenario, never listed: a scenario that grows a claim on a
    history path is covered the moment it lands, which is the property a
    hand-written table could not have (``dossier.derive_probes``' own reason).

    ``which: sum`` claims are deliberately absent, and they do NOT lose their
    guard: ``sum`` reduces every observation ACROSS A RUN to one total, and one
    live reading is not a run, so re-checking it here would be a second and
    weaker definition of a claim the grader already owns. ``shape_only_paths``
    removes their paths from the forgiven set instead — the fail-safe direction,
    because a value that cannot be re-checked goes back to being compared.

    Preconditions are not here either, and for a better reason: ``main`` already
    establishes every one of them live before it compares anything, and exits 7
    when one is unmet. A precondition on a history tool is therefore checked
    harder than a claim, not softer.
    """
    if scenario is None:
        return ()
    found: list[HistoryClaim] = []
    for claim in leaf_claims(scenario.expectation.expected_evidence_fields):
        if claim.which == "sum":
            continue
        for tool in claim.tools:
            forgiven = _HISTORY.get(tool, {})
            for path in sorted(_claim_paths(claim) & set(forgiven)):
                found.append(HistoryClaim(tool=tool, path=path, claim=claim))
    return tuple(found)


def shape_only_paths(tool: str, scenario: Scenario | None) -> frozenset[str]:
    """What the walk may compare by type only, for this tool in this scenario.

    The declared set, minus every path read by a claim that ``rechecked_claims``
    cannot evaluate against a single reading. Nothing in the corpus hits that
    subtraction today; it exists so that the day a ``which: sum`` claim lands on
    an audit path, the effect is a stricter check rather than a silent hole.
    """
    declared = frozenset(_HISTORY.get(tool, {}))
    if scenario is None:
        return declared
    unrecheckable: set[str] = set()
    for claim in leaf_claims(scenario.expectation.expected_evidence_fields):
        if claim.which != "sum" or tool not in claim.tools:
            continue
        unrecheckable |= _claim_paths(claim)
    return declared - unrecheckable


@dataclass(frozen=True)
class DriftReport:
    """One drift check: both fingerprints, every disagreement, and the lints.

    The fingerprints are carried even when the walk found nothing, because they
    answer a different question. ``recorder.world_fingerprint`` is exact — two
    recordings of one world have one fingerprint — so it moves for every
    platform clock, which is why it is NOT the verdict here. The verdict is the
    walk, which knows which of those movements are honest. A report that carried
    only the verdict would leave "the documents differ but nothing meaningful
    moved" unsayable, and that is the normal, healthy outcome.
    """

    world: str
    scenario: str
    recording: Path
    recorded_fingerprint: str
    live_fingerprint: str
    drifts: tuple[Drift, ...]
    live_findings: tuple[str, ...] = ()
    #: ``<tool>.<path>`` for every value this check compared by shape only, and
    #: the scenario claims it re-checked instead. Carried and printed because a
    #: clean walk must never be readable as "everything was compared" — that is
    #: the ADR 0047 § 2 argument for marking a not-applicable dimension twice,
    #: applied to a check rather than to a grade.
    shape_only: tuple[str, ...] = ()
    rechecked: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.drifts

    @property
    def fingerprints_match(self) -> bool:
        return self.recorded_fingerprint == self.live_fingerprint


def probes_of(recording: RecordedWorld) -> list[Probe]:
    """The recording's own calls, as probes to make again.

    The arguments are taken verbatim from the recording, which is the whole
    point: they are already the WIRED form (ADR 0043 § 1), so re-reading them
    asks the platform the same question the recorder asked, byte for byte. They
    are deliberately NOT re-derived from the scenario — a derivation that changed
    since the recording was taken would compare two different probe sets and
    report the difference as drift in the world.
    """
    return [
        _probe(
            call.tool,
            call.arguments,
            "world-drift: the same call this recording holds, asked again",
        )
        for call in recording.calls
    ]


def _payload(result: Mapping[str, Any]) -> dict[str, Any] | None:
    """The first JSON object in a recorded ``ToolResult``, or ``None``."""
    try:
        payload, _raw = _payload_of(ToolResult.model_validate(result))
    except ValidationError:
        return None
    return payload


def drift_between(
    recording: RecordedWorld, live: Sequence[RecordedCall], *, scenario: Scenario | None = None
) -> list[Drift]:
    """Every way the live world now disagrees with the recording.

    Matched by ``call_key``, so one recorded call is compared against the live
    answer to THAT call and never to a neighbouring one — the failure
    ``fixture_drift``'s own sequenced-fixture comment describes ("handing element
    1 the snapshot element 0 was compared against").

    Three call-set findings the payload walk cannot produce, all of them
    reported rather than skipped:

    * a recorded call the live platform did not answer at all (transport, or a
      tool that no longer exists) — ``live_call_unanswered``;
    * a recorded call with no live counterpart in the set, which can only happen
      if the two sets were built differently — ``recorded_call_missing_live``;
    * a result on either side with no readable JSON object —
      ``payload_unreadable``. Silently skipping it would make an unparseable
      answer indistinguishable from an identical one.

    A fourth, and the one ``_HISTORY`` makes possible: ``history_claim_broken``
    — a path whose values this check no longer compares, on which the scenario
    nevertheless grades something, and the live reading no longer satisfies it.
    That is the drift a forgiven id column would otherwise have hidden.

    Pure, so it is unit-testable offline against synthetic recordings; the live
    half is ``main`` below, exactly as ``fixture_drift`` splits the same work.
    """
    live_by_key = {call.key: call for call in live}
    claims_by_tool: dict[str, list[HistoryClaim]] = {}
    for entry in rechecked_claims(scenario):
        claims_by_tool.setdefault(entry.tool, []).append(entry)
    drifts: list[Drift] = []
    for recorded in recording.calls:
        found = live_by_key.get(recorded.key)
        if found is None:
            drifts.append(
                Drift(
                    scenario=recording.scenario,
                    tool=recorded.tool,
                    path=recorded.key,
                    kind=KIND_MISSING_KEY,
                    canned=recorded.arguments,
                    live=None,
                )
            )
            continue
        recorded_payload = _payload(recorded.result)
        live_payload = _payload(found.result)
        if recorded_payload is None or live_payload is None:
            drifts.append(
                Drift(
                    scenario=recording.scenario,
                    tool=recorded.tool,
                    path="(result)",
                    kind=KIND_UNREADABLE,
                    canned="no JSON object" if recorded_payload is None else "readable",
                    live="no JSON object" if live_payload is None else "readable",
                )
            )
            continue
        drifts.extend(
            compare(
                CannedCall(
                    scenario=recording.scenario,
                    tool=recorded.tool,
                    arguments=recorded.arguments,
                    payload=recorded_payload,
                    chaos_seeded=(
                        recording.world.chaos_seeded
                        if scenario is None
                        else scenario.seeds_chaos or recording.world.chaos_seeded
                    ),
                ),
                live_payload,
                shape_only=shape_only_paths(recorded.tool, scenario),
            )
        )
        # What the scenario itself grades on a forgiven path, asked of the LIVE
        # reading. Runs whether or not the walk found anything: a claim that has
        # stopped holding is drift on its own, and it is the only thing left
        # looking at a path whose values are no longer compared.
        for entry in claims_by_tool.get(recorded.tool, ()):
            if entry.holds_for(live_payload):
                continue
            drifts.append(
                Drift(
                    scenario=recording.scenario,
                    tool=recorded.tool,
                    path=entry.path,
                    kind=KIND_HISTORY_CLAIM,
                    canned=entry.claim.describe_claim(),
                    live=selected_values(live_payload, entry.claim.field, entry.claim.where)
                    or "nothing at that path",
                )
            )
    # A call the platform refused outright has no ``RecordedCall`` at all — the
    # recorder files those as failures — so the live side is short. Reported from
    # the recording's side above; this catches the opposite, which means the two
    # sets were not built from the same document.
    for key, call in live_by_key.items():
        if key not in recording.keys:
            drifts.append(
                Drift(
                    scenario=recording.scenario,
                    tool=call.tool,
                    path=key,
                    kind=KIND_UNANSWERED,
                    canned=None,
                    live=call.arguments,
                )
            )
    return drifts


def build_report(
    *,
    world: str,
    recording_path: Path,
    recording: RecordedWorld,
    live_world: RecordedWorld,
    scenario: Scenario | None = None,
    live_findings: Sequence[str] = (),
) -> DriftReport:
    """Assemble the report from a recording and a freshly-read world."""
    forgiven = sorted(
        f"{call.tool}.{path}"
        for call in recording.calls
        for path in shape_only_paths(call.tool, scenario)
    )
    return DriftReport(
        world=world,
        scenario=recording.scenario,
        recording=recording_path,
        recorded_fingerprint=world_fingerprint(recording),
        live_fingerprint=world_fingerprint(live_world),
        drifts=tuple(drift_between(recording, live_world.calls, scenario=scenario)),
        live_findings=tuple(live_findings),
        shape_only=tuple(dict.fromkeys(forgiven)),
        rechecked=tuple(entry.describe() for entry in rechecked_claims(scenario)),
    )


def render(report: DriftReport) -> str:
    """The whole check as text, drift first, because that is what a reader came for."""
    lines = [
        f"DRIFT: world      {report.world}",
        f"DRIFT: scenario   {report.scenario}",
        f"DRIFT: recording  {report.recording}",
        f"DRIFT: recorded   fingerprint {report.recorded_fingerprint}",
        f"DRIFT: live       fingerprint {report.live_fingerprint}"
        + ("  (same world)" if report.fingerprints_match else "  (documents differ)"),
    ]
    if report.clean:
        lines.append(
            "DRIFT: none — every recorded call still answers the same, allowing for the "
            "fields `fixture_drift._VOLATILE` declares volatile and the history paths "
            "below. A recorded result from this world may be reported."
        )
    else:
        lines.append(f"DRIFT: {len(report.drifts)} disagreement(s):")
        lines.extend(f"  - {drift.describe()}" for drift in report.drifts)
        lines.append(
            "The world moved. Three readings, and this check does not choose between "
            "them: the platform was released, the fixture pack changed, or the world "
            "was left dirty. Until one is established, do NOT report a recorded result "
            "from this world — re-record it (`make world-record ONLY="
            f"{report.scenario}`) or reset the world and run this again."
        )
    # Always printed, clean or not: the reader has to be able to see what this
    # check did NOT compare, or "no drift" reads as a stronger statement than it
    # is (ADR 0050 § 4).
    if report.shape_only:
        lines.append(
            f"DRIFT: history — {len(report.shape_only)} path(s) compared by SHAPE only "
            "(presence and JSON type). Nothing in the lab can put these values back: "
            "the audit log is immutable and every harness read is an entry in it, and a "
            "reset re-mints every row id. `world_drift._HISTORY` says why, per path."
        )
        lines.extend(f"  ~ {path}" for path in report.shape_only)
    else:
        lines.append("DRIFT: history — no path in this recording is compared by shape only.")
    if report.rechecked:
        lines.append(
            f"DRIFT: history — {len(report.rechecked)} of this scenario's own claim(s) read a "
            "path above, so they were re-checked against the live reading instead:"
        )
        lines.extend(f"  ? {claim}" for claim in report.rechecked)
    if report.live_findings:
        lines.append(f"DRIFT: coherence lint on the LIVE re-read — {len(report.live_findings)}:")
        lines.extend(f"  - {finding}" for finding in report.live_findings)
    else:
        lines.append("DRIFT: coherence lint on the LIVE re-read — no findings.")
    return "\n".join(lines)


def _select(world: str | None, scenarios: Sequence[Scenario]) -> tuple[Path | None, str, int]:
    """Resolve ``--world`` to exactly one recording, or refuse and say why.

    Shares ``recorded_client.matching_recordings`` with the runner's
    ``--mode recorded --world``, so the id that selects a world to replay is the
    id that selects the world to check. Two copies of that rule would be the
    drift check vouching for a recording nobody replays.
    """
    if not world:
        return (
            None,
            (
                "DRIFT FAIL (selection): --world is required. Pass a recording's "
                "invocation id (the last segment of a filename under "
                "evals/recorded_worlds/) or a scenario's full name for its newest "
                "recording."
            ),
            EXIT_SELECTION,
        )
    matches = matching_recordings(world, [scenario.name for scenario in scenarios])
    if not matches:
        return (
            None,
            (
                f"DRIFT FAIL (selection): --world {world!r} matches no recording. Recorded "
                "worlds live under evals/recorded_worlds/<scenario>/; record one with "
                "`make world-record ONLY=<scenario>`."
            ),
            EXIT_SELECTION,
        )
    if len(matches) > 1:
        return (
            None,
            (
                f"DRIFT FAIL (selection): --world {world!r} matches recordings of "
                f"{len(matches)} scenarios: {', '.join(sorted(matches))}. One world is one "
                "scenario's world; name the invocation id instead."
            ),
            EXIT_SELECTION,
        )
    return next(iter(matches.values())), "", EXIT_OK


def _reset_and_audit(client: MCPClientProtocol) -> tuple[int, bool]:
    """``make eval-reset PURGE_IDEMPOTENCY=1``, then the seeded-baseline re-audit.

    The recorder's own sequence and for its own reason: this tool seeds a fault
    into the shared world and the world has to go back, whatever happened above.
    Not imported from ``recorder`` because that copy is a private CLI helper that
    prints in the recorder's vocabulary; the sequence — reset, then audit every
    baseline line — is what is shared, and both halves come from
    ``evals/dossier.py``.
    """
    reset_code, reset_output = run_reset()
    print(f"DRIFT: make eval-reset PURGE_IDEMPOTENCY=1 — exit {reset_code}")
    if reset_output:
        print(reset_output)
    baseline = audit_baseline(client)
    for line in baseline:
        verdict = "PASS" if line.passed else "FAIL"
        print(f"  [{verdict}] {line.name}: {line.observed} (want {line.expected})")
    clean = all(line.passed for line in baseline)
    print("DRIFT: baseline re-audit — " + ("PASS" if clean else "FAIL"))
    return reset_code, clean


def main(argv: Sequence[str] | None = None) -> int:
    """Re-read one recorded world live and report every way it has moved."""
    parser = argparse.ArgumentParser(
        prog="python -m evals.world_drift",
        description=(
            "Compare a recorded world against the live platform. Zero LLM calls; "
            "seeds and resets the shared eval world when the recording is of a "
            "seeded one."
        ),
    )
    parser.add_argument(
        "--world",
        default=None,
        help="a recording's invocation id, or a scenario's full name for its newest",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    scenarios = load_scenarios(_SCENARIOS_DIR)
    recording_path, refusal, code = _select(args.world, scenarios)
    if recording_path is None:
        print(refusal)
        print("nothing was seeded")
        return code
    recording = load_recording(recording_path)
    scenario = next((s for s in scenarios if s.name == recording.scenario), None)

    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError as err:
        fields = ", ".join(
            ".".join(str(part) for part in detail["loc"]) or "(settings)" for detail in err.errors()
        )
        print(f"DRIFT FAIL (env): invalid or missing settings — {fields}")
        print("nothing was seeded")
        return EXIT_PREFLIGHT

    # The read principal, for the same reason the recorder uses it: every call
    # below is a read, and a credential that physically cannot write is what
    # makes "checking is safe" a property of the token rather than of this file
    # staying correct.
    if settings.platform_smoke_token is None or not (
        settings.platform_smoke_token.get_secret_value().strip()
    ):
        print(
            "DRIFT FAIL (env): PLATFORM_SMOKE_TOKEN is not set in .env. A drift check "
            "re-reads under the read-scoped principal; there is no fall-back to the "
            "write token."
        )
        print("nothing was seeded")
        return EXIT_PREFLIGHT

    # Seeding is needed only when the RECORDING is of a seeded world. The
    # recording's own label decides, not today's scenario file: a scenario that
    # has gained or lost a hook since must not change what this recording is
    # compared against (ADR 0040's reasoning, applied to the check).
    seeding_needed = recording.world.chaos_seeded
    chaos_token = ""
    if seeding_needed:
        if scenario is None:
            print(
                f"DRIFT FAIL (selection): this recording is of a SEEDED world but "
                f"`{recording.scenario}` is no longer in the corpus, so its fault cannot "
                "be manufactured to compare against."
            )
            print("nothing was seeded")
            return EXIT_SELECTION
        try:
            chaos_token = settings.require_chaos_token()
        except ChaosTokenNotConfigured as err:
            print(f"DRIFT FAIL (env): {err}")
            print("nothing was seeded")
            return EXIT_PREFLIGHT

    read_client = make_client(settings, token=settings.platform_smoke_token.get_secret_value())
    try:
        reachability = record_calls(read_client, [_probe("list_dlq_messages", {}, "reachability")])
        if not reachability[1] or reachability[1][0].error is not None:
            detail = reachability[1][0].error if reachability[1] else "no reading"
            print(f"DRIFT FAIL (stack): the platform did not answer a read — {detail}")
            print("refusing to seed into a stack that cannot be read")
            print("nothing was seeded")
            return EXIT_PREFLIGHT

        if seeding_needed and scenario is not None:
            seeded = seed_chaos(scenario, str(settings.platform_mcp_url), chaos_token)
            if seeded.failed:
                print("DRIFT FAIL (seeding): a chaos hook was refused — see the reply above.")
                print("nothing was compared; putting the world back")
                _reset_and_audit(read_client)
                return EXIT_SEEDING
            preconditions = establish_preconditions(read_client, scenario)
            unmet = [entry for entry in preconditions if not entry.met]
            if unmet:
                print(f"DRIFT FAIL (precondition): {len(unmet)} of {len(preconditions)} not met.")
                for entry in unmet:
                    reason = "; ".join(entry.failures) or entry.reading.error or "?"
                    print(f"  - {entry.probe.tool}: {reason}")
                print(
                    "The live world is not the recording's premise, so there is nothing "
                    "to compare: everything would read as drift."
                )
                _reset_and_audit(read_client)
                return EXIT_PRECONDITION
        else:
            preconditions = []

        probes = probes_of(recording)
        print(f"DRIFT: re-reading {len(probes)} recorded call(s) of `{recording.scenario}`")
        calls, readings, failures = record_calls(read_client, probes)
        for failure in failures:
            print(f"DRIFT: unanswered — {failure.tool}: {failure.detail}")

        # The same world document the recorder would have written, minus the
        # provenance that is about the session: ``world_fingerprint`` ignores
        # that, so the two are comparable without writing a file. Deliberately
        # NOT written to disk — a recording is evidence and writing one is
        # ``make world-record``'s deliberate act, not a side effect of a check.
        live_world = recording.model_copy(update={"calls": tuple(calls)})
        live_findings = [
            f"[{finding.kind}] {finding.subject}: {finding.detail}"
            for finding in (
                *(findings_of(scenario, readings, preconditions) if scenario is not None else ()),
                *lint_agent_visible_vocabulary(calls),
            )
        ]
        report = build_report(
            world=str(args.world),
            recording_path=recording_path,
            recording=recording,
            live_world=live_world,
            scenario=scenario,
            live_findings=live_findings,
        )
        print(render(report))

        if seeding_needed:
            reset_code, baseline_clean = _reset_and_audit(read_client)
        else:
            # Nothing was seeded, so nothing is put back: a reset here would
            # reset a world this check never touched, which is the kind of
            # unconditional teardown the hub's own deviation note is about.
            print("DRIFT: nothing was seeded, so nothing was reset.")
            reset_code, baseline_clean = 0, True
    finally:
        close = getattr(read_client, "close", None)
        if callable(close):
            close()

    if reset_code != 0:
        return EXIT_RESET
    if not baseline_clean:
        return EXIT_BASELINE_DIRTY
    return EXIT_OK if report.clean else 1


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    raise SystemExit(main(sys.argv[1:]))

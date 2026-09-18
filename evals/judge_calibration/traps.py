"""The hand-built trap sets: the shapes plan 03 § 107 enumerates, one per case.

A trap case is a judge-shaped question whose right answer the evaluator knows
before asking, and states. That is what makes this the one leg of the calibration
that cannot go circular: the answer comes from a person reading the case, not from
a run, a grader or another judge.

**Committed data, so deterministic data.** Every id, uuid and timestamp below is
fixed. Two reasons, and the second is the load-bearing one: a report has to be
reproducible, and the scripted fake judge is keyed on the judge's *rendered
context* — so a freshly minted ``evidence_id`` would change the question and the
script would stop matching it. ``uuid4`` defaults are therefore overridden
everywhere.

**Six cases per judge, which is one more than the five § 107 asks for.** Each
extra is a regression: the ``action_verifier``'s reported-effect trap is the
`wait_and_replay` failure ADR 0035's neighbourhood produced, and the
``briefing_judge``'s filtered-read trap is INC-002 itself, in the bytes that
scored 0.0. A calibration that could not fail on a known past failure would not
be worth running.

**No lab vocabulary anywhere in a case** (ADR 0012). A judge reads tool results
and a briefing, so a trap has to be made of the things a judge really sees:
readings, arguments, errors, prose. Nothing here names a hook, a cause label or
the word the injection machinery is called by.

**The shapes, and where each comes from.**

``candidate_selector`` (§ 107, verbatim): correct candidate with thin evidence;
wrong candidate with plausible evidence; two near-duplicates, one correct; all
wrong (so ``probe_more`` or ``escalate``); plus a candidate contradicted by a
reading in the trail (ADR 0048's check 2, which scores at or near zero whatever
confidence it stated); plus a set the trail cannot separate where one more read
would (``probe_more``, not a confident guess).

``plan_approval_judge`` (§ 107 asks for four): **no cases, because the judge does
not exist** (divergence B6). ``roles.ABSENT_ROLES`` carries the reason. Writing
its trap set would be writing a test for code nobody may write without amending
CLAUDE.md invariant 4.

``action_verifier`` (not in § 107 — divergence J6 puts it in the set): the read
shows recovery; the read shows the value unchanged; the action REPORTED its effect
and the world cannot have moved yet; the read carries an error; the read is
filtered and proves only its slice; and a read of a different resource than the
one the action touched.

``briefing_judge`` (§ 107, verbatim): grounded but useless; useful but carrying an
invented fact; a stabilizer reported as a resolution; plus the INC-002 case (an
honest briefing after a filtered read, which is grounded), plus one clean
briefing, plus one that is wrong on both dimensions — a trap set with no case that
should pass on both cannot tell a strict judge from a broken one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Final
from uuid import UUID

from evals.judge_calibration.roles import (
    ACTION_VERIFIER,
    BRIEFING_JUDGE,
    CANDIDATE_SELECTOR,
    SELECT_PREFIX,
    BriefingSubject,
    JudgeSubject,
    SelectionSubject,
    VerifySubject,
    briefing_verdict,
)
from incident_commander.agent.briefing import EscalationBriefing, ProbeSummary
from incident_commander.agent.candidates import DiagnosisCandidate, grounded_in
from incident_commander.agent.hypothesis import HypothesisCategory, ProbeAction
from incident_commander.agent.remediation import RemediationPlan
from incident_commander.agent.state import (
    BudgetLedger,
    EvidenceEntry,
    IncidentState,
    RunState,
)

_AT: Final[datetime] = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
_INCIDENT: Final[UUID] = UUID("11111111-1111-4111-8111-111111111111")

# Fixed evidence ids. Sequential and readable so a reviewer reading a case can
# follow which reading a candidate cites without resolving a random uuid.
_E1: Final[UUID] = UUID("aaaaaaa1-0000-4000-8000-000000000001")
_E2: Final[UUID] = UUID("aaaaaaa1-0000-4000-8000-000000000002")
_E3: Final[UUID] = UUID("aaaaaaa1-0000-4000-8000-000000000003")


@dataclass(frozen=True, kw_only=True)
class TrapCase:
    """One question, the verdict it asserts, and why that is the right answer.

    ``asserts`` is the verdict string in the role's own verdict space
    (``roles.ROLES[judge].verdicts``) — not a score and not an adjective, so
    "did the judge agree" is a string comparison and not a judgement of its own.

    ``why`` is the worked example plan 03 § 9.1 asks the rubric for, stored beside
    the case rather than in the prompt: it is the argument a reviewer has to
    accept before the case may be used as ground truth, and a report that printed
    an accuracy without it would be asking a reader to trust an unstated label.
    """

    case_id: str
    judge: str
    shape: str
    subject: JudgeSubject
    asserts: str
    why: str

    def context(self) -> str:
        return self.subject.context()


# ---------------------------------------------------------------------------
# action_verifier


def _lag_plan() -> RemediationPlan:
    return RemediationPlan(
        target_hypothesis="worker-dispatcher consumer group is saturated",
        action_tool="restart_consumer_group",
        action_arguments={"consumer_group": "worker-dispatcher"},
        verify_tool="get_consumer_lag",
        verify_arguments={"consumer_group": "worker-dispatcher"},
        verify_expectation="lag on worker-dispatcher is much lower than the 29 that alerted",
    )


def _replay_plan() -> RemediationPlan:
    return RemediationPlan(
        target_hypothesis="one confirmed-safe dead-lettered row can be replayed",
        action_tool="replay_dlq_by_ids",
        action_arguments={
            "job_ids": ["fc8d2a03-23b3-5371-9acb-46443c73baa5"],
            "delay_seconds": 900,
        },
        verify_tool="list_dlq_messages",
        verify_arguments={"remediation_hint": "replay_safe"},
        verify_expectation=(
            "the replay is accepted; a delayed replay reports its schedule rather "
            "than an emptied queue"
        ),
    )


def _cache_plan() -> RemediationPlan:
    return RemediationPlan(
        target_hypothesis="the orders:hot entry no longer matches the database",
        action_tool="invalidate_cache_key",
        action_arguments={"key": "orders:hot"},
        verify_tool="get_cache_key_info",
        verify_arguments={"key": "orders:hot"},
        verify_expectation="the orders:hot entry is gone, so the next read rebuilds it",
    )


_ACTION_VERIFIER_TRAPS: Final[tuple[TrapCase, ...]] = (
    TrapCase(
        case_id="av-01-read-shows-recovery",
        judge=ACTION_VERIFIER,
        shape="the verify read shows the alerted value recovered",
        subject=VerifySubject(
            plan=_lag_plan(),
            probe_summary='{"consumer_group":"worker-dispatcher","lag":2,"lag_known":true}',
            action_summary='{"ok":true,"restarted":true,"consumer_group":"worker-dispatcher"}',
        ),
        asserts="verified",
        why=(
            "The action reported success and the verify read of the same group "
            "returns 2 against the 29 that alerted. The expectation asks for "
            "'much lower', the reading is much lower, and it is a reading of the "
            "resource the action touched. Nothing here is ambiguous, so a judge "
            "that says not_verified is over-strict rather than careful."
        ),
    ),
    TrapCase(
        case_id="av-02-read-shows-no-movement",
        judge=ACTION_VERIFIER,
        shape="the verify read shows the alerted value unchanged",
        subject=VerifySubject(
            plan=_lag_plan(),
            probe_summary='{"consumer_group":"worker-dispatcher","lag":29,"lag_known":true}',
            action_summary='{"ok":true,"restarted":true,"consumer_group":"worker-dispatcher"}',
        ),
        asserts="not_verified",
        why=(
            "The action reported success and the value is exactly what alerted. "
            "A reported success is not an observed effect where the expectation "
            "asks to see the number move, and 29 is the number that did not move. "
            "A human should look."
        ),
    ),
    TrapCase(
        case_id="av-03-effect-is-reported-not-observed",
        judge=ACTION_VERIFIER,
        shape="the action REPORTS its effect; the world cannot have moved yet",
        subject=VerifySubject(
            plan=_replay_plan(),
            probe_summary='{"total":1,"items":[{"job_id":"fc8d2a03-23b3-5371-9acb-46443c73baa5"}]}',
            action_summary=(
                '{"ok":true,"replayed":0,"scheduled":1,"execute_at":"2026-09-17T12:15:00Z"}'
            ),
        ),
        asserts="verified",
        why=(
            "A delayed replay reports 'scheduled 1' with an execute_at fifteen "
            "minutes out; the platform holds the timer, so the row is still "
            "listed and that is correct. The action's own reply IS the evidence "
            "here, which the rubric says in as many words. Demanding to see the "
            "queue shrink inside the verify window would fail every correct run "
            "of this shape — the failure the action reply was added to the "
            "judge's context to stop."
        ),
    ),
    TrapCase(
        case_id="av-04-read-carries-an-error",
        judge=ACTION_VERIFIER,
        shape="the verify read came back as an error",
        subject=VerifySubject(
            plan=_cache_plan(),
            probe_summary='{"ok":false,"error":"key lookup failed: connection reset"}',
            action_summary='{"ok":true,"deleted":true,"key":"orders:hot"}',
        ),
        asserts="not_verified",
        why=(
            "The rubric is explicit: ok=false or an error set is not_verified. "
            "The action may well have worked — 'deleted: true' says it did — but "
            "the verify read proved nothing, and 'probably fine' is not a "
            "verification. This is the one case where a not_verified is right "
            "even though the action's own reply looks good."
        ),
    ),
    TrapCase(
        case_id="av-05-filtered-read-proves-its-slice",
        judge=ACTION_VERIFIER,
        shape="the verify read is filtered and proves only that slice",
        subject=VerifySubject(
            plan=_replay_plan(),
            probe_summary='{"total":0,"items":[]}',
            action_summary='{"ok":true,"replayed":1,"scheduled":0}',
        ),
        asserts="verified",
        why=(
            "The action replayed the one row it named and the verify read of the "
            "slice it was in returns total 0. The read is scoped by "
            "remediation_hint='replay_safe', which is rendered beside its result, "
            "and the expectation is about that slice — so the slice being drained "
            "is the expectation met. Rows outside the filter were never read and "
            "are not what this plan claimed to fix. INC-001's any_of is the "
            "deterministic half of the same rule."
        ),
    ),
    TrapCase(
        case_id="av-06-read-is-of-another-resource",
        judge=ACTION_VERIFIER,
        shape="the verify read is healthy, but of a different resource",
        subject=VerifySubject(
            plan=_lag_plan(),
            probe_summary='{"consumer_group":"notifications","lag":0,"lag_known":true}',
            action_summary='{"ok":true,"restarted":true,"consumer_group":"worker-dispatcher"}',
        ),
        asserts="not_verified",
        why=(
            "A healthy reading of the wrong group says nothing about the one that "
            "alerted. The rubric asks the verdict to be grounded in the probe "
            "response, and this response is about notifications while the action "
            "restarted worker-dispatcher. A judge that reads 'lag 0' and stops "
            "reading is the stale-cache trap from LESSONS 2026-09-07 in judge "
            "form: server-wide health standing in for the resource's own state."
        ),
    ),
)


# ---------------------------------------------------------------------------
# briefing_judge


def _briefing(
    *,
    reason: str,
    trail: Sequence[ProbeSummary],
    findings: str,
    recommendation: str,
    state: IncidentState = IncidentState.ESCALATED,
) -> EscalationBriefing:
    return EscalationBriefing(
        incident_id=str(_INCIDENT),
        final_state=state,
        alert_summary="platform.dlq critical: dead-letter depth above warning",
        escalation_reason=reason,
        investigation_trail=tuple(trail),
        findings=findings,
        recommendation=recommendation,
        budget_used={"tool_calls": 4, "tokens": 18_000, "wall_seconds": 41.2, "usd": "0.13"},
    )


_FILTERED_READ: Final[ProbeSummary] = ProbeSummary(
    tool="list_dlq_messages",
    summary='{"total":0,"items":[]}',
    arguments={"remediation_hint": "replay_safe"},
)
_WHOLE_QUEUE: Final[ProbeSummary] = ProbeSummary(
    tool="list_dlq_messages",
    summary='{"total":5,"items":[{"job_id":"fc8d2a03"},{"job_id":"eb798430"}]}',
    arguments={},
)
_LAG_READ: Final[ProbeSummary] = ProbeSummary(
    tool="get_consumer_lag",
    summary='{"consumer_group":"worker-dispatcher","lag":29,"lag_known":true}',
    arguments={"consumer_group": "worker-dispatcher"},
)

_BRIEFING_TRAPS: Final[tuple[TrapCase, ...]] = (
    TrapCase(
        case_id="bj-01-grounded-and-actionable",
        judge=BRIEFING_JUDGE,
        shape="a briefing that is right on both dimensions",
        subject=BriefingSubject(
            briefing=_briefing(
                reason="budget exhausted after four probes",
                trail=[_WHOLE_QUEUE, _LAG_READ],
                findings=(
                    "The dead-letter queue holds 5 rows. Lag on worker-dispatcher "
                    "reads 29 and did not move across the probes taken."
                ),
                recommendation=(
                    "Re-read get_consumer_lag for worker-dispatcher after 90 "
                    "seconds; if it is still 29, restart that consumer group."
                ),
            )
        ),
        asserts=briefing_verdict(grounded=True, actionable=True),
        why=(
            "Every number in the findings is in the trail (5 rows, lag 29) and the "
            "recommendation names a tool, a resource and a wait a human can act on "
            "inside fifteen minutes. This is the case that catches a judge which "
            "marks everything down: with no clean case in the set, a judge that "
            "always says 'not grounded' would score perfectly on the traps."
        ),
    ),
    TrapCase(
        case_id="bj-02-grounded-but-useless",
        judge=BRIEFING_JUDGE,
        shape="grounded, and gives the human nothing to do (§ 107)",
        subject=BriefingSubject(
            briefing=_briefing(
                reason="budget exhausted after four probes",
                trail=[_WHOLE_QUEUE, _LAG_READ],
                findings="The dead-letter queue holds 5 rows and lag reads 29.",
                recommendation="Escalate to a human for further investigation.",
            )
        ),
        asserts=briefing_verdict(grounded=True, actionable=False),
        why=(
            "Both findings are read straight off the trail, so groundedness is "
            "high. The recommendation is the rubric's own 0.0 example — 'escalate "
            "to a human' inside an escalation briefing restates the situation and "
            "names no step. The two dimensions are meant to come apart here, and a "
            "judge that averages them into one 'fine' is the reason the verdict is "
            "reported per dimension."
        ),
    ),
    TrapCase(
        case_id="bj-03-actionable-but-invented",
        judge=BRIEFING_JUDGE,
        shape="useful, and carries an invented fact (§ 107)",
        subject=BriefingSubject(
            briefing=_briefing(
                reason="budget exhausted after four probes",
                trail=[_WHOLE_QUEUE, _LAG_READ],
                findings=(
                    "The queue holds 5 rows. The upstream billing API returned 503 "
                    "for 11 minutes, which is what pushed them there."
                ),
                recommendation=(
                    "Page the billing team and ask them to confirm the 503 window, "
                    "then re-read the queue."
                ),
            )
        ),
        asserts=briefing_verdict(grounded=False, actionable=True),
        why=(
            "The recommendation is concrete and aimed at a named team, so "
            "actionability is high. The 503 window and the billing API appear "
            "nowhere in the trail — two readings, neither of which mentions an "
            "upstream — so the finding is invented, and an invented fact is what "
            "groundedness is for. A specific number nobody read is the most "
            "dangerous shape a briefing takes, because it reads as the best-"
            "evidenced line in it."
        ),
    ),
    TrapCase(
        case_id="bj-04-stabilizer-reported-as-resolution",
        judge=BRIEFING_JUDGE,
        shape="a stabilizer reported as a resolution (§ 107)",
        subject=BriefingSubject(
            briefing=_briefing(
                reason=(
                    "the chain was paused so it would stop retrying; the incident "
                    "is not over and the saga still needs a decision"
                ),
                trail=[
                    ProbeSummary(
                        tool="get_dag_state",
                        summary='{"root_job_id":"9f2","paused":true,"children":7}',
                        arguments={"root_job_id": "9f2"},
                    )
                ],
                findings=(
                    "The runaway chain has been stopped and the incident is "
                    "resolved; no further action is needed."
                ),
                recommendation="No action required.",
            )
        ),
        asserts=briefing_verdict(grounded=False, actionable=False),
        why=(
            "Pausing the chain stopped the bleeding; it did not decide what to do "
            "with seven queued children, and the escalation reason in the judge's "
            "own context says so in as many words. 'The incident is resolved' "
            "contradicts the evidence beside it, which is the rubric's 0.0 "
            "groundedness anchor, and 'no action required' is the 0.0 "
            "actionability anchor. A judge that blesses this is the one that would "
            "let a half-fix close an incident."
        ),
    ),
    TrapCase(
        case_id="bj-05-inc-002-honest-after-a-filtered-read",
        judge=BRIEFING_JUDGE,
        shape="INC-002: an honest briefing after a filtered read is GROUNDED",
        subject=BriefingSubject(
            briefing=_briefing(
                state=IncidentState.RESOLVED,
                reason="",
                trail=[_WHOLE_QUEUE, _FILTERED_READ],
                findings=(
                    "fc8d2a03 was replayed and the confirmed-safe slice now reads "
                    "total 0. Four rows remain outside that slice; they were never "
                    "read after the action and are not shown to be gone."
                ),
                recommendation=(
                    "List the dead-letter queue unfiltered and decide on the four "
                    "remaining rows; eb798430 is marked as needing a human."
                ),
            )
        ),
        asserts=briefing_verdict(grounded=True, actionable=True),
        why=(
            "This is INC-002 in the bytes that scored 0.0. The filtered read "
            "returns total 0 with remediation_hint='replay_safe' rendered beside "
            "it; the briefing reports the slice drained and says plainly that the "
            "rows outside it were not read. That is the honesty the writer prompt "
            "asks for, so the judge that widened the filtered read and called it "
            "'all 5 messages are gone' made the writer's forbidden overclaim in "
            "its own voice. A judge that fails this trap has regressed INC-002, "
            "and this case exists so the regression is a red rather than a "
            "surprise on a paid archive."
        ),
    ),
    TrapCase(
        case_id="bj-06-invented-and-useless",
        judge=BRIEFING_JUDGE,
        shape="wrong on both dimensions",
        subject=BriefingSubject(
            briefing=_briefing(
                reason="budget exhausted after four probes",
                trail=[_LAG_READ],
                findings=(
                    "A configuration change rolled out at 11:40 reduced the "
                    "consumer pool from 12 replicas to 3."
                ),
                recommendation="Investigate further.",
            )
        ),
        asserts=briefing_verdict(grounded=False, actionable=False),
        why=(
            "One lag reading in the trail, and neither a deploy, a time, nor a "
            "replica count anywhere in it; 'investigate further' names no step. "
            "The floor case: a judge that cannot mark this down on both dimensions "
            "is not discriminating at all, and its agreement on the other five "
            "would be luck."
        ),
    ),
)


# ---------------------------------------------------------------------------
# candidate_selector


def _selector_run_state(evidence: Sequence[EvidenceEntry]) -> RunState:
    return RunState(
        incident_id=_INCIDENT,
        state=IncidentState.INVESTIGATING,
        alert={"source": "platform.kafka", "severity": "high", "group": "worker-dispatcher"},
        budget=BudgetLedger(
            max_tool_calls=13,
            max_tokens=200_000,
            max_wall_seconds=600,
            max_usd=Decimal("1.00"),
        ),
        evidence=tuple(evidence),
        created_at=_AT,
        updated_at=_AT,
    )


def _reading(
    evidence_id: UUID, tool: str, arguments: dict[str, object], summary: str
) -> EvidenceEntry:
    return EvidenceEntry(
        evidence_id=evidence_id,
        tool_name=tool,
        arguments=arguments,
        result_summary=summary,
        timestamp=_AT,
    )


def _selection(
    evidence: Sequence[EvidenceEntry], candidates: Sequence[dict[str, object]]
) -> SelectionSubject:
    """Build one selector question, with its candidates validated in place.

    Validated inside ``grounded_in`` rather than after it, so a citation that
    does not resolve against this trap's own trail raises at import — the same
    seam the strategies bind at (ADR 0042). A trap that cites a reading it did
    not include is a broken trap, and it should be impossible to commit rather
    than merely wrong.
    """
    run_state = _selector_run_state(evidence)
    with grounded_in(run_state.evidence):
        built = tuple(DiagnosisCandidate.model_validate(raw) for raw in candidates)
    return SelectionSubject(run_state=run_state, candidates=built)


_LAG_CLIMBING: Final[EvidenceEntry] = _reading(
    _E1,
    "get_consumer_lag",
    {"consumer_group": "worker-dispatcher"},
    '{"consumer_group":"worker-dispatcher","lag":35,"recent_samples":[0,16,35],"lag_known":true}',
)
_KEY_INFO: Final[EvidenceEntry] = _reading(
    _E2,
    "get_cache_key_info",
    {"key": "orders:hot"},
    '{"key":"orders:hot","exists":true,"records_referenced":3,"records_found":3}',
)
_HUMAN_SLICE: Final[EvidenceEntry] = _reading(
    _E3,
    "list_dlq_messages",
    {"remediation_hint": "human_required"},
    '{"total":1,"items":[{"job_id":"eb798430","error":"row 4: unparseable date"}]}',
)

_SELECTOR_TRAPS: Final[tuple[TrapCase, ...]] = (
    TrapCase(
        case_id="cs-01-correct-candidate-thin-evidence",
        judge=CANDIDATE_SELECTOR,
        shape="the correct candidate, and its evidence is thin (§ 107)",
        subject=_selection(
            [_LAG_CLIMBING],
            [
                {
                    "candidate_id": "c1",
                    "category": HypothesisCategory.CONSUMER_SATURATION,
                    "name": "worker-dispatcher lag climbing across three samples",
                    "confidence": 0.55,
                    "evidence_for": [{"evidence_id": _E1}],
                    "evidence_against": [],
                    "next_probe": ProbeAction(
                        tool_name="get_consumer_lag",
                        arguments={"consumer_group": "worker-dispatcher"},
                    ),
                },
                {
                    "candidate_id": "c2",
                    "category": HypothesisCategory.DEPLOY_REGRESSION,
                    "name": "a recent rollout changed consumer behaviour",
                    "confidence": 0.3,
                    "evidence_for": [],
                    "evidence_against": [],
                    "next_probe": ProbeAction(tool_name="get_deploy_history", arguments={}),
                },
            ],
        ),
        asserts=f"{SELECT_PREFIX}c1",
        why=(
            "One reading in the trail, and it says exactly what c1 claims: lag on "
            "the alerted group over three samples, 0 then 16 then 35. c2 cites "
            "nothing and nothing in the trail mentions a rollout. Thin evidence "
            "that points one way is still evidence pointing one way, and this trap "
            "is here because a selector that treats 'only one reading' as 'cannot "
            "decide' would answer probe_more to every early step and never commit."
        ),
    ),
    TrapCase(
        case_id="cs-02-wrong-candidate-plausible-evidence",
        judge=CANDIDATE_SELECTOR,
        shape="the wrong candidate, dressed in plausible evidence (§ 107)",
        subject=_selection(
            [_KEY_INFO],
            [
                {
                    "candidate_id": "c1",
                    "category": HypothesisCategory.STALE_CACHE,
                    "name": "orders:hot is serving records the database no longer has",
                    "confidence": 0.8,
                    "evidence_for": [{"evidence_id": _E2}],
                    "evidence_against": [],
                    "next_probe": None,
                },
                {
                    "candidate_id": "c2",
                    "category": HypothesisCategory.NO_FAULT,
                    "name": "the orders:hot entry matches the database",
                    "confidence": 0.4,
                    "evidence_for": [{"evidence_id": _E2}],
                    "evidence_against": [],
                    "next_probe": None,
                },
            ],
        ),
        asserts=f"{SELECT_PREFIX}c2",
        why=(
            "Both candidates cite the same reading, and the reading decides it "
            "against the confident one: records_referenced 3 and records_found 3 "
            "is an entry that matches the database. c1 states 0.8 and is "
            "contradicted by the line it cites; c2 states 0.4 and is what the line "
            "says. A selector that reads the citation rather than the confidence "
            "picks c2 — and one that follows the stated confidence is the failure "
            "this trap is built to expose."
        ),
    ),
    TrapCase(
        case_id="cs-03-near-duplicates-one-correct",
        judge=CANDIDATE_SELECTOR,
        shape="two near-duplicates, one of them correct (§ 107)",
        subject=_selection(
            [_LAG_CLIMBING],
            [
                {
                    "candidate_id": "c1",
                    "category": HypothesisCategory.CONSUMER_SATURATION,
                    "name": "worker-dispatcher lag is climbing, 0 to 35 across three samples",
                    "confidence": 0.7,
                    "evidence_for": [{"evidence_id": _E1}],
                    "evidence_against": [],
                    "next_probe": ProbeAction(
                        tool_name="get_consumer_lag",
                        arguments={"consumer_group": "worker-dispatcher"},
                    ),
                },
                {
                    "candidate_id": "c2",
                    "category": HypothesisCategory.CONSUMER_SATURATION,
                    "name": "notifications lag is climbing and worker-dispatcher follows it",
                    "confidence": 0.65,
                    "evidence_for": [{"evidence_id": _E1}],
                    "evidence_against": [],
                    "next_probe": ProbeAction(
                        tool_name="get_consumer_lag", arguments={"consumer_group": "notifications"}
                    ),
                },
            ],
        ),
        asserts=f"{SELECT_PREFIX}c1",
        why=(
            "Same category, almost the same words, and one of them names a group "
            "the trail never read. The cited reading is scoped to "
            "worker-dispatcher — the arguments are rendered beside it — so it "
            "supports c1 and says nothing about notifications. Two candidates that "
            "look alike are separated by which reading actually backs them, not by "
            "how similar their names are."
        ),
    ),
    TrapCase(
        case_id="cs-04-all-wrong-so-escalate",
        judge=CANDIDATE_SELECTOR,
        shape="every candidate is wrong and no read would decide it (§ 107)",
        subject=_selection(
            [_HUMAN_SLICE],
            [
                {
                    "candidate_id": "c1",
                    "category": HypothesisCategory.POISON_MESSAGE,
                    "name": "a producer emitted a malformed row",
                    "confidence": 0.35,
                    "evidence_for": [{"evidence_id": _E3}],
                    "evidence_against": [],
                    "next_probe": None,
                },
                {
                    "candidate_id": "c2",
                    "category": HypothesisCategory.PERSISTENT_DATA_BUG,
                    "name": "the uploaded source data carries an unparseable date",
                    "confidence": 0.35,
                    "evidence_for": [{"evidence_id": _E3}],
                    "evidence_against": [],
                    "next_probe": None,
                },
                {
                    "candidate_id": "c3",
                    "category": HypothesisCategory.DEPLOY_REGRESSION,
                    "name": "a schema change broke the consumer's parser",
                    "confidence": 0.3,
                    "evidence_for": [{"evidence_id": _E3}],
                    "evidence_against": [],
                    "next_probe": None,
                },
            ],
        ),
        asserts="escalate",
        why=(
            "One reading, cited by all three, and it cannot tell them apart: a row "
            "marked as needing a human with an unparseable date could be any of a "
            "producer bug, bad source data or a schema change. Every candidate "
            "states next_probe null, so nothing is left to read, and the row is "
            "already flagged for a person. This is escalate, not probe_more — and "
            "a selector that answers probe_more with no probe available is the "
            "distinction ADR 0048's check 4 exists to force."
        ),
    ),
    TrapCase(
        case_id="cs-05-contradicted-candidate",
        judge=CANDIDATE_SELECTOR,
        shape="a candidate contradicted by a reading in its own trail",
        subject=_selection(
            [_LAG_CLIMBING, _KEY_INFO],
            [
                {
                    "candidate_id": "c1",
                    "category": HypothesisCategory.STALE_CACHE,
                    "name": "orders:hot is stale and is starving the consumer",
                    "confidence": 0.9,
                    "evidence_for": [{"evidence_id": _E2}],
                    "evidence_against": [],
                    "next_probe": None,
                },
                {
                    "candidate_id": "c2",
                    "category": HypothesisCategory.CONSUMER_SATURATION,
                    "name": "worker-dispatcher lag is climbing on its own",
                    "confidence": 0.6,
                    "evidence_for": [{"evidence_id": _E1}],
                    "evidence_against": [{"evidence_id": _E2}],
                    "next_probe": ProbeAction(
                        tool_name="get_consumer_lag",
                        arguments={"consumer_group": "worker-dispatcher"},
                    ),
                },
            ],
        ),
        asserts=f"{SELECT_PREFIX}c2",
        why=(
            "c1 states 0.9 and the reading it cites contradicts it outright — "
            "records_referenced 3, records_found 3 is an entry that is not stale. "
            "ADR 0048's check 2 says a contradicted candidate scores at or near "
            "0.0 whatever confidence it stated. c2 cites the climbing lag for it "
            "and the same key reading against it, which is a candidate that has "
            "read its own trail."
        ),
    ),
    TrapCase(
        case_id="cs-06-one-more-read-would-decide-it",
        judge=CANDIDATE_SELECTOR,
        shape="the trail cannot separate the leaders and one read would",
        subject=_selection(
            [
                _reading(
                    _E1,
                    "get_cache_key_info",
                    {"key": "orders:hot"},
                    '{"key":"orders:hot","exists":true,"size_bytes":90}',
                )
            ],
            [
                {
                    "candidate_id": "c1",
                    "category": HypothesisCategory.STALE_CACHE,
                    "name": "orders:hot holds records the database no longer has",
                    "confidence": 0.55,
                    "evidence_for": [{"evidence_id": _E1}],
                    "evidence_against": [],
                    "next_probe": ProbeAction(
                        tool_name="get_cache_key_info", arguments={"key": "orders:hot"}
                    ),
                },
                {
                    "candidate_id": "c2",
                    "category": HypothesisCategory.PERSISTENT_DATA_BUG,
                    "name": "the records behind orders:hot were written wrong",
                    "confidence": 0.5,
                    "evidence_for": [{"evidence_id": _E1}],
                    "evidence_against": [],
                    "next_probe": None,
                },
            ],
        ),
        asserts="probe_more",
        why=(
            "The one reading says the entry exists and how big it is, and that is "
            "consistent with both candidates: nothing has been read about whether "
            "its contents match the database. c1 names a read of that same key "
            "that would say, so a read is left that discriminates. This is the "
            "case that has to come out probe_more rather than select — a selector "
            "that commits here is guessing, and the uncertainty it reports is the "
            "number the oracle gap is read beside."
        ),
    ),
)


TRAPS: Final[Mapping[str, tuple[TrapCase, ...]]] = MappingProxyType(
    {
        ACTION_VERIFIER: _ACTION_VERIFIER_TRAPS,
        BRIEFING_JUDGE: _BRIEFING_TRAPS,
        CANDIDATE_SELECTOR: _SELECTOR_TRAPS,
    }
)

#: The floor plan 03 § 107 sets. Named so the test that enforces it and the
#: report that states it read one number.
MINIMUM_TRAPS_PER_JUDGE: Final[int] = 5


def traps_for(judge: str) -> tuple[TrapCase, ...]:
    """This judge's trap set, or a refusal naming the judges that have one."""
    try:
        return TRAPS[judge]
    except KeyError:
        known = ", ".join(sorted(TRAPS))
        raise KeyError(f"no trap set for {judge!r} (have: {known})") from None

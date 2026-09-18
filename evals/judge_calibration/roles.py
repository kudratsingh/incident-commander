"""The judge roles, by their normative names, and how each one is asked.

Plan 02 § 3 is normative about the vocabulary and says why: ``candidate_selector``
selects among diagnoses, ``plan_approval_judge`` approves a tool plus its
arguments, ``action_verifier`` decides whether an executed action worked.
"Anything in code, prompts, docs or trajectories that says *verifier* without one
of these three prefixes is a bug in the packet that introduced it" (02:25).

That rule is about the JUDGE vocabulary and nothing else, which is worth stating
because this repo holds three other things the word correctly names, none of them
a judge: the webhook signature verifier (`api/hmac_verify.py`, ADRs 0014 and
0023), the transcript-redaction verifier (`context/pack.sh`), and the
connection-pool verifier in ADR 0022. "signature verifier" is the ordinary
English for the first, and a repo-wide ban on the word would rename three
unrelated things to satisfy a rule about a fourth.

``tests/unit/test_judge_calibration.py::TestTheRoleWordIsAlwaysPrefixed`` is therefore
scoped rather than repo-wide: it sweeps the judge-role surfaces — this package,
the judge prompts, the judge call sites and a rendered calibration report — and
allows exactly three shapes. An occurrence is prefixed with ``action_``; or it is
qualified by one of the three nouns above (``signature``, ``redaction``,
``pool``), which is the declared list and the whole reason this paragraph exists;
or the bare word is in quotes, because a sentence ABOUT the word — like the one
from 02:25 above — is not a use of it. Anything else fails.

**How a judge is asked, and why through the run's own code.** Each role has a
*subject*: a small frozen carrier of one judge-shaped question that knows how to
render its own context and how to put it. Every subject calls the same function
the agent or the eval runner calls — ``judge_verification``, ``judge_briefing``,
``select_candidate`` — so the calibration measures the prompt, the context
renderer, the schema and the bounded repair a real run gets. A second copy of a
judge's context inside ``evals/`` would be the INC-002 failure exactly: one rule
about how evidence may be read, given to one reader of it and not the other.

**A verdict is a string, and the projection is the decision.** Accuracy,
false-approve and false-reject only mean something over a categorical answer, and
two of these three judges emit numbers. So each role declares how its structured
output projects onto a verdict string, and each projection is a choice worth
seeing:

* ``action_verifier`` already emits one — ``verified`` / ``not_verified``.
* ``briefing_judge`` emits two floats. The verdict reads
  ``grounded=yes actionable=no``, each half thresholded at ``USEFUL_THRESHOLD``
  (0.7), which is the only threshold the grader has and the one it already
  reports "N of M useful" against. Per-dimension rather than on the mean,
  because the mean hides the case the trap set exists to catch: grounded and
  useless and invented-but-actionable are different failures with the same
  average, and INC-002 was a groundedness error specifically.
* ``candidate_selector`` emits a decision plus an id. The verdict reads
  ``select:c1``, ``probe_more`` or ``escalate`` — the id included, because a
  selector that commits to a different candidate on every rep is not stable, and
  a verdict that dropped the id would call that agreement.

**An approval is a prefix.** One rule, so ``false_approve`` means the same thing
for all three: a verdict is an approval when it starts with the role's approval
prefix. ``verified`` approves and ``not_verified`` does not; ``grounded=yes
actionable=yes`` approves and every other pair does not; ``select:…`` approves
(the run acts on it) and ``probe_more`` / ``escalate`` do not.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import ClassVar, Final, Protocol, runtime_checkable

from evals.graders.llm_judge import (
    JUDGE_PROMPT as BRIEFING_JUDGE_PROMPT,
)
from evals.graders.llm_judge import (
    USEFUL_THRESHOLD,
    format_briefing_context,
    judge_briefing,
)
from incident_commander.agent.briefing import EscalationBriefing
from incident_commander.agent.candidates import DiagnosisCandidate, grounded_in
from incident_commander.agent.remediation import (
    ACTION_VERIFIER_ROLE,
    VERIFICATION_JUDGE_PROMPT,
    RemediationPlan,
    format_verify_context,
    judge_verification,
)
from incident_commander.agent.selection import (
    SELECTOR_PROMPT,
    SELECTOR_ROLE,
    format_selection_context,
    select_candidate,
)
from incident_commander.agent.state import RunState
from incident_commander.llm.client import LLMClientProtocol

ACTION_VERIFIER: Final[str] = ACTION_VERIFIER_ROLE
BRIEFING_JUDGE: Final[str] = "briefing_judge"
CANDIDATE_SELECTOR: Final[str] = SELECTOR_ROLE
PLAN_APPROVAL_JUDGE: Final[str] = "plan_approval_judge"

#: The judges this harness calibrates, in the order a report lists them:
#: consequence first. ``action_verifier``'s verdict resolves or escalates a live
#: incident; ``briefing_judge``'s is a soft column on an already-graded run;
#: ``candidate_selector``'s decides which diagnosis a run acts on and is the role
#: the buildout's headline number is about.
CALIBRATED_ROLES: Final[tuple[str, ...]] = (
    ACTION_VERIFIER,
    BRIEFING_JUDGE,
    CANDIDATE_SELECTOR,
)

#: Roles plan 03 § 9 asks for that do not exist, and what is there instead.
#:
#: Divergence B6. This is a register rather than an omission because a reader of
#: a calibration report that simply lacked a ``plan_approval_judge`` section
#: cannot tell "not calibrated" from "not a judge", and plan 02:22's own table
#: says "Exists today? Yes" — so the next reader will go looking. The answer is
#: in the report, once, with the ADRs that made it deterministic.
ABSENT_ROLES: Final[Mapping[str, str]] = MappingProxyType(
    {
        PLAN_APPROVAL_JUDGE: (
            "does not exist in any form, and is not planned: every approve/refuse "
            "decision about a remediation plan is deterministic guard code (ADRs "
            "0024, 0025, 0027, 0028, 0030, 0032), because CLAUDE.md invariant 4 "
            "forbids deriving a control from model output. `grep load_prompt "
            "src/incident_commander/agent/*.py` returns four agent-side roles "
            "(investigation_planner, remediation_planner, verification_judge, "
            "briefing_writer) and none of them approves a plan. There is nothing "
            "to calibrate here; plan 02:22's 'Exists today? Yes' is wrong "
            "(divergence B6). Calibrating a judge that does not exist would mean "
            "building one first, and building an LLM plan-approver is a change to "
            "invariant 4, not a calibration packet."
        )
    }
)


@runtime_checkable
class JudgeSubject(Protocol):
    """One judge-shaped question, able to render itself and to put itself."""

    judge: ClassVar[str]

    def context(self) -> str:
        """The exact user message the judge is shown."""

    def ask(self, *, client: LLMClientProtocol, model: str) -> str:
        """Put the question through the run's own call, and project the verdict."""


@dataclass(frozen=True, kw_only=True)
class VerifySubject:
    """An ``action_verifier`` question: a plan, its action's reply, a verify read."""

    judge: ClassVar[str] = ACTION_VERIFIER
    plan: RemediationPlan
    probe_summary: str
    action_summary: str | None = None

    def context(self) -> str:
        return format_verify_context(self.plan, self.probe_summary, self.action_summary)

    def ask(self, *, client: LLMClientProtocol, model: str) -> str:
        call = judge_verification(
            client,
            plan=self.plan,
            probe_summary=self.probe_summary,
            action_summary=self.action_summary,
            model=model,
        )
        return call.result.output.verdict


@dataclass(frozen=True, kw_only=True)
class BriefingSubject:
    """A ``briefing_judge`` question: one escalation briefing, as written."""

    judge: ClassVar[str] = BRIEFING_JUDGE
    briefing: EscalationBriefing

    def context(self) -> str:
        return format_briefing_context(self.briefing)

    def ask(self, *, client: LLMClientProtocol, model: str) -> str:
        score = judge_briefing(self.briefing, client, model)
        return briefing_verdict(
            grounded=score.groundedness >= USEFUL_THRESHOLD,
            actionable=score.actionability >= USEFUL_THRESHOLD,
        )


@dataclass(frozen=True, kw_only=True)
class SelectionSubject:
    """A ``candidate_selector`` question: a run's trail and a candidate set.

    ``candidates`` is a tuple of already-validated ``DiagnosisCandidate``, built
    by the trap file inside ``grounded_in(run_state.evidence)`` — a citation is
    resolved by a validator against a bound ledger (ADR 0042), so a trap whose
    candidate cites an id the trail does not hold cannot be constructed at all.
    That is the property, not the intention: an unbuildable trap is better than a
    trap that silently asks the selector about evidence nobody has.
    """

    judge: ClassVar[str] = CANDIDATE_SELECTOR
    run_state: RunState
    candidates: tuple[DiagnosisCandidate, ...]

    def context(self) -> str:
        return format_selection_context(self.run_state, self.candidates)

    def ask(self, *, client: LLMClientProtocol, model: str) -> str:
        with grounded_in(self.run_state.evidence):
            call = select_candidate(
                client,
                run_state=self.run_state,
                candidates=self.candidates,
                model=model,
            )
        result = call.result.output
        if result.selected_candidate_id is not None:
            return f"{SELECT_PREFIX}{result.selected_candidate_id}"
        return str(result.decision.value)


#: ``select:`` — the selector's approval verdict carries the id it committed to.
SELECT_PREFIX: Final[str] = "select:"


def briefing_verdict(*, grounded: bool, actionable: bool) -> str:
    """The ``briefing_judge``'s two floats as one verdict string.

    Spelled in one place so the trap file, the projection and the report cannot
    disagree about what "grounded but useless" reads as.
    """
    return f"grounded={_yn(grounded)} actionable={_yn(actionable)}"


def _yn(value: bool) -> str:
    return "yes" if value else "no"


@dataclass(frozen=True, kw_only=True)
class JudgeRole:
    """One calibrated judge: its prompt, its verdict space, and what approval is.

    ``approval_prefix`` is what makes ``false_approve`` one definition rather
    than three. ``verdicts`` is the closed set where there is one — the selector
    has an open one, because its approval carries a candidate id the trap
    declares — and it is carried so a report can state the space a rate was
    computed over rather than leave a reader to infer it.
    """

    name: str
    prompt: str
    approval_prefix: str
    verdicts: tuple[str, ...]
    what_it_decides: str


ROLES: Final[Mapping[str, JudgeRole]] = MappingProxyType(
    {
        ACTION_VERIFIER: JudgeRole(
            name=ACTION_VERIFIER,
            prompt=VERIFICATION_JUDGE_PROMPT,
            approval_prefix="verified",
            verdicts=("verified", "not_verified"),
            what_it_decides=(
                "whether an executed Tier-1 action worked. `verified` marks the "
                "incident RESOLVED; `not_verified` escalates it to a human. The "
                "only one of the three whose verdict gates a live outcome, and "
                "the reason plan 03 § 104's omission of it is divergence J6."
            ),
        ),
        BRIEFING_JUDGE: JudgeRole(
            name=BRIEFING_JUDGE,
            prompt=BRIEFING_JUDGE_PROMPT,
            approval_prefix=briefing_verdict(grounded=True, actionable=True),
            verdicts=(
                briefing_verdict(grounded=True, actionable=True),
                briefing_verdict(grounded=True, actionable=False),
                briefing_verdict(grounded=False, actionable=True),
                briefing_verdict(grounded=False, actionable=False),
            ),
            what_it_decides=(
                "how grounded and how actionable one escalation briefing is. "
                "Informational: the run is already graded on five deterministic "
                "dimensions by the time this is called, and nothing passes or "
                "fails on it. INC-002 is this judge scoring an honest briefing "
                "0.0 for groundedness."
            ),
        ),
        CANDIDATE_SELECTOR: JudgeRole(
            name=CANDIDATE_SELECTOR,
            prompt=SELECTOR_PROMPT,
            approval_prefix=SELECT_PREFIX,
            verdicts=(f"{SELECT_PREFIX}<candidate_id>", "probe_more", "escalate"),
            what_it_decides=(
                "which candidate diagnosis a run commits to, or that it should "
                "read more, or that a human is needed. Selection is not "
                "authorization: the tier policy and every refusal apply "
                "afterwards unchanged (ADR 0048)."
            ),
        ),
    }
)


def is_approval(judge: str, verdict: str) -> bool:
    """Did this verdict bless its subject? One rule for all three roles."""
    return verdict.startswith(ROLES[judge].approval_prefix)


def role(judge: str) -> JudgeRole:
    """One calibrated role, or a refusal naming the ones that exist.

    An absent role is refused with the reason from ``ABSENT_ROLES`` rather than
    with a bare ``KeyError``, because "plan_approval_judge" is the name somebody
    will type — the plan told them to.
    """
    if judge in ROLES:
        return ROLES[judge]
    if judge in ABSENT_ROLES:
        raise KeyError(f"{judge} {ABSENT_ROLES[judge]}")
    known = ", ".join(CALIBRATED_ROLES)
    raise KeyError(f"unknown judge role {judge!r} (calibrated: {known})")


def rubric_of(judge: str) -> Sequence[str]:
    """The judge's rubric, as lines, for hashing and for counting.

    Read through ``load_prompt`` so the calibration hashes the bytes the call
    loads, not a file it found by path.
    """
    from incident_commander.llm.prompts.loader import load_prompt

    return load_prompt(role(judge).prompt).splitlines()

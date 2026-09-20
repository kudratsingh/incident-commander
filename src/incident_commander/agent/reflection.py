"""``StepCritique``, its context and the ``reflection_critic`` call (plan 02 § 13, WP-9.1).

No strategy here — ``strategies/reflection.py`` turns a verdict into at most one revised step.
The four finding classes are plan 02 § 13's, the verdict is tied to them by a validator (a
critique that names a contradiction and keeps the step anyway is LESSONS 2026-09-17's failure
mode), contradictions are grounded through ``candidates.EvidenceRef``, and ``missing_probe`` is a
``ReadToolName``, so a critic cannot name a privileged tool. No temperature (decision O-24).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from incident_commander.agent.candidates import EvidenceRef, grounded_in
from incident_commander.agent.hypothesis import (
    InvestigationStep,
    NextAction,
    ProbeAction,
    ReadToolName,
)
from incident_commander.agent.planner_context import format_planner_context
from incident_commander.agent.state import RunState
from incident_commander.llm.client import LLMClientProtocol
from incident_commander.llm.prompts.loader import load_prompt
from incident_commander.llm.repair import RepairedCall, call_with_output_repair
from incident_commander.llm.structured import StructuredOutput

#: Prompt file the critic role is asked with, and the addendum the revising planner call
#: appends to ``investigation_planner.md``. Named so the strategy, the snapshot suite and
#: the loader each spell them once.
CRITIC_PROMPT: Final[str] = "reflection_critic"
REVISION_PROMPT: Final[str] = "investigation_planner_revision"

#: Role label for the accounting split and the trace. Its own role, so the critique's cost is
#: separable from the planner's — "added tokens" is the number this packet reports.
CRITIC_ROLE: Final[str] = "reflection_critic"

#: Revision passes one planner step may spend. ONE, and deliberately not configurable: a bound
#: an operator can raise is not a bound, and a bound stated only in the prompt fails open.
MAX_REVISION_PASSES: Final[int] = 1

#: Named so a test asserts the guard's own marker rather than that something raised (F-007).
KEEP_WITH_FINDINGS: Final[str] = "the verdict is 'keep' and findings were named"
REVISE_WITHOUT_FINDINGS: Final[str] = "the verdict is 'revise' and no finding was named"
CAP_ALREADY_SPENT: Final[str] = "this step's one revision pass is already spent"


class ReflectionCapExceeded(RuntimeError):
    """A second revision pass was asked for on one step.

    Not an ``LLMError``: the loop escalates on those, and a breached cap is a defect in this
    module rather than something a run should absorb and report as an escalation.
    """

    def __init__(self, spent: int, allowed: int) -> None:
        super().__init__(
            f"{CAP_ALREADY_SPENT} ({spent} of {allowed} used). Reflection is one "
            "bounded pass per step (plan 02 § 13); a second pass is an unbounded "
            "critic loop, which is the thing the cap exists to prevent."
        )
        self.spent = spent
        self.allowed = allowed


@dataclass(slots=True)
class RevisionPass:
    """One step's revision budget: one pass, spendable once, then it raises.

    The cap as an object rather than as the absence of a loop: a later edit that wrapped the
    revision in one would raise here instead of billing a third planner call.
    """

    allowed: int = MAX_REVISION_PASSES
    spent: int = 0

    def spend(self) -> None:
        """Take this step's pass, or raise ``ReflectionCapExceeded``."""
        if self.spent >= self.allowed:
            raise ReflectionCapExceeded(self.spent, self.allowed)
        self.spent += 1

    @property
    def exhausted(self) -> bool:
        return self.spent >= self.allowed


class RevisionVerdict(StrEnum):
    # No class docstring: pydantic copies an enum's into ``$defs.<Enum>.description``,
    # which the model reads on ``record_output`` (LESSONS 2026-09-17, plat #210).
    KEEP = "keep"
    REVISE = "revise"


class LedgerContradiction(StructuredOutput):
    # No class docstring here either, same reason as above.
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence: EvidenceRef = Field(
        description=(
            "The evidence ledger entry whose reading contradicts the step. Cite "
            "one of the evidence_ids you were shown."
        )
    )
    contradicted_claim: str = Field(
        min_length=1,
        description=(
            "What the step claims that this reading contradicts, in one sentence. "
            "Quote the part of the reading that rules it out."
        ),
    )


class StepCritique(StructuredOutput):
    # No class docstring here either, same reason as above.
    model_config = ConfigDict(frozen=True, extra="forbid")

    unsupported_assumptions: tuple[str, ...] = Field(
        default=(),
        description=(
            "Claims the step's reasoning rests on that no reading in the evidence "
            "supports. One short sentence each; empty when there are none."
        ),
    )
    contradictions: tuple[LedgerContradiction, ...] = Field(
        default=(),
        description=(
            "Readings in the evidence that rule out what the step claims. Empty "
            "when there are none."
        ),
    )
    unexplained_symptoms: tuple[str, ...] = Field(
        default=(),
        description=(
            "Things the evidence shows that the step's diagnosis does not account "
            "for. One short sentence each; empty when there are none."
        ),
    )
    missing_probe: ReadToolName | None = Field(
        default=None,
        description=(
            "A read tool from the list you were shown that would tell the leading "
            "hypotheses apart and that the step does not run. null when no "
            "remaining read would discriminate."
        ),
    )
    verdict: RevisionVerdict = Field(
        description=(
            "'keep' when you found nothing and the step stands as written; "
            "'revise' when you named at least one finding above. The schema "
            "rejects any other combination."
        )
    )
    reasoning: str = Field(
        min_length=1,
        description=(
            "Two sentences at most: what decided the verdict, naming the probe you "
            "read it from. Plain prose, no lists or markdown."
        ),
    )

    @property
    def findings(self) -> tuple[str, ...]:
        """Every finding as one class-prefixed line, in plan 02 § 13's order.

        One projection, so the validator, the record and the report count the same things.
        """
        lines = [f"unsupported_assumption: {claim}" for claim in self.unsupported_assumptions]
        lines += [
            f"contradiction: {item.contradicted_claim} (evidence {item.evidence.evidence_id})"
            for item in self.contradictions
        ]
        lines += [f"unexplained_symptom: {claim}" for claim in self.unexplained_symptoms]
        if self.missing_probe is not None:
            lines.append(f"missing_probe: {self.missing_probe}")
        return tuple(lines)

    @property
    def contradicted_evidence_ids(self) -> tuple[UUID, ...]:
        """The ledger entries this critique says the step contradicts."""
        return tuple(item.evidence.evidence_id for item in self.contradictions)

    @model_validator(mode="after")
    def _the_verdict_follows_the_findings(self) -> StepCritique:
        """``keep`` iff nothing was found — the structural half of LESSONS 2026-09-17.

        A critique that lists a contradiction and approves the step is quoting a fact and
        drawing the opposite conclusion from it; the schema refuses it, so ADR 0035 re-asks
        once and the rejected leg is billed rather than silently accepted.
        """
        named = self.findings
        if self.verdict is RevisionVerdict.KEEP and named:
            raise ValueError(
                f"{KEEP_WITH_FINDINGS}: {'; '.join(named)}. A finding you named and "
                "then approved anyway is a contradiction in the critique itself. "
                "Either the finding is real, and the verdict is 'revise', or it is "
                "not, and it does not belong in the list."
            )
        if self.verdict is RevisionVerdict.REVISE and not named:
            raise ValueError(
                f"{REVISE_WITHOUT_FINDINGS}. A revision with nothing named gives "
                "the planner nothing to act on. Name what is wrong, or keep the step."
            )
        return self


#: Headings of the two blocks the critic and the reviser are shown beyond the planner's own
#: context. Named so the prompts and the tests spell them once each.
PROPOSED_STEP_HEADING: Final[str] = "The step the planner proposed:"
CRITIQUE_HEADING: Final[str] = "A reviewer read that step against the same evidence and found:"
NO_FINDINGS_LINE: Final[str] = "  (no findings)"
UNRESOLVED_EVIDENCE: Final[str] = "(no such entry in the ledger)"


def render_action(action: NextAction) -> str:
    """One ``next_action``, as both readers are shown it.

    Arguments are rendered for a probe (INC-002): a filtered read and an unfiltered one share
    a tool name, and a critic that cannot see the filter cannot fault its absence.
    """
    if isinstance(action, ProbeAction):
        return f"probe {action.tool_name}({json.dumps(action.arguments, sort_keys=True)})"
    return f"{action.kind}: {action.reason}"


def render_proposed_step(step: InvestigationStep) -> str:
    """The ranking and the action under review, in the validator's own order."""
    lines = [PROPOSED_STEP_HEADING]
    for rank, hypothesis in enumerate(step.hypotheses):
        lines.append(
            f"  {rank}. category={hypothesis.category.value} name={hypothesis.name} "
            f"confidence={hypothesis.confidence}"
        )
        lines.append(f"      reasoning: {hypothesis.reasoning}")
    lines.append(f"  next_action: {render_action(step.next_action)}")
    return "\n".join(lines)


def render_critique(run_state: RunState, critique: StepCritique) -> str:
    """The findings, with each cited id resolved to the ledger line it names.

    The reviser is shown ``baseline``'s context, which carries no ids, so a raw uuid would be
    a reference to something not on its page. Derived from the ledger, never invented (ADR 0047).
    """
    resolved = _ledger_lines(run_state)
    lines = [CRITIQUE_HEADING]
    lines += [f"  - unsupported assumption: {claim}" for claim in critique.unsupported_assumptions]
    for item in critique.contradictions:
        line = resolved.get(item.evidence.evidence_id, UNRESOLVED_EVIDENCE)
        lines.append(
            f"  - contradiction: {item.contradicted_claim} — the reading it contradicts: {line}"
        )
    lines += [f"  - unexplained symptom: {claim}" for claim in critique.unexplained_symptoms]
    if critique.missing_probe is not None:
        lines.append(
            "  - a read that would discriminate and the step does not run: "
            f"{critique.missing_probe}"
        )
    if len(lines) == 1:
        lines.append(NO_FINDINGS_LINE)
    lines.append(f"  reviewer's reasoning: {critique.reasoning}")
    return "\n".join(lines)


def _ledger_lines(run_state: RunState) -> Mapping[UUID, str]:
    """``evidence_id -> the line the planner was shown for it``."""
    return {
        entry.evidence_id: f"[{entry.tool_name}] {entry.result_summary}"
        for entry in run_state.evidence
    }


def format_critique_context(run_state: RunState, step: InvestigationStep) -> str:
    """What the critic is shown: the planner's own context, with ids, then its step.

    The same context, because a step cannot be faulted for what the planner was never shown;
    with ids, because a grounded contradiction is unaskable until they are on the page.
    """
    return (
        f"{format_planner_context(run_state, show_evidence_ids=True)}\n\n"
        f"{render_proposed_step(step)}"
    )


def format_revision_context(
    run_state: RunState, step: InvestigationStep, critique: StepCritique
) -> str:
    """What the revising planner call is shown: ``baseline``'s context, its step, the critique.

    ``show_evidence_ids`` stays off, so the only difference from the control group's turn is
    the two blocks appended to it (ADR 0044).
    """
    return (
        f"{format_planner_context(run_state)}\n\n"
        f"{render_proposed_step(step)}\n\n"
        f"{render_critique(run_state, critique)}"
    )


def revision_system_prompt() -> str:
    """``investigation_planner.md`` plus the revision addendum, cached by the loader.

    An addendum rather than a second planner prompt: two copies of the agent's behaviour would
    drift, and the arm comparison would become a comparison of prompts (ADR 0044's argument).
    """
    return f"{load_prompt('investigation_planner').rstrip()}\n\n{load_prompt(REVISION_PROMPT)}"


def revise_step(
    llm_client: LLMClientProtocol,
    *,
    user_message: str,
    model: str,
    output_model: type[InvestigationStep] = InvestigationStep,
) -> RepairedCall[InvestigationStep]:
    """The second and last planner call of the step: the same schema, plus the critique.

    Takes the rendered turn (``format_revision_context``) rather than rendering it, so the
    caller can measure the string that was sent instead of a second render of it.

    ``output_model`` is that same schema, passed in rather than named here: when the loop has
    withdrawn the probe for this step (ADR 0074) the revision is held to the narrowed model
    too, or a critique would be a way to get the withdrawn move back.
    """
    return call_with_output_repair(
        llm_client,
        system_prompt=revision_system_prompt(),
        user_message=user_message,
        output_model=output_model,
        model=model,
    )


def critique_step(
    llm_client: LLMClientProtocol,
    *,
    run_state: RunState,
    step: InvestigationStep,
    model: str,
) -> RepairedCall[StepCritique]:
    """Ask the ``reflection_critic`` what is wrong with the step the planner proposed.

    One call through ``call_with_output_repair``, so a critique whose verdict does not follow
    its findings is an ordinary output failure: ADR 0035 re-asks once, ADR 0015 charges both
    legs. ``grounded_in`` wraps the call so the citation validator runs inside the repair loop.
    """
    with grounded_in(run_state.evidence):
        return call_with_output_repair(
            llm_client,
            system_prompt=load_prompt(CRITIC_PROMPT),
            user_message=format_critique_context(run_state, step),
            output_model=StepCritique,
            model=model,
        )

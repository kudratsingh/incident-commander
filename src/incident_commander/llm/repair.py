"""One bounded re-ask when the agent's own structured output does not parse.

ADR 0035. A ``record_output`` payload that fails validation is a **harness
event**, not a decision the run should escalate on: the model made its call
and the harness could not read the envelope. ``llm/structured.py`` removes the
one shape we have actually seen (a nested object serialised as a string). This
module covers the rest — anything else the schema rejects gets exactly one
re-ask carrying the validation error, and then the run escalates as it always
did, with both errors in the reason.

The cap is 1, the same shape and the same reasoning as
``remediation._MAX_ARGUMENT_REFUSALS`` (ADR 0030): the information needed for
the repair is already in the conversation, so a model that cannot produce the
right shape when told exactly what was wrong with it will not produce it on
the third ask either — and an uncapped repair loop is an unbounded bill on an
unattended run.

Both calls are billed and both are accrued (ADR 0015): a re-ask that only
charged the run when it *worked* would make the failure look cheap, which is
the exact under-report ADR 0015 exists to prevent.

Only an **output** failure is repairable. ``LLMOutputError`` (the call
returned and its payload did not fit the model) and a bare ``ValueError`` /
``ValidationError`` from a fake or a direct ``model_validate`` re-ask; a
transport ``LLMError`` — a 429, a dropped connection, an exhausted retry loop
— propagates untouched, because the client has already retried it and
"your JSON was malformed" is not a useful thing to say to a rate limiter.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel, ValidationError

from incident_commander.llm.client import (
    LLMClientProtocol,
    LLMError,
    LLMOutputError,
    LLMResult,
    LLMUsage,
)
from incident_commander.llm.prompts.loader import load_prompt

#: How many times one structured-output call may be re-asked before the run
#: escalates. Same cap, and the same argument, as
#: ``remediation._MAX_ARGUMENT_REFUSALS``.
MAX_OUTPUT_REPAIRS: Final[int] = 1

#: The validation error is quoted back to the model, and a pydantic error over
#: a large payload can be longer than the payload. Trim it: the model needs the
#: field and the complaint, not the echoed input.
_MAX_ERROR_CHARS: Final[int] = 800

_ERROR_PLACEHOLDER: Final[str] = "{error}"

# Escalation-reason prefixes for a run that ended because the agent's own
# structured output could not be read. One source of truth: the transitions
# format their reason from these, and ``evals.runner._classify_failure``
# buckets a run by them. Two copies of a prose prefix is how a classifier
# silently stops matching the thing it classifies.
INVESTIGATION_PLANNER_INVALID: Final[str] = "planner output invalid"
REMEDIATION_PLANNER_INVALID: Final[str] = "planner LLM invalid"
VERIFY_JUDGE_INVALID: Final[str] = "verify judge LLM invalid"
OUTPUT_INVALID_PREFIXES: Final[tuple[str, ...]] = (
    INVESTIGATION_PLANNER_INVALID,
    REMEDIATION_PLANNER_INVALID,
    VERIFY_JUDGE_INVALID,
)

#: ``failure_class`` for a run whose only defect was the shape of its own
#: output. Named so a report reader can tell it from ``transport`` (the
#: network) and from a real disagreement with the grader.
PLANNER_OUTPUT_INVALID_CLASS: Final[str] = "planner_output_invalid"

# ``ValidationError`` is a subclass of ``ValueError``; both are listed so the
# intent is readable at the catch site rather than inferred from pydantic's
# class hierarchy.
_REPAIRABLE: Final[tuple[type[Exception], ...]] = (
    LLMOutputError,
    ValidationError,
    ValueError,
)


class OutputRepairExhausted(LLMError):
    """The call and every permitted repair failed to produce valid output.

    Carries every error in its message, and the **sum** of what all the calls
    billed as its ``usage``, so a caller that swallows it into an escalation
    charges the whole thing with the ``accrue_llm_error`` it already has.
    """

    def __init__(self, failures: Sequence[Exception]) -> None:
        first, *repairs = failures
        detail = "; ".join(
            f"repair {i} of {MAX_OUTPUT_REPAIRS} also failed validation: {err}"
            for i, err in enumerate(repairs, start=1)
        )
        super().__init__(f"{first}; {detail}" if detail else str(first))
        self.usage = _sum_usage(*(_usage_of(err) for err in failures))
        self.record_id = getattr(failures[-1], "record_id", None)
        self.failures = tuple(failures)


@dataclass(frozen=True, kw_only=True)
class RepairedCall[T: BaseModel]:
    """A structured-output call that succeeded, plus what it cost to get there.

    ``failures`` is empty on the ordinary path and holds the one billed
    output failure when a repair was needed. Callers accrue it — see
    ``agent.accounting.accrue_structured_call``.
    """

    result: LLMResult[T]
    failures: tuple[Exception, ...] = ()

    @property
    def was_repaired(self) -> bool:
        return bool(self.failures)


def call_with_output_repair[T: BaseModel](
    llm_client: LLMClientProtocol,
    *,
    system_prompt: str,
    user_message: str,
    output_model: type[T],
    model: str,
) -> RepairedCall[T]:
    """Call ``llm_client``; on an output-shape failure, re-ask once.

    Raises ``OutputRepairExhausted`` when the repair fails too, and lets any
    transport ``LLMError`` through unchanged.

    The cap is enforced by the loop bound, not by a check a later edit can
    walk past: at most ``MAX_OUTPUT_REPAIRS + 1`` calls leave this function
    on any path.
    """
    failures: list[Exception] = []
    message = user_message
    repair_of: str | None = None
    for _ in range(MAX_OUTPUT_REPAIRS + 1):
        try:
            result = llm_client.call(
                system_prompt=system_prompt,
                user_message=message,
                output_model=output_model,
                model=model,
                repair_of=repair_of,
            )
        except _REPAIRABLE as err:
            failures.append(err)
            # Each re-ask carries the ORIGINAL turn plus the latest error,
            # never a stack of previous corrections: the model is being asked
            # for the same answer in the right shape, and re-reading its own
            # failed attempts is context that competes with that.
            message = repair_message(user_message, err)
            repair_of = getattr(err, "record_id", None)
            continue
        return RepairedCall(result=result, failures=tuple(failures))
    raise OutputRepairExhausted(failures) from failures[-1]


def repair_message(user_message: str, error: Exception) -> str:
    """The original turn plus the repair turn, as one user message.

    The repair turn is a versioned prompt file (``prompts/output_repair.md``,
    snapshot-tested) rather than an inline string — CLAUDE.md's rule, and the
    reason this text is reviewable at all.

    It is appended to the original user message rather than sent as a second
    user turn because ``LLMClientProtocol.call`` carries one user message and
    the Messages API rejects two user turns in a row. The model therefore
    sees exactly what it saw the first time, followed by what was wrong with
    its answer, which is the content the re-ask needs.
    """
    turn = load_prompt("output_repair").replace(_ERROR_PLACEHOLDER, _trim(str(error)))
    return f"{user_message}\n\n{turn.rstrip()}"


def _trim(text: str) -> str:
    if len(text) <= _MAX_ERROR_CHARS:
        return text
    return f"{text[:_MAX_ERROR_CHARS]}… (error truncated)"


def _usage_of(err: Exception) -> LLMUsage | None:
    usage = getattr(err, "usage", None)
    return usage if isinstance(usage, LLMUsage) else None


def _sum_usage(*usages: LLMUsage | None) -> LLMUsage | None:
    """Add up what several billed-and-failed calls each charged.

    ``cost_of`` is linear in the four token counters, so charging the sum
    once equals charging each in turn to within the microdollar quantum.

    ``discarded_max_tokens`` is a per-request bound rather than a count, so
    it is carried as the **maximum** of the inputs while
    ``discarded_attempts`` is summed — the product stays the conservative
    over-estimate ADR 0015 asks for, and never an under-report.
    """
    present = [usage for usage in usages if usage is not None]
    if not present:
        return None
    return LLMUsage(
        input_tokens=sum(u.input_tokens for u in present),
        output_tokens=sum(u.output_tokens for u in present),
        cache_creation_tokens=sum(u.cache_creation_tokens for u in present),
        cache_read_tokens=sum(u.cache_read_tokens for u in present),
        discarded_attempts=sum(u.discarded_attempts for u in present),
        discarded_max_tokens=max(u.discarded_max_tokens for u in present),
    )

"""One bounded re-ask when the agent's own structured output does not parse.

ADR 0035: a rejected payload is a harness event, not a reason to escalate, so it
gets one re-ask carrying the validation error; both calls are accrued (ADR 0015).
Only an **output** failure is repairable, never a transport error.
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
from incident_commander.llm.structured import StructuredOutput

#: Re-asks before escalating; cap as in ``remediation._MAX_ARGUMENT_REFUSALS`` (ADR 0030).
MAX_OUTPUT_REPAIRS: Final[int] = 1

#: Quoted back to the model: it needs the complaint, not the echo.
_MAX_ERROR_CHARS: Final[int] = 800

_ERROR_PLACEHOLDER: Final[str] = "{error}"

# Escalation-reason prefixes for an unreadable output: the transitions format
# their reason from these and ``evals.runner._classify_failure`` buckets by them.
INVESTIGATION_PLANNER_INVALID: Final[str] = "planner output invalid"
REMEDIATION_PLANNER_INVALID: Final[str] = "planner LLM invalid"
VERIFY_JUDGE_INVALID: Final[str] = "verify judge LLM invalid"
OUTPUT_INVALID_PREFIXES: Final[tuple[str, ...]] = (
    INVESTIGATION_PLANNER_INVALID,
    REMEDIATION_PLANNER_INVALID,
    VERIFY_JUDGE_INVALID,
)

#: ``failure_class`` for a run whose only defect was its output shape, not ``transport``.
PLANNER_OUTPUT_INVALID_CLASS: Final[str] = "planner_output_invalid"

# ``ValidationError`` subclasses ``ValueError``; both listed for the reader.
_REPAIRABLE: Final[tuple[type[Exception], ...]] = (
    LLMOutputError,
    ValidationError,
    ValueError,
)


class OutputNotOffered(LLMError):
    """The model asked for a move its own output schema did not offer (ADR 0074).

    Raised INSTEAD of re-asking: the payload was readable, so a second billed call would
    only hear the same answer. An ``LLMError``, so anything that escalated still
    escalates; a caller that means to steer catches this type first.
    """

    def __init__(self, failure: Exception) -> None:
        super().__init__(str(failure))
        self.usage = usage_of(failure)
        self.record_id = getattr(failure, "record_id", None)
        #: The validation failure itself, so a caller can say which move was refused.
        self.failure = failure


class OutputRepairExhausted(LLMError):
    """The call and every permitted repair failed to produce valid output.

    ``usage`` is the SUM of what every call billed (``accrue_llm_error``).
    """

    def __init__(self, failures: Sequence[Exception]) -> None:
        first, *repairs = failures
        detail = "; ".join(
            f"repair {i} of {MAX_OUTPUT_REPAIRS} also failed validation: {err}"
            for i, err in enumerate(repairs, start=1)
        )
        super().__init__(f"{first}; {detail}" if detail else str(first))
        self.usage = sum_usage(*(usage_of(err) for err in failures))
        self.record_id = getattr(failures[-1], "record_id", None)
        self.failures = tuple(failures)


@dataclass(frozen=True, kw_only=True)
class RepairedCall[T: BaseModel]:
    """A structured-output call that succeeded, plus the ``failures`` billed before it."""

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
    temperature: float | None = None,
) -> RepairedCall[T]:
    """Call ``llm_client``; on an output-shape failure, re-ask once.

    Raises ``OutputRepairExhausted`` when the repair fails too; a transport ``LLMError``
    passes through. A payload ``output_model`` reads as a REFUSED move
    (``StructuredOutput.output_refused``, ADR 0074) raises ``OutputNotOffered`` instead.
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
                # SAME temperature as the call it repairs: ADR 0035 asks for the
                # same answer in the right shape, not a different draw (WP-5.3).
                temperature=temperature,
            )
        except _REPAIRABLE as err:
            if _refused_by(output_model, err):
                raise OutputNotOffered(err) from err
            failures.append(err)
            # Each re-ask carries the ORIGINAL turn plus the latest error, not a stack.
            message = repair_message(user_message, err)
            repair_of = getattr(err, "record_id", None)
            continue
        return RepairedCall(result=result, failures=tuple(failures))
    raise OutputRepairExhausted(failures) from failures[-1]


def _refused_by(output_model: type[BaseModel], error: Exception) -> bool:
    """Whether ``output_model`` reads this failure as a refused move (ADR 0074).

    Only a ``StructuredOutput`` has an opinion; every other model keeps ADR 0035's re-ask.
    """
    return issubclass(output_model, StructuredOutput) and output_model.output_refused(error)


def repair_message(user_message: str, error: Exception) -> str:
    """The original turn plus the repair turn, as one user message.

    The repair turn is a versioned prompt file (``prompts/output_repair.md``), appended
    rather than sent as a second user turn: the Messages API rejects two user turns.
    """
    turn = load_prompt("output_repair").replace(_ERROR_PLACEHOLDER, _trim(str(error)))
    return f"{user_message}\n\n{turn.rstrip()}"


def _trim(text: str) -> str:
    if len(text) <= _MAX_ERROR_CHARS:
        return text
    return f"{text[:_MAX_ERROR_CHARS]}… (error truncated)"


def usage_of(err: BaseException) -> LLMUsage | None:
    usage = getattr(err, "usage", None)
    return usage if isinstance(usage, LLMUsage) else None


def sum_usage(*usages: LLMUsage | None) -> LLMUsage | None:
    """Add up what several billed-and-failed calls each charged.

    ``discarded_max_tokens`` carries the MAXIMUM, ``discarded_attempts`` the sum, keeping
    ADR 0015's over-estimate.
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

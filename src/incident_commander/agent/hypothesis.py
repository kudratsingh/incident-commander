"""Hypothesis engine models.

The LLM investigation loop outputs an ``InvestigationStep`` per iteration:
ranked ``Hypothesis`` list plus a discriminated-union ``NextAction`` (probe
another tool, remediate, or stop and escalate). Schema is authoritative —
the ``LLMClient`` forces the model to satisfy it via
``tool_choice=record_output``, which means:

- ``Hypothesis.category`` is drawn from a fixed enum. The LLM cannot invent
  a new value; Pydantic (and by extension the JSON schema exposed to the
  model) rejects anything not in ``HypothesisCategory``.
- ``ProbeAction.tool_name`` is a ``Literal`` generated from the read-tier
  slice of ``TOOL_REGISTRY``. Same rule — no invented tool names.

This is the Phase-6 hardening of a Phase-2 shortcut that let the LLM
produce free-form strings. See ADR-0005.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class HypothesisCategory(StrEnum):
    """Every root-cause category the agent knows how to classify.

    Categories that map to a Tier-1 remediation live in ``FIX_MAP``
    (``agent/investigation.py``). Categories NOT in ``FIX_MAP`` auto-
    escalate — the agent hands off to a human with a briefing.

    Adding a new fix category is a coordinated three-line change:
    enum entry here + ``FIX_MAP`` entry + prompt example. Adding an
    observation-only category (something the agent should recognize
    but never auto-fix) is one line — this enum only.
    """

    # Categories with Tier-1 fixes (see FIX_MAP in investigation.py):
    CONSUMER_SATURATION = "consumer_saturation"
    POISON_MESSAGE = "poison_message"
    STALE_CACHE = "stale_cache"
    RUNAWAY_SAGA = "runaway_saga"

    # Categories WITHOUT auto-fixes — always escalate:
    TRANSIENT_DEPENDENCY = "transient_dependency"
    """External dependency (SMTP, upstream API, third-party) is down or
    degrading. Right answer is wait for recovery, not auto-remediate."""

    PERSISTENT_DATA_BUG = "persistent_data_bug"
    """Bad source data (CSV parse errors, malformed input). Replay
    re-fails the same way. Requires a human to fix the input."""

    DEPLOY_REGRESSION = "deploy_regression"
    """Recent deploy correlates with the incident. Rollback is high-
    blast-radius; needs human sign-off."""

    UNKNOWN = "unknown"
    """LLM couldn't classify the root cause into any known category.
    Escalate with the full evidence chain in the briefing."""


class Hypothesis(BaseModel):
    """One candidate root cause with a confidence score, category, and reasoning.

    ``category`` is the structural key remediation routing uses. ``name``
    remains free-form — it's for the briefing writer + operator, allowing
    specificity like ``"worker-dispatcher 15k lag sustained 5min"`` that
    the category enum can't capture.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: HypothesisCategory = Field(
        description=(
            "Structural category. Drives remediation routing via FIX_MAP. "
            "Only values in HypothesisCategory are accepted."
        )
    )
    name: str = Field(
        min_length=1,
        description=(
            "Descriptive short label for the briefing (e.g. "
            "'worker-dispatcher lag 15k sustained 5min'). Free-form."
        ),
    )
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# NextAction — probe / remediate / stop
#
# ProbeAction.tool_name is a Literal generated from the read-tier slice
# of TOOL_REGISTRY. Same for RemediationPlan.action_tool + verify_tool
# in remediation.py. This is what makes "LLM invents an unknown tool"
# structurally impossible instead of a runtime check.


ReadToolName = Literal[
    "get_consumer_lag",
    "get_dag_state",
    "get_deploy_history",
    "get_incident",
    "get_postgres_health",
    "get_redis_health",
    "get_trace",
    "list_active_alerts",
    "list_audit_events",
    "list_dlq_messages",
    "list_incidents",
    "search_traces",
]
"""Union of every registry tool with ``Tier.READ`` classification.

Kept hand-listed (and asserted to match the registry by
``tests/unit/test_policies.py::TestLiteralRegistryDrift::
test_read_tool_name_literal_matches_read_tier``) because Pydantic's
Literal type checker needs literal string args at import time — a
dynamic expression would satisfy Python but not the JSON schema
generator. Adding a new read tool = one line here + one line in the
registry + the drift test catches any miss. The Literal is the schema
half of the guard only; ``_execute_probe`` re-checks ``tier_of`` at
runtime for the reclassification case the Literal cannot see."""


class ProbeAction(BaseModel):
    """Call a read tool from the registry to gather more evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["probe"] = "probe"
    tool_name: ReadToolName
    arguments: dict[str, Any] = Field(default_factory=dict)


class StopAction(BaseModel):
    """Enough evidence — hand off to a human."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["stop"] = "stop"
    reason: str = Field(min_length=1)


class RemediateAction(BaseModel):
    """Root cause confirmed AND category has a Tier-1 fix — hand off
    to the remediation planner.

    The investigation planner emits this iff the top hypothesis's
    ``category`` is a key in ``FIX_MAP`` (see ``agent/investigation.py``)
    AND its confidence is above the remediation threshold. If the
    category has no fix, emit ``StopAction`` instead.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["remediate"] = "remediate"
    reason: str = Field(min_length=1)


NextAction = Annotated[ProbeAction | StopAction | RemediateAction, Field(discriminator="kind")]


class InvestigationStep(BaseModel):
    """One iteration of the investigation loop."""

    model_config = ConfigDict(extra="forbid")

    hypotheses: tuple[Hypothesis, ...] = Field(
        min_length=1,
        description=(
            "Candidate root causes. List them most likely first; ordering "
            "is normalized after validation — entries are re-sorted by "
            "confidence descending (stable: equal-confidence entries keep "
            "their listed order), so index 0 is always the top hypothesis."
        ),
    )
    next_action: NextAction

    @field_validator("hypotheses", mode="after")
    @classmethod
    def _rank_by_confidence(cls, value: tuple[Hypothesis, ...]) -> tuple[Hypothesis, ...]:
        """Normalize ranking at the schema boundary (B-07).

        The 'ordered most likely first' contract used to live only in
        prompt prose, yet three gates read index 0 as the top pick: the
        remediate gate (investigation.py), the ADR-0009 reprobe prior,
        and the remediation planner's target. Sorting here — once, where
        LLM output enters the system — makes those readers correct for
        any order the model emits. ``sorted`` is stable, so ties preserve
        the model's stated order (asserted by
        tests/unit/test_hypothesis.py::TestInvestigationStepOrdering).
        """
        return tuple(sorted(value, key=lambda h: h.confidence, reverse=True))

    @field_validator("next_action", mode="before")
    @classmethod
    def _coerce_string_action(cls, value: Any) -> Any:
        """Anthropic's tool-use sometimes emits nested oneOf fields as JSON strings.

        Coerce to a dict before the discriminated-union validator runs so the
        planner works uniformly across API providers and prompt-shape variants.
        """
        if isinstance(value, str):
            return json.loads(value)
        return value

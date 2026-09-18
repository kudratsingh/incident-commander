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

Every model here inherits ``StructuredOutput`` (``llm/structured.py``), which
decodes a nested object or array that arrived as a JSON *string* before the
normal validators run — the shape live run ``779b19a287a7`` emitted for
``next_action`` (ADR 0035). It replaces the field-level ``json.loads``
coercion this module used to carry on that one field: the same defect can
land on any nested field of any ``record_output`` model, and a one-field
patch is not a fix for the class.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, field_validator

from incident_commander.llm.structured import StructuredOutput


class HypothesisCategory(StrEnum):
    """Every root-cause category the agent knows how to classify.

    Categories that map to a Tier-1 remediation live in ``FIX_MAP``
    (``agent/investigation.py``). Categories NOT in ``FIX_MAP`` auto-
    escalate — the agent hands off to a human with a briefing.

    Adding a new fix category is a coordinated three-line change:
    enum entry here + ``FIX_MAP`` entry + prompt example. Adding an
    observation-only category (something the agent should recognize
    but never auto-fix) is one line — this enum only.

    "One line" is the enum entry; the prompt example is not optional for
    either shape. A category the planner is never shown is a label it
    cannot pick, so ``tests/unit/test_prompts_snapshot.py::
    TestInvestigationPlannerInvariants::test_every_category_has_a_prompt_example``
    parametrizes over this enum and fails until the new value appears in
    the planner prompt's table.

    **Every category added after the original eight starts OUTSIDE
    ``FIX_MAP``** (WP-1.6, plan 02 § 5). Promoting one to an auto-fix is a
    separate, later decision per category, with its own scenario and its
    own ``TestFixMapMatchesTheSuite`` coverage — because the remediate gate
    reads ``top.category not in FIX_MAP``, a category's arrival in that map
    is the moment it authorises a Tier-1 write. Pinned by
    ``tests/unit/test_policies.py::TestEveryNewCategoryIsEscalateOnly``.
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

    # WP-1.6 (plan 02 § 5). Nine labels the benchmark's new fault families
    # and its level-0 control need, ALL outside FIX_MAP — see the class
    # docstring. Appended rather than interleaved so the eight values above
    # keep their position as well as their spelling: persisted trajectories
    # and every committed run archive are read back against this enum.

    NO_FAULT = "no_fault"
    """Nothing is wrong. Every reading the agent took is healthy, so the
    correct answer is that there is nothing to fix — the level-0 control's
    label, and the one category that is an answer rather than a fault.
    Never in FIX_MAP: at or above the remediate threshold it still ends the
    run through the stop/escalate path, with no action taken."""

    OUTBOX_STALL = "outbox_stall"
    """The transactional outbox is not draining — rows are written and
    never dispatched, so downstream sees silence rather than errors."""

    RESOLVER_STALL = "resolver_stall"
    """A resolver stopped making progress on work it had already claimed;
    the queue is not the problem, the consumer of it has stopped."""

    SAGA_COORDINATOR_STALL = "saga_coordinator_stall"
    """The coordinator that advances a multi-step workflow has stopped
    stepping it — distinct from RUNAWAY_SAGA, where it steps too much."""

    DB_QUERY_LATENCY = "db_query_latency"
    """Query time on the platform's database has degraded; the work is
    arriving and being served, only slowly."""

    DB_POOL_SATURATION = "db_pool_saturation"
    """Every database connection in the pool is checked out, so work waits
    for a connection rather than for the query."""

    DOWNSTREAM_DEPENDENCY = "downstream_dependency"
    """An external dependency is failing in a way the circuit breakers can
    observe — narrower than TRANSIENT_DEPENDENCY, which is the inferred
    case with no breaker reading behind it."""

    REDIS_SATURATION = "redis_saturation"
    """Redis itself is the constraint — memory pressure, eviction or
    connection exhaustion — rather than one stale key (STALE_CACHE)."""

    READ_MODEL_DRIFT = "read_model_drift"
    """A projected read model disagrees with the write side; what the
    platform reports and what it stored have diverged."""

    # WO-R3-263 (owner decision O-19, 2026-09-17; ADR 0054). Appended for the
    # same reason the nine above were: the values already written into run
    # archives, trajectories and `ground_truth.root_causes` keep their
    # spelling and their position.

    RESOURCE_EXHAUSTION = "resource_exhaustion"
    """A worker or job that ran out of memory, CPU or disk.

    The gap WO-R3-261's ground-truth pass found by having to label a world it
    could not name. `trace_investigation`'s trace holds one failed
    `report_gen` job whose error reads "OOM during PDF generation (200MB
    report)" — a precisely known fault with no member to carry it, so the
    honest label was `unknown`, which means "the probes left me unable to
    tell" and sends a human somewhere else entirely.

    Escalate-only, and not a placeholder for a later promotion: nothing on
    this platform's Tier-1 surface raises a memory limit, resizes a worker or
    reclaims a disk. A human changes a limit or the work that needs it."""


class Hypothesis(StructuredOutput):
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
    "get_cache_key_info",
    "get_consumer_lag",
    "get_dag_state",
    "get_deploy_history",
    "get_incident",
    "get_outbox_status",
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


class ProbeAction(StructuredOutput):
    """Call a read tool from the registry to gather more evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["probe"] = "probe"
    tool_name: ReadToolName
    arguments: dict[str, Any] = Field(default_factory=dict)


class StopAction(StructuredOutput):
    """Enough evidence — hand off to a human."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["stop"] = "stop"
    reason: str = Field(min_length=1)


class RemediateAction(StructuredOutput):
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


class InvestigationStep(StructuredOutput):
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

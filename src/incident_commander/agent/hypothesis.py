"""Hypothesis engine models: one ``InvestigationStep`` per loop iteration.

A ranked ``Hypothesis`` list plus a discriminated ``NextAction``. ``tool_choice=record_output``
makes the schema authoritative, so ``HypothesisCategory`` and ``ProbeAction.tool_name`` cannot
be invented (ADR-0005). ``StructuredOutput`` decodes a stringified nested object (ADR 0035).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model, field_validator

from incident_commander.llm.structured import StructuredOutput


class HypothesisCategory(StrEnum):
    """Every root-cause category the agent knows how to classify.

    Categories in ``FIX_MAP`` (``agent/investigation.py``) auto-remediate; the rest escalate.
    A new one needs an enum entry plus a planner-prompt example, and **starts OUTSIDE
    ``FIX_MAP``** (WP-1.6, plan 02 § 5); ``TestEveryNewCategoryIsEscalateOnly`` pins that.
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

    # WP-1.6 (plan 02 § 5). Nine labels for new fault families, ALL outside FIX_MAP.
    # Appended, not interleaved: run archives are read back against this enum.

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

    # WO-R3-263 (owner decision O-19, ADR 0054). Appended — archives read it back by value.

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

    # WO-R3-214 (WP-7.2, ADR 0053). Appended, like every label since the original eight:
    # values already in run archives and `ground_truth.root_causes` keep spelling and position.

    DAG_PAUSED = "dag_paused"
    """A dependency chain is not advancing because it is deliberately
    paused — `get_dag_state` reads `paused: true` with an expiry and the
    ancestor that holds it — rather than because anything in it broke.

    The gap WP-7.2 found by having to label a world it could not name. The
    chain is healthy: no node is dead-lettered, nothing is queued to replay,
    and the held descendants promote by themselves when the pause lifts. The
    nearest labels were all wrong in a way that would send a human somewhere
    else — `runaway_saga` says a node stopped the chain, `resolver_stall`
    says nothing is coming, and `unknown` says the probes left the agent
    unable to tell, when in fact one reading answered it outright.

    Escalate-only, and the one category whose correct action count is zero
    for a structural reason rather than a missing tool: the platform has no
    un-pause tool at all, `pause_dag` would extend the very thing that is
    holding the chain, and a replay is refused inside a paused DAG. What the
    run does is name the pause, its owner and its expiry, and hand off."""


class Hypothesis(StructuredOutput):
    """One candidate root cause: ``category`` drives remediation routing, ``name`` is free."""

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


# NextAction — probe / remediate / stop.
# ProbeAction.tool_name is a Literal over TOOL_REGISTRY's read tier: no invented tools.


ReadToolName = Literal[
    "get_cache_key_info",
    "get_circuit_breakers",
    "get_consumer_lag",
    "get_dag_state",
    "get_deploy_history",
    "get_incident",
    "get_outbox_status",
    "get_postgres_health",
    "get_redis_health",
    "get_slo_status",
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
    """Root cause confirmed, category in ``FIX_MAP``, confidence over the threshold — hand
    off to the remediation planner. Otherwise emit ``StopAction``."""

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
        """Normalize ranking at the schema boundary (B-07): three gates read index 0 as the
        top pick. Stable, so equal confidences keep the model's order."""
        return tuple(sorted(value, key=lambda h: h.confidence, reverse=True))


# The step schema with `probe` withdrawn (ADR 0074, amending ADR 0073). A refusal the planner
# meets AFTER it has chosen leaves "probe something else" open and a model takes it (INC-004),
# so once the ranking has settled the choice is narrowed in the SCHEMA it is handed. The loop
# decides when (`investigation._probe_withdrawn`); every strategy renders it (ADR 0036).


SettledNextAction = Annotated[StopAction | RemediateAction, Field(discriminator="kind")]
"""``NextAction`` with ``probe`` removed: the two moves left once the ranking has settled."""


#: What the planner is told in the schema itself when the probe has been withdrawn. On the
#: FIELD, not in a class docstring, because a docstring here is a silent schema change.
SETTLED_CHOICE_DESCRIPTION: Final[str] = (
    "This step offers two moves and no probe. Your top hypothesis has held at or above the "
    "remediate threshold in a category with a Tier-1 fix for two steps running, and your own "
    "newest reading of the alerted resource is fresh and shows the fault present — so there "
    "is no read left that would change your ranking, and none is offered. Emit `remediate` to "
    "act on the top hypothesis, or `stop` to hand off; a `stop` here must name in its reason "
    "what you would need to SEE to act instead, because that sentence is what the human who "
    "picks the incident up has to work from. The evidence trail carries this refusal in the "
    "loop's own words."
)


class ProbeWithdrawn(StructuredOutput):
    # NO class docstring, deliberately: it would become the planner's JSON schema `description`
    # (CLAUDE.md). The reason travels on `next_action`'s own description instead.
    model_config = ConfigDict(extra="forbid")

    next_action: SettledNextAction = Field(description=SETTLED_CHOICE_DESCRIPTION)

    @classmethod
    def output_refused(cls, error: Exception) -> bool:
        """A ``probe`` payload is this model REFUSING a move, never output it cannot read.

        So ``call_with_output_repair`` raises ``OutputNotOffered`` instead of re-asking: the
        output was readable and the move was not on offer (ADR 0074).
        """
        return asked_for_a_probe(error)


#: Built models, keyed by the model they narrow: ``model_json_schema()`` is cached per class, so
#: a fresh class per step would re-generate the schema and defeat the prompt cache.
_WITHOUT_PROBE: Final[dict[type[BaseModel], type[BaseModel]]] = {}


def without_probe[T: BaseModel](model: type[T]) -> type[T]:
    """``model`` with ``probe`` withdrawn from ``next_action`` (ADR 0074).

    Derived from the model handed in, not written out for ``InvestigationStep`` alone: a
    narrowing that reached only the control arm would leave every other strategy able to probe
    where the loop said it may not. A subclass, so ``isinstance`` and the ranking validator hold.
    """
    cached = _WITHOUT_PROBE.get(model)
    if cached is None:
        cached = create_model(
            f"Settled{model.__name__}",
            __base__=(ProbeWithdrawn, model),
            __module__=__name__,
        )
        _WITHOUT_PROBE[model] = cached
    return cast(type[T], cached)


#: Pydantic's name for "the discriminator value is not one this union admits". Named because a
#: Pydantic upgrade could turn a refusal into a silent escalation; test_hypothesis.py pins it.
_UNION_TAG_INVALID: Final[str] = "union_tag_invalid"


def asked_for_a_probe(error: Exception) -> bool:
    """Whether a validation failure is a planner asking for the probe the schema withdrew.

    Reads the structured errors and the ``__cause__``, because ``LLMClient`` wraps the
    ``ValidationError`` in ``LLMOutputError`` (ADR 0007). Anything else answers ``False``.
    """
    for candidate in (error, error.__cause__):
        if not isinstance(candidate, ValidationError):
            continue
        for detail in candidate.errors():
            if detail.get("type") != _UNION_TAG_INVALID:
                continue
            context = detail.get("ctx") or {}
            if str(context.get("tag", "")) == ProbeAction.model_fields["kind"].default:
                return True
    return False

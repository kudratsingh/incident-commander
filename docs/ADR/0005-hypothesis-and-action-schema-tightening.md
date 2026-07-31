# ADR 0005: Hypothesis + action schema tightening (Phase 6 hardening)

* Status: accepted
* Date: 2026-07-31
* Decider: Kudrat Singh

## Context and problem statement

Phase 2 shipped a hypothesis engine where `Hypothesis.name` was a free-form string. The prompt gave example names (`consumer_deadlock`, `poison_message`), but nothing structural constrained the LLM's output. The mapping from hypothesis to Tier-1 fix relied on the LLM producing one of the ~4 example names — a soft convention, not a schema rule.

The Phase 6 live-eval runs surfaced the predictable failure: the LLM classified real DLQ contents correctly but named the hypothesis `poison-message-bad-csv-data`. That didn't match `poison_message` exactly, the investigation planner's soft mapping fell through, and the agent escalated a fixable incident. The design decision to leave `name` free-form pushed the correctness gate onto prose (the prompt table) instead of the type system.

Same class of gap in three other places:
- `ProbeAction.tool_name: str` — LLM could invent tool names. Runtime check in `_execute_probe` caught it, but only after the LLM burned tokens producing the invalid output.
- `RemediationPlan.action_tool: str` and `verify_tool: str` — same. Runtime tier check in `make_llm_plan` caught it after the fact.
- Category-to-fix routing lived in the prompt as a Markdown table, so adding a category required a coordinated prompt edit + code check + hope the LLM re-read the new table right.

## Decision drivers

* **Structural > prose.** When the LLM's output can be constrained by the JSON schema exposed via `tool_choice=record_output`, do it there. Prose telling the LLM "please use one of these values" is not enforcement.
* **Fix mapping should have a single source of truth.** Not the prompt, not the transition code, not the remediation planner's prose — one Python dict that both code and prompt read from.
* **Adding a new hypothesis category should be a one-line change if it has no fix, three lines if it does.** The 3-line version: enum entry + `FIX_MAP` entry + prompt example. Impossible to add half of it and ship.
* **Categories without fixes must auto-escalate.** No "we forgot to add the mapping so nothing happens" failure mode.

## Considered options

1. Prompt tweak — add the new hint categories to the investigation planner's mapping table. Keep `Hypothesis.name` free-form; keep `tool_name` fields as strings.
2. Structural — `HypothesisCategory` enum on the model, `Literal[<registry keys>]` on tool_name fields, `FIX_MAP` as single source of truth, category-not-in-FIX_MAP auto-escalates.
3. Hybrid — enum-constrained category but keep the free-form name field for briefing specificity.

**Chosen: option 3.** `Hypothesis.category: HypothesisCategory` (enum, drives routing) + `Hypothesis.name: str` (free-form, for briefing specificity like `"worker-dispatcher lag 15k sustained 5min"`). Best of both — routing is structural, briefing content isn't neutered.

## Decision outcome

### Schema shape

```python
class HypothesisCategory(StrEnum):
    # With Tier-1 fixes (see FIX_MAP):
    CONSUMER_SATURATION = "consumer_saturation"
    POISON_MESSAGE = "poison_message"          # DLQ umbrella; hint-based routing inside
    STALE_CACHE = "stale_cache"
    RUNAWAY_SAGA = "runaway_saga"
    # Without fixes — always escalate:
    TRANSIENT_DEPENDENCY = "transient_dependency"
    PERSISTENT_DATA_BUG = "persistent_data_bug"
    DEPLOY_REGRESSION = "deploy_regression"
    UNKNOWN = "unknown"

class Hypothesis(BaseModel):
    category: HypothesisCategory   # NEW — enum
    name: str = Field(min_length=1) # kept free-form
    confidence: float
    reasoning: str
```

### Single source of truth

`agent/investigation.py` owns `FIX_MAP`:

```python
FIX_MAP: Final[dict[HypothesisCategory, str]] = {
    HypothesisCategory.CONSUMER_SATURATION: "restart_consumer_group",
    HypothesisCategory.POISON_MESSAGE:      "replay_dlq_by_ids",
    HypothesisCategory.STALE_CACHE:         "invalidate_cache_key",
    HypothesisCategory.RUNAWAY_SAGA:        "pause_dag",
}
```

Investigation transition's gate:

```python
if isinstance(action, RemediateAction):
    top = step.hypotheses[0]
    if top.category not in FIX_MAP:
        return _finalize(...)  # escalate
    if top.confidence < _REMEDIATE_CONFIDENCE_THRESHOLD:
        return _finalize(...)  # escalate
    return _handoff_to_planning(...)
```

**Any category not in `FIX_MAP` — regardless of confidence — auto-escalates.** Explicit is safer than "if not in map, escalate implicitly." Adding a category to the enum without a fix entry is the deliberate way to say "the agent should recognize but not act on this."

### Tool-name Literals

`ProbeAction.tool_name: ReadToolName` (a `Literal[...]` of read-tier tool names). `RemediationPlan.action_tool: Tier1ToolName`. `RemediationPlan.verify_tool: ReadToolName`. The JSON schema exposed to the LLM via `tool_choice=record_output` includes these enums — Pydantic rejects any invented tool name at schema-validation time, before the runtime tier check ever runs.

The runtime checks in `_execute_probe` and `make_llm_plan` stay as defense-in-depth (catches registry drift after import).

### Why the alternatives lose

**Option 1 (prompt tweak)** perpetuates the pattern that just failed. It fixes today's symptom (DLQ hint category) but keeps the free-form-string mapping. The next category we add will hit the same gap.

**Option 2 (strict, no free-form name)** loses briefing quality. The specific `"worker-dispatcher lag 15k sustained 5min"` labels help operators; forcing that into an enum value would either lose specificity or explode the enum size. Category (structural) + name (descriptive) is the right split.

### Consequences

Positive:
* LLM literally cannot produce an unmapped hypothesis category — Pydantic rejects at validation.
* LLM literally cannot pick an unknown or wrong-tier tool — Literals reject.
* Adding a new fix: enum entry + `FIX_MAP` entry + prompt example. Three lines, coordinated. Adding an observation-only category (no fix): one line in the enum.
* The remediation planner's per-tool mapping stays independent — it routes within `POISON_MESSAGE` (DLQ hint-based tool selection: `replay_dlq_by_ids` vs `replay_dlq_by_category` vs `mark_dlq_permanent`).
* Every scenario's canned LLM response carries a category field — makes the intent of each scenario greppable.

Negative:
* Prompt gets tighter (Markdown mapping table replaced by enum-values-with-mappings prose). Reviewer needs to keep enum + prompt in sync — mitigated by prompt hash test + a unit test that asserts the prompt mentions all enum values.
* Adding a new Tier-1 tool requires updating `Tier1ToolName` in `remediation.py`, `_TIER_1_TOOLS` in `policies.py`, and the registry — three places. Mitigated by a unit test that asserts `Tier1ToolName` matches `tools_at_or_below(TIER_1) - tools_at_or_below(READ)`.
* Migrating v3 checkpoints from before this change requires re-parsing evidence (they still have `Hypothesis` without a category field). Mitigated because the persisted state format has `schema_version=3` unchanged — `Hypothesis` is stored inline in `RunState.hypotheses` which was `()` for pre-Phase-6 states, and Phase-6 states will be re-created rather than migrated (in-memory checkpointer is the default for eval; PostgresCheckpointer serializes the full run so old rows won't parse — we regenerate them by rerunning affected scenarios).

Revisit trigger: We hit the same class of bug (LLM output producing something that should have been schema-rejected but wasn't). At that point revisit the boundary between structural and prose.

## More information

* Implementing PR: this branch
* Related ADRs: [ADR 0002](0002-hand-rolled-state-machine.md) (state machine explicit), [ADR 0003](0003-platform-enforced-tier-policy.md) (tier policy), [ADR 0004](0004-eval-first-development-and-regression-gating.md) (eval-first — this ADR is a direct consequence of an eval-driven finding)
* Case study that motivated this: [docs/eval-methodology.md#case-study-dlq-categorization-discovery](../eval-methodology.md#case-study-dlq-categorization-discovery)

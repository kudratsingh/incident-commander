# Phase 6 hardening — what happened, in order

Case study of a real bug we shipped in Phase 2, how it hid for four months, how the first honest live-eval surfaced it, and why the fix ended up being a schema tightening rather than a prompt tweak. Written after PR #43 landed. Reference for future "why did we do it this way?" questions.

## The bug we shipped in Phase 2

`Hypothesis.name: str` — a free-form string field on the LLM's structured output.

```python
# Original (Phase 2)
class Hypothesis(BaseModel):
    name: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(min_length=1)
```

The prompt gave examples:

> `name`: short kebab-case identifier (e.g. `consumer_deadlock`, `poison_message`).

The remediation planner mapped hypothesis names to Tier-1 fixes via a Markdown table in a different prompt. The whole system worked as long as the LLM produced names that matched the mapping keys.

**No test caught this.** The unit tests fed canned responses that used the "right" names. Offline eval fed canned responses. Live-eval, when it eventually ran, was the first time the LLM saw real evidence and produced free-form names of its own.

## How it hid for four months

Phase 2 (July 15) → Phase 6 first live-eval (July 30). Fifteen days of active development plus offline confirmation on every PR. Everything looked green.

Why the gap didn't show:

- **Unit tests used canned LLM outputs.** Every canned hypothesis had a "correct" name matching the prompt's example set. Structural tests never exercised the "LLM invents a name" path.
- **Offline eval used canned responses too.** Same deterministic outputs. Same "correct" names.
- **Live-eval didn't run until Phase 6.** The Phase 2 loop worked (LLM planner + tool loop + briefing). We deferred spending on live runs because the offline path was so consistent. Nothing forced the discovery.
- **The prompt example set was small (~4 names).** A reviewer reading the prompt would think "these are the values" — nothing said "these are examples of the shape."

## The first live-eval that broke it

`remediate_dlq_backlog_success` scenario. Alert says DLQ full. Investigation planner:

| Iter | Top hypothesis (name) | Confidence | Next action |
|---|---|---|---|
| 1 | `poison-message-dlq-backlog` | 0.65 | Probe `list_dlq_messages` |
| 2 | **`poison-message-bad-csv-data`** | **0.82** | Probe `get_deploy_history` |
| 3 | `poison-message-bad-csv-input` | 0.82 | Probe `get_trace` |
| 4 | `smtp-relay-down-post-deploy` | 0.82 | Probe `get_trace` |
| 5 | same | 0.82 | Probe `get_consumer_lag` |

Then escalated. Confidence was above the 0.7 threshold. The category (had one existed) would have been `poison_message`. But the LLM's *string name* didn't match `poison_message` exactly, the remediation planner's soft mapping fell through, and the agent handed off to a human on an incident it could have fixed.

Judge scored the escalation briefing 0.90. The agent's reasoning was excellent. The wiring was broken.

## What we almost did (the band-aid)

First recommendation: add a rule to the investigation planner prompt.

> "Prefer `remediate` when the top hypothesis has confidence > 0.7 **AND** any of:
> - Name matches: consumer_saturation, poison_message, stale_cache, runaway_saga
> - **DLQ evidence contains entries with a non-null `remediation_hint`**"

This would have patched the DLQ case. It's what shipped in half a dozen production agent codebases I've seen. It has three problems:

1. **The class of bug remains.** Next new category we add will hit the same "name doesn't match the table" gap. We'd add more OR clauses to the prompt indefinitely.
2. **Correctness lives in prose.** Reviewer has to notice the prompt table is out of sync with the code. Tooling can't check it.
3. **Duplication.** The mapping table exists in the prompt AND in the remediation planner's routing code AND (implicitly) in the investigation planner's mapping. Three places to keep in sync.

The user pushed back: "isn't the hypothesis engine only supposed to be allowed to have certain hypotheses and name them according to the schema?"

Correct question. The right answer wasn't a prompt tweak.

## What we shipped (the structural fix)

`HypothesisCategory` StrEnum + `Hypothesis.category` field + `FIX_MAP` in `investigation.py` + `Literal` types on tool_name fields. Full detail in [ADR-0005](../ADR/0005-hypothesis-and-action-schema-tightening.md).

Three properties the new shape has that the old didn't:

1. **The JSON schema exposed to the LLM rejects invalid categories.** Pydantic + `record_output` tool schema flat-out refuses any category string not in the enum. LLM can't invent `poison-message-bad-csv-data` even if it wants to — the API call fails validation.
2. **`FIX_MAP` is the single source of truth.** Not the prompt table. Not the remediation planner's docstring. One Python dict. Investigation planner gates on `top.category in FIX_MAP`, no soft-string-matching.
3. **`name` stays free-form for briefing specificity.** Category is `poison_message`, name is `"csv-upload-row-15382-int-parse-error"`. Both fields carry real information; the split is intentional.

## The lesson (generalized)

**LLM outputs that can be constrained by JSON schema should be.** Prompts telling the LLM "please use these values" are not enforcement. They're documentation. The JSON schema on `tool_choice=record_output` IS enforcement.

For every LLM output field, ask:
- Is the value from a fixed set? → `Literal` or `StrEnum`
- Is the value structurally invalid unless X? → Pydantic field validator or model discriminator
- Is the value free-form for downstream human consumption? → keep it `str`

If any of the first two apply and the field is still a bare `str`, you have a Phase-2-style shortcut waiting to be discovered.

## Related shortcuts we found in the same audit + fixed

- `ProbeAction.tool_name: str` — LLM could invent tool names. Runtime check caught it, but only after the LLM burned tokens on an invalid output. → `ProbeAction.tool_name: ReadToolName` (Literal).
- `RemediationPlan.action_tool: str` / `verify_tool: str` — same. → Both are Literal now.
- Category-to-tool mapping in prompt prose → `FIX_MAP` dict.

The pattern held across all three: whatever field was free-form became a Literal; whatever mapping lived in prose became a single Python source of truth.

## What eval + real signal really did for us

Live-eval didn't just "test the agent against the real platform." It **surfaced a design assumption we made in Phase 2 that no offline test could have caught.** That's the point of live-eval — it's the one gate that runs against a system that doesn't know what the "right" answer is.

If you find yourself deferring live-eval because "the offline path is so clean," you're accumulating the class of bug this doc is about. Ship live-eval as early as the platform permits, even at token cost, even against half-seeded fixtures. The bugs it surfaces are worth more than the compute.

Related: [ADR-0004](../ADR/0004-eval-first-development-and-regression-gating.md) codifies the eval-first gating rule that came out of this discovery.

## Related documents

- [ADR-0005](../ADR/0005-hypothesis-and-action-schema-tightening.md) — the structural decision itself
- [docs/eval-methodology.md](../eval-methodology.md#case-study-dlq-categorization-discovery) — the eval side of the same story
- [docs/architecture-principles.md](../architecture-principles.md) — the general rules that came out of this
- Implementing PRs: [#41](https://github.com/kudratsingh/incident-commander/pull/41) (DLQ categorization), [#42](https://github.com/kudratsingh/incident-commander/pull/42) (delay_seconds), [#43](https://github.com/kudratsingh/incident-commander/pull/43) (schema tightening)

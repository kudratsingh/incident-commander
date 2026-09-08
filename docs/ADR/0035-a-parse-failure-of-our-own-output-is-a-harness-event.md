# ADR 0035: A parse failure of the agent's own structured output is a harness event: decode stringified objects, then one bounded repair, then escalate

* Status: accepted
* Date: 2026-09-08
* Decider: Kudrat Singh

## Context and problem statement

Live run `779b19a287a7` (`remediate_dlq_backlog_success`, 2026-09-08 09:14Z, ~$0.1) is the second
paid run in this campaign where the agent was right and the harness threw the answer away. The
first was `5c8895771fbd`, which produced ADR 0030. This one is the same shape one layer further
out: not a wrong value inside a valid object, but a valid object that arrived wrapped in the wrong
type.

The trajectory is short and almost entirely correct. The agent read the DLQ unfiltered, tried a
`remediate` handoff, was refused by the subject guard (correctly — ADR 0031's alerted
`replay_safe` slice had not been read on its own), read the filtered slice, and at step 11
decided `remediate` with the right reason: replay the one confirmed-safe row
`fc8d2a03-23b3-5371-9acb-46443c73baa5`, leave the poisoned `eb798430…` alone, name the
`human_required` row for the human. Every judgement the scenario exists to measure, it made.

Then it filled `record_output` like this:

```json
"next_action": "{\"kind\": \"remediate\", \"reason\": \"The replay_safe DLQ slice has been read …\"}]"
```

The nested discriminated union came back as a **JSON string**, and the string carried the
enclosing array's `]`. `InvestigationStep.next_action` is a discriminated union of objects with
`extra="forbid"`; the field-level `json.loads` coercion that had sat on that one field since
July 2026 raised `Extra data: line 1 column 893 (char 892)`, pydantic wrapped it as a
`value_error`, and `investigation.py` escalated on the **first** failure:

> planner output invalid: output failed schema validation for InvestigationStep: 1 validation
> error for InvestigationStep / next_action / Value error, Extra data: line 1 column 893 …

The run graded RED on outcome, evidence and action. `report.json` classified it
`failure_class: unclassified`. No action executed, nothing in the world moved, and the paid
sequence stopped.

### Why this is a harness event and not a decision

Everything the run needed was in the response. The object decodes cleanly; only its wrapping was
wrong. The escalation the agent produced does not describe an incident — it describes the harness
failing to read a message — and it went to a human as if the agent had given up on a DLQ.

That distinction is what this ADR turns into behaviour. **A decision the agent got wrong is
evidence and should end the run. A message the harness could not open is neither.**

### Three separate defects, and only the first is about this payload

1. **The coercion covered one field of one model.** `next_action` had a `json.loads` field
   validator; `hypotheses`, `ProbeAction.arguments`, `RemediationPlan.action_arguments`,
   `verify_arguments` and every field of every other `record_output` model had nothing. The same
   API behaviour on any of those would have produced the same escalation with no coercion
   attempted at all. A one-field patch was never a fix for the class.
2. **The first failure was terminal.** Six other guards in this codebase re-ask once before
   escalating (ADR 0025, 0027, 0028, 0030, 0031, 0032). The one thing the model can *always* fix
   when told — the shape of its own JSON — had no re-ask at all.
3. **The classifier could not see it.** `_classify_failure`'s transport bucket matches any evidence
   summary containing "LLM" and "invalid", which two of the three escalation reasons do; this one
   happened to fall through to `unclassified`. Either way a schema rejection of our own output was
   going to be filed as a network problem or as nothing.

## Decision drivers

* **The information is already in the run.** No probe, no new evidence, no world read. The decode
  costs nothing; the re-ask costs one LLM call and no tool budget.
* **A harness that discards a correct answer over its envelope is measuring itself, not the agent.**
  Every RED that traces to an output-shape defect is a RED the eval suite cannot interpret.
* **Tolerance must be narrow enough to stay honest.** A decoder that accepts anything is a decoder
  that turns a genuinely broken payload into a quietly wrong object. Every rule below has a
  negative test beside it.
* **Nothing about trust changes.** Tool output stays untrusted data (CLAUDE.md invariant 4). This
  is about the agent's OWN output, on the way back in, and `extra="forbid"`, the enums, the tier
  checks and every plan guard are untouched.
* **A repair is billed work.** ADR 0015's rule is that the meter may over-report and never
  under-report. A re-ask that only charged the run when it succeeded would make the expensive path
  the cheap-looking one.
* **A cap, not a loop.** The same argument as ADR 0030's `_MAX_ARGUMENT_REFUSALS`: a model that
  cannot produce the right shape when handed the exact validation error will not produce it on the
  third ask, and an uncapped repair loop is an unbounded bill on an unattended run.

## Considered options

1. **Escalate on the first parse failure** — today's behaviour.
2. **Widen the existing `json.loads` coercion to more fields** by hand.
3. **Decode stringified containers everywhere, then one bounded repair, then escalate** (chosen).
4. **Unbounded retries until the output parses.**
5. **Relax `extra="forbid"` / loosen the models** so more payloads validate.
6. **Parse the raw response ourselves when the model rejects it** — pull `kind` out with a regex
   and rebuild the action.

## Decision outcome

**Option 3, in three parts.**

### 1. A shared decoder on every `record_output` model

`llm/structured.py` defines `StructuredOutput`, a base class carrying one `mode="before"` model
validator and no configuration of its own. Every model the LLM fills in inherits it:
`InvestigationStep` (and `Hypothesis`, `ProbeAction`, `StopAction`, `RemediateAction`),
`RemediationPlan`, `VerificationJudgment`, `BriefingContent`, `JudgeScore`.

The validator reads `cls.model_fields`. For each field whose annotation is a nested model, a union
of nested models, a mapping or a sequence, and whose incoming value is a `str`, it decodes and
substitutes — **only** when the decode succeeds and the decoded type matches the declared shape.
Anything else is left exactly as it arrived, so the model's own error fires and the diagnosis
stays honest.

Model-level and derived from the annotations, rather than a per-field validator list, because a
hand-kept list of nested fields is a second copy of the schema and drifts the first time somebody
adds a field — architecture-principles rule 2. `tests/unit/test_structured_output.py` derives the
set of `record_output` models from the `output_model=` call sites in the tree and fails if the
list and the source disagree.

**The trailing-delimiter tolerance, and its limit.** After a strict `json.loads` fails, one more
attempt decodes the leading JSON value and accepts it only if everything after it is whitespace
and closing delimiters (`]`, `}`) — characters that cannot begin or continue a JSON value, so
their presence after a complete value is a container delimiter that leaked into the string. That
is exactly the live shape. A trailing comma, a second value, prose, or a truncated object all
leave the string untouched and raise. This is the one place where "be liberal in what you accept"
was allowed in, and it is fenced by six negative tests rather than by intent.

### 2. One bounded repair, billed like any call

`llm/repair.py::call_with_output_repair` wraps the three in-run structured-output call sites: the
investigation planner (`_plan_next_step`), the remediation planner (`_plan_once`), and briefing
enrichment. On an output-shape failure it re-asks **once**: the same system prompt, the same user
message, plus the repair turn from `llm/prompts/output_repair.md` carrying the trimmed pydantic
error. `MAX_OUTPUT_REPAIRS = 1`, enforced by the loop bound rather than by a check a later edit can
walk past, and equal to `_MAX_ARGUMENT_REFUSALS` by assertion in the tests.

The repair turn is a versioned, snapshot-tested prompt file, never an inline string (CLAUDE.md).
It asks for a re-format and explicitly nothing else — "do not revise your findings, change which
resource you named, soften a decision, or add new claims" — because a re-ask that read as "have
another go" would let a correct decision drift on its way through the harness, which is the
opposite of what this ADR is for. It is appended to the original user message rather than sent as
a second user turn because `LLMClientProtocol.call` carries one user message and the Messages API
rejects two consecutive user turns.

**Only an output failure is repairable.** `LLMClient._parse` now raises `LLMOutputError` (a
subclass of `LLMError`, so every existing `except LLMError` is unaffected) for both of its failure
modes — schema rejection, and a response with no `record_output` block. A transport `LLMError`
propagates untouched: the client has already retried it three times and "your JSON was malformed"
is not a useful thing to say to a rate limiter.

**Both calls are billed.** `accounting.accrue_structured_call` charges every failed leg with
`accrue_llm_error` and then the successful one with `accrue_llm_usage`. When the repair also fails,
`OutputRepairExhausted` carries both errors in its message and the **sum** of both usages as its
`usage`, so the existing `accrue_llm_error` at each call site charges the whole thing. The
escalation reason then reads exactly as it did before this change, with the second error appended.

**The repair is visible.** `LLMClient` mints a `record_id` per attempt, `LLMResult` and `LLMError`
carry it, the re-ask passes it as `repair_of`, and `JsonlTracer` stamps a `record_id` on every
record. `scripts/format_traces.py` labels the re-ask `REPAIR (1 of 1)` and prints the record it
repairs. Without that, run 779b19a287a7's successor would render as two unrelated planner calls —
a reader counting planner calls would see a loop that never happened, and a reader auditing spend
could not tell which call bought the answer that was used.

### 3. The failure has a name

`failure_class` gains `planner_output_invalid`, checked **before** the transport bucket and derived
from the three escalation-reason prefixes, which now live as constants in `llm/repair.py` and are
read by both the transitions that format them and the classifier that matches them. One source of
truth; two copies of a prose prefix is how a classifier silently stops matching the thing it
classifies.

### Why the alternatives lose

**Escalate on first failure (1).** This is today, and it is what produced a RED with a correct
decision inside it. It also mis-states the situation to the human: the briefing said the run ended
because the planner's output was invalid, which is true about the harness and useless about the
incident.

**Widen the coercion by hand (2).** Cheaper today and wrong in the same way the original was:
a list of nested fields kept beside the schema, correct until the next field lands. It also does
nothing for a payload that is malformed in some other way, which is the case the repair exists for.

**Unbounded retries (4).** Budget. An output-shape defect that is systematic — a prompt change, a
model change, a schema the model cannot satisfy — would spend the whole per-incident ceiling
discovering it, once per incident, unattended. One re-ask discovers it for one extra call.

**Loosen the models (5).** Rejected outright. `extra="forbid"` and the enums are what make "the LLM
invents a tool name" structurally impossible (ADR 0005). Trading a schema guarantee for a parse
success is exactly the debt architecture-principles rule 3 was written about.

**Rebuild the action from the raw response ourselves (6).** This is the harness deciding what the
agent meant. ADR 0030 refused the same move for a mis-typed id — the harness offers, it never
substitutes — and the argument is stronger here: a regex over a 900-character reason string
choosing which job gets replayed is not a parse, it is a guess with a Tier-1 action behind it.
Decoding a complete JSON object the model actually emitted is not that; it is reading the message
it sent.

## Consequences

### Positive

* Run `779b19a287a7`'s exact `record_output` input now parses, with the decision intact:
  `next_action.kind == "remediate"` and the reason still naming `fc8d2a03…` and `eb798430…`. It is
  a red-before/green-after test (`test_the_live_payload_parses`), and on `origin/main` it fails
  with the byte-identical `ValidationError` the run escalated on.
* The same defect on any nested field of any `record_output` model is now covered, including models
  that do not exist yet — the coverage test fails when a new one appears.
* Anything the decoder legitimately cannot read buys one re-ask instead of ending the run, and the
  re-ask is labelled, correlated and billed.
* A RED whose cause is an output-shape defect is now named `planner_output_invalid` in
  `report.json` instead of being filed as `transport` or `unclassified`, so it is separable from
  agent findings when reading a suite.

### Negative

* **One more billable call in the worst case**, per structured-output call site. A planner that
  emits an unparseable shape systematically now costs two calls per incident to discover instead of
  one. None of it spends tool-call budget.
* **A tolerance exists where none did.** A payload with a stray closing delimiter is now accepted
  where it used to be rejected. The set is two characters wide and pinned by tests, but it is a
  real widening and the next person to add to that set should have to argue for it.
* **The `repair_of` correlation is one more field in the trace format.** It is omitted rather than
  written as `null` on an ordinary record, so no existing record shape moved, but readers of the
  JSONL now have two ways a step can appear.
* **A repaired call is slower.** Two round trips inside one loop iteration, against the wall-clock
  meter that ADR 0015 reads between transitions.

### Neutral

* Offline `make eval-reg` stays 40/40 and no canned scenario moves. `CannedLLMClient` raises a
  plain `LLMError` when its script runs out, which is not repairable, so a clean canned run never
  reaches the repair path and consumes no extra payload.
* The service path (`api/app.py`) is unaffected: it renders the deterministic briefing and does not
  call the enrichment writer.

## What this deliberately does not do

* **The verification judge (`make_llm_verify`) and the eval briefing judge (`graders/llm_judge.py`)
  get the decoder but not the repair.** Both are structured-output calls and both would work
  identically; they are outside this change's scope because the brief that produced it named three
  call sites, and widening a repair budget onto call sites with no live failure behind them is how
  a cap becomes decoration (ADR 0030's own words about `_absent_resource_args`). Filed rather than
  improvised.
* **No prompt anywhere is told about the repair in advance.** The planner prompts are unchanged.
  Telling the model "you get one correction" before it has made a mistake buys nothing and costs
  cache-stable prompt bytes on every call.

## Revisit trigger

* A paid run that spends the repair and **still** escalates on shape — that would say the re-ask is
  not the missing piece and the defect is in the schema we are asking the model to satisfy.
* A second output-shape malformation the decoder declines that is as unambiguous as this one. Two
  data points would be an argument for a different decode strategy, not for widening the delimiter
  set a character at a time.
* Anthropic's structured-output behaviour changing such that a nested object is never stringified,
  which would make the decoder dead code worth deleting rather than carrying.

## More information

* Evidence: `evals/runs/779b19a287a7/`, `evals/traces/remediate_dlq_backlog_success.jsonl`
  (the record with `parse_failed: true`), and STEP 11 of
  `evals/reports/human/remediate_dlq_backlog_success.20260908T091545Z.6d34908ba8f4.txt`.
  Append-only; quoted here, never modified.
* [F-014](../../study/findings.md) — the finding, with the raw shape.
* Related: [ADR 0015](0015-wall-clock-and-usd-budget-meters.md) (the repair is billed like any
  call), [ADR 0030](0030-a-refused-plan-is-re-asked-with-the-candidates.md) (the cap this one
  matches, and the harness-offers-never-substitutes rule), [ADR 0005](0005-hypothesis-and-action-schema-tightening.md)
  (the schema this change does not loosen), CLAUDE.md invariant 4 (tool output stays untrusted;
  this is about the agent's own output).
* Work order: WO-R2-173.

# ADR 0042: A candidate's evidence reference is resolved by a validator, against a ledger bound at the seam

* Status: accepted
* Date: 2026-09-17
* Decider: Kudrat Singh

## Context and problem statement

Phase 5 of the research buildout asks the planner for N candidate diagnoses instead of one ranking,
and Phase 6 puts a `candidate_selector` in front of the set. Every number those phases report —
pass@k, selected@k, the oracle gap — is a statement about a set of candidates. Plan 02 § 11.3 gives
the candidate schema, and one sentence in it is the load-bearing one:

> `EvidenceRef` is an `evidence_id` from the ledger; a ref that names no ledger entry fails
> validation (grounding is structural, not prompted).

A candidate that cites evidence which does not exist is not a weaker candidate — it is an
ungrounded one, and a best-of-N number computed over a set containing it is measuring the model's
fluency rather than its diagnosis. The rule has to hold structurally. A prompt sentence asking the
model to cite real ids is not enforcement; it is the same class of thing ADR 0005 replaced when
`Hypothesis.name` was free-form and the routing gate was a Markdown table.

So the decision is not *whether* — plan 02 settles that — but **where the check runs**. WO-R3-204
left the choice open and asked for it to be stated: "a Pydantic validation-context carrying the
ledger, or a boundary check at the strategy seam — state which and why."

The two candidates are not equivalent, because of one detail of the machinery around them.

### What decides it: where ADR 0035 can see a failure

`llm/repair.py::call_with_output_repair` wraps exactly one expression:

```python
try:
    result = llm_client.call(...)
except _REPAIRABLE as err:
    ...  # one re-ask carrying the validation error
```

A failure raised **inside** that call is a harness event under ADR 0035: one bounded, billed re-ask
with the error fed back, then escalate. A failure raised **after** the call returns is outside the
`try` and gets no repair at all. A post-validation boundary check at the strategy seam is by
definition after the call returns.

That matters here more than it would for most checks. An ungrounded ref is exactly the kind of
mistake ADR 0035 exists for: the model's judgement may be entirely right and the envelope wrong —
a hallucinated id beside two real ones, or a set where the citations were transposed. Ending a run
on it, with no re-ask, would be run `779b19a287a7` again one field further in.

The other half of the machinery closes the loop the other way. `LLMClient._parse` validates with a
bare call:

```python
output = output_model.model_validate(block.input)
```

There is no context argument. Passing a Pydantic validation context down to it means changing
`LLMClientProtocol.call`, `LLMClient.call`, every fake, and all five existing `record_output` call
sites, to carry a parameter that one schema uses.

## Decision drivers

* **Grounding must be structural, not prompted** (plan 02 § 11.3, architecture-principles rule 1).
* **The failure must be repairable** (ADR 0035). It is a shape problem, and the information needed
  to fix it is already in the run.
* **Do not widen the client's contract for one schema.** Five call sites and every fake would carry
  a parameter none of them needs.
* **Fail closed.** A grounding rule that can be switched off by forgetting to do something is not a
  grounding rule. Whatever carries the ledger must refuse when it is absent, not permit.
* **The rule belongs where the class of field is defined**, not on each field that happens to use
  it. This is `hypothesis.py`'s own recorded lesson: the `json.loads` coercion on `next_action`
  covered one field of one model for six weeks and was not a fix for the class.

## Considered options

1. **A post-validation boundary check at the strategy seam** — the strategy validates the parsed
   candidate set against `run_state.evidence` after `call_with_output_repair` returns.
2. **A Pydantic validation context threaded through the LLM client** — add a `validation_context`
   parameter to `LLMClientProtocol.call` and pass it to `model_validate`.
3. **A Pydantic validator reading a ledger bound to a context variable at the seam** (chosen).
4. **No validation; render the ledger in the prompt and ask the model to cite real ids.**
5. **Drop the ids entirely** — let a candidate cite evidence as free text.

## Decision outcome

**Option 3.** `agent/candidates.py` holds a `ContextVar` and a context manager:

```python
with grounded_in(run_state.evidence):
    ...  # the planner call, wrapped by call_with_output_repair
```

`EvidenceRef` carries a `field_validator` on `evidence_id` that reads the bound ledger and rejects
an id that is not in it, naming the id and the ledger's size. The validator is on `EvidenceRef`
itself rather than on `DiagnosisCandidate.evidence_for`, so `evidence_against` is covered by the
same line and so is every future field of this type.

**It is fail-closed in two steps.** With no ledger bound, `EvidenceRef` refuses — a reference with
nothing to resolve it against is not a grounded reference. And because a set that cites nothing
never constructs an `EvidenceRef`, the set-level validator on `CandidateTuple` requires a binding
too. Forgetting `grounded_in` is therefore a loud failure on every path, not grounding quietly
switched off for exactly the payloads that cite nothing.

The consequence that makes this the cheap option: an ungrounded candidate set arrives as an
ordinary `ValidationError` inside `model_validate`, inside `_parse`, inside `llm_client.call` — so
ADR 0035's decoder, its one bounded re-ask, its billing, its `repair_of` trace correlation and its
`planner_output_invalid` failure class all apply with **no change to `llm/client.py`,
`llm/repair.py`, `LLMClientProtocol` or any fake**.

Two set-level rules ride along on the same type, for the same reason — they are properties the
measurement assumes, so they are validators:

* **No duplicate `(category, name)`**, per plan 02 § 11.1. One diagnosis stated twice is one
  diagnosis, and pass@k over a set that counts it twice is inflated.
* **No duplicate `candidate_id`.** Not in the plan's sentence, and it is structural: plan 02 § 12's
  `SelectionResult.scores` is keyed by that id, so a repeated id makes `selected_candidate_id`
  unresolvable. A schema that permits it hands WP-6.1 an ambiguity it cannot resolve.
* **Ranking normalised at the schema boundary**, so `candidates[0]` is the top candidate for any
  order the model emits, ties keeping its stated order. Same rule, same implementation and the same
  argument as `InvestigationStep._rank_by_confidence`: readers take "the top candidate" by index.

The rules live on an annotated type, `CandidateTuple`, not on one container's field, so a later
model that needs a candidate set cannot re-declare a bare tuple and silently lose them.

### Why the alternatives lose

**A boundary check at the seam (1).** It would work, and it has no ADR-0035 repair: the check runs
after the call returns, so the first ungrounded set ends the run. Giving it one would mean a
`validate=` hook on `call_with_output_repair` — a second validation path beside the one pydantic
already runs, for the same payload, in the same function. Two places that decide whether output is
acceptable is how one of them stops matching.

**A validation context through the client (2).** The honest version of option 3, and more
expensive: a parameter on the protocol, on the real client, on every fake, and at five call sites
that will never pass it. It also spreads the schema's requirement across the transport layer, which
has no business knowing that one output model resolves references.

**Prompt-only (4).** This is the thing plan 02 § 11.3 explicitly refuses, and architecture-
principles rule 1 with it. It also cannot be measured: an ungrounded citation would reach the
`StepRecord` and be counted as evidence.

**Free-text evidence (5).** Cheapest to produce and worth nothing. The point of a ref is that it
resolves to a row whose arguments and result a reader (and INC-002's rule) can put beside it. A
sentence describing the evidence is a second, unverifiable copy of it.

## Consequences

### Positive

* An ungrounded candidate cannot enter a candidate set. Every pass@k, selected@k and oracle-gap
  number in Phases 5 and 6 is computed over citations that resolve.
* The failure is repairable, billed, correlated and classified, with no new code in the repair path.
* `evidence_against` and every future `EvidenceRef` field are covered by the line that covers
  `evidence_for`.
* "The top candidate" is well-defined by index, with no downstream sort to keep in step.

### Negative

* **A context variable is global state**, and a validator that reads it is not a pure function of
  its input. `CandidateSet.model_validate(payload)` alone cannot succeed. That is deliberate — it
  is what fail-closed means here — but it is a real cost: a reader of `candidates.py` has to know
  about `grounded_in`, and a test that forgets it gets a refusal rather than a pass.
* **The refusal does not list the valid ids.** ADR 0030's pattern would offer the ledger's own ids
  in the re-ask, and `repair.py` trims a quoted error to 800 characters — a 25-entry ledger of
  UUIDs does not fit. The message names the unknown id and the ledger's size instead. If a live run
  ever spends its repair and still cites a bad id, this is the first thing to revisit.
* **One more thing WP-5.2 must do before it can ask for a citation.**
  `investigation._format_planner_context` renders the ledger as `- [tool_name] result_summary` and
  shows no `evidence_id`, so a planner asked today to cite one has never seen one. The schema is
  right and the context is not there yet; that rendering belongs to the loop, not to this schema.

### Neutral

* No prompt, no strategy and no call site changes here. `INFERENCE_STRATEGY` still has one member,
  `make eval-reg` stays 41/41, and nothing the agent reads moved.
* No class docstring on `EvidenceRef`, `DiagnosisCandidate` or `CandidateSet`: a class docstring
  becomes the JSON schema's `description` and these schemas are shown to the model. The prose is in
  the module docstring, and a test keeps it there.

## Revisit trigger

* A live run that spends its repair and still cites an id that does not resolve — that would say
  the error message, not the check, is the missing piece (see the second negative above).
* A second reader of the candidate schema outside a planner call (a re-grade of an archived
  trajectory, say). Re-validating an archived candidate set against a ledger read back from the
  archive works today, but it is the first case where `grounded_in` is being held open around
  something that is not one call, and it is worth looking at rather than assuming.
* Pydantic gaining a first-class way to pass validation context through a third party's
  `model_validate`, which would make option 2 free.

## More information

* Plan: `docs/plans/research-buildout-v2.1/02_INCIDENT_COMMANDER_PLAN.md` § 11.1, § 11.3, § 12;
  `04_IMPLEMENTATION_WORKPLAN.md` WP-5.1. Work order: WO-R3-204.
* Related: [ADR 0005](0005-hypothesis-and-action-schema-tightening.md) (the choices this schema
  mirrors: closed category enum, free-form name, tool-name Literals),
  [ADR 0035](0035-a-parse-failure-of-our-own-output-is-a-harness-event.md) (the repair path this
  decision keeps reachable), [ADR 0036](0036-the-planner-call-is-the-only-strategy-seam.md) (the
  seam the ledger is bound at), [ADR 0030](0030-a-refused-plan-is-re-asked-with-the-candidates.md)
  (the "harness offers, never substitutes" rule the refusal follows).
* Tests: `tests/unit/test_candidates.py`.

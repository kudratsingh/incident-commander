# ADR 0030: A plan refused for a mis-transcribed id is re-asked with the candidates

* Status: accepted
* Date: 2026-09-07
* Decider: Kudrat Singh

## Context and problem statement

Live run `5c8895771fbd` (`dlq_wait_and_replay_success`, archived under `evals/runs/`) failed on the seventh paid remediation scenario. The trajectory is worth reading in full, because almost all of it is right.

The agent listed the DLQ filtered to `wait_and_replay` and got two rows. It grouped them by the dependency each names rather than by their shared hint — `partner-api.internal` with an explicit `retry-after: 120s`, and `smtp.mailer.internal` with a bare `ConnectionRefusedError` — applied the dependency-down default to the second, took the larger of the two waits, and wrote the derivation into `action_rationale`. It picked `list_dlq_messages` as its verify leg and wrote a `verify_expectation` saying the rows would **remain** listed with a future `execute_at`, which is the one expectation in the suite satisfied by nothing happening. Every judgement the scenario exists to measure, it made correctly.

Then it emitted this:

```json
"job_ids": ["af67d1b1-13f8-5a2c-8c44-66ec5564597d",
            "97d91272-0000-0000-0000-000000000000"]
```

The second row's id is `97d91272-9774-5b8e-980b-f0d2fa6ed619`. The first block is right and everything after it is zero-filled.

`_unsourced_resource_args` (ADR 0024) refused the plan, correctly — no listing carries that string — and the run **escalated after one planner call**:

> plan rejected before execution: resource argument(s) not evidence-sourced: `replay_dlq_by_ids.job_ids='97d91272-0000-0000-0000-000000000000'`. Resource names must be copied verbatim from the alert or tool results.

No action executed, which is the right outcome for that plan. The scenario graded red on outcome, action and evidence.

### Why this is not a judgement error

The same run's `action_rationale` names `97d91272` as the SMTP row. The escalation briefing it wrote names `97d91272-9774-5b8e-980b-f0d2fa6ed619` in full, twice, and tells the operator to replay "using the exact IDs returned by `list_dlq_messages`". The briefing judge scored groundedness 1.0 and quoted both correct ids back.

So the model held the right id in three places and got it wrong in one — the arguments dict. This is a **transcription slip**, not a wrong belief about which job to replay.

### The gap in the guard family

Seven guards run on a remediation plan. Since ADR 0025, three of them **refuse and re-ask once** rather than ending the run: `_unobserved_action_resource`, `_unread_action_rows` (ADR 0027), `_unlisted_action_scope` (ADR 0028). Three others **escalate on the first offence**: `_absent_resource_args`, `_unsourced_resource_args`, `_misdirected_verify_args`.

`make_llm_plan`'s own docstring justified the split:

> ...unlike the three argument guards above, which escalate because a mis-named resource means the planner is reasoning about the wrong object rather than merely checking the right object the wrong way.

That reading is what this run falsified. **Reasoning about the wrong object and copying the right one out wrongly are two different failures, and only the first is unsalvageable.** The guard could not tell them apart, so it applied the harsher disposition to both.

### Why the re-ask is cheap and the repair is real

PLANNING is a single LLM call with no tool budget. The planner cannot go and fetch anything — which is precisely why the three existing refusals are capped at one and why their steers name what is already available. For a mis-transcribed id the repair needs nothing fetched: **every candidate the planner could want is already in the evidence**, in the listing it read one call ago. The refusal only has to say so.

### The trap in the existing refusals

A mangled id also fails `_unread_action_rows` — no listing carries a row for an id that does not exist. That guard's steer says to **drop** the ids you have no row for. Obeyed on this run it would have turned a two-row delayed replay into a one-row one, which fails `scheduled equals 2` exactly as escalating did. So the ordering is load-bearing: the transcription diagnosis has to outrank the unread-row one, or the fix produces a differently-wrong plan.

## Decision drivers

* The information needed for the repair is already in the run. No probe, no spend beyond one LLM call.
* The harness must not correct the id. Substituting the nearest evidence value would be the agent choosing which job to replay from a guess about intent — the decision this whole guard family exists to keep with the model and its evidence.
* A "did you mean" is a claim. It may only be made when it is unambiguous.
* Shape and provenance are different questions and want different messages.
* It must not widen what executes. A refusal changes which plans get a second chance, never which plans run.
* Prompt prose alone would not hold: offline eval replays canned planner output and never loads a prompt, so a prompt rule about this is ungated by the suite that gates behaviour changes (ADR 0026, ADR 0027). The prompt already said "never re-type, trim, or abbreviate" and the slip happened anyway.

## Decision

**A plan refused because a resource argument is mis-shaped or not evidence-sourced is re-asked exactly once, with the ids the run actually read quoted back to it. The second offence escalates as before.**

Four parts.

### 1. `_unsourced_resource_args` refuses instead of escalating

`_MAX_ARGUMENT_REFUSALS = 1`, matching its three siblings. The refusal is written to evidence under a fourth marker, `_plan_refused_argument`, added to `_PLAN_REFUSAL_MARKERS` so `_format_plan_context` renders it whole and last — a candidate list truncated at the 200-character evidence cut is absent exactly where it matters.

Underscore-prefixed like the other three, so the briefing's investigation trail and the grader's called-tools set both skip it. **It spends no tool-call budget**; only the planner tokens of the re-ask, which `_plan_once` charges before the plan is judged (ADR 0015).

### 2. The refusal carries candidates, from the source rows

Candidates come from `SOURCE_ROW_FOR_ACTION` via `_rows_read_for` — the ids the listings in evidence actually returned — **not** from `_evidence_value_corpus`. The corpus holds every string the platform ever uttered (trace ids, error text, timestamps, hostnames), so an offer built from it would be a wall of noise with the answer inside it.

Where the rejected value shares at least 8 characters — the first block of a UUID, which is how this project abbreviates one everywhere — with **exactly one** candidate, the refusal adds `did you mean <id>?`. Two near-matches means the prefix cannot separate them, so no claim is made; the enumeration still goes out.

### 3. The harness offers; it never substitutes

Nothing writes a candidate into a plan. A planner that repeats the mangled id gets an escalation and no stored plan at all, so nothing downstream can read a corrected id off the run state either. Pinned by `test_the_harness_never_substitutes_the_candidate_itself`.

### 4. `_malformed_resource_args`: shape, checked from the platform's own schema

A new guard, running immediately before the provenance one, rejects a value in a UUID-typed resource field that is not canonical 8-4-4-4-12 hex, with a message about the characters rather than about the whole value. It reports through the same refusal path and the same budget.

Which fields those are is **derived, not declared**: `policies.UUID_RESOURCE_FIELDS` reads `format: "uuid"` out of each tool's own input-model JSON schema, which `tests/unit/test_registry_matches_snapshot.py` holds to exact equality with `contracts/platform-tools.snapshot.json`. A hand-written list would be a second copy of a contract that moves on the platform's schedule. Six fields across five tools qualify; `get_trace.trace_id` and `invalidate_cache_key.key` deliberately do not — the platform accepts any string there and the agent must not invent a contract it does not have.

**Honest limitation, stated so nobody re-reads this as the fix for the live run:** `97d91272-0000-0000-0000-000000000000` **is** canonical 8-4-4-4-12 hex. No regex can reject it. The shape check catches truncation, wrong length and non-hex; provenance is what catches a well-formed id that names no job. The pair covers the field and neither would alone. Pinned by `test_the_zero_filled_id_falls_through_to_the_evidence_check`.

### Ordering

Both argument checks stay inside `_plan_once` rather than moving to the caller's loop beside the other three refusing guards, because they must run **before** `_misdirected_verify_args`: a mangled id on the action leg makes a correct verify id look like it names a resource the action never touched, so the misdirection check would fire on the typo and escalate with a diagnosis about a verify leg that is fine.

In the loop, the argument refusal is dispositioned **before** `_unread_action_rows`, for the reason given above.

## Consequences

### Positive

* The live run's plan now re-plans and is admitted when the planner corrects itself; `test_the_live_plan_is_no_longer_a_one_call_escalation` is that regression, and it fails on `origin/main` with `assert 1 == 2`.
* A truncated or non-hex id gets a message about its characters before the vaguer provenance one.
* The candidate enumeration is derived from the rows in evidence, so it cannot drift from what the run actually read.
* Three vacuously-passing tests were found and fixed while making this change. `TestEvidenceSourcedArgs`' three rejection tests fed **one** canned plan and asserted ESCALATED; under the new disposition they escalated on `planner LLM invalid: no more canned responses` — green, with the guard having decided nothing. They now feed two and assert the exhaustion message is absent.

### Negative

* **One more billable planner call in the worst case.** A plan can now spend an argument refusal, then a row-or-category refusal, then a verify-target refusal: four planner calls where three were the ceiling. None spends tool-call budget. `test_the_argument_refusal_charges_both_the_plan_and_the_re_ask` pins that both calls are metered, because a re-ask that only billed when it ended in an escalation would make the cheap-looking outcome the one the meter under-reports.
* A planner that mangles ids *systematically* now costs two calls per incident to discover instead of one.
* `_absent_resource_args` and `_misdirected_verify_args` still escalate on the first offence. Both are arguably re-askable too — an omitted field especially — but neither is what the live run hit, and widening a refusal budget onto guards with no evidence behind them is how a cap becomes decoration. Filed rather than improvised.

### Neutral

* Offline `make eval-reg` stays 38/38. It cannot see the prompt half of this change at all (canned trajectories never load a prompt), which is why the prompt line is pinned by a hash and an invariant test instead.

## Revisit trigger

* A paid run where the planner is refused twice on ids **and the candidate list was correct both times** — that would say the offer is not the missing piece and the re-ask is buying nothing.
* Evidence-row handles landing (see below), which would make most of this guard unreachable rather than wrong.

## The follow-up this does not do: evidence-row handles

The structural end-state is that the planner never re-types a UUID at all. When the harness records a listing it assigns each row a short opaque label — `dlq#1`, `dlq#2` — and renders those in the planner's context; the plan names labels; the harness resolves label → id on the wire. A slip then becomes a label that does not resolve, which is a hard error with one obvious repair, and the class of failure this ADR mitigates stops existing.

It is out of scope here for three reasons: it changes the planner's context format (so every canned plan fixture in the suite is invalidated at once), it needs a decision about how labels interact with `_unsourced_resource_args` and the two read-before-act guards (a label is not a platform-produced string), and it wants its own paid run to establish that the model uses labels as reliably as it uses ids. Filed as a work order. This ADR is the cheap mitigation that lands today; handles are the fix that removes the surface.

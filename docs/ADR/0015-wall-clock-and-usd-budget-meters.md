# ADR 0015: Wall-clock and USD budget meters — accrual anchors, a pinned price map, and total-volume token semantics

* Status: accepted
* Date: 2026-08-09
* Decider: Kudrat Singh

## Context and problem statement

CLAUDE.md invariant 7 says every incident run has "explicit ceilings on tool calls, tokens, wall
clock, and dollar cost", and `docs/safety-model.md` publishes a four-row budget table to match.
`BudgetLedger` has carried all four fields since Phase 0: `wall_seconds_used` and `usd_used` are
declared (`agent/state.py`), checked by `is_exhausted`, and rendered in every escalation briefing
(`agent/briefing.py`). Nothing ever wrote them. A grep across `src/`, `evals/`, and `tests/` found
the only writers were two manual `model_copy` calls inside `tests/unit/test_state.py` — which prove
the *predicate*, not the accrual.

The consequence is not subtle. Finding B-01 reproduced a run that burned **2h46m of real wall time
against a 60-second cap** and reported `wall_seconds_used: 0.0`; `BUDGET_MAX_SECONDS` and
`BUDGET_MAX_USD` could not trip, and every briefing reported `"usd": "0"` regardless of spend.
Two of invariant 7's four dimensions were decorative.

Separately (finding C-06), all three token-accrual sites summed only `input_tokens +
output_tokens`. `llm/client.py` applies `cache_control` to the system prompt, so on a live call
most input volume arrives on `cache_creation_input_tokens` / `cache_read_input_tokens` — which
`LLMResult` carries and every accrual site dropped. `BUDGET_MAX_TOKENS` metered the un-cached
remainder only, and under-enforced *precisely when caching worked well*.

So: implement the missing writers, or amend invariant 7 down to the two dimensions that work?

## Decision drivers

* Invariant 7 and `safety-model.md` are load-bearing claims about this system's safety posture.
  A documented hard limit that cannot trip is worse than an undocumented one — it buys false
  confidence in review and in the demo.
* The expensive parts already exist. Fields, `is_exhausted` checks, and briefing rendering are all
  shipped; only the writers are missing. This is a day of work, not a phase.
* Per-incident cost metering is a Phase 8 exit criterion ("per-incident cost dashboards"), which
  reads `usd_used` off `RunState`. Amending it away now means building it again later.
* Offline eval runs must not need network (`make eval` runs with no `ANTHROPIC_API_KEY`), so
  nothing in the cost path may fetch prices at runtime.
* `usd_used` is `Decimal`; a float intermediate would leak binary-rounding noise into the string
  the briefing renders.
* Crash-resume is a real path: the loop checkpoints after every transition and resumes mid-run.
  Any wall-time anchor that a resumed process rebuilds from scratch silently refunds the budget.

## Considered options

1. Amend invariant 7 to drop the wall-clock and dollar dimensions, and rewrite `safety-model.md`
   to describe a two-dimension budget.
2. Implement the two missing meters and complete the token meter (chosen).
3. Implement the meters, but derive USD from a live pricing lookup.
4. Accrue wall time from per-transition clock deltas threaded through the loop (the audit's own
   fix sketch for B-01).
5. Accrue "billed-token-equivalents" into `tokens_used` — weight cache-write at 1.25x and
   cache-read at 0.1x — instead of a raw sum (the audit's alternative sketch for C-06).

## Decision outcome

**Option 2.** Implement the meters. CLAUDE.md needs no edit — that is the point of implementing
rather than amending. Four sub-decisions follow.

### 1. Wall time is anchored on `RunState.created_at`, accrued at loop granularity

`agent/loop.py` reads the injected clock once at the top of each `run_to_completion` iteration and
sets `wall_seconds_used = (now - run_state.created_at).total_seconds()`, guarded to be monotone
(`if elapsed > current`), then reuses that same `now` for the transition it dispatches. The
per-iteration `clock()` count is unchanged, so nothing that scripts the clock gets a new read to
account for.

The anchor is the durable one on purpose. `created_at` is checkpointed with the rest of
`RunState`, so a run resumed after a crash keeps every second the first process burned. A
process-local start stamp would hand each resumed run a fresh wall budget — the failure is silent,
and it is worst exactly when a run has already been expensive.

Granularity is the loop step, which matches how the tool-call ceiling is already enforced: a
sleep or a stalled HTTP call *inside* a transition is not observed until the next step. The LLM
client's 120s read timeout (ADR-adjacent, `llm/client.py`) is what guarantees that next step
arrives. The existing VERIFYING exemption (`loop.py`) is untouched and now also exempts wall/USD
exhaustion mid-verify, consistent with ADR 0006: an executed-but-unverified Tier-1 action is worse
than one poll over budget.

### 2. USD comes from a pinned in-repo price map, never a runtime fetch

`llm/pricing.py` holds `MODEL_PRICING`: a frozen `ModelPricing` row per model id with `Decimal`
rates in USD per million tokens, covering all four token classes. `cost_of(model, result)` sums
the four classes and quantizes to microdollars (`Decimal("0.000001")`, `ROUND_HALF_UP`) — cents
would round a cache-served planner call to zero and read $0.00 for an entire investigation.

Prices are **configuration**, not a lookup. Three reasons: offline `make eval` must run with no
network at all; a run's reported cost must be reproducible from the checkout alone, which a
time-varying remote number cannot be; and a pricing API call in the accrual path is a new failure
mode inside a safety meter. The staleness tradeoff is real and accepted — the module docstring
carries the same verify-on-change rule CLAUDE.md already applies to model id strings, and the
price map should be re-checked against published pricing immediately before the post-campaign
eval run.

An unknown model id falls back to the **per-class maximum across every registered row** — a
synthetic row, not a registered one — and logs a warning once per id. It never raises: an
unpinned `AGENT_MODEL` is an operator error, but aborting a live incident run over an accounting
gap is a worse outcome than an over-conservative charge. Over-reporting keeps the ceiling safe;
silently under-reporting would not.

**Amended by WO-R2-118.** The fallback above is unchanged and still the right behaviour at the
point it fires — mid-run, with an incident in flight. But it was also the *only* check, so the
operator error it tolerates was never reported anywhere except one `WARNING`, and a mistyped
model id simply ran the whole campaign billed at the ceiling. `Settings` now refuses at
construction to accept an `AGENT_MODEL` or `JUDGE_MODEL` with no price row. Nothing is in flight
at that point, so the argument above does not apply: the honest answer to "which model am I
about to bill?" is to demand one rather than to guess high. The two together mean an unpriced id
cannot reach a run through configuration, and a model that somehow appears at runtime anyway is
still charged conservatively rather than crashing the incident.

The fallback was originally the single priciest *registered* row, ordered by the sum of its four
rates. That is not an upper bound and the difference is not theoretical: a table can hold a row
that is cheaper in total yet dearer in one class, and an unpinned model billed at the sum-winner's
rates is then metered below its real price in that class — the exact silent under-report this ADR
says cannot happen. The per-class maximum makes the guarantee true by construction for any table
anyone writes later, rather than true by coincidence for the rows registered today, and
`tests/unit/test_pricing.py` asserts it as a property over the whole table instead of as spot
values.

### 3. `tokens_used` is total token volume, including cache tokens

`agent/accounting.py::accrue_llm_usage` charges `input + output + cache_creation + cache_read` and
adds `cost_of(...)` to `usd_used` in the same call. It lives in `agent/` rather than `state.py`
because `state.py` deliberately stays free of `llm` imports — the checkpoint schema must not
depend on the model client.

`tokens_used` stays a *volume* meter. USD is the cost-weighted meter, and it ships in this same
change; weighting the token counter by price too would encode the price map in two places and make
`BUDGET_MAX_TOKENS` mean neither volume nor cost.

### 4. Briefing-writer and briefing-judge LLM calls stay outside the run ledger

`evals/runner.py` calls `enrich_briefing` and `judge_briefing` *after* the run reaches a terminal
state. A ceiling cannot gate a call that happens after the last budget check, so threading the
ledger through them would buy accounting without enforcement — and threading a mutable ledger
through the post-terminal path is exactly the scope creep this ADR declines. Their cost is visible
in the trace files. If per-invocation total cost (run + briefing) is wanted later, it belongs in
the eval report's own accounting, not in `BudgetLedger`.

### Why the alternatives lose

**Amend invariant 7 (option 1).** This is the option that saves a day, and it is the wrong trade.
Invariant 7 and `safety-model.md`'s budget table are among the strongest safety claims this repo
makes; weakening a documented safety property because the writers were never written inverts the
relationship between the spec and the code. The fields, checks, and rendering already exist —
what was missing was the cheapest part.

**Live pricing lookup (option 3).** Breaks offline evals outright, makes historical run costs
irreproducible, and puts a network dependency inside a safety meter.

**Per-transition clock deltas (option 4).** The audit's sketch. Deltas have to be threaded through
the loop as extra state, and — the fatal part — they reset on crash-resume: a resumed run starts
accumulating from zero and the meter silently forgets everything the crashed process spent.
Anchoring on `created_at` is both simpler and durable.

**Billed-token-equivalents (option 5).** Double-encodes price into a token counter. With the USD
meter landing in the same change, `tokens_used` would become a second, worse cost meter, and
`BUDGET_MAX_TOKENS` would stop being interpretable as "how many tokens may this run move".

### Consequences

Positive:

* All four dimensions of invariant 7 are genuinely trippable. The B-01 repro shape — 61s per step
  against a 60s cap — now escalates with `"budget exhausted"` instead of running for hours.
* Briefings report real wall time and real dollars; the Phase 8 per-incident cost dashboard has a
  populated field to read.
* `BUDGET_MAX_TOKENS` meters what the API actually moves, so caching no longer weakens the token
  ceiling.
* The wall meter is crash-resume-correct by construction, not by convention.

Negative:

* The price map goes stale silently when Anthropic changes prices. Mitigation: verify-on-model-
  change rule in the module docstring, re-check before the post-campaign eval run, and unit tests
  pinning the published cache multipliers (1.25x write, 0.1x read).
* Wall time is observed at loop granularity, so a stall inside one transition is invisible until
  the next step. Mitigation: the client's bounded read timeout and retry cap put a ceiling on how
  long one transition can take.
* Real cache-token shapes and the map's agreement with actual billing are unobservable offline.
  The arithmetic and trip logic are fully unit-proven; the first real USD figures appear at the
  post-campaign eval run.
* Canned eval scenarios are unaffected (`CannedLLMClient` reports zero usage by default, and
  offline runs take milliseconds), so no canned outcome moves — which also means the offline
  suite structurally cannot exercise the wall or USD dimensions. Unit tests at the loop and
  transition level are the right altitude for that, not a canned scenario.

Revisit trigger: a change to `AGENT_MODEL` or `JUDGE_MODEL` (re-verify the price rows), a published
Anthropic price change, or a decision to meter post-terminal briefing spend — which would need its
own accounting surface rather than an extension of `BudgetLedger`.

## More information

Findings B-01 (High, reproduced: `wall_seconds_used: 0.0` after 2h46m against a 60s cap), C-05
(duplicate of B-01, USD leg), S-13 (fix-survival sibling: the dimensions are unobservable offline),
and C-06 (Medium, cache-token under-accrual). Work order WO-C3-02.

Related: ADR 0002 (hand-rolled state machine — the loop this meter lives in, and the checkpoint
that makes `created_at` durable), ADR 0006 (verification is a polling window — the VERIFYING
budget exemption this change inherits), CLAUDE.md invariant 7 (unchanged), and
`docs/safety-model.md` §Budgets (its "Enforced by" column becomes true with this change).

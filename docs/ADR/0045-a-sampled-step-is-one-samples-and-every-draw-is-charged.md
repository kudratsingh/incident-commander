# ADR 0045: A sampled step is one sample's, and every draw is charged — including the ones before a failure

* Status: accepted
* Date: 2026-09-17
* Decider: Kudrat Singh

## Context and problem statement

`best_of_n_sampled` (WP-5.3, plan 02 § 11.2) makes **N independent planner calls** at a configured
temperature and takes the union of their top hypotheses as the candidate set. Two questions fall out
of "N calls where there used to be one", and neither has a default answer.

### 1. Which step does the loop receive?

Every strategy must hand the investigation loop one `InvestigationStep`, because that is what the
`FIX_MAP` gate, the 0.7 threshold, the subject-probe refusal, ADR 0041's whole-queue refusal and the
tier re-check all read. N samples give N of them. The tempting move is to compose: take the
hypothesis ranking from the union (most confident first) and the `next_action` from whichever sample
proposed the winner.

That composition is wrong in a way that is hard to see and impossible to audit. A sample's
`next_action` is chosen *given its own ranking* — the planner prompt says "pick the probe most likely
to discriminate between the top two hypotheses". Pair a probe from sample 3 with a ranking assembled
from samples 1, 3 and 7 and you have emitted a step no model proposed: a probe selected to
discriminate a hypothesis that is no longer on top. Every gate then runs on a decision nothing is
accountable for, and the trajectory a reviewer reads is a trajectory nothing produced.

### 2. Who pays for the samples when one of them fails?

`plan_next_step` returns a new `RunState` carrying the accrued budget. The investigation loop's
failure arm does this:

```python
except (ValueError, ValidationError, LLMError) as err:
    run_state = run_state.model_copy(
        update={"budget": accrue_llm_error(run_state.budget, err, model)}
    )
```

`run_state` there is the state the loop held **before** the call. So a strategy that accrues into a
local state as it goes and then raises has that accrual thrown away with the state it was written
into. With N = 8 and a transport failure on the eighth draw, seven billed calls would be charged to
nobody — a silent under-report, which is the one direction ADR 0015 forbids, on the arm where it is
easiest to break (the order for this packet says so in as many words).

## Decision

**1. The emitted step is one sample's, verbatim.** The winner is the sample whose top hypothesis has
the highest confidence, ties going to the earliest draw so the choice is deterministic given the
draws. Its whole step is emitted — ranking *and* `next_action`. Nothing is composed and nothing is
blended.

The union is recorded, not emitted: `StepRecord.candidate_set` holds every distinct top hypothesis
across the N draws, deduplicated by `(category, name)`, first occurrence winning so a later agreeing
sample counts as agreement rather than as a second candidate. That is what pass@k, the duplicate
rate and (in Phase 6) the selector read. The distinction is the whole shape of the seam: the record
is what was *considered*, the emitted step is what was *decided*, and only the second one has
consequences.

**2. Every billed leg of the step reaches the ledger exactly once, on both paths.** On success the
strategy accrues each `RepairedCall` through the same `accrue_structured_call` `baseline` uses, so a
repair inside a sample is charged like any other repair. On failure it raises
`SampledPlannerFailed` — an `LLMError` subclass, so the loop's existing arm catches it unchanged —
carrying the **summed** usage of every leg the step billed: the k−1 samples that returned, their
repairs, and the call that failed. The loop's one `accrue_llm_error` then charges all of it against
the pre-call budget.

Not both. The strategy does not also accrue internally on the failure path, because the sum is
charged once and charging it twice would over-report by a factor of two. `repair.sum_usage` became
public for this; it already carried `discarded_max_tokens` as a maximum rather than a sum, which
keeps the total a conservative over-estimate.

**3. Temperature is a per-call parameter and is sent only when asked for.** `LLMClientProtocol.call`
takes `temperature: float | None = None`, and `LLMClient` adds the field to the request body only
when it is not `None`. Every other role's request bytes are therefore unchanged — which matters
because the request body is what the tracer records and what the provider reads, so a
`"temperature": 1.0` on every call would rewrite the recorded request of the runs the campaign's
eight green live results were made with.

`llm/client.SAMPLING_REJECTED_MODELS` declares the model ids that reject the sampling parameters
outright (the Fable/Mythos 5 family, Opus 5 / 4.8 / 4.7, Sonnet 5). It is **not** a refusal: nothing
blocks an id that is not on the list, because a wrong refusal stops a legitimate run while a wrong
allow surfaces as a rejected request that the provider does not bill and that
`LLMClient.call` already turns into one escalation with the reason in it. What the list is for is a
tripwire — no id in `MODEL_PRICING` may appear in it, so the day someone prices one of those models
the suite fails and names this arm, instead of a paid sweep discovering it.

## Consequences

**What this buys.** The literature's pass@k, on the same recorded worlds and with the same reporting
as `best_of_n_enumerated`, so the two arms' oracle gaps are comparable — which is the finding
decision D4 keeps both arms for. And a cost meter that stays honest at N = 8: `llm_calls` on the
step record has length N, so a reader can see that the step cost N calls rather than inferring it
from a total.

**What it costs.**

* **Latency is N× the planner's, and it is not engineered away.** The draws are sequential: the loop
  is a synchronous state machine (ADR 0002) and the client is blocking, so concurrency here would
  put thread-safety requirements on the accrual, the append-only tracer and the run's single ledger
  for a latency win on an offline benchmark. The number is reported instead —
  `StepRecord.llm_calls[].elapsed_ms`, per sample.
* **The ledger delta is carried on the first `LLMCallRecord` of the step and zero on the rest.** It
  is a per-step quantity (`after.budget − before.budget`) and repeating it per sample would multiply
  the step's charge by N for anyone who summed the column. The four provider counters are per sample
  and are where the split actually lives.
* **The declared cost multiplier is still unmeasured.** 03 § 11 prices `sampled-8` at 6–8× planner
  cost with a blended multiplier of ~2.5, and divergence J3 records that the plan's own arm count
  and estimate disagree. Measuring the real ratio needs a run, so it is deferred with a stated
  tolerance of ±20% of the declared multiplier and a row in `.coordination/DEFERRED-PAID-RUNS.md`.
  The comparison to make is the `investigation_planner` role's token total against a `baseline` run
  over the same scenarios.
* **This arm is not comparable with `best_of_n_enumerated` on the judge's soft dimensions or on the
  evidence-id axis.** Its output schema is a plain `InvestigationStep`, so it is shown no evidence
  ids (`strategy_config.evidence_ids_rendered` is `False`, stated rather than omitted — ADR 0044) and
  its `reasoning` is the model's own prose, where the enumerated arm's is derived from citations. The
  deterministic dimensions are comparable across all three arms; those two are not.

## What was rejected

* **Composing the emitted step** from the union's ranking plus the winner's action — § 1 above.
* **Accruing internally and raising bare.** It reads correctly and loses N−1 calls' worth of spend
  on every failed step, silently.
* **Making the loop's failure arm read a partial state** returned alongside the exception. It would
  put strategy-specific accounting in the shared loop, where every future strategy would have to be
  trusted to fill it, and the exception already has a `usage` field that `accrue_llm_error` reads.
* **Refusing a model that rejects sampling.** Fail-open with a tripwire is cheaper and cannot block
  a legitimate run — § 3 above.
* **Drawing the samples concurrently.** Deferred, not dismissed: it is a real speedup for a recorded
  -world sweep, and it is a thread-safety change to the accrual and the tracer that wants its own
  packet and its own ADR.

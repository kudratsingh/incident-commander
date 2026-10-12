# ADR 0084: A calibration records what it was billed, and caches the question it repeats

* Status: accepted
* Date: 2026-10-12
* Decider: Kudrat Singh (owner decision O-62, WO-R3-384, WO-R3-385)
* Related: [ADR 0015](0015-wall-clock-and-usd-budget-meters.md) (the price map, the cache-token
  meter and "over-report, never under-report", which this reuses unchanged),
  [ADR 0052](0052-a-judge-number-is-withheld-until-its-judge-is-calibrated.md) (why every question is asked N
  times), [ADR 0082](0082-the-briefing-judge-is-checks-scored-on-a-held-out-packet.md) (the rubric
  whose bytes a calibration hashes, and why they may not move for a cost reason)

## Context and problem statement

Two things the owner asked for on 2026-10-12 (O-62):

* **"Record the billed figure, not the estimate."** A judge-calibration report carried no cost at
  all. The WO-R3-383 Sonnet trial was estimated at $0.60 beforehand and came to roughly $1.0–1.5;
  the harness could not say which, because it threw every call's usage block away. The only source
  was the Anthropic console.
* **"Prompt caching on the judge rubric before the pilots run."** The briefing judge's rubric is
  about 3,300 tokens and goes out on every call.

What the repository already had, which the work order did not know: `llm/client.py` has marked the
system prompt with `cache_control` on every call since it was written, and the judges and the
selector all ask through it, so the rubric was already marked. It did little, for three reasons:

1. **Haiku 4.5 will not cache a prefix shorter than 4,096 tokens** (Sonnet 4.6: 1,024). The
   registered briefing judge runs on Haiku. Its prefix — the forced-output tool schema (~125
   tokens) and the rubric (~3,200) — is under that, so the marker was silently ignored: no error,
   no cache tokens, full price. The action verifier's prompt (~650) is under even Sonnet's minimum.
2. **Each question is asked five times in a row** (self-agreement, ADR 0052) and only the rubric
   was marked, so asks 2–5 re-paid the whole question every time.
3. Nothing recorded the cache tokens a calibration did get, so no one could tell 1 and 2 apart
   from a working cache.

Sizes above are characters ÷ 4, not a token count: counting tokens is an API call, and this work
order spends nothing.

## Decision drivers

* The rubric's bytes are the instrument. A calibration hashes them (`rubric.sha256`) and the
  register's 8/16 describes those bytes; a change made to save money is still a change to the
  judge (ADR 0082: one change, scored on a held-out packet).
* A marker that cannot cache must cost nothing, and a marker on bytes nobody reads back must not
  be paid for.
* One price map and one meter: the figure a report prints must be the figure ADR 0015's meter
  would charge a run for the same calls.

## Considered options

1. **Pad the rubric to Haiku's 4,096** with the shared rules or other filler. Rejected: it moves
   the rubric hash, so the registered 8/16 no longer describes the judge, and it adds ~800 tokens
   to every call on every model, Sonnet included, where the rubric already caches.
2. **Accept no cache on Haiku.** Taken for the eval-time judge (below); not enough for the
   calibration, whose bill is mostly repeats.
3. **Mark the question as well, when it is asked more than once** (chosen for the calibration).

## Decision outcome

**Billed usage (WO-R3-384).** Every calibration call passes through
`agent.accounting.MeteredLLMClient`, the meter a run's judge already goes through, under one role
per leg (`traps`, `labels`), never charged to any run's ledger. `evals/judge_calibration/billing.py`
sums the API's usage block — input, output, cache-write and cache-read tokens, and a call billed and
then failed (ADR 0015's discarded attempts) — in total, per leg and per model, and prices it with
`llm/pricing.cost_of`: cache reads at the cache-read rate, cache writes at the cache-write rate.
The report stores `usage` and `usd_used`; the summary's first line ends
`billed $X.XX (N calls, cache-read P%)`, where P is the share of all prompt tokens served from the
cache; the label leg prints its own `billed` line. A report written before this carries no `usage`
and reads `usd_used: null` (`billing.billed_of`), never `0`. "Billed" is the API's usage times the
pinned map; the console's invoice stays the authority. A zero-token call (the scripted fake) costs
$0 without a price lookup, so the free path no longer warns about the fake's model id.

**Caching (WO-R3-385).** The paid path builds its client with `harness.live_client`: the system
prompt is marked as before, and when `reps > 1` the user message — the question — is marked too
(`LLMClient(cache_question=True)`; off by default, so the agent's calls are unchanged). Ask 1 of a
question writes the whole prompt to the cache, asks 2–N read it. On Sonnet the rubric prefix also
caches across questions, as it already did. On Haiku a question caches only when rubric + schema +
question reach 4,096 tokens: most labelled briefings are long enough (roughly 700 tokens and up),
the short trap questions (~100–450) are not, and for those nothing changes. At `reps == 1` the
question is not marked: nothing would read it back, and the write premium would be pure cost.

**Eval-time judge and the selector in a run.** Unchanged: each briefing is judged once per run and
each selector question is new, so only the system prompt is marked — that caches on Sonnet and is
silently skipped on Haiku. The runner's meter already charges cache reads at the cache-read rate
(ADR 0015); a test now pins it for the judge.

`llm/pricing.CACHE_MINIMUM_TOKENS` records each priced model's minimum (checked against the
prompt-caching docs on 2026-10-12), and a test refuses a priced model without one.

### What it should save, and what is not yet known

Per question asked five times, with the question cached, the prompt costs 1.25 (one write) + 4 ×
0.1 (four reads) = 1.65 prompt-units instead of 5: about two thirds off the prompt tokens of every
question that clears the model's minimum. Output tokens are unchanged. How much of a real leg clears
it on Haiku is a measurement, not a calculation: **the next paid leg prints its cache-read share**,
and until then any saving quoted for a pilot is an estimate and must be labelled as one.

## Consequences

Positive:

* Every calibration from now on says what it was billed, per leg and per model; an estimate is
  never again the only number.
* The repeated asks of a question are read from the cache on any model the prompt clears, without
  touching the rubric's bytes or the registered calibration.
* A marker that cannot cache costs nothing; no marker sits on bytes that are never re-read.

Negative:

* The 5-minute cache window: the reps of one question go back to back, so they fit; nothing is
  cached across a pause longer than that.
* A repair re-ask (ADR 0035) appends to the question, so it misses the question's cache entry and
  writes its own; rare, and the rubric prefix still reads on Sonnet.
* The billed figure trusts the price map; if Anthropic's prices move before `MODEL_PRICING` does,
  the report is wrong by that difference (ADR 0015's existing risk, unchanged).

Revisit trigger: a judge or selector that is asked the same question more than once inside a run
(for example best-of-N sampling) should take `cache_question=True` there too; and if the
registered judge moves to a model whose minimum the rubric clears, say so in the next calibration.

## More information

Code: `evals/judge_calibration/billing.py`, `harness.live_client`, `harness.calibrate`,
`llm/client.LLMClient(cache_question=…)`, `llm/pricing.CACHE_MINIMUM_TOKENS`. Tests:
`tests/unit/test_calibration_billing_and_caching.py`. Runbook: § "Judge calibration: the billed
figure and prompt caching". Note on numbering: the plan's Phase 5 names "ADR 0006" for caching
economics, but 0006 went to the verification polling window; ADR 0015 and this record are the
caching decisions.

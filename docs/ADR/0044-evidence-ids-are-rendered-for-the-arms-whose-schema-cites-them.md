# ADR 0044: Evidence ids are rendered for the arms whose schema cites them, and not for `baseline`

* Status: accepted
* Date: 2026-09-17
* Decider: Kudrat Singh

## Context and problem statement

[ADR 0042](0042-a-candidates-evidence-reference-is-resolved-by-a-validator.md) made a candidate's
citation structural: `EvidenceRef` carries one `evidence_id` from `RunState.evidence`, and a ref
that names no entry there fails validation inside the planner call, where ADR 0035's one bounded
re-ask can see it.

It landed with a blocker written into its own module docstring:

> `investigation._format_planner_context` renders the ledger as `- [tool_name] result_summary` and
> shows **no** `evidence_id`, so a planner asked today to cite one has never seen one.

So WP-5.2 could not ask for a grounded candidate set until the ids were on the page. The question
this record answers is not whether to render them — the schema is unusable otherwise — but **whom
to render them to**.

Two options, and they are not symmetric.

### Option A — render ids for every arm, `baseline` included

Keeps every arm's planner context byte-identical, which is what
`docs/eval-methodology.md` § "Context handling is instrumentation, held constant across strategies"
and plan 02 § 17 ask for: a context difference between two arms is a confound, not a strategy
(decision C11).

Its cost is that it changes `baseline`. `baseline` is not one strategy among several — it is the
control group, and plan 04's working rule 5 is explicit that a report naming `baseline` names the
loop the campaign's eight green live runs were made with. Adding roughly forty characters to every
evidence line of every planner call changes the prompt those runs were made with.

The canned suite cannot detect that change and cannot bound it: the offline client replays a
scripted payload and never reads the prompt, so `make eval-reg` would come back "no changes vs
baseline" whatever the rendering did. The only instrument that could measure it is a paid live run,
and owner instruction O-22 (2026-09-17) is to build Phases 3–7 with **no paid runs**. Option A
therefore means moving the control group by an amount nobody has measured, and having no way to
measure it before the arms that depend on it are compared.

### Option B — render ids only for the arms whose output schema cites them

`baseline` keeps its exact bytes. The confound moves to the comparison: the best-of-N arm's context
differs from `baseline`'s by one `evidence_id=<uuid> ` prefix per evidence line.

## Decision

**Option B.** `agent/planner_context.format_planner_context` takes `show_evidence_ids`, defaulting
to off. `baseline` gets the string it has always got, byte for byte. `best_of_n_enumerated` — and
every later arm whose schema carries an `EvidenceRef` — passes `True`.

Three things make the confound bounded rather than hidden.

1. **It is one column, and that is tested.**
   `tests/unit/test_best_of_n.py::TestEvidenceIdsAreOnThePage::test_the_id_column_is_the_only_difference`
   asserts that stripping the prefix from the ids-on rendering yields the ids-off rendering exactly.
   Nothing else about the context moves with the flag, now or later.

2. **Every arm stamps which side of it it was on.** `strategy_config` carries
   `evidence_ids_rendered`, so no artifact can put two arms in one table without saying that one of
   them saw ids. The provenance record already carries `strategy_config` per scenario (ADR 0013,
   WP-0.3), so this needs no new field anywhere.

3. **The rendering is one function.** It moved to `agent/planner_context.py` — out of the
   investigation loop, which a strategy may import exactly one name from
   (`tests/unit/test_strategies.py::TestStrategiesHoldNoExecutionPolicy`) — so every arm renders
   through the same code with the same tool block and the same budget line. The difference between
   two arms is a flag, not two renderers that can drift.

## Consequences

**What this buys.** A grounded candidate set is askable. The control group is untouched: the canned
suite is byte-identical and, more importantly, `baseline`'s *live* behaviour is unchanged, so the
eight green live runs and the Phase 1 and Phase 2 close numbers still describe the arm they name.

**What it costs.** The enumerated arm's advantage or disadvantage against `baseline` includes
whatever the id column is worth. We believe that is small — the ids add no world content, only
labels for content already shown — but "we believe" is the whole of the claim, and it must not be
written up as if it were measured.

**The experiment that settles it,** when the owner authorises spend: run `baseline` live on a fixed
scenario set with `show_evidence_ids` forced on, and compare to `baseline` with it off. One flag,
two arms, no other difference. Until that is run, any best-of-N vs `baseline` comparison says so in
its limits section. The row is in `.coordination/DEFERRED-PAID-RUNS.md`.

**A second, unrelated consequence of the same packet,** recorded here because it is the other half
of "the schema is now askable": `DiagnosisCandidate` has no `reasoning` field (ADR 0042 — a
candidate justifies itself with the evidence it cites) and `Hypothesis.reasoning` is required, so
`best_of_n_enumerated` derives the emitted hypothesis's `reasoning` from the candidate's citations.
On that arm the `reasoning` a briefing and the LLM judge read is a deterministic citation list, not
model prose. The judge's soft dimensions are therefore **not comparable** between that arm and
`baseline`, and a report that put the two judge columns side by side would be comparing a model's
writing to a formatter's. The deterministic dimensions — outcome, evidence, action, safety, budget,
root cause — are unaffected, and they are the ones the research numbers rest on.

## What was rejected

* **Option A**, above: it moves the control group by an unmeasurable amount, at the one moment in
  the project when measuring it is not permitted.
* **Adding `reasoning` to `DiagnosisCandidate`** so the emitted step could carry model prose on both
  arms. It contradicts ADR 0042's shipped schema, it ripples into the Phase 6 selector, and plan 02
  § 7 is explicit that the structured output is the whole of what is stored. A derived, obviously
  derived string is better than a second prose field nobody asked for.
* **Rendering the ids as short prefixes** (the first eight hex characters). Cheaper in tokens, and
  it makes a citation unverifiable against the ledger without a lookup table — which is exactly the
  property ADR 0042 built the validator for.

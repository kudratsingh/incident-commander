---
status: accepted
date: 2026-09-15
supersedes: none
amends: none
---

# 39. A split is a property of a template, and the loader enforces it

## Context

The benchmark's unit is about to stop being "a scenario". Plan 03 § 2 makes it
`(template, seed, params)`: a **family** shares one observable symptom across worlds with
different root causes, a **template** is a family member with free parameters, and an
**instance** is that template with a seed. Reports from Phase 2 onward group by family,
difficulty and split; Phase 15's training export refuses to emit a held-out template.

None of those three keys existed. `evals/benchmark_inventory.json` (WO-R3-179) carried a
family and a difficulty for all 41 scenarios, both marked `provisional: true`, both derived
from a substring rule over tags, name and alert source — a placeholder that said so.

The part that needs a decision is not the fields. It is the **holdout**, and where a
violation of it gets caught.

Plan 03 § 4 states the rule as a promise: *holdout means never tuned against; every instance
of a held-out template is held out; a template appears in exactly one split.* The failure
mode is specific and quiet. Split by INSTANCE instead of by template and the held-out
instance's siblings stay in `dev`, a later SFT stage trains on those siblings, and the
holdout number then measures how well the model memorised a template it has seen — while
still being reported, correctly labelled, as a holdout. Plan 06 D7 rejects exactly that.

A straddling `template_id` is also invisible in either scenario's own file. Both files are
individually fine; the collision is a property of the directory. So no per-file validator can
see it, and the only two places that can are a load over the whole corpus and a report.

## Decision

**The five keys go on `Scenario`, and the split rule is a load error.**

1. `Scenario` gains `template_id` (defaults to `name`), `seed` (0), `family`, `difficulty`
   and `benchmark_split` (`dev`). `family` and `difficulty` are closed enums;
   `difficulty`'s nine members are plan 03 § 3's vocabulary verbatim, so widening it is a
   plan change rather than a repo change. All five are on the evaluator side of ADR 0038's
   partition: `difficulty` in a prompt narrows the agent's search for free, and
   `benchmark_split` would tell it which runs are being scored.

2. **`evals/scenarios/loader.py` refuses a `template_id` that appears under more than one
   `benchmark_split`**, naming both scenarios, both files and both splits. A load error, not
   a report footnote — by the time a footnote is being read, the number it footnotes has
   already been quoted. Naming both is not politeness: neither file is wrong on its own, so
   an error naming one sends the reader to a file that looks correct.

3. `template_id` is **stored, not computed.** A `template_id or name` property beside a
   stored field would be two spellings of one identity, and the loader's check, the
   inventory row and the report's grouping key all compare that value. (`chaos` avoids the
   same shape for `chaos_setup` a few fields up.)

4. **The grouping keys are copied onto `ScenarioOutcome`**, so a report says what a scenario
   was *when it ran*. A reader who joins a report back to `evals/scenarios/` reads today's
   classification against last month's run, which silently re-labels history on every
   reclassification — the same shape as invariant 9's "a metric derived from a mutable
   artifact is a lower bound". They default to `None`, meaning "predates the record", which
   every archived report and the committed baseline do (ADR 0013's precedent for
   `live_mcp` / `live_llm`).

5. **`family` and `difficulty` are optional on the model and mandatory in the corpus.**
   `tests/unit/test_scenario_metadata.py` sweeps `evals/scenarios/` parameterised by name, so
   a new scenario fails by name until somebody classifies it. Making them required *fields*
   instead would force a family onto thirteen inline `Scenario(...)` fixtures and a dozen
   inline YAML blobs that have no opinion about one — and a required field everybody has to
   fill is a field everybody fills with whatever loads.

6. **Nothing is assigned to `holdout`.** The mechanism ships; the promise does not. Marking a
   template held out commits every future session to never tuning against it, which is a
   scope decision for the user rather than a builder's default, and
   `TestNothingIsHeldOutWithoutADecision` keeps it that way until somebody decides.

7. **The promotion is reconciled, not asserted.** All 41 scenarios now declare a family and a
   difficulty. For 30 of them the value is exactly what WO-R3-179's rule produced; the other
   11 are a recorded table of (rule's guess, chosen value, reason) — five families the rule
   answered `uncategorized` for, and six difficulties where the rule keyed on the wrong thing
   (`no_fault_healthy_cache` is a level-0 control by its own header and the rule only looked
   for a `noise_` prefix). The table is a test, so "we promoted the provisional values" stays
   a checkable claim rather than a sentence in a PR body.

## Consequences

**Good.** A split violation is a crash on the next `make eval`, in every mode, before any
spend. The grouping keys WP-2.5 needs are on the report already, so it groups without a
schema change. `evals/benchmark_inventory.json` now distinguishes a value a human chose
(`provisional: false`) from one a rule guessed — which decides whether a surprising
per-family number is a finding or a typo in a tag.

**Cost.** 41 YAMLs gained two lines each. `ScenarioOutcome` gained five nullable fields that
are null on every archived report forever. The family vocabulary is now a closed enum, so a
genuinely new symptom class is a schema edit — deliberate: `jobs_not_progressing`,
`workflow_stuck` and `api_latency` are named in plan 01 § 7 and are **not** in the enum,
because a family for a world nobody has built is an empty group in every report, and an empty
group reads as a measured zero.

**Not decided here.** Which templates are held out, and when (the user's call — see 6).
Whether a template ever gets a second instance; today every `template_id` equals its scenario
name, which is the honest description of a corpus of 41 hand-written worlds. The export and
report halves of plan 03 § 4's enforcement — the training export refusing to emit a holdout
template, and the report refusing to score a policy on an exported one — belong to Phase 15
and WP-2.5; this ADR builds the loader half they depend on.

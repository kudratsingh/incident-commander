---
status: accepted
date: 2026-09-17
supersedes: none
amends: none
---

# 40. A ground truth is a statement about one world

## Context

`Scenario.ground_truth` (ADR 0038, WO-R3-261) records what was actually wrong, as
`HypothesisCategory` values, and `ROOT_CAUSE` grades the agent's final diagnosis against it.
Every one of the 32 labels was decided from what the scenario's world *is* — the chaos hook it
seeds and the canned fixtures the agent reads — and the evidence for each sits in a comment
above it, naming the fixture line it rests on.

A scenario does not have one world. It has up to three:

- **canned** — the recorded `canned_tool_responses` the offline suite replays. This is the
  world every label was read from.
- **seeded live** — the live platform plus the fault the scenario's own chaos plan planted.
- **unseeded live** — the live platform as it happens to be, with no fault planted. The
  read-only smoke pass is exactly this, by construction: `Scenario.in_smoke_pass` admits a
  scenario only when it seeds no chaos.

On 2026-09-17 the Phase 2 close bought a read-only live pass (`make eval-smoke`, archive
`0db6fe722f7c`, $2.15, 27 scenarios) and the grader applied the canned labels to the unseeded
live world. `postgres_slow`'s label is `db_query_latency`, read off a canned 420 ms ping; the
live database answered in 1.6 ms with one connection; the agent said `no_fault`, which is the
correct answer about the world it was in, and was graded wrong. Seven of the 27 rows failed,
every one of them on `ROOT_CAUSE` alone, and the pass reported a live root-cause accuracy of
"11/18 (61%)" that is a statement about nothing. It is recorded as INC-003 — a wrong verdict
about the agent, which is the class of failure `context/INCIDENTS.md` exists to catch.

Nothing about the labels was wrong. Nothing about the agent was wrong. What was missing is
that a label never said which world it was true of, so nothing could notice when the run was
in another one.

## Decision

**An expected value carries the world it is true of, and a grader that cannot establish that
world does not grade — it reports that it did not.**

Concretely, for `ROOT_CAUSE`:

1. `evals/graders/root_cause.py::label_describes_this_world(live_mcp, chaos_seeded)` is the
   one place the rule lives. A canned run is in the label's world by definition. A live run
   whose scenario seeded its own fault is in the label's world. A live run that seeded nothing
   is not.
2. The fact is **passed into `grade()`**, beside the label it qualifies
   (`world_matches_ground_truth`), exactly as `ground_truth` itself is passed. `grade()` stays
   a pure function of its arguments: it has no idea whether a platform was live, and it must
   not acquire one, because the offline re-grade of a locked archive reads that fact out of the
   archive instead (`ScenarioOutcome.live_mcp` + `chaos_hooks`).
3. Where the label does not apply, the dimension is **vacuous, not green and not red**: it
   passes with the detail `not graded: the label describes a world this run did not have — …`,
   which `is_vacuous_detail` recognises. So the row leaves the accuracy denominator, the
   regression gate keeps its vacated-assertion check over the dimension, and the run summary
   reports graded and not-graded counts as two numbers.
4. The rule is about the **world**, never about whether the row would pass. Holding back the
   reds of a mismatched world and keeping the greens would buy the same invalid number back
   with a friendlier sign.

**A paid archive is re-graded, never re-written.** `scripts/regrade_archive.py`
(`make regrade-archive ARCHIVE=<id>`) re-grades an archived run from its own trajectories under
today's rules and writes a NEW versioned report (`evals/reports/regrades/`), sha256-verifying
that the archive is unchanged. The archive stays locked (ADR 0021, invariant 9), and the
correction is a document beside it rather than an edit to it.

## Consequences

**Good.** The first live root-cause number this project reports will be one whose labels were
about the world the run was in. `make eval-smoke` stops exiting non-zero on correct behaviour.
The offline suite is unaffected — canned *is* the label's world, so `make eval-reg` reports
41/41 and `root cause: 32/32` unchanged. And a locked archive can now be corrected without
being touched, which is the only way to correct one at all.

**Cost, and it is real.** The read-only pass measures less than it appeared to: 2 of its 27
rows carry a graded diagnosis (the canned ones), not 18. That is the honest number, and the
alternative was a dishonest one. A live root-cause accuracy needs scenarios that seed their own
fault — the remediation legs do, and a future read-only pass would need a seeded variant to say
anything about diagnosis.

**What this does not do.** It fixes one shape: the `ROOT_CAUSE` label. Other expectations on
the smoke path are also claims about the world's contents rather than about the agent's
conduct, and each needs its own judgement about what an unseeded live world guarantees:
`consumer_lag_missing_group`'s `lag is_null: true` (a group that does not exist canned, and
does exist live — the one row still failing after the re-grade),
`consumer_lag_null_unknown_state`'s same claim, `consumer_lag_healthy_zero`'s `lag equals 0`
and `consumer_lag_shipping_extreme`'s `lag at_least 50000`. They are reported in
`docs/eval-methodology.md` and left standing rather than loosened here, because loosening an
assertion to make a pass green is the other way to lose a measurement.

**The general rule, for the next expectation somebody writes.** Ask which world the value is
true of before asking whether it is true. Canned, seeded-live and unseeded-live are three
different worlds; a pass that swaps one for another may keep only the assertions that are about
the agent's conduct.

## Relation to 0038

ADR 0038 decided who may *see* the answer key and made that a type. This one decides where the
answer key *applies*. Neither changes the other: the projection is untouched, `ground_truth`
remains evaluator-only, and the world fact travels beside the label into the grader rather than
towards the agent.

Implemented by WO-R3-265.

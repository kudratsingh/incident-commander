---
status: accepted
date: 2026-09-15
supersedes: none
amends: none
---

# 38. The agent's view of a scenario is an allow-list projection

## Context

`Scenario` now carries `ground_truth` — what was actually wrong, as `HypothesisCategory`
values — and `discriminating_probes`, the reads that tell this fault apart from the ones it
resembles. Both exist for the root-cause grader and the strategy comparisons that follow it.
Both are answer keys. An agent that can read either is not being measured on diagnosis.

That is not a new class of requirement so much as a newly *load-bearing* one. The benchmark's
whole claim rests on a trust boundary between the world the agent reads and the record of what
made it that way, and until now the boundary existed only because nothing on `Scenario`
happened to be secret.

The obvious enforcement is an exclusion list: serialize the scenario, drop the secret keys.
It fails in the direction that matters. The next field added is included by default, the
mistake is an omission, and an omission does not announce itself in a diff — it announces
itself as a benchmark number that was never measuring what it claimed. `LESSONS` records two
hand-maintained exclusion lists in this repo going stale (the Makefile's smoke patterns, the
briefing trail's writer list), and `docs/architecture-principles.md` § 3 says lead with the
structural fix rather than the rule somebody has to remember.

There was also nothing that *named* the boundary. The runner reached into a `Scenario` for
each thing it handed the agent — the alert in one place, the tool-call cap in another, the
canned platform responses in a third — so the set of agent-visible fields was whatever those
call sites happened to read, discoverable only by grepping, and correct only until somebody
wrote a fourth call site.

## Decision

**Everything the agent sees is built from one allow-list projection, and the projection is a
type.**

1. `Scenario.agent_visible()` returns an `AgentVisibleScenario`: frozen, `extra="forbid"`,
   carrying exactly the alert, the tool-call cap, and the canned tool responses. Those three
   are the complete set of scenario-derived inputs to the agent under test.
2. `evals/runner.py` builds the run from that object and nothing else. It does not read
   `scenario.alert`, `scenario.expectation.max_tool_calls`, or
   `scenario.canned_tool_responses` directly any more.
3. `Scenario.AGENT_VISIBLE_FIELDS` and `EVALUATOR_ONLY_FIELDS` partition every field on the
   model, and the partition is checked **at import**. A field on neither side raises before a
   scenario can be loaded, so "nobody decided which side this is on" is not a state the schema
   can be in.
4. `tests/unit/test_ground_truth_never_leaks.py` renders all four agent-visible contexts —
   investigation planner, remediation planner, verification judge, briefing writer — for every
   scenario in the corpus, twice: as the scenario ships, and with a maximal ground truth
   attached. The two renderings must be byte-identical.

The consequence is the point: a field added to `Scenario` tomorrow is invisible to the agent
by construction. Making it visible takes a deliberate edit to `AgentVisibleScenario`, to the
projection, and to the declared partition — three places a reviewer will see, rather than one
place nobody thought about.

`max_tool_calls` is lifted out of the graded `ScenarioExpectation` and is the one number that
crosses. That is deliberate and narrow: the agent is *told* its budget (ADR 0019), and a
ceiling is a constraint on the run rather than a fact about the fault. `canned_llm_responses`
deliberately does not cross — those are the model's own scripted replies, an output of the
agent rather than an observation it reads.

Byte-identity is the primary assertion rather than a substring hunt for root-cause labels,
because a substring test cannot distinguish "the label leaked" from "the label was always in
the alert": `poison_message` is also a chaos tool name, and a scenario's alert legitimately
carries words that name categories. Identical output under an arbitrary ground truth says the
ground truth changed nothing, whatever it said, and cannot produce a false red. The substring
and per-label assertions are kept beside it because they name the leaked thing when something
does leak.

## Consequences

**Good.** The trust boundary is a type, an import-time check and a corpus-wide test instead of
a convention. The sweep is parameterised over the scenario directory, so a new scenario is
covered the moment it lands. A reviewer asking "can the agent see this?" reads one small class
rather than grepping the runner.

**Cost.** Two spellings of the same three fields to keep in step — `AgentVisibleScenario`'s
fields and `AGENT_VISIBLE_FIELDS` — which is itself pinned by a test asserting they are equal.
Widening the agent's view is now a three-file change rather than a one-line one. That friction
is the feature.

**Not decided here.** How the root-cause dimension is *graded* from this record. `grade()`
takes only `RunState` and `ScenarioExpectation`, and neither the `Scenario` nor the per-step
candidate set is reachable from it (divergence C4). WP-2.2 decides between changing that
signature and adding a second grader the runner calls beside it; this ADR only guarantees the
record exists, is typed, and cannot reach a prompt.

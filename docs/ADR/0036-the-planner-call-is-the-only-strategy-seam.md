# ADR 0036: The planner call is the only strategy seam — strategies propose, the loop decides

* Status: accepted
* Date: 2026-09-15
* Decider: Kudrat Singh

## Context

The research buildout (plan `docs/plans/research-buildout-v2.1/`, phases 5, 6, 9, 12 and 13) adds
inference-time reasoning policies: best-of-N enumerated and sampled, a candidate selector,
reflection, search, adaptive. Each of them is a different way of answering one question — *what
should this investigation do next?* — and each is a separate experiment whose result only means
something if the rest of the agent held still while it ran.

Two facts about this repo make that possible, and one makes it fragile.

The first fact: **there is exactly one LLM call in the investigation loop.**
`agent/investigation.py::_plan_next_step(run_state, at, llm_client, model) -> (RunState,
InvestigationStep)` is the whole of the agent's inference during INVESTIGATING. It is wrapped by
`call_with_output_repair` (ADR 0035) and accrued by `accrue_structured_call` (ADR 0015).

The second fact: **everything that decides whether an action is allowed sits around that call, not
inside it.** The `FIX_MAP` gate, the 0.7 remediate threshold, the alert-subject probe refusal (ADR
0032) and its two-refusal cap, the ADR-0009 freshness re-probe, `max_iterations`, hint routing, and
`_execute_probe`'s runtime tier re-check are all in `transition_llm_investigate` and its helpers.

The fragility: without a named seam, each of the six later packets would have to reach into the
loop, and the first one to move a gate "while it was in there" would silently change the thing
every other packet is measured against. Plan 04's working rule 5 — never change `baseline`
behaviour silently — is not enforceable by review alone across six packets and several sessions.

There is a second, quieter problem. Today a run's report cannot say which loop produced it. WP-0.3
(ADR 0013's amendment, cmd #223) added `RunProvenance` with `strategy` and `strategy_config`
fields, and filled the first with the literal `"builtin"` because there was nothing else to fill it
with. A number that cannot name the policy that produced it is not a benchmark row.

## Decision

**A strategy replaces exactly one call — the planner call — and nothing else.**

`agent/strategies/` holds the seam:

```python
class InvestigationStrategy(Protocol):
    name: str
    config: Mapping[str, Any]
    def plan_next_step(
        self, run_state: RunState, at: datetime, ctx: StrategyContext
    ) -> tuple[RunState, InvestigationStep, StepRecord]: ...
```

* `StrategyContext` is what a strategy is handed: `llm_client`, `model`, the loop's `iteration`,
  the strategy's own settings block, and the sink its records go to. That list is the whole of a
  strategy's reach — there is no MCP client in it, no tool registry, no run, and nothing that can
  act on the world. `tests/unit/test_strategies.py` pins the field set.
* `StepRecord` is the trace-side record of one planner step (plan 02 § 7): the candidate set, the
  selector decision, the ranking either side of the call, and what the call billed. It goes to the
  trace, **never** to `RunState` — the checkpoint stays a frozen `schema_version = 3` record of the
  latest ranking, which is all a resume needs.
* `BaselineStrategy` calls the existing `_plan_next_step` body verbatim and emits a one-candidate
  `StepRecord`. It is the current behaviour, and it is the control group.
* `StrategyRegistry` maps name → strategy and refuses anything else, listing the names it knows.
  `INFERENCE_STRATEGY` (bare env var, default `baseline`) is typed as the `StrategyName` enum, so
  an unknown value is refused when `Settings` is constructed, before a run starts and before
  anything is spent.
* The runner resolves `INFERENCE_STRATEGY` once and passes the strategy object to both the loop and
  the provenance record, so the name stamped on a row is the name of the thing that produced it.
  `"builtin"` is gone.

**The execution policy is shared and stays exactly where it is.** Plan 02 § 2's rule, stated as a
property of the import graph rather than an intention: no module under `agent/strategies/` may
import `tools.policies`, `tools.registry`, `tools.wire` or `tools.mcp_client`, may reference
`FIX_MAP`, `tier_of`, `TOOL_REGISTRY`, `alert_subject`, the threshold constants or the guard maps,
or may take anything from `agent/investigation.py` other than `_plan_next_step` itself.
`TestStrategiesHoldNoExecutionPolicy` scans the AST of every module in the package for exactly
that, and a companion test asserts each forbidden name still exists in `investigation.py` or
`tools/policies.py` so a rename cannot turn the scan into a list of strings that match nothing.

## Decision drivers

* **`baseline` has to be an honest control.** The canned suite must come out byte-identical, which
  is the proof that nothing moved. That is why the control group *calls* the existing body instead
  of holding a copy of it: a copy is a second thing to keep in step, and the first drift between
  them would be invisible in every offline run.
* **The gates are the safety architecture.** Every one of them is an ADR that a live run paid for:
  0025, 0027, 0028, 0030, 0031, 0032, 0033. A strategy that could reach one could undo one.
* **Later packets should be small.** Each new strategy is one file, one enum member and one
  registry line. Nothing in the loop changes again.
* **A reported number must name its policy.** The seam and the provenance stamp are one change for
  this reason; landing the seam without the stamp would leave every future row un-attributable.

## Considered options

**1. Chosen: a strategy object behind the planner call, resolved from config at the runner.**

**2. A subclass of the investigation transition.** Rejected. It puts the gates inside the
inheritance chain, so a strategy that overrode one more method than it meant to would change the
execution policy and still pass every test that does not run that path.

**3. A callable passed as `planner=` with no protocol, record or registry.** Rejected — it is the
same indirection with none of the parts that make the experiment legible: nothing names the
strategy in the artifact, nothing refuses an unknown name, and nothing records what was considered
versus what was emitted, which is the measurement Phases 5 and 6 exist to make.

**4. Wait until the first real strategy needs it (Phase 5).** Rejected, and this is the heart of
the packet. Introducing the seam while the only implementation *is* the current behaviour is what
makes it provable: the canned suite is byte-identical today, so the seam is known to be
behaviour-free before any strategy uses it. Introduced alongside best-of-N, the same PR would carry
both a refactor and a behaviour change, and a report moving would have two candidate causes.

## Consequences

**Positive.**

* Acceptance is falsifiable in one line: delete the indirection and the canned report is unchanged;
  change the strategy and it is not.
* `make_llm_investigate` gains one parameter and the loop one call site. The gates, the re-probe,
  the iteration cap and `_execute_probe` are untouched.
* Every run artifact names its strategy and its config, from this packet onward.
* The one-candidate `StepRecord` gives WP-2.1 a record shape that already exists for the control
  group, so "baseline versus best-of-8" is a comparison between two populations of the same record.

**Negative, and accepted.**

* `strategies/baseline.py` imports a private name (`_plan_next_step`) from `investigation.py`, and
  `investigation.py` imports the registry inside a function to keep that from being a cycle. Both
  are the price of leaving the planner body in the module that owns the loop, in the one packet
  whose acceptance is that nothing moved. The alternative — move the body into the strategy — is
  available later, after the seam has been exercised by a second strategy.
* `StepRecord` is produced and returned, but nothing writes it to a trace yet. `TraceKind` is a
  closed enum whose every member must have a human-report formatter in the same PR
  (`tests/unit/test_format_traces.py::TestEveryKindRenders`), and the tracer is opt-in — the
  `make eval` target behind `make eval-reg` never builds one. The `step` kind, its formatter and
  the runner wiring belong to WP-2.1, the packet that reads these records. Until then the sink is
  an unused seam, which is visible in the code rather than implied.
* `LLMCallRecord` carries the budget ledger's delta for the step rather than the four-way token
  split, because `_plan_next_step`'s return type is pinned verbatim by plan 02 § 4 and this packet
  does not widen it. `elapsed_ms` and `planner_input_tokens` are `None` for the same reason: named
  so WP-2.1 fills a field instead of adding one, and absent rather than fabricated.
* `StrategyName` has one member. Plan 02 § 4 lists seven; each arrives with its implementation, on
  the same reasoning `ExecutionMode` uses for withholding `recorded` until something can produce
  one.

## Relationship to other decisions

* **ADR 0002** (hand-rolled state machine) is unchanged and is why this works: the topology is
  static, the intelligence lives inside a state, and a strategy is that intelligence — not a node,
  not an edge.
* **ADR 0035** and **ADR 0015** are preserved by construction: `baseline` calls the wrapped,
  accrued body. A strategy that bypassed either would make every budget number a lower bound, so
  the first test any new strategy owes is that its ledger moved.
* **ADR 0013** (run provenance) gets its placeholder filled: `strategy` and `strategy_config` now
  come from the object that ran.
* **Plan 02 § 6** reserves the number 0036 for retry-with-reinvestigation (WP-10). That packet is
  in Phase 10 and this one is in Phase 0; the number is taken here, and WP-10's ADR takes the next
  free one when it lands.

## Revisit trigger

The second strategy. If `best_of_n_enumerated` (WP-5.x) cannot be written without reaching past
`StrategyContext` — for a probe result, a tool schema, or a gate — this seam is in the wrong place,
and the right response is to move the seam deliberately in that packet, with the canned suite
re-proved byte-identical, rather than to widen the context field by field.

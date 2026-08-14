---
status: accepted
date: 2026-08-13
supersedes: none
---

# 19. A scenario's `max_tool_calls` is the run's runtime ceiling, not only its grading cap

## Context

`ScenarioExpectation.max_tool_calls` was, by explicit decision, a **grading** cap only.
`docs/eval-methodology.md` said so, and every remediation scenario repeated it in a YAML
comment: *"Grading cap only — the runtime `BudgetLedger` ceiling (invariant 7) is untouched."*

The consequence was two numbers for one thing. `evals/runner.py` called
`start_run(alert, settings, now)`, which seeds the ledger from
`settings.budget_max_tool_calls` — the fleet default of 25 — so a scenario declaring a cap of
5 ran with a ceiling of 25 and was told off afterwards for using 6. The cap could only ever
be discovered by the agent as a post-hoc grade, never respected as a budget.

Two things followed from that, and the second is worse than the first.

**The margin rule was an annotation.** `docs/eval-methodology.md` sets remediation caps at 13
— a 10-call correct live path plus a ≥30% margin. That margin is meant to be headroom inside
a real ceiling. With no ceiling in force it described nothing: a run could spend 25 calls and
the margin never entered the world.

**The agent was told the wrong number, in every scenario.** `investigation.py`'s
`_format_planner_context` renders

```
Budget remaining: tool_calls={max_tool_calls - tool_calls_used}, tokens=...
```

from the same ledger. So the investigation planner — the component that decides how many
probes it can afford before it must stop and escalate — read `tool_calls=25` in every
scenario in the suite, including `consumer_lag_high`, whose entire subject is what the agent
does when it cannot afford a remediate-plus-verify cycle. That scenario's premise was
communicated to the grader and withheld from the agent.

## Decision

`start_run` takes a keyword-only `max_tool_calls` override, and `evals/runner.py` passes
`scenario.expectation.max_tool_calls`. The declared cap becomes the run's `BudgetLedger`
ceiling, enforced at every loop step by the existing invariant-7 machinery, and reported to
the planner by the existing context renderer. One number.

Ingress (`api/app.py`) does not pass it. A production incident is bounded by configuration,
not by its caller.

### A cap of 0 is ignored

Nine scenarios declare `max_tool_calls: 0` — the five `noise_*` filters, the three tool-error
paths, and `planner_stops_immediately`. `BudgetLedger.is_exhausted` is `used >= max`, so a
zero ledger is **born exhausted**: `run_to_completion` escalates before `TRIAGE` ever
classifies the alert, and the run ends before doing the thing the scenario exists to observe.
`tool_missing_response` would never attempt the probe whose failure it is named after.

A cap of 0 is therefore not a runtime ceiling this ledger can express. It is a claim about the
outcome — *a correct run makes no tool call* — and the BUDGET dimension still grades it
post-hoc, unchanged. `start_run` falls back to the setting for that case.

### Reaching the cap is now a BUDGET failure

Wiring the ceiling makes `used > cap` unreachable through the runner: the loop stops at
`used >= max`, so the ledger can reach the cap but never pass it. Grading only the
strict-greater case would have left a dimension that cannot fail — the vacuous assertion this
suite refuses everywhere else, and one we would have created while claiming to strengthen the
budget story.

So `_grade_budget` fails at `used == cap` as well. This is what the cap already meant: *a
correct run finishes inside this budget*, which is the whole content of the ≥30% margin rule.
A run that spent its last allowed call was cut off, not finished. The strict-greater branch
stays for runs graded outside the runner, where no ceiling was derived from the cap; the
`cap == 0` case is excluded, since spending all of a zero allowance is the correct outcome
there.

## Consequences

**A budget overrun now escalates instead of being graded.** That is invariant 7 working —
"exhausting a budget triggers escalation with a briefing, never silent continuation" — applied
to the eval suite for the first time. A scenario whose live path outgrows its cap now surfaces
as an OUTCOME failure (escalated where RESOLVED was expected) or a BUDGET failure at the
boundary, rather than as a scenario that quietly spent 25 calls.

**Scenario caps are now load-bearing and must be set deliberately.** A cap set carelessly
low no longer produces a red grade on an otherwise-correct run; it produces a *different run*.
`tests/unit/test_scenario_loader.py` already enforces the remediation-class floor of 13, which
is what keeps this safe.

**A cap has to admit the polling profile.** The 13 comes from 2 investigation probes + 1
ADR-0009 freshness re-probe + 1 Tier-1 action + up to 6 ADR-0006 verify polls = 10, plus
margin. With the ceiling in force, a cap below that does not fail the run at grading time —
it truncates the verify loop. `tests/unit/test_phase6_hardening.py` holds the arithmetic.

**No baseline re-bless.** No shipped scenario comes within its cap offline (canned runs spend
1–3 calls against caps of 5–13), so no grade moves and the regression gate is unaffected.

## Alternatives considered

**A separate `runtime_max_tool_calls` field.** Keeps `max_tool_calls` purely for grading and
adds a second knob. Rejected: two numbers for one concept is the defect, and nothing would
have kept them consistent — the drift that produced this ADR would simply have moved fields.

**Floor the override at 1 so cap-0 scenarios survive.** A ledger of 1 is not exhausted at
`used == 0`, so TRIAGE runs — but the pre-probe check `if run_state.budget.is_exhausted` also
passes at 0, so the probe fires anyway. The floor buys nothing and misrepresents the budget.

**Leave `_grade_budget` alone.** Cheapest, and the reason it was rejected is the point of this
whole document: it would have left a graded dimension that no run can fail, while the PR
description claimed the budget was now real.

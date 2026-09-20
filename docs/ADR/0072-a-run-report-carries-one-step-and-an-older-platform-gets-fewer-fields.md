# ADR 0072 — a run report carries one step, and an older platform gets fewer fields

Status: accepted (2026-09-20, WO-R3-329). **Amends [ADR 0068](0068-the-agent-reports-its-run-and-reporting-is-never-a-tool-call.md)**
decision 1 (what a report contains and how often one is sent). Everything else in ADR 0068 —
fail-open, never a tool call, off unless asked for, the model never chooses, the derived run
id — stands unchanged.

## Context

The owner recorded the first live take with the reporter of ADR 0068 and it was unwatchable.
The console's agent panel was empty for the whole run, and the reason was not a frontend bug:
`report_agent_run` carried a state, a top hypothesis and a tool NAME, and nothing else. So
nothing about the ranking, the alternatives, the plan, the verify verdicts, the budget, or
what any read returned reached the platform at all. The audit log could not fill the gap
either — an `agent.tool_invoked` row carries the tool, the arguments and the latency but *not
the result*, so "what did the agent see" existed nowhere a human could read.

Platform WO-R3-328 widens `report_agent_run` to take all of it: `hypotheses`, `plan`,
`verification`, `budget`, and a `step` the platform APPENDS to a per-run list — **one step per
call**. That last detail is the constraint this ADR is mostly about.

Two facts about this repo shape the rest. First, the evidence ledger (`RunState.evidence`) is
not a call log: it holds a parsed summary per SUCCESSFUL call, with no timing, no outcome and
no entry at all for a call the platform refused. Second, the merge order is
328 → release v0.6.16 → re-pin → 330 → 329, so this reporter runs against a platform that
does *not* know the new fields until the coordinator re-pins.

## Decision

**1. One report per step; the run's own state travels on the last of them.**
`ReportingCheckpointer` still fires once per checkpoint, but a checkpoint can cover several
calls (a verify leg polls), and the platform appends one step per call. So the seam sends one
report per pending item, in order, with a monotonic `seq`. Reports before the last one carry
the state the run was **in** while it made those calls, not the state the transition ended on.
That is the honest stamp — a verify poll happens while the run is `verifying` — and it is also
what keeps a terminal transition's backlog reportable: a terminal state CLOSES the run, and
every later report is refused with `agent_run_already_finished`.

**2. The steps come from the client seam; the ledger decides their order.**
`ToolCallLog` is an `MCPClient` tracer hook, composed beside the JSONL tracer rather than
replacing it. It is the only place a call's latency, its outcome (`ok` / `refused` /
`error: …`) and the raw text of its first content block exist. The ledger is still what orders
them and is still the source of the verdicts; a call the ledger never recorded is reported
after that order rather than dropped, because it is a call the agent made. Three exclusions
are structural: the reporter's own two tools are never observed (each report would otherwise
arrive as the next report's step, forever), the run's precondition probes are forgotten before
the run starts (they are the EVALUATOR's reads — ADR 0038), and the log is built only when
`AGENT_RUN_REPORTING` is on, so a graded run's tracer is byte-identical to before.

**3. Excerpts, not outputs, and every cap is honoured on this side.** 280 characters for a
reasoning or a rationale, 400 for a reading, 128 for a name or a tool, 64 for a category or a
verdict — truncated by the reporter with the cut made visible. The platform **refuses** a
field over its cap as invalid params rather than trimming it (plat #230), and a refusal is not
a small loss here: it costs the whole report, and this module would read it as an older
platform and narrow for the rest of the run. `Hypothesis.name` is free-form model output, so
that path is real rather than theoretical. The platform stores what it is sent verbatim, so
the bound belongs on this side. ADR 0012 is untouched either way: there is still no read tool
for `agent_runs`, and an excerpt the agent cannot read back tells it nothing.

The list's ORDER is the ranking — best first, stored as sent, never re-sorted by the platform.
`hypotheses`, `plan`, `verification` and `budget` are never cleared by omission, so a report
with nothing new to say cannot erase what an earlier one supplied; `current_hypothesis` and
`last_step` DO clear on omission, so both are sent on every report.

**4. The verdicts come from the ledger's markers, not from the judge alone.** A
`_verify_judge` entry is one poll's verdict with its `{attempt, of}`; a
`_remediation_attempt_failed` entry is an ATTEMPT's closing verdict, and the only place
`verified_stabilizer` and `verified_unresolved` exist (ADR 0026, ADR 0056: the judge says
`verified` and the run then decides the incident is not over). A console fed only the judge
would show a run that verified and then escalated, with nothing in between. The marker name is
now a constant in `planner_context.py` beside the other two, because it has three readers.

**5. The plan is reported once per distinct plan**, read from `RunState.remediation_plan`
rather than from the `_planner_plan` marker, which carries only the target hypothesis. A
second attempt's different plan is reported again (ADR 0056), because the console must show
the plan the run is on and not the one it abandoned.

**6. An older platform gets the fields it declares, once.** The widened fields are additive
and optional, and `report_agent_run`'s input model forbids unknown ones — so against v0.6.15
a widened report is refused WHOLE, state included. The first refusal that could plausibly be
about the arguments narrows the payload to the fields v0.6.15 declares (`NARROW_FIELDS`),
logs that once, and resends. Every later report is built narrow, so the cost is one refusal
per run and not one per call.

*Which refusals narrow* is the part that was measured rather than assumed. The 2026-09-20
rehearsal on v0.6.15 answered a widened report with a **JSON-RPC error**
(`MCP error -32602: invalid tool arguments`), not with the 200-carrying-`isError` the refusal
path was reading — so the first version of this fallback never fired and the whole rehearsal
reported nothing. Both routes are now read, and only two things narrow: JSON-RPC
`-32602` (invalid params), and a tool-level refusal that names none of the platform's
run-level codes. An HTTP 403 does **not** narrow — a token minted before `agent_runs:write`
existed is a re-mint, and hiding it behind a thinner console is the wrong repair.

**7. The payload is validated locally before it is sent, and the mirror that validates it is
pinned against the snapshot.** A private Pydantic mirror of the tool's input schema gates every
report; a payload that fails it is logged and the narrow form is sent instead. The mirror is a
gate and not a serializer — the dict this module assembled is what goes on the wire — and its
schema reaches no prompt, no tool contract and no OpenAPI document.

It is kept rather than dropped once v0.6.16's real schema landed in the snapshot, for one
reason: **fail-open makes a refusal silent.** The console simply goes thin, and on a recording
nobody notices until afterwards, so a payload bug has to surface at the seam. What the mirror
may not be is a second hand-maintained copy of a generated contract, so
`TestTheMirrorMatchesTheContract` compares the two field by field — names, `maxLength`, `enum`,
numeric bounds, and required-ness in the one safe direction (the mirror may be stricter, never
laxer). That test earned itself immediately: it found `step.outcome` capped at 64 where this
module had written 128, which an `error: MCPError…` outcome exceeds — a refusal that would have
narrowed every later report of that run.

## Consequences

A run of the demo scenario sends roughly one report per transition plus one per tool call
instead of one per transition: ~12 calls where there were 6. All of them are off the budget
and off the evidence ledger (ADR 0068 decision 3 is untouched and re-pinned by test), and each
still writes `agent.run_reported` audit rows the agent cannot see — so the agent's own
`list_audit_events` totals drop a little further, in the direction the runbook's bump walk
already warns about.

The console can now show a whole run — ranking, plan, verdicts, budget, and an action ledger
with what each call returned — without reading anything on the operator's laptop. Until the
re-pin it shows what it showed before and says why in the log.

The fallback becomes dead code the moment the commander is pinned to v0.6.16. It stays: the
two repos are versioned independently, a demo stack can be older than the commander that
talks to it, and the failure it prevents is the one that has now happened once.

## Alternatives considered

- **Enrich the steps from the evidence ledger alone.** Rejected: the ledger has no latency, no
  outcome, and no entry for a refused call — and "the agent tried and the platform said no" is
  exactly what a person watching needs to see.
- **Wrap the MCP client in a decorator for the transitions.** Rejected: the client is wired
  into four transitions and a branch prober, the tracer hook already sees every call, and a
  second observation seam is a second thing that can disagree with the trace.
- **Send the whole tool output instead of an excerpt.** Rejected: the platform stores it
  verbatim and interprets nothing, a DLQ listing is kilobytes per row, and a console panel
  shows a sentence. An excerpt with a visible cut is honest about being one.
- **Batch several steps into one report.** Rejected: WO-R3-328's seam appends one step per
  call, which is what makes the list append-only and the order recoverable.
- **Refuse to report at all against an older platform.** Rejected: the state IS the thing a
  watching operator cannot do without, and the fields it cannot have are the ones added last.

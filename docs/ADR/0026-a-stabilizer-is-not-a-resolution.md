# ADR 0026: A stabilizer is not a resolution

* Status: accepted
* Date: 2026-09-07
* Decider: Kudrat Singh

## Context and problem statement

The remediation loop has exactly one transition to `RESOLVED`, in `agent/remediation.py`'s `transition_verify`, under exactly one condition:

```python
if judgment.verdict == "verified":
    return run_state.with_state(IncidentState.RESOLVED, at_attempt)
```

The verification judge is asked one question: *did the action do what the plan expected?* Until this ADR, the state machine treated a `yes` to that question as a `yes` to a different one: *is the incident over?*

For six of the seven Tier-1 tools the two questions have the same answer. For `pause_dag` they come apart, and the platform's own tool description is what pulls them apart:

> This is the verification surface for pause_dag — a successful pause reads as `paused=true` with children still in `waiting`.

So the reading a *working* pause produces is `paused: true`, children `waiting`. Hand that to a judge holding the expectation "children should stop advancing" and the honest verdict is `verified`. The run then reported **RESOLVED on a chain that was exactly as stuck as before**, and that would be stuck again the moment the pause's 10-minute TTL lapsed and the held children promoted back behind the same dead-lettered root.

There is no judge prompt that fixes this. The judge is not wrong. The question was.

### How it was reachable

Found on 2026-09-07 by a read-only pre-spend sweep of `remediate_runaway_saga_success`, before that scenario's first paid run — so this is a defect caught by reading rather than by a red result, and nothing had ever executed a `pause_dag` plan live.

Every layer pointed the agent at the pause:

* **The planner prompt.** `runaway_saga` / `stuck_dag` → `pause_dag`.
* **`FIX_MAP`.** `HypothesisCategory.RUNAWAY_SAGA: "pause_dag"`.
* **The pinned tool descriptions.** `get_dag_state` calls itself "the verification surface for pause_dag"; `replay_dlq_by_ids` never mentions DAG roots. The reciprocal statement — that replaying the root is what genuinely unsticks the chain — exists only in `create_stuck_dag`'s description, a chaos tool that is not in `TOOL_REGISTRY` and that the agent therefore never sees.
* **Every plan guard admitted it.** Right tier, real tools, resource named on both legs and evidence-sourced, and `VERIFY_PROBE_FOR_ACTION` maps `pause_dag` → `get_dag_state.job_id`, which is correct: `get_dag_state` genuinely observes what a pause changes. Observing the action was never the problem.

Meanwhile PR #173 had redesigned the scenario around `replay_dlq_by_ids` on the dead-lettered root and put `pause_dag` into its `forbidden_action_tools` — because the platform **refuses to replay any job inside a paused DAG** (`find_blocking_pause`, `backend/app/utils/dag_pause.py`, called from `JobService.replay_job`). A pause does not merely fail to fix the chain; while it holds, it breaks the fix.

That drift was invisible for the whole period. `FIX_MAP`'s values are never read at runtime — only `top.category not in FIX_MAP` gates the handoff — and offline eval replays canned planner output and never loads the prompt at all. 38/38 stayed green throughout.

### The class, stated generally

An action whose entire effect is to hold a system still can be verified perfectly and has resolved nothing. Verification asks whether the action worked. Nothing was asking what a working action is *worth*.

## Decision drivers

* **Tier and worth are different questions.** Tier says how much damage an action can do. It says nothing about whether success ends an incident, and the codebase had no place to say that at all.
* **"Fix that fixes nothing" is worse than a failed fix.** A failed remediation escalates and a human looks. A stabilizer reported as a resolution closes the incident, and the on-call learns about it when the TTL lapses.
* **The judge cannot be the fix (principle 3).** Re-prompting the judge to "consider whether the incident is really over" asks a per-run LLM judgement for a per-tool fact that is knowable at design time.
* **Silence must not mean "resolves".** The `pause_dag` case existed because nobody had written down that a stabilizer is different, so it inherited the default every other tool had. That is exactly the shape ADR 0003 refused for tiering.
* **A stabilizer is not a lesser action.** Buying a human time is legitimate and sometimes the only safe move. The requirement is that it be reported as what it is.

## Considered options

1. Fix the prompt and `FIX_MAP` so the planner never picks `pause_dag` for a stuck chain; leave the loop alone.
2. Forbid `pause_dag` outright — drop it from `_TIER_1_TOOLS`.
3. Strengthen the verification judge's prompt to ask whether the incident is genuinely over.
4. Classify every Tier-1 action as resolve-or-stabilize, and make a verified stabilizer escalate instead of resolving (chosen).

## Decision outcome

**Option 4**, with option 1's prompt and `FIX_MAP` corrections alongside it as the steering half.

`policies.RESOLUTION_CLASS` maps every Tier-1 tool to a `ResolutionPolicy(resolution, rationale)`. `Resolution.STABILIZES` says a verified success holds the incident still and leaves its cause in place; `Resolution.RESOLVES` says it removes the cause. `transition_verify` consults it at the one `RESOLVED` transition: a verified stabilizer escalates, with the tool's own rationale quoted into the escalation reason and the acted-on resource named.

| Tier-1 action | Class |
|---|---|
| `pause_dag` | **STABILIZES** |
| `restart_consumer_group` | RESOLVES |
| `invalidate_cache_key` | RESOLVES |
| `replay_dlq_by_ids` | RESOLVES |
| `replay_dlq_by_category` | RESOLVES |
| `replay_dlq_messages` | RESOLVES |
| `mark_dlq_permanent` | RESOLVES *(under review — see below)* |

Four properties carry the design.

**It runs after execution, not at plan time.** A stabilizer is a legitimate plan: it executes, its verify leg runs and is judged, and only then does the class decide the terminal state. Refusing it at plan time would remove a real capability — sometimes stopping promotion while a human decides is the right call — and would also throw away the evidence that the stabilization actually landed, which is the most useful thing the briefing can carry.

**The map is total over the Tier-1 slice, and `resolution_class_of` raises rather than defaulting.** `tests/unit/test_policies.py::TestResolutionClass` fails on any Tier-1 tool with no entry. A `PolicyCoverageError` at the enforcement point escalates rather than crashing the run — fail closed toward the human, because the wrong way to resolve a missing safety decision is to resolve the incident.

**The rationale is load-bearing text, not a comment.** `_stabilized_reason` quotes it verbatim into the escalation reason, which `agent/briefing.py` reads into `EscalationBriefing.escalation_reason`. For a pause, the on-call is told that the action worked, that the chain is unchanged, that the pause self-expires on a TTL, and that it blocks the replay while it holds. The escalation also carries `attempted_tool`, so the briefing writer knows a pause is holding and — per its own prompt rule — never recommends repeating it.

**The escalation is not a failure.** The reason opens `STABILIZED, NOT RESOLVED` and says so explicitly. The agent did the right thing and is handing over deliberately; a reader who cannot tell that apart from a botched remediation will discount both.

### The steering half

`FIX_MAP[RUNAWAY_SAGA]` is now `replay_dlq_by_ids`, and the planner prompt carries a *Stuck dependency chains* section: a `dead_letter` root with `waiting` descendants and `paused: false` routes to an immediate `replay_dlq_by_ids` on the root's own id, verified with `get_dag_state` on that same id. It says the DAG-root case does not require a `list_dlq_messages` reading — the correct trajectory never lists the DLQ, and the hint-routing table it would otherwise fall under opens with "when the evidence includes `list_dlq_messages` output". It counters the two pinned descriptions by name, because until the platform ships new ones (filed separately) the agent is handed text that steers it wrong. And it states that `pause_dag` never resolves an incident, which is the prompt-side statement of the rule above.

`tests/unit/test_policies.py::TestFixMapMatchesTheSuite` is the check that would have caught the original drift: **the tool a hypothesis category is steered toward may not be a tool that category's own scenarios forbid.** Scoped to scenarios expecting `resolved`, because escalate-only scenarios forbid every Tier-1 tool on purpose. Categories whose specific tool comes from the platform's per-row `remediation_hint` rather than from the map's value are declared in `investigation.HINT_ROUTED_CATEGORIES` — a distinction that previously lived only in a comment, which is how it stayed accurate while the value beside it did not.

### Why the alternatives lose

**Prompt and `FIX_MAP` only.** Principle 3's exact case. It fixes the one incident family somebody noticed and leaves the class open: the loop would still resolve on any future stabilizer, and the *reason* it resolved — that nothing distinguishes holding from fixing — would be undocumented. It is also the weaker half in practice, since a prompt is documentation and the loop is enforcement.

**Forbid `pause_dag`.** It is a real capability with a legitimate use — halting promotion while a human decides is sometimes the only safe move on a chain nobody understands yet — and the platform ships it as a compensator. Removing it would trade a reporting bug for a capability gap, and would leave the class unstated for the next stabilizer.

**Strengthen the judge prompt.** Asks an LLM, per run, to re-derive a fact that is fixed per tool and knowable at design time; and it asks the judge to answer a question it was not given the context for. It would also make the guarantee non-deterministic, which is the property this loop least wants at its only `RESOLVED` edge.

### Consequences

Positive:

* The "fix that fixes nothing" class is closed for every scenario at once, not just the saga family, and closed at the state machine rather than in prose.
* A stabilizer now produces the most useful artifact it can: an escalation that says the chain was stabilized, names the resource still needing a decision, and records that the pause is on a clock.
* The classification is a written decision per tool. A tool shipped tomorrow cannot inherit "of course it resolves" by silence.
* `FIX_MAP`'s values became checkable against the scenario corpus, closing a drift that was invisible to 38/38 offline runs.

Negative:

* **A run that legitimately only needed stabilizing now ends ESCALATED and grades as an escalation.** That is the intent, but it means any future scenario whose correct outcome is "pause and hand over" must expect `escalated`, not `resolved`. No scenario uses `pause_dag` as its action today, so nothing changed colour.
* **`mark_dlq_permanent` is classified RESOLVES and the classification is arguable.** The platform says the mark "doesn't change job.status — the entry stays in DLQ", the planner prompt routes it as "mark, then `stop` (escalate)", and the scenario exercising it is named `dlq_human_required_escalates` — yet that scenario asserts `expected_terminal_state: resolved`. Reclassifying it would turn a green scenario red, and that scenario is queued for a paid live run. The contradiction is real and is recorded at the map entry rather than settled quietly here; flipping the entry and the scenario is one decision, taken together.
* **One more map to keep true.** Mitigated the same way as `VERIFY_PROBE_FOR_ACTION` and `RESOURCE_ARG_FIELDS`: a totality test that names the missing decision.

Revisit trigger: a Tier-1 action that resolves some incidents and stabilizes others — at which point the class belongs on the plan, derived from evidence, rather than on the tool. Nothing on the current surface behaves that way.

## More information

Implemented in `src/incident_commander/tools/policies.py` (`Resolution`, `ResolutionPolicy`, `RESOLUTION_CLASS`, `resolution_class_of`, `stabilize_only_tools`) and `src/incident_commander/agent/remediation.py` (`_stabilized_reason`, the guard at the `RESOLVED` transition). Steering in `src/incident_commander/llm/prompts/remediation_planner.md` and `investigation.FIX_MAP` / `investigation.HINT_ROUTED_CATEGORIES`.

Tests: `tests/unit/test_remediation.py::TestStabilizeOnlyActionsNeverResolve`, `tests/unit/test_policies.py::TestResolutionClass`, `tests/unit/test_policies.py::TestFixMapMatchesTheSuite`, `tests/unit/test_prompts_snapshot.py::TestRemediationPlannerInvariants`.

Related: [ADR 0025](0025-a-verify-leg-must-observe-the-action.md) — that ADR made the verify leg observe the action; this one says that observing it is not the same as being done. [ADR 0024](0024-plan-arguments-name-their-resource.md), [ADR 0008](0008-single-attempt-remediation.md) (one attempt, so the terminal state that attempt produces is the whole answer), [ADR 0003](0003-platform-enforced-tier-policy.md) (the "no default classification" posture this map copies), [docs/safety-model.md](../safety-model.md#a-stabilizer-is-not-a-resolution), [docs/lessons/live-eval-sequence-2026-09.md §8](../lessons/live-eval-sequence-2026-09.md).

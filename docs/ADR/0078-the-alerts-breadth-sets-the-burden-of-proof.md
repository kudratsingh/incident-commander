# ADR 0078: The alert's breadth sets the burden of proof, and the loop holds the verdict to it

* Status: accepted
* Date: 2026-10-10
* Decider: Kudrat Singh (owner rulings O-44 parts 1 and 2, WO-R3-363, INC-006)
* Related: [ADR 0041](0041-read-the-whole-queue-before-you-replay-part-of-it.md) (the whole-queue
  read, which this record extends from "before a dead-letter action" to "before any verdict about
  a stuck chain"), [ADR 0053](0053-family-c-is-one-chain-under-four-faults.md) (the
  `workflow_stuck` family), [ADR 0066](0066-the-api-latency-family-is-one-page-the-platform-disagrees-with.md)
  (the `api_latency` family), [ADR 0074](0074-once-the-ranking-is-settled-the-planner-acts-or-hands-off.md)
  (the narrowed-schema shape this record mirrors in the other direction),
  [ADR 0073](0073-a-confirming-read-is-bounded-by-the-loop.md) and
  [ADR 0077](0077-resolved-needs-a-reading-taken-after-the-action.md) (the same "a structural rule
  in the loop, not a prompt hint" pattern), [ADR 0036](0036-the-planner-call-is-the-only-strategy-seam.md)
  (strategies propose, the loop decides), INC-006 in `audit-ws/context/INCIDENTS.md`

## Context

On 2026-10-08 seven live runs scored outcome, action and safety 7/7 and EVIDENCE 0/7 (INC-006):

* `api_latency_healthy_control` (`a0354a3f9cba`) read the consumer lag, the SLO and the outbox,
  ranked `no_fault` at 0.85 and stopped. It never read Postgres, Redis or the circuit breakers,
  which are the three places a latency page can come from in this family.
* `workflow_stuck_paused_dag` ×3 (`6e3abb9954d6`, `eb25a58feb70`, `03f50e776217`) read the chain
  once, diagnosed `dag_paused` correctly and stopped without listing the dead-letter queue.
* `workflow_stuck_resolver_stall` ×3 (`e7fd45fb8a7a`, `86e3b8006caf`, `4e729b6803a2`) read the
  chain three times, never listed the queue, and ran out of steps. The third had the right
  diagnosis; the first two did not.

Two readings were possible: the grader asks for too much, or the agent proved too little. The owner
ruled the second for both families (O-44): "for api latency it should check all areas where it can
come from, not just 1", and a stuck-chain verdict needs the whole queue — "that's obvious fault for
this". Then, binding on how the rule is scoped: "it's all dependent on the scenario — if the
scenario is asking for something broad, the agent must check all routes; if it's a narrower
scenario then only check what's necessary."

## Decision

**The alert's breadth sets the burden of proof; the scenario declares it; the loop enforces what
is declared and nothing more.**

### 1. The declaration

`Scenario.required_before_verdict` is a list of `{tool, when}` (`agent/required_reads.py`):
`tool` is a read tool (`ReadToolName`), `when` one of

* `no_fault` — the verdict's top hypothesis is `no_fault`;
* `stuck_chain` — the alert names a dependency chain (its subject is read by `get_dag_state`),
  whatever the verdict says, escalation included;
* `any` — every verdict.

An omitted field takes the family default in `evals/scenarios/schema.py::FAMILY_REQUIRED_BEFORE_VERDICT`;
`[]` declares none. Two families have a default, because they are the two whose page names no
single cause:

| Family | Default | Why |
|---|---|---|
| `api_latency` | `get_slo_status`, `get_postgres_health`, `get_redis_health`, `get_circuit_breakers`, each `when: no_fault` | The page names an objective, not a resource. "Nothing is wrong" must have read the objective and every shared dependency that moves in a sibling world (`README-api-latency.md`) |
| `workflow_stuck` | `list_dlq_messages`, `when: stuck_chain` | One chain under five faults, so the page could be any of them. Any conclusion about it lists the whole queue first (ADR 0041's unfiltered read; a filtered listing does not count, paging does) |

Every other scenario's alert names its subject — a consumer group, a cache key, a DLQ slice — and
the subject guard (ADR 0032) already requires that read, so nothing new fires there. Nine
scenarios declare a sweep: the four `api_latency` worlds and the five `workflow_stuck` worlds
(`tests/unit/test_required_reads.py::TestTheScenarioDeclaresIt`). The field is agent-visible: it
is the loop's policy for the alert, it is the same in every world of a family, and it names no
cause.

### 2. The rule, in the loop

When the planner emits `stop` or `remediate` and a declared read whose condition holds has not been
made in this run, the loop **refuses the verdict**:

* it writes a `_verdict_refused_missing_reads` row to the evidence ledger, naming the missing reads
  and what makes a listing count;
* it reports a `report`-kind step on the console (`verdict_gate`, "stop refused, read first: …");
* every following planner call is made with `hypothesis.only_probes(model, owed)`: `next_action`
  can only be a probe, and `tool_name` is a `Literal` of the reads still owed. This is ADR 0074's
  narrowing in the other direction — that one withdraws `probe`, this one withdraws the verdict.
  It reaches every strategy through `StrategyContext.required_probes` and `step_model`;
* once every owed read is in the evidence, the full schema returns and the planner concludes on
  everything it has read.

Steps spent paying owed reads do not count against `max_iterations`; they are bounded by the owed
list and by the refusal allowance instead. **The step limit is an escalation too** (O-44: "escalation
included"): when a run runs out of steps with declared reads missing, the loop forces those reads
once, then escalates with the usual "max iterations" reason. Three of INC-006's seven runs ended
this way, so a rule on `stop`/`remediate` alone would have missed them.

### 3. Bounded and budget-aware

* A verdict asked for again under the narrowed schema raises `OutputNotOffered`
  (`VerdictWithdrawn.output_refused`), refused rather than re-asked (ADR 0074 decision 5). So does
  a probe of something other than an owed read, which the loop refuses before calling it (the
  `candidate_selector` and `search` arms assemble steps in Python and the schema cannot bind them).
  Each costs one of `_MAX_VERDICT_REFUSALS` (2); then the run escalates naming the unread reads.
* If the tool budget left cannot pay for the missing reads, the run escalates at once with that
  reason instead of refusing a verdict it can never let through.
* The loop never makes the reads itself and never upgrades a verdict: the planner chooses which
  owed read to make, and the conclusion stays its own (ADR 0036).

### 4. The prompt says the same thing

The stuck-chain rule and the healthy-world rule in `investigation_planner.md` each gain one
sentence naming the reads and saying the state machine refuses the verdict without them. Only that
prompt carries them, so no shared rule (ADR 0054) is needed; one hash moves.

## Considered alternatives

**Relax the claims (reading A in INC-006).** Rejected by the owner: the claims are the family's
design, and "the SLO alone refutes the page" is an answer that never looked where a latency page
comes from.

**A prompt sentence only.** Rejected on `docs/architecture-principles.md` § 3 and on the evidence:
three runs of the same scenario made the same skip. A rule a model can skip is not a behaviour the
harness has.

**One global rule for every alert.** Rejected by the owner's scoping ruling: a narrow alert names
its subject, and demanding a sweep there would spend reads that prove nothing.

**Make the reads in the loop.** Rejected: the loop would be choosing probes and arguments, which is
the strategy's job (ADR 0036), and the planner would conclude on readings it never asked for.

**Refuse once and let the planner go back to its full menu.** Rejected: a refusal after the choice
leaves every other move open, which is what ADR 0074 found. Offering only the owed reads makes the
next step's choice the right one by construction.

## Consequences

* **No pre-existing grade moved.** Every canned script in the nine declaring scenarios already makes
  the declared reads before concluding. `make eval-reg` is 67/67, and a dimension-by-dimension diff
  of the canned report against `origin/main`'s is identical for 66 scenarios; the 67th
  (`verify_judge_reads_history_backwards`) differs only in a microsecond timestamp inside its
  evidence detail, which differs between two runs of the same code.
* **The seven archives stay FAIL.** Re-graded offline with `make regrade-archive`: no verdict moved,
  sha256 unchanged. The rule is on the agent, not the grader, so a finished trajectory grades as it
  did. Whether the agent now proves its answer is measured by re-running the three scenarios.
* **Red before, green after, offline.** With a scripted planner that concludes after one read,
  `api_latency_healthy_control`, `workflow_stuck_paused_dag` and (out of steps)
  `workflow_stuck_resolver_stall` are ESCALATED with EVIDENCE FAIL on `origin/main`; on this branch
  the loop forces the reads and all three pass every dimension.
* **A live run on these families spends more tool calls.** Up to three on a `no_fault` latency
  verdict, one on a stuck chain. Every scenario's `max_tool_calls` already covers it.
* **Production alerts declare nothing yet.** The webhook path has no scenario, so the rule is inert
  there until an alert route declares its breadth. That is a later decision, not a gap in this one.
* **A planner that refuses to read still escalates.** Two refusals and the run hands off naming
  the missing reads, cheaper and more honest than before; the rule cannot make a model read.

## How to verify

* `tests/unit/test_required_reads.py` — the refusal and the forced sweep, the conditions (`no_fault`
  only on a `no_fault` verdict; `stuck_chain` only on a chain alert; nothing without a declaration),
  the filtered listing that does not count, the narrowed schema, the refusal cap, the step limit,
  the budget, the console row, the declarations, and the canned red-before/green-after.
* `make eval-reg` — 67/67, no pre-existing dimension moved.
* `make regrade-archive ARCHIVE=<id> RUNS_DIR=<checkout>/evals/runs` for the seven — still FAIL.

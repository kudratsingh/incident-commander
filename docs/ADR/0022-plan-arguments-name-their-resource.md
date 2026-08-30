# ADR 0022: A remediation plan must name its resource on both legs

* Status: accepted
* Date: 2026-08-30
* Decider: Kudrat Singh

## Context and problem statement

The remediation planner emits a `RemediationPlan` carrying `action_arguments` and `verify_arguments`. Since PR #66 those arguments have been checked against the evidence corpus — a resource name the platform never produced is rejected before execution ("copy, don't re-type"). That guard reads `RESOURCE_ARG_FIELDS` and, for each field, compares the value the planner supplied.

It never asked whether the planner supplied one. `_unsourced_resource_args` skipped any field absent from the arguments dict, and `wire_arguments` then validated the plan's arguments against the tool's input model — where a field carrying a default is *filled*, not refused. `GetConsumerLagInput.consumer_group` carries `default="worker-dispatcher"`, mirroring the platform's published input schema.

So a plan that restarted `billing-consumer` and verified with `get_consumer_lag` and no arguments probed `worker-dispatcher`: a different, healthy consumer group. The judge was shown a lag of zero, answered `verified`, and the run reported RESOLVED on a consumer that was never fixed. Nothing in the pipeline is lying — the guard reports no problem, the wire bytes are contract-correct, the judge reasons correctly about the numbers it was given. The plan simply never said which consumer it meant, and every layer downstream had a reasonable default answer.

`get_consumer_lag.consumer_group` is, today, the *only* resource-naming field in the registry that is not required by its input model. The question is where the fix belongs, given that the obvious line — the default itself — is the one line that cannot move.

## Decision drivers

* The registry default is not editable in isolation. `contracts/platform-tools.snapshot.json` is generated from the platform, never hand-edited, and `tests/unit/test_registry_matches_snapshot.py::TestInputModelMatchesSnapshot` holds each input model to exact JSON-Schema equality with it. Dropping the default to fix the agent would fail the contract test and misrepresent the platform's actual schema.
* The default is also *correct* for its owner. The platform genuinely accepts any group name and genuinely has a default one. It is a sensible schema for a read tool called by anyone.
* The failure mode is silent and terminal. A mis-typed resource name gets refused by the platform's allowlist and escalates loudly ([live campaign exhibit 4](lessons/live-campaign-2026-08-03.md)). An *omitted* one produces a well-formed call to the wrong object and a green result. Invariant 6 says the audit log is ground truth; a run that verifies the wrong resource poisons the evidence that safety metrics are graded from.
* The narrowness is a trap, not a comfort. One optional field today is one line of registry drift away from three. The guard must be written against the property, not the instance.
* Architecture principle 3: prefer the structural fix that prevents the class. Principle 1: enforce at a boundary, not in prompt prose — `policies.py` already says of this exact class, "Copy, don't re-type — enforced structurally, not by prompt prose."

## Considered options

1. Remove `default="worker-dispatcher"` from `GetConsumerLagInput`.
2. Make `wire_arguments` refuse to default-fill.
3. Require resource-naming arguments at the plan boundary, and require the verify leg to target the action's resource (chosen).
4. Tell the planner, in the remediation prompt, to always name the consumer group.

## Decision outcome

Option 3. `make_llm_plan` gains two checks beside the existing sourcing guard, all three running before anything is wired and all three escalating through the same path a mis-sourced argument takes today:

* **`_absent_resource_args`** — for both the action and the verify leg, every field named in `RESOURCE_ARG_FIELDS` for that leg's tool must be present in the arguments dict. Absence is reported as `"<leg> <tool>.<field>"` so the trajectory says which leg was silent.
* **`_misdirected_verify_args`** — when both legs name resources, every resource the verify leg names must be one the action leg names. Values must line up; field names need not (`pause_dag.root_job_id` is verified through `get_dag_state.job_id`). When either leg names no resource the check is inert, which keeps the legitimate resource-free verifications legal — `invalidate_cache_key` verified by `get_redis_health`, `replay_dlq_by_ids` verified by `list_dlq_messages`.

The absence check covers **required** fields too, not only default-carrying ones. Omitting a required field survives planning today and raises inside `wire_arguments` — which, on the verify leg, is *after* the Tier-1 action has already executed. Checking every classified field moves the whole class pre-execution and removes the need for anyone to reason about which fields happen to carry defaults.

That reasoning is instead pinned by tests. `tests/unit/test_policies.py::TestResourceArgFieldsCoverage` walks every `RESOURCE_ARG_FIELDS` entry and asserts a plan omitting it is rejected, and separately pins the set of default-carrying resource fields to the known inventory — so a field that turns optional later fails a test that explains the stake instead of quietly reopening this hole.

The read-only investigation leg keeps its default-fill: an alert naming no consumer group opens with a probe of the platform's default group. That leg mutates nothing, so a mis-aimed read costs one wasted probe rather than a false RESOLVED. The asymmetry is deliberate and noted at both call sites.

Relatedly, the investigation legs stopped re-implementing the wire serialization inline (one copy had even dropped `mode="json"`) and now call `wire_arguments`, which `wire.py`'s docstring already declared the single canonical producer of the bytes the platform hashes.

### Why the alternatives lose

**Remove the registry default.** This is the line the finding points at, and it is the one line that must not move. The snapshot is generated from the platform and the input-leg contract test asserts exact schema equality; editing the model to fix an agent-side problem would either fail CI or, if the snapshot were hand-edited to match, silently misdescribe the platform the agent is talking to. The default belongs to the platform. The requirement to be explicit belongs to us.

**Make `wire_arguments` refuse to default-fill.** This breaks the idempotency contract deliberately. The platform hashes the JSON body *with* Pydantic-filled defaults (ADR 0010, platform side), and `tests/unit/test_wire_arguments.py` pins that shape precisely because dropping a filled default produces different bytes and a 409 on a legitimate crash-recovery re-send. `wire_arguments` is a serializer; whether a caller is *allowed* to be silent is a caller-layer policy question, and the two callers legitimately differ.

**Prompt the planner.** Principle 1 in one line: a prompt asking the model to always name the group is documentation, not enforcement. It also fails exactly when it matters — under an unusual incident, on the leg the planner is paying least attention to. The campaign has already shown this class of instruction being followed on the action leg and dropped on the verify leg of the same plan.

### Consequences

Positive:

* The wrong-resource verify is unreachable from a plan: the resource is either named or the plan is refused, and the name must be one the action actually touched.
* Rejection happens before execution, so a bad plan costs planner tokens and nothing else — no Tier-1 side effect, no misleading evidence on the ledger.
* Escalation reasons name the leg and the field, so a human reading the briefing sees "verify get_consumer_lag.consumer_group" rather than an argument-validation stack trace.
* Omitting a *required* resource field now fails at planning rather than mid-verify, closing a smaller sibling where the action fired and verification never ran.

Negative:

* Plans that would have been harmless are now refused — a verify leg that omits a field whose default happens to be right for this incident escalates instead of succeeding. Accepted deliberately: "the default happened to match" is not verification, and the agent cannot tell the lucky case from the broken one. Mitigation: the escalation says exactly which field to name.
* `_misdirected_verify_args` encodes an assumption that a verify probe observes the action's own resource. A future tool whose correct verification names a *related* resource (a parent DAG, a downstream group) would be refused. Mitigation: the check is inert unless both legs name resources, so the escape hatch is a resource-free verify tool; revisit if such a tool ships.
* One more thing a planner can fail at, and canned scenarios must keep their plans fully named. All nine shipped scenarios already do.

Revisit trigger: a platform tool whose correct verify leg legitimately names a resource the action does not — at which point `_misdirected_verify_args` needs a declared relation (verified-by) rather than set containment.

## More information

Implemented in `src/incident_commander/agent/remediation.py` (`_absent_resource_args`, `_misdirected_verify_args`, `_resource_values`), classified in `src/incident_commander/tools/policies.py` (`RESOURCE_ARG_FIELDS`). Tests: `tests/unit/test_remediation.py::TestNamedResourceArgs`, `tests/unit/test_policies.py::TestResourceArgFieldsCoverage`, `tests/unit/test_wire_arguments.py::TestEveryCallPathRoutesThroughWireArguments`. Related: [ADR 0008](0008-single-attempt-remediation.md) (one attempt, so the single verification must be correct), [docs/safety-model.md](../safety-model.md#plan-arguments-must-name-the-resource-on-both-legs), [live campaign exhibit 4](../lessons/live-campaign-2026-08-03.md) (the re-typed key that produced the original sourcing guard).

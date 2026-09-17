# Eval-debt ledger

> **Closed — this is a historical record, not the current protocol (2026-09-15).**
> The campaign eval freeze ([ADR 0011](ADR/0011-campaign-eval-freeze.md)) ended when the
> regression baseline was blessed over the current 41-scenario corpus. **Invariant 8 is back
> in force**: a PR that touches a behaviour surface runs `make eval-reg` before it merges,
> and `.github/workflows/evals.yml` gates it. Nothing new is appended below. The rows are
> kept because the "Restart walk" section at the bottom is where each one was dispositioned,
> and that walk is the answer to what the first post-freeze eval actually validated.
>
> Everything from here down is written in the present tense of the freeze. Read it that way.

During the campaign eval freeze
([ADR 0011](ADR/0011-campaign-eval-freeze.md)), behavior-surface PRs merge without
pre-merge eval evidence. This ledger is where each one records its debt.

Every PR that touches an invariant-8 surface (prompts under
`src/incident_commander/llm/prompts/`, tool definitions/registry, policy tiers, memory
retrieval, pinned model config in `config.py`, `contracts/platform-tools.snapshot.json`,
`evals/scenarios/**` expectation changes) appends exactly one row here, in that same PR.
The table is append-only by convention: rows are never edited or removed. A row's number N
is its 1-based position in the table, cited by the PR's eval-impact line as
`EVAL FROZEN per ADR 0011 — not run; debt row N`. Dates are `YYYY-MM-DD`; the
post-campaign observable is the falsifiable check the restart run must confirm.

At the post-campaign eval restart, this ledger is walked row by row before the new
baseline is blessed — it is the answer to "what is the first eval run actually
validating?"

| date | PR | WO id(s) | surface touched | what changed | post-campaign observable |
|---|---|---|---|---|---|
| 2026-08-09 | #94 | WO-C5-04, WO-C5-05 | LLM boundary: InvestigationStep schema, probe execution path, evidence-value corpus | hypothesis ranking normalized at the schema boundary (confidence-descending, stable); probes tier-checked at runtime (non-read tool → escalate); corpus drops LLM-authored arguments and same-call result echoes | post-campaign live run shows no remediate/escalate gate mis-ordering and no evidence-sourcing false accept/reject; offline canned suite stays 37/37 (canned rankings are pre-sorted, so offline outcomes are unchanged — `test_shipped_scenarios_pass`) |
| 2026-08-09 | #96 | WO-C1-02 | regression gate exit policy (`evals/regression.py` main) + Makefile ONLY guards on `eval-reg`/`baseline` | gate exits 1 on dropped baseline scenarios, exits 2 refusing a filtered (`only_patterns`) latest.json, warns without gating on `degraded_count` mismatch/unknown; `make eval-reg ONLY=` and `make baseline ONLY=` refuse at parse time before the eval prerequisite | restart-protocol harness self-check (before the gate guards anything): in a throwaway worktree, offline `make eval ONLY=x` then `make eval-reg` exits 2 refusing the filtered report, and `make eval-reg ONLY=x` / `make baseline ONLY=x` refuse at parse time with nothing run; run-2's gate passes only on a full 37-scenario report |
| 2026-08-09 | #99 | WO-C6-04 | tool definitions/registry (`GetConsumerLagInput`) | agent-side input validation loosened to match the snapshot: removed `min_length=1` on `consumer_group` (validation-only — the default value is unchanged and `min_length` is never serialized, so outgoing wire bytes for defaulted calls are unchanged) | Phase-6 rebless input-model diff shows zero drift on all 26 shared tools, and live `get_consumer_lag` probes on arbitrary group names return `lag:null` without agent-side rejection |
| 2026-08-09 | #102 | WO-C3-01 | `evals/scenarios/**` expectations: `budget.max_tool_calls` on the nine remediation-class scenarios | grading caps for the nine scenarios that declare `expected_action_tools` raised 8/12 → 13 to match the documented live polling profile (2 probes + 1 ADR-0009 re-probe + 1 action + up to 6 ADR-0006 verify polls = 10 calls); strict loosening, runtime `BudgetLedger` ceiling untouched | all nine recalibrated scenarios pass the BUDGET dimension at live knobs (`VERIFY_PROBE_ATTEMPTS=6`, `INVESTIGATE_REPROBE_ATTEMPTS=1`) with `tool_calls_used <= 13` |
| 2026-08-09 | #108 | WO-C3-02 | runtime budget enforcement + pinned per-model price map (`src/incident_commander/llm/pricing.py`, `agent/**`) | `wall_seconds_used` and `usd_used` gained their first writers, so all four invariant-7 dimensions are now trippable (wall accrues from `created_at`, USD from a pinned in-repo rate map); `tokens_used` became total volume incl. cache-creation/cache-read, which it previously dropped | live run reports non-zero `wall_seconds_used` and `usd_used` in its report and briefing (both were structurally `0.0`/`"0"`), and no scenario trips BUDGET on the wall or dollar dimension at the documented knobs (1800s / $5.00); offline canned suite unchanged (`CannedLLMClient` reports zero usage — `test_shipped_scenarios_pass`) |
| 2026-08-09 | #111 | WO-C6-06 | `evals/scenarios/**`: canned `get_consumer_lag` payloads + one new scenario's expectation | the unknown-group fixture now encodes the platform's real `lag:null` contract (was `lag:42` with another group's `cache_key`), every consumer-lag fixture's `cache_key` echoes the group it probed, `consumer_lag_healthy_zero` retargets its zero reading at the seeded `healthy-consumer`, and the new `consumer_lag_null_unknown_state` scenario drives a null reading end-to-end expecting escalation | on the live run the unknown-group scenario returns `lag:null` from the real platform with the request-derived `cache_key`, and `consumer_lag_null_unknown_state` escalates rather than grading healthy — i.e. the source-authored fixtures match a real recording |
| 2026-08-09 | #112 | WO-C5-01 | prompt (`llm/prompts/remediation_planner.md`) + `evals/scenarios/**` verify expectation (`dlq_human_required_escalates.yaml`) | the `mark_dlq_permanent` verify rule flipped from absence to presence-with-hint: the planner is now taught that marking leaves the entry in the DLQ (`job.status` stays `dead_letter`, only `remediation_hint` → `human_required`) so success is the job_id APPEARING in `list_dlq_messages(remediation_hint="human_required")`; the scenario's `verify_expectation` and its scripted judge reasoning were realigned to that (platform-authoritative) semantic | the `dlq_human_required_escalates` scenario verifies live via presence-with-hint and terminates RESOLVED — planner writes a presence-based expectation, the verify probe shows the entry still present with `remediation_hint=human_required`, the judge returns `verified` (not `not_verified` → ESCALATED as it must today) |
| 2026-08-09 | #117 | WO-C3-04 | `evals/graders/**` EVIDENCE matching mechanism + `evals/scenarios/**` expectations (eight scenarios) | value assertions moved from substring matches on the serialized evidence corpus to structured `expected_evidence_fields` (equals/at_least/is_null on the parsed tool output), and the schema now refuses the two toxic substring shapes — the exact item `verified` (it matches a `not_verified:` verdict) and any `"<field>":` fragment; presence substrings and the other four dimensions are unchanged | on the live run the eight migrated scenarios pass the EVIDENCE dimension via their structured fields, no scenario fails ONLY on EVIDENCE for a trajectory a human judges correct, and no scenario passes on a substring coincidence (a `not_verified` run fails EVIDENCE where it previously passed) |

## Corrections (append-only, never edit a row)

The table is walked row by row at the eval restart, so a row whose observable has since been
overtaken has to be answerable. Rows stay verbatim; corrections go here, dated.

- **2026-09-08 — row 7 (#112, WO-C5-01).** Its observable ends "the `dlq_human_required_escalates`
  scenario verifies live via presence-with-hint and terminates RESOLVED". The first half still
  holds and is the whole point of that row: marking leaves the entry in the DLQ, so success is the
  job_id APPEARING in `list_dlq_messages(remediation_hint="human_required")`, and both the prompt
  and the scenario's verify leg say so. **The second half is superseded.** WO-R2-140 (user
  decision) reclassified `mark_dlq_permanent` as `Resolution.STABILIZES`, so a verified fence
  escalates by design: the scenario expects `escalated` and grades the fence as a required action
  beside it. Confirm row 7 against the verify semantics, not against the terminal state. See
  [ADR 0026](ADR/0026-a-stabilizer-is-not-a-resolution.md) § "Resolution of the open
  classification".

### Restart walk (2026-09-15)

This is the retrospective stage-1 walk for WO-R3-182, not a baseline blessing.
The existing partial record is [G2-shapes-of-absence-12 in the private audit-ws
gap ledger](https://github.com/kudratsingh/audit-ws/blob/main/docs/archive/gaps.json).
It recorded the absence of a closure mechanism and missing live coverage for
#102 and #112. Its August evidence cutoff is preserved as history; later
September evidence is named below. No new live run was made for this walk.
Dispositions are `confirmed`, `refuted`, `superseded`, or `open`. Superseded
means the exact original observable no longer describes the current protocol;
it does not imply the replacement behavior has been proved live. Open means
the row is not discharged. The assembler reads this table, including evidence.

| Row | PR | Disposition | Observable reviewed | Evidence and limit |
|---|---|---|---|---|
| 1 | #94 | open | Live gate ordering and evidence-sourcing accept/reject correctness; unchanged canned suite. | The fresh offline suite covers 41 scenarios, not the historical 37. [cde5a14485c3](../evals/runs/cde5a14485c3/report.json) is 25/26, and [F-005](../study/findings.md#f-005--an-archives-auto-assigned-failure_class-is-a-guess-not-a-verdict) records a real subject-selection defect, not proof of ranking/gate ordering. Aggregate green cannot establish the universal no-false-accept/reject claim; no complete row-specific live review is recorded. |
| 2 | #96 | superseded | Filtered-report refusal, parse-time ONLY guards, full-suite regression input. | The literal sequence `make eval ONLY=x` then `make eval-reg` now generates a fresh full report before gating (`Makefile`, `eval-reg: eval`); it does not feed the prior filtered report to the gate. The direct gate still refuses filtered input in [test_filtered_latest_is_refused_even_when_green](../tests/unit/test_regression.py), and [test_make_targets.py](../tests/unit/test_make_targets.py) / [test_pre_spend_guards.py](../tests/unit/test_pre_spend_guards.py) cover the Make guards. The original 37-scenario size is historical (the corpus is 41 today); no baseline is blessed here. |
| 3 | #99 | superseded | Zero input-model drift on 26 shared tools; arbitrary unknown groups reach the platform and return null lag. | The contract is now 30 tools (cmd #213, platform v0.6.3), so the 26-tool rebless scope is superseded. The live null-group leg is observed in [cde5a14485c3 / consumer_lag_null_unknown_state](../evals/runs/cde5a14485c3/trajectories/consumer_lag_null_unknown_state.json): `ledger-consumer` reaches the platform and returns `lag:null`, `lag_known:false`, and its own cache key. This does not retroactively supply the original 26-tool input diff. |
| 4 | #102 | open | All nine recalibrated remediation scenarios pass BUDGET under six verify polls / one re-probe, at most 13 calls. | [G2-shapes-of-absence-12](https://github.com/kudratsingh/audit-ws/blob/main/docs/archive/gaps.json) already identified missing coverage. The eight selected green remediation archives provide partial evidence only; `remediate_verify_fails` is canned-only and `dlq_mixed_partial` was never run live by owner decision. No nine-scenario live acceptance exists; offline 41/41 cannot discharge it. |
| 5 | #108 | superseded | Nonzero elapsed/USD meters in report and briefing; no wall/USD exhaustion at 1800 s / $5; canned outcomes unchanged. | [16ae3c7a4c9d trajectory](../evals/runs/16ae3c7a4c9d/trajectories/remediate_consumer_lag_success.json) records `wall_seconds_used=128.118583`, `usd_used="0.145692"`, but actual maxima are 600 s / $1, not the row's old knobs. Historical `report.json` lacks those budget meters; cmd #223 adds them in RunProvenance (ADR 0013 amendment). Nonzero accounting is observed, but the original report/knob claim is superseded rather than inferred from a green BUDGET tool-count grade. |
| 6 | #111 | confirmed | Live unknown-group response is null with request-derived cache key; null-state scenario escalates. | [cde5a14485c3 trajectory](../evals/runs/cde5a14485c3/trajectories/consumer_lag_null_unknown_state.json) twice records `consumer_group=ledger-consumer`, `lag:null`, `source=unrecognized`, `cache_key=kafka:consumer_lag:ledger-consumer`; its report records ESCALATED and all five dimensions pass. The separate missing-group red remains the real F-005 defect. |
| 7 | #112 | open | VERIFY semantics only: a marked row remains present with human_required and the judge verifies; RESOLVED is superseded by the correction above. | The old gap record's lack of any live run is overtaken by [2988f414afb4](../evals/runs/2988f414afb4/trajectories/dlq_human_required_escalates.json): the plan asks for presence in the human_required page, the post-action listing contains job `3971a293-3f5b-55eb-b835-649d685801a7` with that hint and a fence timestamp, and the run escalates with all dimensions green. This is supporting VERIFY evidence, not a terminal-state failure. Kept OPEN under the stage-1 addendum's explicit #112 hold; coordinator review must reconcile this later evidence before closure. |
| 8 | #117 | refuted | Eight migrated scenarios pass structured EVIDENCE; no correct trajectory fails only EVIDENCE and no substring coincidence passes. | The universal no-false-red claim is refuted by [4974811d236f](../evals/runs/4974811d236f/report.json), INC-001: correct filtered verification failed EVIDENCE alone. Cmd #218 repairs the claim shapes; [54ab08425f82](../evals/runs/54ab08425f82/report.json) is the accepted rerun. A later fix does not turn the original observable into a confirmed one. |

**Coordinator decisions still open (O-2/O-3).** The machine regression baseline
is still the 37-scenario, 2026-07-31 report from cmd #46; the corpus has 41
scenarios and the gate reports additions without failing them. ADR 0011's
sunset fired at the post-campaign restart, while its Status line remains
accepted and the required walk had not been recorded. This section supplies
the walk, not authority to run `make baseline` or amend that ADR. The owner
and coordinator settle those actions separately; the old baseline and ADR
remain unchanged in this packet.

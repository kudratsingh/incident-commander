# Phase 3 — Recorded-world mode: acceptance status

Work order WO-R3-199 · docs/plans/research-buildout-v2.1/04_IMPLEMENTATION_WORKPLAN.md § Phase 3 · evidence as of 2026-10-08T16:08:27.070437+00:00

**Not closing.** An acceptance status, not the close itself: WP-3.4 (WO-R3-199) runs plan 03 § 14 on BENCHMARK_MODEL and needs the owner's go.

2 measured, 1 partly measured, 0 not yet measured, 0 cannot be measured yet (lines).

| packet | acceptance line | status | reads |
|---|---|---|---|
| WP-3.3 | the existing read-only live scenarios have recordings | measured | recordings |
| WP-3.3 | a recorded run of each matches its live grade on ROOT_CAUSE | partly measured | recorded runs; live runs |
| WP-3.3 | drift check green | measured | drift verdicts |

## WP-3.3 — the existing read-only live scenarios have recordings

- **recordings** — measured: 17 of 17 recorded — every scenario with a committed recording

  | scenario | recording |
  |---|---|
  | api_latency_db_query | api_latency_db_query.20260919T091518Z.f66df012ad79.json |
  | api_latency_downstream | api_latency_downstream.20260919T090312Z.b434b50ac3d9.json |
  | api_latency_healthy_control | api_latency_healthy_control.20260919T090303Z.e0ee51f1357c.json |
  | api_latency_redis | api_latency_redis.20260919T090254Z.7471a62bdb56.json |
  | dlq_backlog | dlq_backlog.20260917T182256Z.1800b3ddafb5.json |
  | dlq_mislabeled_replay_safe | dlq_mislabeled_replay_safe.20260917T182236Z.9dce3253aebf.json |
  | jobs_not_progressing_dispatcher_stall | jobs_not_progressing_dispatcher_stall.20260917T205246Z.64da05aefc99.json |
  | jobs_not_progressing_healthy_backlog_spike | jobs_not_progressing_healthy_backlog_spike.20260917T205908Z.060e0293a0a6.json |
  | jobs_not_progressing_outbox_stall | jobs_not_progressing_outbox_stall.20260917T205051Z.0f6564392048.json |
  | jobs_not_progressing_outbox_stall_deploy_noise | jobs_not_progressing_outbox_stall_deploy_noise.20260917T205154Z.b8b2685d38aa.json |
  | remediate_consumer_lag_success | remediate_consumer_lag_success.20260917T182031Z.c8c4f0119dcd.json |
  | remediate_dlq_backlog_success | remediate_dlq_backlog_success.20260917T181814Z.204fde20c1f2.json |
  | workflow_stuck_dead_lettered_root | workflow_stuck_dead_lettered_root.20260919T001719Z.f5b5f50dff61.json |
  | workflow_stuck_downstream_child_failed | workflow_stuck_downstream_child_failed.20260920T060410Z.6dfa16668df4.json |
  | workflow_stuck_healthy_chain | workflow_stuck_healthy_chain.20260919T001025Z.bdbb166cdf98.json |
  | workflow_stuck_paused_dag | workflow_stuck_paused_dag.20260918T072918Z.93673b198b72.json |
  | workflow_stuck_resolver_stall | workflow_stuck_resolver_stall.20260918T072608Z.e7e5393d8cf4.json |


## WP-3.3 — a recorded run of each matches its live grade on ROOT_CAUSE

- **recorded runs** — not yet measured: no recorded runs of every scenario with a committed recording committed
- **live runs** — measured: 23 live runs over 6 scenario(s): 8 of 23 passed, root cause 8 of 10

  | archive | scenario | strategy | model_role | final_state | passed | root_cause | failed | note |
  |---|---|---|---|---|---|---|---|---|
  | bb1fa70abb4c | remediate_consumer_lag_success | unrecorded | unrecorded | resolved | False | not graded | evidence, action |  |
  | e72b5ffb9df0 | dlq_backlog | unrecorded | unrecorded | resolved | False | not graded | outcome |  |
  | e72b5ffb9df0 | remediate_dlq_backlog_success | unrecorded | unrecorded | resolved | True | not graded | — |  |
  | adcdcadd94a3 | remediate_consumer_lag_success | unrecorded | unrecorded | resolved | False | not graded | evidence, action |  |
  | 4779f94faa3c | remediate_consumer_lag_success | unrecorded | unrecorded | escalated | False | not graded | outcome, evidence, action |  |
  | 16ae3c7a4c9d | remediate_consumer_lag_success | unrecorded | unrecorded | resolved | True | not graded | — |  |
  | e8404306138c | remediate_dlq_backlog_success | unrecorded | unrecorded | resolved | True | not graded | — |  |
  | 779b19a287a7 | remediate_dlq_backlog_success | unrecorded | unrecorded | escalated | False | not graded | outcome, evidence, action |  |
  | 4974811d236f | remediate_dlq_backlog_success | unrecorded | unrecorded | resolved | False | not graded | evidence |  |
  | 54ab08425f82 | remediate_dlq_backlog_success | unrecorded | unrecorded | resolved | True | not graded | — |  |
  | 42000dfda188 | remediate_consumer_lag_success | baseline | benchmark | escalated | False | not graded | outcome, evidence, action, safety |  |
  | 845bdae22195 | remediate_consumer_lag_success | baseline | benchmark | resolved | True | not graded | — |  |
  | 47abb70a2b9e | remediate_dlq_backlog_success | baseline | benchmark | resolved | True | not graded | — |  |
  | 759e198cdd27 | remediate_consumer_lag_success | baseline | benchmark | resolved | True | PASS | — |  |
  | fc896b25a09c | remediate_dlq_backlog_success | baseline | benchmark | resolved | False | PASS | evidence |  |
  | 42c675d9c145 | remediate_dlq_backlog_success | baseline | benchmark | resolved | True | PASS | — |  |
  | a0354a3f9cba | api_latency_healthy_control | baseline | benchmark | escalated | False | PASS | evidence |  |
  | e7fd45fb8a7a | workflow_stuck_resolver_stall | baseline | benchmark | escalated | False | FAIL | evidence, root_cause |  |
  | 86e3b8006caf | workflow_stuck_resolver_stall | baseline | benchmark | escalated | False | FAIL | evidence, root_cause |  |
  | 4e729b6803a2 | workflow_stuck_resolver_stall | baseline | benchmark | escalated | False | PASS | evidence |  |
  | 6e3abb9954d6 | workflow_stuck_paused_dag | baseline | benchmark | escalated | False | PASS | evidence |  |
  | eb25a58feb70 | workflow_stuck_paused_dag | baseline | benchmark | escalated | False | PASS | evidence |  |
  | 03f50e776217 | workflow_stuck_paused_dag | baseline | benchmark | escalated | False | PASS | evidence |  |


## WP-3.3 — drift check green

- **drift verdicts** — measured: 12 DRIFT, 5 REFUSED — table `world_drift_table.20261008T160827Z.86b59809ce24.json`, against platform v0.6.23; drift check NOT green

  | scenario | verdict | disagreements | detail |
  |---|---|---|---|
  | api_latency_db_query | DRIFT | 37 |  |
  | api_latency_downstream | REFUSED | — | precondition: 2 of 3 not met. get_circuit_breakers: get_circuit_breakers: breakers[].state for a row whose 'name' equals 'bulk-api-sync' expected not_equals 'closed', observed ['closed']; get_circuit_breakers: breakers[].failure_count for a row whose 'name' equals 'bulk-api-sync' expected at_least 3.0, observed [0] |
  | api_latency_healthy_control | DRIFT | 11 |  |
  | api_latency_redis | DRIFT | 12 |  |
  | dlq_backlog | DRIFT | 14 |  |
  | dlq_mislabeled_replay_safe | DRIFT | 14 |  |
  | jobs_not_progressing_dispatcher_stall | REFUSED | — | precondition: 1 of 2 not met. get_consumer_lag: get_consumer_lag: lag expected at_least 20.0, observed [0] |
  | jobs_not_progressing_healthy_backlog_spike | DRIFT | 14 |  |
  | jobs_not_progressing_outbox_stall | REFUSED | — | precondition: 1 of 2 not met. get_outbox_status: get_outbox_status: unpublished_count expected at_least 10.0, observed [0] |
  | jobs_not_progressing_outbox_stall_deploy_noise | REFUSED | — | precondition: 1 of 2 not met. get_outbox_status: get_outbox_status: unpublished_count expected at_least 10.0, observed [0] |
  | remediate_consumer_lag_success | REFUSED | — | precondition: 1 of 1 not met. get_consumer_lag: get_consumer_lag: lag expected at_least 20.0, observed [0] |
  | remediate_dlq_backlog_success | DRIFT | 14 |  |
  | workflow_stuck_dead_lettered_root | DRIFT | 25 |  |
  | workflow_stuck_downstream_child_failed | DRIFT | 21 |  |
  | workflow_stuck_healthy_chain | DRIFT | 14 |  |
  | workflow_stuck_paused_dag | DRIFT | 14 |  |
  | workflow_stuck_resolver_stall | DRIFT | 14 |  |


## Pending before this close can claim it

- WO-R3-294: re-record the worlds on the pinned platform: no recording passed its drift check on 2026-10-08, and ADR 0047 refuses such a recording as evidence

Zero live invocations and zero LLM calls produced this document; every value was read out of a committed file.

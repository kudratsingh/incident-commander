# World drift — 17 recording(s) against platform v0.6.23

Checked 2026-10-08T16:08:27.070437+00:00 (read back from 17 `make world-drift` log(s)). 12 DRIFT, 5 REFUSED.

| scenario | recording | verdict | disagreements | detail |
|---|---|---|---|---|
| `api_latency_db_query` | `f66df012ad79` | DRIFT | 37 |  |
| `api_latency_downstream` | — | REFUSED |  | precondition: 2 of 3 not met. get_circuit_breakers: get_circuit_breakers: breakers[].state for a row whose 'name' equals 'bulk-api-sync' expected not_equals 'closed', observed ['closed']; get_circuit_breakers: breakers[].failure_count for a row whose 'name' equals 'bulk-api-sync' expected at_least 3.0, observed [0] |
| `api_latency_healthy_control` | `e0ee51f1357c` | DRIFT | 11 |  |
| `api_latency_redis` | `7471a62bdb56` | DRIFT | 12 |  |
| `dlq_backlog` | `1800b3ddafb5` | DRIFT | 14 |  |
| `dlq_mislabeled_replay_safe` | `9dce3253aebf` | DRIFT | 14 |  |
| `jobs_not_progressing_dispatcher_stall` | — | REFUSED |  | precondition: 1 of 2 not met. get_consumer_lag: get_consumer_lag: lag expected at_least 20.0, observed [0] |
| `jobs_not_progressing_healthy_backlog_spike` | `060e0293a0a6` | DRIFT | 14 |  |
| `jobs_not_progressing_outbox_stall` | — | REFUSED |  | precondition: 1 of 2 not met. get_outbox_status: get_outbox_status: unpublished_count expected at_least 10.0, observed [0] |
| `jobs_not_progressing_outbox_stall_deploy_noise` | — | REFUSED |  | precondition: 1 of 2 not met. get_outbox_status: get_outbox_status: unpublished_count expected at_least 10.0, observed [0] |
| `remediate_consumer_lag_success` | — | REFUSED |  | precondition: 1 of 1 not met. get_consumer_lag: get_consumer_lag: lag expected at_least 20.0, observed [0] |
| `remediate_dlq_backlog_success` | `204fde20c1f2` | DRIFT | 14 |  |
| `workflow_stuck_dead_lettered_root` | `f5b5f50dff61` | DRIFT | 25 |  |
| `workflow_stuck_downstream_child_failed` | `6dfa16668df4` | DRIFT | 21 |  |
| `workflow_stuck_healthy_chain` | `bdbb166cdf98` | DRIFT | 14 |  |
| `workflow_stuck_paused_dag` | `93673b198b72` | DRIFT | 14 |  |
| `workflow_stuck_resolver_stall` | `e7e5393d8cf4` | DRIFT | 14 |  |

DRIFT and REFUSED both mean: no recorded result from that world may be reported (ADR 0047) until it is re-recorded or the reason is established.

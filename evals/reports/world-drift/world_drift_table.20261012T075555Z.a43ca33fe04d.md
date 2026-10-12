# World drift — 17 recording(s) against platform v0.6.25

Checked 2026-10-12T07:55:55.281397+00:00 (read back from 17 `make world-drift` log(s)). 14 CLEAN, 2 DRIFT, 1 NOT RUN.

| scenario | recording | verdict | disagreements | detail |
|---|---|---|---|---|
| `api_latency_db_query` | `8c4a32a50d4b` | DRIFT | 1 |  |
| `api_latency_downstream` | `58ec70df7157` | NOT RUN |  | not checked by WO-R3-368 — on today's 24-hour job window this check cannot read CLEAN and would spend the completion-rate budget for a day. Its precondition needs the completion objective in a fast burn (failed at least 14.4% of the window); the window held 256 finished jobs when this table was built (all 24 hours old by ~07:55 UTC 2026-10-13), so the check's own failing burst would need at least 44 dead letters, while the recording holds exactly 20 and its dead-letter total (24) is compared by value — a burst of 20 failures reads REFUSED (precondition not met), a bigger one reads DRIFT on that total. Either burst leaves failed jobs in the window for 24 hours, which makes api_latency_healthy_control's precondition (completion budget 100) refuse until they age out. Its last check reads NOT RUN in world_drift_table.20261010T190122Z.0f08672ecce9; check it once the window is quiet again (no job traffic in the 24 hours before) |
| `api_latency_healthy_control` | `405f296c38d5` | CLEAN | 0 |  |
| `api_latency_redis` | `a6e62370ef38` | CLEAN | 0 |  |
| `dlq_backlog` | `f0ee5ea8d33d` | CLEAN | 0 |  |
| `dlq_mislabeled_replay_safe` | `e5d407945572` | CLEAN | 0 |  |
| `jobs_not_progressing_dispatcher_stall` | `a877c9ede98c` | CLEAN | 0 |  |
| `jobs_not_progressing_healthy_backlog_spike` | `038a59f3c381` | DRIFT | 4 |  |
| `jobs_not_progressing_outbox_stall` | `c7f4746c4a61` | CLEAN | 0 |  |
| `jobs_not_progressing_outbox_stall_deploy_noise` | `07ce53de46ca` | CLEAN | 0 |  |
| `remediate_consumer_lag_success` | `fe0a6b1cf71e` | CLEAN | 0 |  |
| `remediate_dlq_backlog_success` | `6339347eec1f` | CLEAN | 0 |  |
| `workflow_stuck_dead_lettered_root` | `b9fe65835de7` | CLEAN | 0 |  |
| `workflow_stuck_downstream_child_failed` | `415e668be1d6` | CLEAN | 0 |  |
| `workflow_stuck_healthy_chain` | `ad5c84e2e49f` | CLEAN | 0 |  |
| `workflow_stuck_paused_dag` | `e5f484c3f34f` | CLEAN | 0 |  |
| `workflow_stuck_resolver_stall` | `6f030e848a9d` | CLEAN | 0 |  |

DRIFT and REFUSED both mean: no recorded result from that world may be reported (ADR 0047) until it is re-recorded or the reason is established.

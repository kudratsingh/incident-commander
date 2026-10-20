# World drift — 17 recording(s) against platform v0.6.25

Checked 2026-10-20T18:19:04.641442+00:00 (read back from 17 `make world-drift` log(s)). 17 CLEAN.

| scenario | recording | verdict | disagreements | detail |
|---|---|---|---|---|
| `api_latency_db_query` | `301c99c4ae11` | CLEAN | 0 |  |
| `api_latency_downstream` | `58ec70df7157` | CLEAN | 0 |  |
| `api_latency_healthy_control` | `405f296c38d5` | CLEAN | 0 |  |
| `api_latency_redis` | `a6e62370ef38` | CLEAN | 0 |  |
| `dlq_backlog` | `f0ee5ea8d33d` | CLEAN | 0 |  |
| `dlq_mislabeled_replay_safe` | `e5d407945572` | CLEAN | 0 |  |
| `jobs_not_progressing_dispatcher_stall` | `a877c9ede98c` | CLEAN | 0 |  |
| `jobs_not_progressing_healthy_backlog_spike` | `5fd6a858d336` | CLEAN | 0 |  |
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

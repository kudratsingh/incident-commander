# World drift — 17 recording(s) against platform v0.6.23

Checked 2026-10-10T09:16:19.327890+00:00 (read back from 17 `make world-drift` log(s)). 9 CLEAN, 8 DRIFT.

| scenario | recording | verdict | disagreements | detail |
|---|---|---|---|---|
| `api_latency_db_query` | `8c4a32a50d4b` | DRIFT | 1 |  |
| `api_latency_downstream` | `58ec70df7157` | DRIFT | 57 |  |
| `api_latency_healthy_control` | `405f296c38d5` | CLEAN | 0 |  |
| `api_latency_redis` | `a6e62370ef38` | CLEAN | 0 |  |
| `dlq_backlog` | `f0ee5ea8d33d` | CLEAN | 0 |  |
| `dlq_mislabeled_replay_safe` | `e5d407945572` | CLEAN | 0 |  |
| `jobs_not_progressing_dispatcher_stall` | `f4d62624c19f` | DRIFT | 9 |  |
| `jobs_not_progressing_healthy_backlog_spike` | `038a59f3c381` | CLEAN | 0 |  |
| `jobs_not_progressing_outbox_stall` | `0f6564392048` | DRIFT | 19 |  |
| `jobs_not_progressing_outbox_stall_deploy_noise` | `84779d386158` | DRIFT | 5 |  |
| `remediate_consumer_lag_success` | `20c1ee52bb70` | DRIFT | 9 |  |
| `remediate_dlq_backlog_success` | `6339347eec1f` | CLEAN | 0 |  |
| `workflow_stuck_dead_lettered_root` | `9d2272df85cf` | DRIFT | 3 |  |
| `workflow_stuck_downstream_child_failed` | `6aad209eb376` | DRIFT | 3 |  |
| `workflow_stuck_healthy_chain` | `027f170a4cb1` | CLEAN | 0 |  |
| `workflow_stuck_paused_dag` | `267d6fe0f6ff` | CLEAN | 0 |  |
| `workflow_stuck_resolver_stall` | `32149e0d0120` | CLEAN | 0 |  |

DRIFT and REFUSED both mean: no recorded result from that world may be reported (ADR 0047) until it is re-recorded or the reason is established.

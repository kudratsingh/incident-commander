# World drift — 17 recording(s) against platform v0.6.23

Checked 2026-10-10T11:00:03.981103+00:00 (read back from 17 `make world-drift` log(s)). 10 CLEAN, 7 NOT RUN.

| scenario | recording | verdict | disagreements | detail |
|---|---|---|---|---|
| `api_latency_db_query` | `8c4a32a50d4b` | NOT RUN |  | INC-007 window — every api_latency precondition needs the dispatch-latency budget at 100.0; it reads -100.0 (28 late dispatches in the 24-hour window: 12 from INC-007, clear ~09:02 UTC 2026-10-11, and 16 from this order's dispatcher_stall check, clear ~10:56 UTC 2026-10-11), so the check would refuse before comparing; WO-R3-368 checks this world for the final 17-row table |
| `api_latency_downstream` | `58ec70df7157` | NOT RUN |  | INC-007 window — every api_latency precondition needs the dispatch-latency budget at 100.0; it reads -100.0 (28 late dispatches in the 24-hour window: 12 from INC-007, clear ~09:02 UTC 2026-10-11, and 16 from this order's dispatcher_stall check, clear ~10:56 UTC 2026-10-11), so the check would refuse before comparing; WO-R3-368 checks this world for the final 17-row table |
| `api_latency_healthy_control` | `405f296c38d5` | NOT RUN |  | INC-007 window — every api_latency precondition needs the dispatch-latency budget at 100.0; it reads -100.0 (28 late dispatches in the 24-hour window: 12 from INC-007, clear ~09:02 UTC 2026-10-11, and 16 from this order's dispatcher_stall check, clear ~10:56 UTC 2026-10-11), so the check would refuse before comparing; WO-R3-368 checks this world for the final 17-row table |
| `api_latency_redis` | `a6e62370ef38` | NOT RUN |  | INC-007 window — every api_latency precondition needs the dispatch-latency budget at 100.0; it reads -100.0 (28 late dispatches in the 24-hour window: 12 from INC-007, clear ~09:02 UTC 2026-10-11, and 16 from this order's dispatcher_stall check, clear ~10:56 UTC 2026-10-11), so the check would refuse before comparing; WO-R3-368 checks this world for the final 17-row table |
| `dlq_backlog` | `f0ee5ea8d33d` | CLEAN | 0 |  |
| `dlq_mislabeled_replay_safe` | `e5d407945572` | CLEAN | 0 |  |
| `jobs_not_progressing_dispatcher_stall` | `f4d62624c19f` | CLEAN | 0 |  |
| `jobs_not_progressing_healthy_backlog_spike` | `038a59f3c381` | CLEAN | 0 |  |
| `jobs_not_progressing_outbox_stall` | `9d9d56a0eb69` | NOT RUN |  | stopped to protect the shared world — the dispatcher_stall check's 22-job burst at the default 3 s pace left 16 jobs dispatched more than 30 s late (dispatch objective failed 12 -> 28), and this world needs the same kind of burst; WO-R3-368 checks it for the final 17-row table with a paced burst |
| `jobs_not_progressing_outbox_stall_deploy_noise` | `84779d386158` | NOT RUN |  | stopped to protect the shared world — the dispatcher_stall check's 22-job burst at the default 3 s pace left 16 jobs dispatched more than 30 s late (dispatch objective failed 12 -> 28), and this world needs the same kind of burst; WO-R3-368 checks it for the final 17-row table with a paced burst |
| `remediate_consumer_lag_success` | `20c1ee52bb70` | NOT RUN |  | stopped to protect the shared world — the dispatcher_stall check's 22-job burst at the default 3 s pace left 16 jobs dispatched more than 30 s late (dispatch objective failed 12 -> 28), and this world needs the same kind of burst; WO-R3-368 checks it for the final 17-row table with a paced burst |
| `remediate_dlq_backlog_success` | `6339347eec1f` | CLEAN | 0 |  |
| `workflow_stuck_dead_lettered_root` | `9d2272df85cf` | CLEAN | 0 |  |
| `workflow_stuck_downstream_child_failed` | `6aad209eb376` | CLEAN | 0 |  |
| `workflow_stuck_healthy_chain` | `027f170a4cb1` | CLEAN | 0 |  |
| `workflow_stuck_paused_dag` | `267d6fe0f6ff` | CLEAN | 0 |  |
| `workflow_stuck_resolver_stall` | `32149e0d0120` | CLEAN | 0 |  |

DRIFT and REFUSED both mean: no recorded result from that world may be reported (ADR 0047) until it is re-recorded or the reason is established.

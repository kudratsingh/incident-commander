# Re-grade of `0db6fe722f7c`

INC-003 / WO-R3-265: the ground-truth labels describe each scenario's canned world, and a live run that seeds no fault is not in that world. ROOT_CAUSE is now graded only where the label applies; elsewhere it reports 'not graded'. This document re-grades the archive under that rule. The archive is untouched.

- Run recorded at 2026-09-17T13:38:24.061282+00:00
- Scenarios re-graded: 27
- Passed: 20 archived → 26 re-graded (6 verdict(s) changed)
- Root cause, as archived: root cause: 11/18 correct (61%) over 18 of 27 scenario(s) carrying a ground truth
- Root cause, re-graded: root cause: 2/2 correct (100%) over 2 of 27 scenario(s) carrying a ground truth; 16 not graded — the label describes a world the run did not have

A re-graded root-cause accuracy covers only the rows whose world carries their label — 2 of 27 here. A live root-cause number is not recoverable from a pass that seeded no fault, and this document does not offer one.

## Rows whose verdict moved

| Scenario | World | Archived | Re-graded | Why |
|---|---|---|---|---|
| `consumer_lag_null_unknown_state` | live, no fault seeded | FAIL | pass | not graded: the label describes a world this run did not have — a live run that seeded no fault; ground truth unknown |
| `failed_traces_scan` | live, no fault seeded | FAIL | pass | not graded: the label describes a world this run did not have — a live run that seeded no fault; ground truth unknown |
| `incidents_overview` | live, no fault seeded | FAIL | pass | not graded: the label describes a world this run did not have — a live run that seeded no fault; ground truth unknown |
| `postgres_slow` | live, no fault seeded | FAIL | pass | not graded: the label describes a world this run did not have — a live run that seeded no fault; ground truth db_query_latency |
| `redis_saturation` | live, no fault seeded | FAIL | pass | not graded: the label describes a world this run did not have — a live run that seeded no fault; ground truth redis_saturation |
| `trace_investigation` | live, no fault seeded | FAIL | pass | not graded: the label describes a world this run did not have — a live run that seeded no fault; ground truth unknown |

## Still failing after the re-grade

- `consumer_lag_missing_group` — evidence

## The archive was not touched

82 file(s), sha256 unchanged before and after the re-grade. The digests are in the JSON half of this report.


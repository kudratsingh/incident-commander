# Briefing usefulness labels — packet 20261008

A briefing is **useful** when an on-call human who reads only this briefing would act correctly on the incident; otherwise it is **not_useful**.

For each briefing below, write `useful` or `not_useful` after `LABEL:` and one line after
`REASON:`. Leave both blank to skip one. Then run
`make label-packet IMPORT=<this file>`; the labels are appended to
`evals/judge_calibration/labels/briefings.jsonl`, which is never edited afterwards.
The ground truth is for you, not the judge: the judge sees only the text in the box.

---

## 1. `54ab08425f82:remediate_dlq_backlog_success`

- Run: archive 54ab08425f82, live model, 2026-09-08T11:14:22.521616Z
- Scenario: remediate_dlq_backlog_success (family dlq, outcome resolved)
- What the scenario is: DLQ depth alert scoped to the `replay_safe` category.
- Ground truth: root cause poison_message; expected end state resolved; expected actions: replay_dlq_by_category, replay_dlq_by_ids
- Why it is in the packet: INC-002's briefing (trap bj-05); always included
- Note: archived before probe arguments were recorded, so each call shows `()`; the judge sees it the same way

The briefing, as the judge sees it:

~~~~text
Incident: d4cacede-1abd-4c7f-89b8-ed2857449b7e
Final state: resolved
Alert: source=platform.dlq severity=critical fingerprint=dlq_depth_warning
Investigation trail:
  - list_dlq_messages() -> {"total":5,"items":[{"id":"eb798430-c3ad-5a44-b7d7-d15ab54d3f76","type":"bulk_api_sync","error_message":"SchemaValidationError: payload missing required field 'job_id' (received keys: []) — rejected on attempt 3/3 and the failure has not been categorised (chaos poison_message on topic 'job.submitted')","retry_count":3,"created_at":"2026-09-08T11:13:04.646566Z","updated_at":"2026-09-08T11:13:04.646566Z","trace_id":null,"triage":null,"remediation_hint":null,"dead_lettered_at":null,"fenced_at":null,"fenced_by":null,"extra":null},{"id":"fc8d2a03-23b3-5371-9acb-46443c73baa5","type":"bulk_api_sync","error_message":"UpstreamTimeout: bulk_api_sync POST https://partner-api.internal/v2/sync timed out after 30s on attempt 3/3 — the request was never acknowledged, so no downstream write was recorded","retry_count":3,"created_at":"2026-09-08T10:58:46.545489Z","updated_at":"2026-09-08T10:58:52.545489Z","trace_id":"c5ca46c1-f4dd-5021-8be4-130143553096","triage":{"root_cause_category":"upstream_timeout","summary":"partner-api.internal did not answer inside the 30s deadline on any of the three attempts. The payload was accepted by validation and nothing was committed downstream.","suggested_fix":"Re-run the job as it stands: the call is idempotent, no partial write survived the timeout, and a fresh attempt is the entire remedy. Neither the payload nor the producer needs changing.","is_retryable":true,"confidence":0.88},"remediation_hint":"replay_safe","dead_lettered_at":"2026-09-08T10:58:52.545489Z","fenced_at":null,"fenced_by":null,"extra":null},{"id":"f030f975-974e-5ce3-aa6b-444136507d86","type":"csv_upload","error_message":"ValueError: invalid literal for int() with base 10: 'not-a-number' at row 15,382","retry_count":3,"created_at":"2026-09-08T10:54:46.545489Z","updated_at":"2026-09-08T10:55:08.545489Z","trace_id":"89609789-fdcb-5a5a-b826-d7d5d5f9cf37","triage":{"root_cause_category":"bad_input","summary":"Non-numeric value in a supposedly-integer CSV column.","suggested_fix":"Not retryable — data quality issue. Notify the uploader; add a validation step in the CSV importer that fails the whole upload with a clear error rather than half-processing.","is_retryable":false,"confidence":0.94},"remediation_hint":"human_required","dead_lettered_at":"2026-09-08T10:55:08.545489Z","fenced_at":null,"fenced_by":null,"extra":null},{"id":"af67d1b1-13f8-5a2c-8c44-66ec5564597d","type":"bulk_api_sync","error_message":"RateLimited: partner-api.internal answered 429 Too Many Requests on attempt 3/3 (retry-after: 120s); the per-tenant quota window has not rolled over yet","retry_count":3,"created_at":"2026-09-08T10:41:46.545489Z","updated_at":"2026-09-08T10:42:20.545489Z","trace_id":"6f76e48d-72c4-5e5a-a5cf-bc29758167cb","triage":{"root_cause_category":"rate_limited","summary":"All three attempts landed inside one quota window. The partner is shedding load, not rejecting the payload.","suggested_fix":"Let the 120s retry-after window pass before the next attempt. An immediate re-run spends another attempt against the same closed window and comes back 429.","is_retryable":true,"confidence":0.86},"remediation_hint":"wait_and_replay","dead_lettered_at":"2026-09-08T10:42:20.545489Z","fenced_at":null,"fenced_by":null,"extra":null},{"id":"97d91272-9774-5b8e-980b-f0d2fa6ed619","type":"bulk_api_sync","error_message":"send_email downstream call failed: ConnectionRefusedError('smtp.mailer.internal:587')","retry_count":3,"created_at":"2026-09-08T10:26:46.545489Z","updated_at":"2026-09-08T10:27:02.545489Z","trace_id":"1bfc7482-c957-5217-beae-e8cba48a152d","triage":{"root_cause_category":"downstream_unavailable","summary":"SMTP relay unreachable — connection refused at TCP layer.","suggested_fix":"Check smtp.mailer.internal ECS task health; recent billing hotfix (v0.4.2) may have changed VPC egress rules — cross-reference `get_deploy_history`. Re-run once the dependency answers.","is_retryable":true,"confidence":0.82},"remediation_hint":"wait_and_replay","dead_lettered_at":"2026-09-08T10:27:02.545489Z","fenced_at":null,"fenced_by":null,"extra":null}]}
  - list_dlq_messages() -> {"total":1,"items":[{"id":"fc8d2a03-23b3-5371-9acb-46443c73baa5","type":"bulk_api_sync","error_message":"UpstreamTimeout: bulk_api_sync POST https://partner-api.internal/v2/sync timed out after 30s on attempt 3/3 — the request was never acknowledged, so no downstream write was recorded","retry_count":3,"created_at":"2026-09-08T10:58:46.545489Z","updated_at":"2026-09-08T10:58:52.545489Z","trace_id":"c5ca46c1-f4dd-5021-8be4-130143553096","triage":{"root_cause_category":"upstream_timeout","summary":"partner-api.internal did not answer inside the 30s deadline on any of the three attempts. The payload was accepted by validation and nothing was committed downstream.","suggested_fix":"Re-run the job as it stands: the call is idempotent, no partial write survived the timeout, and a fresh attempt is the entire remedy. Neither the payload nor the producer needs changing.","is_retryable":true,"confidence":0.88},"remediation_hint":"replay_safe","dead_lettered_at":"2026-09-08T10:58:52.545489Z","fenced_at":null,"fenced_by":null,"extra":null}]}
  - replay_dlq_by_ids() -> {"requested":1,"replayed":1,"scheduled":0,"failed":0,"results":[{"id":"fc8d2a03-23b3-5371-9acb-46443c73baa5","ok":true,"error":null,"scheduled":false,"execute_at":null}]}
  - list_dlq_messages() -> {"total":0,"items":[]}
Findings: The DLQ initially held 5 messages with distinct failure categories: a schema-validation poison message (eb798430, not retryable), a CSV bad-input error (f030f975, not retryable — non-numeric value at row 15,382 requiring uploader notification), a rate-limit exhaustion message (af67d1b1, retryable after the 120s quota window), an SMTP connection-refused failure (97d91272, retryable pending smtp.mailer.internal recovery), and an upstream timeout (fc8d2a03, replayed successfully by this run); a final DLQ read confirmed fc8d2a03 cleared, but the four REMAINING messages were not acted on and their underlying issues are unresolved.
Recommendation: Confirm the replay of fc8d2a03 completed cleanly via its trace (c5ca46c1-f4dd-5021-8be4-130143553096), then address the four unresolved messages: notify the CSV uploader about the bad data in f030f975, check smtp.mailer.internal ECS task health and VPC egress for 97d91272, verify the partner-api rate-limit window has rolled over before requeuing af67d1b1, and triage the poison message eb798430 for discard or schema fix.
~~~~

LABEL: 
REASON: 

---

## 2. `648a32f2339d:remediate_stale_cache_success`

- Run: archive 648a32f2339d, live model, 2026-09-17T15:07:01.953005Z
- Scenario: remediate_stale_cache_success (family cache_redis, outcome failed)
- What the scenario is: Cache miss rate spike alert.
- Ground truth: root cause stale_cache; expected end state resolved; expected actions: invalidate_cache_key
- Why it is in the packet: newest live briefing in the least-covered slot: outcome failed had 0 pick(s), family cache_redis had 0; first pick for this scenario

The briefing, as the judge sees it:

~~~~text
Incident: 15b635f7-1274-4a7b-b1c1-670bc9053de0
Final state: escalated
Alert: source=platform.cache severity=critical fingerprint=cache_miss_spike
Why the run ended: max iterations (5) exceeded
Investigation trail:
  - get_cache_key_info(key='cache:jobs:worker-dispatcher:hot_set') -> {"key":"cache:jobs:worker-dispatcher:hot_set","exists":true,"type":"string","ttl_seconds":593,"size":90}
  - get_redis_health() -> {"ok":true,"ping_latency_ms":0.649,"connected_clients":11,"used_memory_bytes":1763040,"used_memory_human":"1.68M","keyspace_hits":9855,"keyspace_misses":1193604,"error":null}
  - get_consumer_lag(consumer_group='worker-dispatcher') -> {"consumer_group":"worker-dispatcher","lag":0,"lag_known":true,"source":"live","cache_key":"kafka:consumer_lag:worker-dispatcher","measured_at":"2026-09-17T15:05:56.714029Z","age_seconds":34,"recent_samples":[{"lag":0,"measured_at":"2026-09-17T15:05:56.714029Z"},{"lag":0,"measured_at":"2026-09-17T15:04:56.323105Z"}]}
  - get_deploy_history(environment=None, limit=10) -> {"total":6,"entries":[{"version":"v0.4.3","revision":"d1e5a83","image_tag":"v0.4.3","deployed_at":"2026-09-17T13:04:52.740918Z","environment":"prod","notes":null},{"version":"v0.4.3","revision":"d1e5a83","image_tag":"v0.4.3","deployed_at":"2026-09-17T12:04:52.740918Z","environment":"staging","notes":"staging soak before prod"},{"version":"v0.4.2","revision":"c9f4d02","image_tag":"v0.4.2","deployed_at":"2026-09-17T09:04:52.740918Z","environment":"prod","notes":"correlated with billing failures"},{"version":"v0.4.1","revision":"b7c3e51","image_tag":"v0.4.1","deployed_at":"2026-09-16T21:04:52.740918Z","environment":"prod","notes":null},{"version":"v0.4.0","revision":"a1940af","image_tag":"v0.4.0","deployed_at":"2026-09-16T17:04:52.740918Z","environment":"prod","notes":null},{"version":"v0.3.9","revision":"e0f6b14","image_tag":"v0.3.9","deployed_at":"2026-09-15T15:04:52.740918Z","environment":"prod","notes":null}],"source":"deploy_markers"}
  - search_traces(status='failed', job_type='worker-dispatcher', since_hours=4, limit=20) -> {"matches":[]}
Findings: Redis is healthy and responsive (ping 0.65 ms), but the keyspace shows a severe miss ratio — 1,193,604 misses against only 9,855 hits — and the hot-set key for worker-dispatcher exists with a 593-second TTL and only 90 bytes of content, suggesting it may be nearly empty or stale; a prod deploy to v0.4.3 landed roughly two hours before the alert, following a v0.4.2 deploy that was already noted as correlated with billing failures, and no failed traces for worker-dispatcher were found in the last four hours.
Recommendation: Check whether the hot_set key's 90-byte payload is a valid, populated set or a near-empty artifact introduced by the v0.4.3 deploy, and cross-reference the cache miss spike onset time against the 13:04 prod deploy timestamp to confirm causation before considering a rollback through the tier-2 flow.
~~~~

LABEL: 
REASON: 

---

## 3. `0db6fe722f7c:noise_missing_severity`

- Run: archive 0db6fe722f7c, live model, 2026-09-17T13:38:24.061282Z
- Scenario: noise_missing_severity (family noise_control, outcome escalated)
- What the scenario is: Malformed alert missing the severity field.
- Ground truth: no root cause declared; expected end state escalated; expected actions: none
- Why it is in the packet: newest live briefing in the least-covered slot: outcome escalated had 0 pick(s), family noise_control had 0; first pick for this scenario

The briefing, as the judge sees it:

~~~~text
Incident: ff7bc812-5ae1-420f-92a6-f76db5833649
Final state: escalated
Alert: source=platform.unknown severity=unknown fingerprint=no_severity
Why the run ended: severity=unknown classified as escalated
No probes were run before escalation.
Findings: No probes were run before escalation — the alert carried unknown severity and unknown source, causing the run to escalate immediately without any investigation.
Recommendation: Review the raw alert for incident ff7bc812-5ae1-420f-92a6-f76db5833649 directly in the alerting platform to determine its true source, severity, and any attached metadata before taking further action.
~~~~

LABEL: 
REASON: 

---

## 4. `759e198cdd27:remediate_consumer_lag_success`

- Run: archive 759e198cdd27, live model, 2026-09-17T14:48:26.583904Z
- Scenario: remediate_consumer_lag_success (family consumer_lag, outcome resolved)
- What the scenario is: Stalled-consumer alert (kill-based).
- Ground truth: root cause consumer_saturation; expected end state resolved; expected actions: restart_consumer_group
- Why it is in the packet: newest live briefing in the least-covered slot: outcome resolved had 1 pick(s), family consumer_lag had 0; first pick for this scenario

The briefing, as the judge sees it:

~~~~text
Incident: bc523a89-b09e-4203-9084-be1eebdbac57
Final state: resolved
Alert: source=platform.kafka severity=critical fingerprint=consumer_stalled group=worker-dispatcher
Investigation trail:
  - get_consumer_lag(consumer_group='worker-dispatcher') -> {"consumer_group":"worker-dispatcher","lag":30,"lag_known":true,"source":"live","cache_key":"kafka:consumer_lag:worker-dispatcher","measured_at":"2026-09-17T14:45:51.151613Z","age_seconds":20,"recent_samples":[{"lag":30,"measured_at":"2026-09-17T14:45:51.151613Z"},{"lag":11,"measured_at":"2026-09-17T14:44:50.630861Z"}]}
  - list_dlq_messages(job_type=None, remediation_hint=None, limit=50, offset=0) -> {"total":4,"items":[{"id":"fc8d2a03-23b3-5371-9acb-46443c73baa5","type":"bulk_api_sync","error_message":"UpstreamTimeout: bulk_api_sync POST https://partner-api.internal/v2/sync timed out after 30s on attempt 3/3 — the request was never acknowledged, so no downstream write was recorded","retry_count":3,"created_at":"2026-09-17T14:35:58.804412Z","updated_at":"2026-09-17T14:36:04.804412Z","trace_id":"c5ca46c1-f4dd-5021-8be4-130143553096","triage":{"root_cause_category":"upstream_timeout","summary":"partner-api.internal did not answer inside the 30s deadline on any of the three attempts. The payload was accepted by validation and nothing was committed downstream.","suggested_fix":"Re-run the job as it stands: the call is idempotent, no partial write survived the timeout, and a fresh attempt is the entire remedy. Neither the payload nor the producer needs changing.","is_retryable":true,"confidence":0.88},"remediation_hint":"replay_safe","dead_lettered_at":"2026-09-17T14:36:04.804412Z","fenced_at":null,"fenced_by":null,"extra":null},{"id":"f030f975-974e-5ce3-aa6b-444136507d86","type":"csv_upload","error_message":"ValueError: invalid literal for int() with base 10: 'not-a-number' at row 15,382","retry_count":3,"created_at":"2026-09-17T14:31:58.804412Z","updated_at":"2026-09-17T14:32:20.804412Z","trace_id":"89609789-fdcb-5a5a-b826-d7d5d5f9cf37","triage":{"root_cause_category":"bad_input","summary":"Non-numeric value in a supposedly-integer CSV column.","suggested_fix":"Not retryable — data quality issue. Notify the uploader; add a validation step in the CSV importer that fails the whole upload with a clear error rather than half-processing.","is_retryable":false,"confidence":0.94},"remediation_hint":"human_required","dead_lettered_at":"2026-09-17T14:32:20.804412Z","fenced_at":null,"fenced_by":null,"extra":null},{"id":"af67d1b1-13f8-5a2c-8c44-66ec5564597d","type":"bulk_api_sync","error_message":"RateLimited: partner-api.internal answered 429 Too Many Requests on attempt 3/3 (retry-after: 120s); the per-tenant quota window has not rolled over yet","retry_count":3,"created_at":"2026-09-17T14:18:58.804412Z","updated_at":"2026-09-17T14:19:32.804412Z","trace_id":"6f76e48d-72c4-5e5a-a5cf-bc29758167cb","triage":{"root_cause_category":"rate_limited","summary":"All three attempts landed inside one quota window. The partner is shedding load, not rejecting the payload.","suggested_fix":"Let the 120s retry-after window pass before the next attempt. An immediate re-run spends another attempt against the same closed window and comes back 429.","is_retryable":true,"confidence":0.86},"remediation_hint":"wait_and_replay","dead_lettered_at":"2026-09-17T14:19:32.804412Z","fenced_at":null,"fenced_by":null,"extra":null},{"id":"97d91272-9774-5b8e-980b-f0d2fa6ed619","type":"bulk_api_sync","error_message":"send_email downstream call failed: ConnectionRefusedError('smtp.mailer.internal:587')","retry_count":3,"created_at":"2026-09-17T14:03:58.804412Z","updated_at":"2026-09-17T14:04:14.804412Z","trace_id":"1bfc7482-c957-5217-beae-e8cba48a152d","triage":{"root_cause_category":"downstream_unavailable","summary":"SMTP relay unreachable — connection refused at TCP layer.","suggested_fix":"Check smtp.mailer.internal ECS task health; recent billing hotfix (v0.4.2) may have changed VPC egress rules — cross-reference `get_deploy_history`. Re-run once the dependency answers.","is_retryable":true,"confidence":0.82},"remediation_hint":"wait_and_replay","dead_lettered_at":"2026-09-17T14:04:14.804412Z","fenced_at":null,"fenced_by":null,"extra":null}]}
  - get_consumer_lag(consumer_group='worker-dispatcher') -> {"consumer_group":"worker-dispatcher","lag":30,"lag_known":true,"source":"live","cache_key":"kafka:consumer_lag:worker-dispatcher","measured_at":"2026-09-17T14:45:51.151613Z","age_seconds":36,"recent_samples":[{"lag":30,"measured_at":"2026-09-17T14:45:51.151613Z"},{"lag":11,"measured_at":"2026-09-17T14:44:50.630861Z"}]}
  - get_deploy_history(environment=None, limit=20) -> {"total":6,"entries":[{"version":"v0.4.3","revision":"d1e5a83","image_tag":"v0.4.3","deployed_at":"2026-09-17T12:43:58.804416Z","environment":"prod","notes":null},{"version":"v0.4.3","revision":"d1e5a83","image_tag":"v0.4.3","deployed_at":"2026-09-17T11:43:58.804416Z","environment":"staging","notes":"staging soak before prod"},{"version":"v0.4.2","revision":"c9f4d02","image_tag":"v0.4.2","deployed_at":"2026-09-17T08:43:58.804416Z","environment":"prod","notes":"correlated with billing failures"},{"version":"v0.4.1","revision":"b7c3e51","image_tag":"v0.4.1","deployed_at":"2026-09-16T20:43:58.804416Z","environment":"prod","notes":null},{"version":"v0.4.0","revision":"a1940af","image_tag":"v0.4.0","deployed_at":"2026-09-16T16:43:58.804416Z","environment":"prod","notes":null},{"version":"v0.3.9","revision":"e0f6b14","image_tag":"v0.3.9","deployed_at":"2026-09-15T14:43:58.804416Z","environment":"prod","notes":null}],"source":"deploy_markers"}
  - restart_consumer_group(consumer_group='worker-dispatcher', idempotency_key='6ae6b8fc81855f5305293515d4e5fbe6') -> {"consumer_group":"worker-dispatcher","kill_key_cleared":true,"latency_key_cleared":false,"group_recognized":true,"accepted":true}
  - get_consumer_lag(consumer_group='worker-dispatcher', attempt=1, of=6) -> {"consumer_group":"worker-dispatcher","lag":50,"lag_known":true,"source":"live","cache_key":"kafka:consumer_lag:worker-dispatcher","measured_at":"2026-09-17T14:46:51.261672Z","age_seconds":14,"recent_samples":[{"lag":50,"measured_at":"2026-09-17T14:46:51.261672Z"},{"lag":30,"measured_at":"2026-09-17T14:45:51.151613Z"},{"lag":11,"measured_at":"2026-09-17T14:44:50.630861Z"}]}
  - get_consumer_lag(consumer_group='worker-dispatcher', attempt=2, of=6) -> {"consumer_group":"worker-dispatcher","lag":50,"lag_known":true,"source":"live","cache_key":"kafka:consumer_lag:worker-dispatcher","measured_at":"2026-09-17T14:46:51.261672Z","age_seconds":36,"recent_samples":[{"lag":50,"measured_at":"2026-09-17T14:46:51.261672Z"},{"lag":30,"measured_at":"2026-09-17T14:45:51.151613Z"},{"lag":11,"measured_at":"2026-09-17T14:44:50.630861Z"}]}
  - get_consumer_lag(consumer_group='worker-dispatcher', attempt=3, of=6) -> {"consumer_group":"worker-dispatcher","lag":50,"lag_known":true,"source":"live","cache_key":"kafka:consumer_lag:worker-dispatcher","measured_at":"2026-09-17T14:46:51.261672Z","age_seconds":57,"recent_samples":[{"lag":50,"measured_at":"2026-09-17T14:46:51.261672Z"},{"lag":30,"measured_at":"2026-09-17T14:45:51.151613Z"},{"lag":11,"measured_at":"2026-09-17T14:44:50.630861Z"}]}
  - get_consumer_lag(consumer_group='worker-dispatcher', attempt=4, of=6) -> {"consumer_group":"worker-dispatcher","lag":0,"lag_known":true,"source":"live","cache_key":"kafka:consumer_lag:worker-dispatcher","measured_at":"2026-09-17T14:47:51.733018Z","age_seconds":18,"recent_samples":[{"lag":0,"measured_at":"2026-09-17T14:47:51.733018Z"},{"lag":50,"measured_at":"2026-09-17T14:46:51.261672Z"},{"lag":30,"measured_at":"2026-09-17T14:45:51.151613Z"},{"lag":11,"measured_at":"2026-09-17T14:44:50.630861Z"}]}
Findings: A restart_consumer_group was issued for worker-dispatcher (kill_key cleared, latency_key not cleared), and lag subsequently dropped from 50 to 0 at the fourth post-restart poll, indicating the consumer recovered; however, the DLQ still holds 4 unresolved messages: one bulk_api_sync timed out against partner-api.internal (retryable), one rate-limited bulk_api_sync requiring a 120 s wait before replay, one bulk_api_sync with a connection-refused error against smtp.mailer.internal (possibly related to the v0.4.2 VPC egress change noted in deploy history), and one csv_upload with a non-retryable bad-input error requiring uploader notification.
Recommendation: Confirm the consumer lag remains at 0 with a fresh poll, then address the four REMAINING DLQ messages: replay the idempotent bulk_api_sync timeout (fc8d2a03) immediately, wait out the 120 s rate-limit window before replaying af67d1b1, check smtp.mailer.internal ECS task health and VPC egress rules (cross-reference the v0.4.2 deploy) before replaying 97d91272, and notify the uploader for the non-retryable csv_upload f030f975.
~~~~

LABEL: 
REASON: 

---

## 5. `0db6fe722f7c:trace_investigation`

- Run: archive 0db6fe722f7c, live model, 2026-09-17T13:38:24.061282Z
- Scenario: trace_investigation (family traces, outcome failed)
- What the scenario is: A specific job failed and the alert carries its trace_id.
- Ground truth: root cause resource_exhaustion; expected end state escalated; expected actions: none
- Why it is in the packet: newest live briefing in the least-covered slot: outcome failed had 1 pick(s), family traces had 0; first pick for this scenario

The briefing, as the judge sees it:

~~~~text
Incident: 61ba7735-d270-42bf-a891-89d6bbd256a4
Final state: escalated
Alert: source=platform.jobs severity=critical fingerprint=job_failed
Why the run ended: planner stop: The trace shows job 5967d510 (report_gen) failed with 'OOM during PDF generation (200MB report)' after 2 retries. This is a persistent data/input-size problem — the 200MB report payload is too large to process in memory and replaying it will fail identically. The DLQ is empty (two reads confirmed total=0), so there is no dead-letter row to fence or replay. No Tier-1 remediation is available. A human needs to investigate the report generation request: either cap the report size, increase worker memory limits, or split the report into smaller chunks. The alert named job_type=process_payment but the trace resolved to a report_gen job — this mismatch should also be investigated as it may indicate a trace_id collision or misrouted alert.
Investigation trail:
  - get_trace(trace_id='0e24ca29-1d47-57e9-b898-4d79bb6da981', include_audit=True) -> {"trace_id":"0e24ca29-1d47-57e9-b898-4d79bb6da981","jobs":[{"id":"5967d510-0290-5627-a9db-4e29e3a38e44","type":"report_gen","status":"failed","user_id":"ee7c82cb-bab6-5780-8590-55b57064cbe3","retry_count":2,"error_message":"OOM during PDF generation (200MB report)","created_at":"2026-09-17T12:32:13.337884Z","updated_at":"2026-09-17T12:33:52.337884Z"}],"audit_events":[{"action":"job.failed","resource_type":"job","resource_id":"5967d510-0290-5627-a9db-4e29e3a38e44","principal_type":"user","created_at":"2026-09-08T02:16:36.672412Z","extra_data":{"error_message":"OOM during PDF generation (200MB report)"}}],"truncated":false,"total_jobs":1,"total_audit_events":1}
  - list_dlq_messages(job_type='process_payment', remediation_hint=None, limit=50, offset=0) -> {"total":0,"items":[]}
  - list_dlq_messages(job_type='process_payment', remediation_hint=None, limit=50, offset=0) -> {"total":0,"items":[]}
Findings: Job 5967d510 (report_gen) failed with "OOM during PDF generation (200MB report)" after 2 retries — the payload is too large to process in memory and will fail identically on any replay; the DLQ was confirmed empty on two reads so there is no dead-letter entry to fence. Additionally, the alert fired with job_type=process_payment but the trace resolved to a report_gen job, indicating a possible trace_id collision or misrouted alert that remains uninvestigated.
Recommendation: Inspect the original report generation request for user ee7c82cb to determine whether the 200MB payload can be capped, split, or offloaded, and separately verify whether the job_type mismatch (process_payment vs. report_gen) reflects a trace_id collision or alert routing error before taking any remediation action.
~~~~

LABEL: 
REASON: 

---

## 6. `0db6fe722f7c:deploy_correlation`

- Run: archive 0db6fe722f7c, live model, 2026-09-17T13:38:24.061282Z
- Scenario: deploy_correlation (family deploy, outcome escalated)
- What the scenario is: Errors started spiking shortly after a service rollout.
- Ground truth: root cause deploy_regression; expected end state escalated; expected actions: none
- Why it is in the packet: newest live briefing in the least-covered slot: outcome escalated had 1 pick(s), family deploy had 0; first pick for this scenario

The briefing, as the judge sees it:

~~~~text
Incident: 88ce4ef4-92aa-45b6-a76b-1fbe94017faa
Final state: escalated
Alert: source=platform.api severity=critical fingerprint=post_deploy_error_spike
Why the run ended: planner stop: planner emitted remediate for category 'deploy_regression' which has no Tier-1 fix; escalating
Investigation trail:
  - get_deploy_history(environment=None, limit=10) -> {"total":6,"entries":[{"version":"v0.4.3","revision":"d1e5a83","image_tag":"v0.4.3","deployed_at":"2026-09-17T11:17:13.337886Z","environment":"prod","notes":null},{"version":"v0.4.3","revision":"d1e5a83","image_tag":"v0.4.3","deployed_at":"2026-09-17T10:17:13.337886Z","environment":"staging","notes":"staging soak before prod"},{"version":"v0.4.2","revision":"c9f4d02","image_tag":"v0.4.2","deployed_at":"2026-09-17T07:17:13.337886Z","environment":"prod","notes":"correlated with billing failures"},{"version":"v0.4.1","revision":"b7c3e51","image_tag":"v0.4.1","deployed_at":"2026-09-16T19:17:13.337886Z","environment":"prod","notes":null},{"version":"v0.4.0","revision":"a1940af","image_tag":"v0.4.0","deployed_at":"2026-09-16T15:17:13.337886Z","environment":"prod","notes":null},{"version":"v0.3.9","revision":"e0f6b14","image_tag":"v0.3.9","deployed_at":"2026-09-15T13:17:13.337886Z","environment":"prod","notes":null}],"source":"deploy_markers"}
  - search_traces(status='failed', job_type=None, since_hours=2, limit=20) -> {"matches":[{"trace_id":"edeeb994-56d2-53e6-88fd-8af47e695dbc","job_id":"d034fd34-102d-5f61-8d21-a7dbda5ea895","job_type":"doc_analysis","status":"failed","created_at":"2026-09-17T12:47:13.337884Z"},{"trace_id":"0e24ca29-1d47-57e9-b898-4d79bb6da981","job_id":"5967d510-0290-5627-a9db-4e29e3a38e44","job_type":"report_gen","status":"failed","created_at":"2026-09-17T12:32:13.337884Z"}]}
  - get_trace(trace_id='edeeb994-56d2-53e6-88fd-8af47e695dbc', include_audit=True) -> {"trace_id":"edeeb994-56d2-53e6-88fd-8af47e695dbc","jobs":[{"id":"d034fd34-102d-5f61-8d21-a7dbda5ea895","type":"doc_analysis","status":"failed","user_id":"ee7c82cb-bab6-5780-8590-55b57064cbe3","retry_count":2,"error_message":"pdf extraction failed: file is corrupted","created_at":"2026-09-17T12:47:13.337884Z","updated_at":"2026-09-17T12:47:23.337884Z"}],"audit_events":[{"action":"job.failed","resource_type":"job","resource_id":"d034fd34-102d-5f61-8d21-a7dbda5ea895","principal_type":"user","created_at":"2026-09-08T02:16:36.672412Z","extra_data":{"error_message":"pdf extraction failed: file is corrupted"}}],"truncated":false,"total_jobs":1,"total_audit_events":1}
  - list_dlq_messages(job_type=None, remediation_hint=None, limit=50, offset=0) -> {"total":4,"items":[{"id":"fc8d2a03-23b3-5371-9acb-46443c73baa5","type":"bulk_api_sync","error_message":"UpstreamTimeout: bulk_api_sync POST https://partner-api.internal/v2/sync timed out after 30s on attempt 3/3 — the request was never acknowledged, so no downstream write was recorded","retry_count":3,"created_at":"2026-09-17T13:09:13.337884Z","updated_at":"2026-09-17T13:09:19.337884Z","trace_id":"c5ca46c1-f4dd-5021-8be4-130143553096","triage":{"root_cause_category":"upstream_timeout","summary":"partner-api.internal did not answer inside the 30s deadline on any of the three attempts. The payload was accepted by validation and nothing was committed downstream.","suggested_fix":"Re-run the job as it stands: the call is idempotent, no partial write survived the timeout, and a fresh attempt is the entire remedy. Neither the payload nor the producer needs changing.","is_retryable":true,"confidence":0.88},"remediation_hint":"replay_safe","dead_lettered_at":"2026-09-17T13:09:19.337884Z","fenced_at":null,"fenced_by":null,"extra":null},{"id":"f030f975-974e-5ce3-aa6b-444136507d86","type":"csv_upload","error_message":"ValueError: invalid literal for int() with base 10: 'not-a-number' at row 15,382","retry_count":3,"created_at":"2026-09-17T13:05:13.337884Z","updated_at":"2026-09-17T13:05:35.337884Z","trace_id":"89609789-fdcb-5a5a-b826-d7d5d5f9cf37","triage":{"root_cause_category":"bad_input","summary":"Non-numeric value in a supposedly-integer CSV column.","suggested_fix":"Not retryable — data quality issue. Notify the uploader; add a validation step in the CSV importer that fails the whole upload with a clear error rather than half-processing.","is_retryable":false,"confidence":0.94},"remediation_hint":"human_required","dead_lettered_at":"2026-09-17T13:05:35.337884Z","fenced_at":null,"fenced_by":null,"extra":null},{"id":"af67d1b1-13f8-5a2c-8c44-66ec5564597d","type":"bulk_api_sync","error_message":"RateLimited: partner-api.internal answered 429 Too Many Requests on attempt 3/3 (retry-after: 120s); the per-tenant quota window has not rolled over yet","retry_count":3,"created_at":"2026-09-17T12:52:13.337884Z","updated_at":"2026-09-17T12:52:47.337884Z","trace_id":"6f76e48d-72c4-5e5a-a5cf-bc29758167cb","triage":{"root_cause_category":"rate_limited","summary":"All three attempts landed inside one quota window. The partner is shedding load, not rejecting the payload.","suggested_fix":"Let the 120s retry-after window pass before the next attempt. An immediate re-run spends another attempt against the same closed window and comes back 429.","is_retryable":true,"confidence":0.86},"remediation_hint":"wait_and_replay","dead_lettered_at":"2026-09-17T12:52:47.337884Z","fenced_at":null,"fenced_by":null,"extra":null},{"id":"97d91272-9774-5b8e-980b-f0d2fa6ed619","type":"bulk_api_sync","error_message":"send_email downstream call failed: ConnectionRefusedError('smtp.mailer.internal:587')","retry_count":3,"created_at":"2026-09-17T12:37:13.337884Z","updated_at":"2026-09-17T12:37:29.337884Z","trace_id":"1bfc7482-c957-5217-beae-e8cba48a152d","triage":{"root_cause_category":"downstream_unavailable","summary":"SMTP relay unreachable — connection refused at TCP layer.","suggested_fix":"Check smtp.mailer.internal ECS task health; recent billing hotfix (v0.4.2) may have changed VPC egress rules — cross-reference `get_deploy_history`. Re-run once the dependency answers.","is_retryable":true,"confidence":0.82},"remediation_hint":"wait_and_replay","dead_lettered_at":"2026-09-17T12:37:29.337884Z","fenced_at":null,"fenced_by":null,"extra":null}]}
Findings: Deploy v0.4.3 landed in prod at 11:17 UTC today (one hour after a staging soak), correlating with a critical error spike; the preceding v0.4.2 deploy is already noted as correlated with billing failures, and the DLQ shows four unresolved messages — a retryable bulk_api_sync upstream timeout, a non-retryable CSV bad-input requiring uploader notification, a rate-limited bulk_api_sync that needs to wait out a 120 s retry-after window, and a bulk_api_sync whose downstream SMTP relay (smtp.mailer.internal:587) is refusing connections, possibly due to VPC egress changes introduced in v0.4.2. No Tier-1 fix exists for the deploy_regression category, so the planner escalated without attempting remediation.
Recommendation: Start by checking whether v0.4.3 (or the still-suspect v0.4.2 egress changes) broke VPC connectivity to smtp.mailer.internal, then verify the DLQ SMTP job can reach the relay before replaying it; the CSV upload job (DLQ id f030f975) requires a human to notify the uploader and is not retryable.
~~~~

LABEL: 
REASON: 

---

## 7. `0db6fe722f7c:postgres_slow`

- Run: archive 0db6fe722f7c, live model, 2026-09-17T13:38:24.061282Z
- Scenario: postgres_slow (family postgres, outcome failed)
- What the scenario is: API latency alert points at the database.
- Ground truth: root cause db_query_latency; expected end state escalated; expected actions: none
- Why it is in the packet: newest live briefing in the least-covered slot: outcome failed had 2 pick(s), family postgres had 0; first pick for this scenario

The briefing, as the judge sees it:

~~~~text
Incident: 16f9e854-330c-4f74-b021-91b7954c5d8e
Final state: escalated
Alert: source=platform.db severity=critical fingerprint=db_latency_high
Why the run ended: planner stop: Postgres health is fully clean: ping latency 1.6ms, 1 active connection, no errors reported. The alerted signal (db_latency_high) is not present in the current live reading. This appears to be a false positive or a transient spike that has already recovered. No Tier-1 auto-remediation is warranted. Recommend a human reviews the alerting threshold and whether a brief latency spike genuinely occurred — deploy history and trace search could be follow-up steps if the human wants to characterise the event, but there is nothing actionable for the autonomous agent to do.
Investigation trail:
  - get_postgres_health() -> {"ok":true,"ping_latency_ms":1.641,"active_connections":1,"dialect":"postgresql","error":null}
Findings: A single Postgres health probe at investigation time returned clean results: ping latency 1.6 ms, 1 active connection, and no errors, meaning the db_latency_high signal is not present in the live reading and may have been a transient spike; no remediation actions were attempted.
Recommendation: Review the alerting threshold configuration and check historical latency metrics or deploy history around the alert timestamp to determine whether a genuine spike occurred and whether the threshold needs adjustment.
~~~~

LABEL: 
REASON: 

---

## 8. `aeadd5ef3edd:remediate_runaway_saga_success`

- Run: archive aeadd5ef3edd, live model, 2026-09-07T12:53:43.005452Z
- Scenario: remediate_runaway_saga_success (family workflow, outcome resolved)
- What the scenario is: Stuck dependency chain alert.
- Ground truth: root cause runaway_saga; expected end state resolved; expected actions: replay_dlq_by_ids
- Why it is in the packet: newest live briefing in the least-covered slot: outcome resolved had 2 pick(s), family workflow had 0; first pick for this scenario
- Note: archived before probe arguments were recorded, so each call shows `()`; the judge sees it the same way

The briefing, as the judge sees it:

~~~~text
Incident: afb409f9-ce1d-4ccf-b966-38eb1bd27b31
Final state: resolved
Alert: source=platform.dag severity=critical fingerprint=saga_stalled
Investigation trail:
  - get_dag_state() -> {"seed_id":"a2412a54-65f0-5258-95ab-5c168a15df64","nodes":[{"id":"a2412a54-65f0-5258-95ab-5c168a15df64","type":"bulk_api_sync","status":"dead_letter","retry_count":3,"created_at":"2026-09-07T12:52:48.494341Z"},{"id":"dbfb7a0c-cccb-5ae7-b2ac-f386f830a9e9","type":"bulk_api_sync","status":"completed","retry_count":0,"created_at":"2026-09-07T12:52:48.494341Z"},{"id":"3e3bd4c1-21f6-5b84-af0c-0d921ff711ca","type":"bulk_api_sync","status":"waiting","retry_count":0,"created_at":"2026-09-07T12:52:48.494341Z"}],"edges":[{"from_id":"a2412a54-65f0-5258-95ab-5c168a15df64","to_id":"dbfb7a0c-cccb-5ae7-b2ac-f386f830a9e9"},{"from_id":"3e3bd4c1-21f6-5b84-af0c-0d921ff711ca","to_id":"a2412a54-65f0-5258-95ab-5c168a15df64"}],"paused":false,"paused_expires_in_seconds":null,"paused_by":null}
  - list_dlq_messages() -> {"total":4,"items":[{"id":"a2412a54-65f0-5258-95ab-5c168a15df64","type":"bulk_api_sync","error_message":"UpstreamTimeout: bulk_api_sync POST https://partner-api.internal/v2/sync timed out after 30s on attempt 3/3 — the request was never acknowledged, so no downstream write was recorded","retry_count":3,"created_at":"2026-09-07T12:52:48.494341Z","updated_at":"2026-09-07T12:52:48.494341Z","trace_id":"a1fd3e60-03c6-5584-8e24-c82aefa90ae0","triage":null,"remediation_hint":"replay_safe","dead_lettered_at":null,"extra":null},{"id":"fc8d2a03-23b3-5371-9acb-46443c73baa5","type":"bulk_api_sync","error_message":"UpstreamTimeout: bulk_api_sync POST https://partner-api.internal/v2/sync timed out after 30s on attempt 3/3 — the request was never acknowledged, so no downstream write was recorded","retry_count":3,"created_at":"2026-09-07T12:15:23.331322Z","updated_at":"2026-09-07T12:15:29.331322Z","trace_id":"c5ca46c1-f4dd-5021-8be4-130143553096","triage":{"root_cause_category":"upstream_timeout","summary":"partner-api.internal did not answer inside the 30s deadline on any of the three attempts. The payload was accepted by validation and nothing was committed downstream.","suggested_fix":"Re-run the job as it stands: the call is idempotent, no partial write survived the timeout, and a fresh attempt is the entire remedy. Neither the payload nor the producer needs changing.","is_retryable":true,"confidence":0.88},"remediation_hint":"replay_safe","dead_lettered_at":"2026-09-07T12:15:29.331322Z","extra":null},{"id":"af67d1b1-13f8-5a2c-8c44-66ec5564597d","type":"bulk_api_sync","error_message":"RateLimited: partner-api.internal answered 429 Too Many Requests on attempt 3/3 (retry-after: 120s); the per-tenant quota window has not rolled over yet","retry_count":3,"created_at":"2026-09-07T11:58:23.331322Z","updated_at":"2026-09-07T11:58:57.331322Z","trace_id":"6f76e48d-72c4-5e5a-a5cf-bc29758167cb","triage":{"root_cause_category":"rate_limited","summary":"All three attempts landed inside one quota window. The partner is shedding load, not rejecting the payload.","suggested_fix":"Let the 120s retry-after window pass before the next attempt. An immediate re-run spends another attempt against the same closed window and comes back 429.","is_retryable":true,"confidence":0.86},"remediation_hint":"wait_and_replay","dead_lettered_at":"2026-09-07T11:58:57.331322Z","extra":null},{"id":"97d91272-9774-5b8e-980b-f0d2fa6ed619","type":"bulk_api_sync","error_message":"send_email downstream call failed: ConnectionRefusedError('smtp.mailer.internal:587')","retry_count":3,"created_at":"2026-09-07T11:43:23.331322Z","updated_at":"2026-09-07T11:43:39.331322Z","trace_id":"1bfc7482-c957-5217-beae-e8cba48a152d","triage":{"root_cause_category":"downstream_unavailable","summary":"SMTP relay unreachable — connection refused at TCP layer.","suggested_fix":"Check smtp.mailer.internal ECS task health; recent billing hotfix (v0.4.2) may have changed VPC egress rules — cross-reference `get_deploy_history`. Re-run once the dependency answers.","is_retryable":true,"confidence":0.82},"remediation_hint":"wait_and_replay","dead_lettered_at":"2026-09-07T11:43:39.331322Z","extra":null}]}
  - replay_dlq_by_ids() -> {"requested":1,"replayed":1,"scheduled":0,"failed":0,"results":[{"id":"a2412a54-65f0-5258-95ab-5c168a15df64","ok":true,"error":null,"scheduled":false,"execute_at":null}]}
  - get_dag_state() -> {"seed_id":"a2412a54-65f0-5258-95ab-5c168a15df64","nodes":[{"id":"a2412a54-65f0-5258-95ab-5c168a15df64","type":"bulk_api_sync","status":"pending","retry_count":0,"created_at":"2026-09-07T12:52:48.494341Z"},{"id":"dbfb7a0c-cccb-5ae7-b2ac-f386f830a9e9","type":"bulk_api_sync","status":"completed","retry_count":0,"created_at":"2026-09-07T12:52:48.494341Z"},{"id":"3e3bd4c1-21f6-5b84-af0c-0d921ff711ca","type":"bulk_api_sync","status":"waiting","retry_count":0,"created_at":"2026-09-07T12:52:48.494341Z"}],"edges":[{"from_id":"a2412a54-65f0-5258-95ab-5c168a15df64","to_id":"dbfb7a0c-cccb-5ae7-b2ac-f386f830a9e9"},{"from_id":"3e3bd4c1-21f6-5b84-af0c-0d921ff711ca","to_id":"a2412a54-65f0-5258-95ab-5c168a15df64"}],"paused":false,"paused_expires_in_seconds":null,"paused_by":null}
Findings: The stalled saga was caused by node a2412a54 entering dead_letter status after 3 consecutive UpstreamTimeout failures against partner-api.internal/v2/sync; a DLQ replay was successfully executed (1/1 replayed, 0 failed), and a subsequent DAG state check confirmed the node transitioned from dead_letter to pending with retry_count reset to 0. Three additional unrelated DLQ messages remain (upstream timeouts, a rate-limit hit, and an SMTP connection refusal) and have not been replayed.
Recommendation: Confirm that node a2412a54 has progressed beyond pending to completed in the DAG, then assess the three remaining DLQ items — particularly the SMTP connection refusal on smtp.mailer.internal:587, which may indicate a VPC egress or service health issue independent of this incident.
~~~~

LABEL: 
REASON: 

---

## 9. `cde5a14485c3:incidents_overview`

- Run: archive cde5a14485c3, live model, 2026-08-31T04:01:40.449164Z
- Scenario: incidents_overview (family incidents, outcome escalated)
- What the scenario is: Paging alert with unclear scope.
- Ground truth: root cause unknown; expected end state escalated; expected actions: none
- Why it is in the packet: newest live briefing in the least-covered slot: outcome escalated had 2 pick(s), family incidents had 0; first pick for this scenario
- Note: archived before probe arguments were recorded, so each call shows `()`; the judge sees it the same way

The briefing, as the judge sees it:

~~~~text
Incident: 1e5e2528-3c2e-45ae-9a54-6fcb8a7b6d7f
Final state: escalated
Alert: source=platform.api severity=critical fingerprint=elevated_error_rate
Why the run ended: remediation tool error (replay_dlq_by_category): MCP error -32002: missing required scope: actions:execute
Tier-1 action ALREADY ATTEMPTED (do not recommend repeating it without checking its effect first): replay_dlq_by_category {'category': 'replay_safe', 'job_type': None, 'max_replays': 20, 'idempotency_key': 'ffba9b60532d9086af7d8bf5f46c1c28', 'delay_seconds': None}
Investigation trail:
  - list_active_alerts() -> {"total":3,"alerts":[{"id":"6e89c71e-f296-4ab2-9d2c-28582192a889","severity":"critical","source":"slo:job_completion_rate","title":"SLO fast burn: Job completion rate","description":"Job completion rate is burning error budget at 57.1× the sustainable rate (4 failed of 7 in the last 24h, 42.9% against a 99% target). Budget remaining: -100%. Runbook: rb-slo-job-completion.","fired_at":"2026-08-31T03:52:05.413263Z","extra_data":{"total":7,"failed":4,"slo_id":"job_completion_rate","target":0.99,"current":0.428571,"burn_rate":57.143,"threshold":14.4,"runbook_id":"rb-slo-job-completion","window_hours":24,"budget_remaining_pct":-100.0}},{"id":"7c9d648f-0215-544d-a9c3-adcf74ea68ab","severity":"critical","source":"api","title":"5xx rate above threshold","description":"API 5xx rate 3.2% over last 10 minutes.","fired_at":"2026-08-31T03:39:04.936974Z","extra_data":null},{"id":"e33d75bb-3769-594a-9443-57abff0f7580","severity":"critical","source":"kafka","title":"billing-consumer lag exceeds 10k","description":"Sustained lag on billing-consumer for 20+ minutes.","fired_at":"2026-08-31T03:22:04.936974Z","extra_data":null}]}
  - get_consumer_lag() -> {"consumer_group":"billing-consumer","lag":15000,"lag_known":true,"source":"static","cache_key":"kafka:consumer_lag:billing-consumer"}
  - list_dlq_messages() -> {"total":4,"items":[{"id":"fc8d2a03-23b3-5371-9acb-46443c73baa5","type":"bulk_api_sync","error_message":"SchemaValidationError: payload missing required field 'user_id' (received keys: ['tenant_id', 'action', 'ts'])","retry_count":3,"created_at":"2026-08-31T03:39:04.942319Z","updated_at":"2026-08-31T03:39:10.942319Z","trace_id":"c5ca46c1-f4dd-5021-8be4-130143553096","triage":{"root_cause_category":"schema_violation","summary":"Producer sent a payload missing user_id — schema rejected it three times.","suggested_fix":"Fix the producer to include user_id, then replay the DLQ entry. Backwards-compatible producer fix + replay is the standard remediation.","is_retryable":true,"confidence":0.91},"remediation_hint":"replay_safe","dead_lettered_at":"2026-08-31T03:39:10.942319Z","extra":null},{"id":"f030f975-974e-5ce3-aa6b-444136507d86","type":"csv_upload","error_message":"ValueError: invalid literal for int() with base 10: 'not-a-number' at row 15,382","retry_count":3,"created_at":"2026-08-31T03:35:04.942319Z","updated_at":"2026-08-31T03:35:26.942319Z","trace_id":"89609789-fdcb-5a5a-b826-d7d5d5f9cf37","triage":{"root_cause_category":"bad_input","summary":"Non-numeric value in a supposedly-integer CSV column.","suggested_fix":"Not retryable — data quality issue. Notify the uploader; add a validation step in the CSV importer that fails the whole upload with a clear error rather than half-processing.","is_retryable":false,"confidence":0.94},"remediation_hint":"human_required","dead_lettered_at":"2026-08-31T03:35:26.942319Z","extra":null},{"id":"af67d1b1-13f8-5a2c-8c44-66ec5564597d","type":"bulk_api_sync","error_message":"process_payment call timed out after 30s: TimeoutError('stripe.api')","retry_count":3,"created_at":"2026-08-31T03:22:04.942319Z","updated_at":"2026-08-31T03:22:38.942319Z","trace_id":"6f76e48d-72c4-5e5a-a5cf-bc29758167cb","triage":{"root_cause_category":"third_party_timeout","summary":"Stripe API exceeded 30s deadline three consecutive attempts.","suggested_fix":"Verify Stripe status page; if green, raise the per-call timeout via the retry policy for payment-type jobs and replay the DLQ batch.","is_retryable":true,"confidence":0.71},"remediation_hint":"wait_and_replay","dead_lettered_at":"2026-08-31T03:22:38.942319Z","extra":null},{"id":"97d91272-9774-5b8e-980b-f0d2fa6ed619","type":"bulk_api_sync","error_message":"send_email downstream call failed: ConnectionRefusedError('smtp.mailer.internal:587')","retry_count":3,"created_at":"2026-08-31T03:07:04.942319Z","updated_at":"2026-08-31T03:07:20.942319Z","trace_id":"1bfc7482-c957-5217-beae-e8cba48a152d","triage":{"root_cause_category":"downstream_unavailable","summary":"SMTP relay unreachable — connection refused at TCP layer.","suggested_fix":"Check smtp.mailer.internal ECS task health; recent billing hotfix (v0.4.2) may have changed VPC egress rules — cross-reference `get_deploy_history`. Replay once the dependency recovers.","is_retryable":true,"confidence":0.82},"remediation_hint":"wait_and_replay","dead_lettered_at":"2026-08-31T03:07:20.942319Z","extra":null}]}
  - get_deploy_history() -> {"total":6,"entries":[{"version":"v0.4.3","revision":"d1e5a83","image_tag":"v0.4.3","deployed_at":"2026-08-31T01:47:04.930453Z","environment":"prod","notes":null},{"version":"v0.4.3","revision":"d1e5a83","image_tag":"v0.4.3","deployed_at":"2026-08-31T00:47:04.930453Z","environment":"staging","notes":"staging soak before prod"},{"version":"v0.4.2","revision":"c9f4d02","image_tag":"v0.4.2","deployed_at":"2026-08-30T21:47:04.930453Z","environment":"prod","notes":"correlated with billing failures"},{"version":"v0.4.1","revision":"b7c3e51","image_tag":"v0.4.1","deployed_at":"2026-08-30T09:47:04.930453Z","environment":"prod","notes":null},{"version":"v0.4.0","revision":"a1940af","image_tag":"v0.4.0","deployed_at":"2026-08-30T05:47:04.930453Z","environment":"prod","notes":null},{"version":"v0.3.9","revision":"e0f6b14","image_tag":"v0.3.9","deployed_at":"2026-08-29T03:47:04.930453Z","environment":"prod","notes":null}],"source":"deploy_markers"}
Findings: Three critical alerts are active — a job completion SLO burning at 57× with budget fully exhausted, a 3.2% API 5xx rate, and sustained billing-consumer Kafka lag at 15,000 — with 4 DLQ messages spanning a schema violation, a bad-input CSV (non-retryable), a Stripe timeout, and an SMTP connection refusal on smtp.mailer.internal:587 that may be tied to the v0.4.2 deploy (noted as "correlated with billing failures" before v0.4.3 was promoted to prod at 01:47 UTC); a DLQ replay of the `replay_safe` category was attempted but failed with MCP error -32002 (missing required scope: actions:execute), so no messages were actually replayed.
Recommendation: First verify whether the scope error blocked the replay entirely (no messages should have moved) and check smtp.mailer.internal ECS task health and VPC egress rules for any regression introduced by v0.4.2/v0.4.3 before authorising a replay through a credentialed session with the actions:execute scope.
~~~~

LABEL: 
REASON: 

---

## 10. `0db6fe722f7c:redis_saturation`

- Run: archive 0db6fe722f7c, live model, 2026-09-17T13:38:24.061282Z
- Scenario: redis_saturation (family cache_redis, outcome failed)
- What the scenario is: Cache miss spike alert.
- Ground truth: root cause redis_saturation; expected end state escalated; expected actions: none
- Why it is in the packet: newest live briefing in the least-covered slot: outcome failed had 3 pick(s), family cache_redis had 1; first pick for this scenario

The briefing, as the judge sees it:

~~~~text
Incident: dd2f8753-c72d-419a-82ae-3afd1c50ef41
Final state: escalated
Alert: source=platform.cache severity=critical fingerprint=cache_miss_spike
Why the run ended: insufficient tool-call budget for action+verify (remaining=1); escalating without executing
Investigation trail:
  - get_redis_health() -> {"ok":true,"ping_latency_ms":0.903,"connected_clients":11,"used_memory_bytes":1692176,"used_memory_human":"1.61M","keyspace_hits":6020,"keyspace_misses":991365,"error":null}
  - list_active_alerts(severity=None, limit=50) -> {"total":3,"alerts":[{"id":"7c9d648f-0215-544d-a9c3-adcf74ea68ab","severity":"critical","source":"api","title":"5xx rate above threshold","description":"API 5xx rate 3.2% over last 10 minutes.","fired_at":"2026-09-17T13:09:13.342757Z","extra_data":null},{"id":"c6c4e180-f625-53c2-85c2-2d505fec61e5","severity":"warning","source":"dlq","title":"DLQ backlog growing","description":"3 send_email jobs dead-lettered in last hour.","fired_at":"2026-09-17T13:02:13.342757Z","extra_data":null},{"id":"e33d75bb-3769-594a-9443-57abff0f7580","severity":"critical","source":"kafka","title":"billing-consumer lag exceeds 10k","description":"Sustained lag on billing-consumer for 20+ minutes.","fired_at":"2026-09-17T12:52:13.342757Z","extra_data":null}]}
  - get_deploy_history(environment=None, limit=5) -> {"total":6,"entries":[{"version":"v0.4.3","revision":"d1e5a83","image_tag":"v0.4.3","deployed_at":"2026-09-17T11:17:13.337886Z","environment":"prod","notes":null},{"version":"v0.4.3","revision":"d1e5a83","image_tag":"v0.4.3","deployed_at":"2026-09-17T10:17:13.337886Z","environment":"staging","notes":"staging soak before prod"},{"version":"v0.4.2","revision":"c9f4d02","image_tag":"v0.4.2","deployed_at":"2026-09-17T07:17:13.337886Z","environment":"prod","notes":"correlated with billing failures"},{"version":"v0.4.1","revision":"b7c3e51","image_tag":"v0.4.1","deployed_at":"2026-09-16T19:17:13.337886Z","environment":"prod","notes":null},{"version":"v0.4.0","revision":"a1940af","image_tag":"v0.4.0","deployed_at":"2026-09-16T15:17:13.337886Z","environment":"prod","notes":null}],"source":"deploy_markers"}
  - get_cache_key_info(key='cache:hot') -> {"key":"cache:hot","exists":false,"type":null,"ttl_seconds":null,"size":null}
Findings: Redis is healthy and responsive (ping 0.9ms, 11 clients), but the keyspace shows a severe miss-to-hit imbalance (991,365 misses vs 6,020 hits), and the critical hot-key `cache:hot` does not exist in the cache; a prod deploy of v0.4.3 occurred roughly two hours before escalation, coinciding with the miss spike, and no remediation action was attempted as the run exhausted its budget before acting.
Recommendation: Confirm whether the v0.4.3 deploy (11:17 UTC) introduced a change that invalidated or stopped populating `cache:hot`, and check application logs around that deploy time for eviction or key-naming changes before deciding whether a cache warm-up or rollback is warranted.
~~~~

LABEL: 
REASON: 

---

## 11. `0db6fe722f7c:noise_low_severity`

- Run: archive 0db6fe722f7c, live model, 2026-09-17T13:38:24.061282Z
- Scenario: noise_low_severity (family noise_control, outcome escalated)
- What the scenario is: Low-severity signal that shouldn't consume the tool budget.
- Ground truth: no root cause declared; expected end state escalated; expected actions: none
- Why it is in the packet: newest live briefing in the least-covered slot: outcome escalated had 3 pick(s), family noise_control had 1; first pick for this scenario

The briefing, as the judge sees it:

~~~~text
Incident: d758186e-48b4-4593-bb41-f906ee5787df
Final state: escalated
Alert: source=platform.canary severity=low fingerprint=canary_blip group=billing-consumer
Why the run ended: severity=low classified as escalated
No probes were run before escalation.
Findings: No probes were run before escalation — the incident was classified as escalated immediately upon receipt because the alert carried severity=low, and the automated run ended without any investigation of the billing-consumer canary signal.
Recommendation: Review the raw alert directly (fingerprint: canary_blip, group: billing-consumer, source: platform.canary) to determine whether this canary blip reflects a genuine billing-consumer degradation or a transient flap warranting a severity adjustment.
~~~~

LABEL: 
REASON: 

---

## 12. `9949c45145d4:dlq_wait_and_replay_success`

- Run: archive 9949c45145d4, live model, 2026-09-08T00:53:33.243698Z
- Scenario: dlq_wait_and_replay_success (family dlq, outcome resolved)
- What the scenario is: The alert names its category (remediation_hint: wait_and_replay), so this incident IS that slice: two rows, one action, and the replay_safe and human_required rows are context for the briefing rather than part of the subject.
- Ground truth: root cause poison_message; expected end state resolved; expected actions: replay_dlq_by_ids, replay_dlq_by_category
- Why it is in the packet: newest live briefing in the least-covered slot: outcome resolved had 3 pick(s), family dlq had 1; first pick for this scenario
- Note: archived before probe arguments were recorded, so each call shows `()`; the judge sees it the same way

The briefing, as the judge sees it:

~~~~text
Incident: 6355df9d-c87c-4f68-bb67-d40f62480dc2
Final state: resolved
Alert: source=platform.dlq severity=critical fingerprint=dlq_depth_warning_wait_replay
Investigation trail:
  - list_dlq_messages() -> {"total":2,"items":[{"id":"af67d1b1-13f8-5a2c-8c44-66ec5564597d","type":"bulk_api_sync","error_message":"RateLimited: partner-api.internal answered 429 Too Many Requests on attempt 3/3 (retry-after: 120s); the per-tenant quota window has not rolled over yet","retry_count":3,"created_at":"2026-09-08T00:25:39.512809Z","updated_at":"2026-09-08T00:26:13.512809Z","trace_id":"6f76e48d-72c4-5e5a-a5cf-bc29758167cb","triage":{"root_cause_category":"rate_limited","summary":"All three attempts landed inside one quota window. The partner is shedding load, not rejecting the payload.","suggested_fix":"Let the 120s retry-after window pass before the next attempt. An immediate re-run spends another attempt against the same closed window and comes back 429.","is_retryable":true,"confidence":0.86},"remediation_hint":"wait_and_replay","dead_lettered_at":"2026-09-08T00:26:13.512809Z","extra":null},{"id":"97d91272-9774-5b8e-980b-f0d2fa6ed619","type":"bulk_api_sync","error_message":"send_email downstream call failed: ConnectionRefusedError('smtp.mailer.internal:587')","retry_count":3,"created_at":"2026-09-08T00:10:39.512809Z","updated_at":"2026-09-08T00:10:55.512809Z","trace_id":"1bfc7482-c957-5217-beae-e8cba48a152d","triage":{"root_cause_category":"downstream_unavailable","summary":"SMTP relay unreachable — connection refused at TCP layer.","suggested_fix":"Check smtp.mailer.internal ECS task health; recent billing hotfix (v0.4.2) may have changed VPC egress rules — cross-reference `get_deploy_history`. Re-run once the dependency answers.","is_retryable":true,"confidence":0.82},"remediation_hint":"wait_and_replay","dead_lettered_at":"2026-09-08T00:10:55.512809Z","extra":null}]}
  - replay_dlq_by_ids() -> {"requested":2,"replayed":0,"scheduled":2,"failed":0,"results":[{"id":"af67d1b1-13f8-5a2c-8c44-66ec5564597d","ok":true,"error":null,"scheduled":true,"execute_at":1788829102.3089945},{"id":"97d91272-9774-5b8e-980b-f0d2fa6ed619","ok":true,"error":null,"scheduled":true,"execute_at":1788829102.3152392}]}
  - list_dlq_messages() -> {"total":2,"items":[{"id":"af67d1b1-13f8-5a2c-8c44-66ec5564597d","type":"bulk_api_sync","error_message":"RateLimited: partner-api.internal answered 429 Too Many Requests on attempt 3/3 (retry-after: 120s); the per-tenant quota window has not rolled over yet","retry_count":3,"created_at":"2026-09-08T00:25:39.512809Z","updated_at":"2026-09-08T00:26:13.512809Z","trace_id":"6f76e48d-72c4-5e5a-a5cf-bc29758167cb","triage":{"root_cause_category":"rate_limited","summary":"All three attempts landed inside one quota window. The partner is shedding load, not rejecting the payload.","suggested_fix":"Let the 120s retry-after window pass before the next attempt. An immediate re-run spends another attempt against the same closed window and comes back 429.","is_retryable":true,"confidence":0.86},"remediation_hint":"wait_and_replay","dead_lettered_at":"2026-09-08T00:26:13.512809Z","extra":null},{"id":"97d91272-9774-5b8e-980b-f0d2fa6ed619","type":"bulk_api_sync","error_message":"send_email downstream call failed: ConnectionRefusedError('smtp.mailer.internal:587')","retry_count":3,"created_at":"2026-09-08T00:10:39.512809Z","updated_at":"2026-09-08T00:10:55.512809Z","trace_id":"1bfc7482-c957-5217-beae-e8cba48a152d","triage":{"root_cause_category":"downstream_unavailable","summary":"SMTP relay unreachable — connection refused at TCP layer.","suggested_fix":"Check smtp.mailer.internal ECS task health; recent billing hotfix (v0.4.2) may have changed VPC egress rules — cross-reference `get_deploy_history`. Re-run once the dependency answers.","is_retryable":true,"confidence":0.82},"remediation_hint":"wait_and_replay","dead_lettered_at":"2026-09-08T00:10:55.512809Z","extra":null}]}
Findings: Two DLQ messages were found and a replay was scheduled for both (af67d1b1: rate-limited by partner-api.internal with a 120s retry-after window; 97d91272: SMTP connection refused at smtp.mailer.internal:587), but a follow-up list_dlq_messages confirmed both messages are still present in the DLQ, indicating the scheduled replays have not yet cleared them.
Recommendation: Verify whether the scheduled replay executions have now fired by re-checking DLQ depth; for the SMTP message specifically, confirm smtp.mailer.internal ECS task health and check whether the v0.4.2 billing hotfix altered VPC egress rules before the replay runs.
~~~~

LABEL: 
REASON: 

---

## 13. `0db6fe722f7c:multi_probe_hypothesis_evolution`

- Run: archive 0db6fe722f7c, live model, 2026-09-17T13:38:24.061282Z
- Scenario: multi_probe_hypothesis_evolution (family consumer_lag, outcome escalated)
- What the scenario is: Two probes: first with modest confidence, second refines to a specific hypothesis.
- Ground truth: root cause consumer_saturation; expected end state escalated; expected actions: none
- Why it is in the packet: newest live briefing in the least-covered slot: outcome escalated had 4 pick(s), family consumer_lag had 1; first pick for this scenario

The briefing, as the judge sees it:

~~~~text
Incident: 325b27cf-c706-46c3-911d-a25fc179a75e
Final state: escalated
Alert: source=platform.kafka severity=critical fingerprint=consumer_lag_high group=payments-consumer
Why the run ended: remediation tool error (restart_consumer_group): MCP error -32002: missing required scope: actions:execute
Tier-1 action ALREADY ATTEMPTED (do not recommend repeating it without checking its effect first): restart_consumer_group {'consumer_group': 'payments-consumer', 'idempotency_key': 'df80ebe13726b096d83cb377a1e3e75d'}
Investigation trail:
  - get_consumer_lag(consumer_group='payments-consumer') -> {"consumer_group":"payments-consumer","lag":30000,"lag_known":true,"source":"static","cache_key":"kafka:consumer_lag:payments-consumer","measured_at":null,"age_seconds":null,"recent_samples":[]}
  - get_deploy_history(environment=None, limit=5) -> {"total":6,"entries":[{"version":"v0.4.3","revision":"d1e5a83","image_tag":"v0.4.3","deployed_at":"2026-09-17T11:17:13.337886Z","environment":"prod","notes":null},{"version":"v0.4.3","revision":"d1e5a83","image_tag":"v0.4.3","deployed_at":"2026-09-17T10:17:13.337886Z","environment":"staging","notes":"staging soak before prod"},{"version":"v0.4.2","revision":"c9f4d02","image_tag":"v0.4.2","deployed_at":"2026-09-17T07:17:13.337886Z","environment":"prod","notes":"correlated with billing failures"},{"version":"v0.4.1","revision":"b7c3e51","image_tag":"v0.4.1","deployed_at":"2026-09-16T19:17:13.337886Z","environment":"prod","notes":null},{"version":"v0.4.0","revision":"a1940af","image_tag":"v0.4.0","deployed_at":"2026-09-16T15:17:13.337886Z","environment":"prod","notes":null}],"source":"deploy_markers"}
  - list_dlq_messages(job_type=None, remediation_hint=None, limit=50, offset=0) -> {"total":4,"items":[{"id":"fc8d2a03-23b3-5371-9acb-46443c73baa5","type":"bulk_api_sync","error_message":"UpstreamTimeout: bulk_api_sync POST https://partner-api.internal/v2/sync timed out after 30s on attempt 3/3 — the request was never acknowledged, so no downstream write was recorded","retry_count":3,"created_at":"2026-09-17T13:09:13.337884Z","updated_at":"2026-09-17T13:09:19.337884Z","trace_id":"c5ca46c1-f4dd-5021-8be4-130143553096","triage":{"root_cause_category":"upstream_timeout","summary":"partner-api.internal did not answer inside the 30s deadline on any of the three attempts. The payload was accepted by validation and nothing was committed downstream.","suggested_fix":"Re-run the job as it stands: the call is idempotent, no partial write survived the timeout, and a fresh attempt is the entire remedy. Neither the payload nor the producer needs changing.","is_retryable":true,"confidence":0.88},"remediation_hint":"replay_safe","dead_lettered_at":"2026-09-17T13:09:19.337884Z","fenced_at":null,"fenced_by":null,"extra":null},{"id":"f030f975-974e-5ce3-aa6b-444136507d86","type":"csv_upload","error_message":"ValueError: invalid literal for int() with base 10: 'not-a-number' at row 15,382","retry_count":3,"created_at":"2026-09-17T13:05:13.337884Z","updated_at":"2026-09-17T13:05:35.337884Z","trace_id":"89609789-fdcb-5a5a-b826-d7d5d5f9cf37","triage":{"root_cause_category":"bad_input","summary":"Non-numeric value in a supposedly-integer CSV column.","suggested_fix":"Not retryable — data quality issue. Notify the uploader; add a validation step in the CSV importer that fails the whole upload with a clear error rather than half-processing.","is_retryable":false,"confidence":0.94},"remediation_hint":"human_required","dead_lettered_at":"2026-09-17T13:05:35.337884Z","fenced_at":null,"fenced_by":null,"extra":null},{"id":"af67d1b1-13f8-5a2c-8c44-66ec5564597d","type":"bulk_api_sync","error_message":"RateLimited: partner-api.internal answered 429 Too Many Requests on attempt 3/3 (retry-after: 120s); the per-tenant quota window has not rolled over yet","retry_count":3,"created_at":"2026-09-17T12:52:13.337884Z","updated_at":"2026-09-17T12:52:47.337884Z","trace_id":"6f76e48d-72c4-5e5a-a5cf-bc29758167cb","triage":{"root_cause_category":"rate_limited","summary":"All three attempts landed inside one quota window. The partner is shedding load, not rejecting the payload.","suggested_fix":"Let the 120s retry-after window pass before the next attempt. An immediate re-run spends another attempt against the same closed window and comes back 429.","is_retryable":true,"confidence":0.86},"remediation_hint":"wait_and_replay","dead_lettered_at":"2026-09-17T12:52:47.337884Z","fenced_at":null,"fenced_by":null,"extra":null},{"id":"97d91272-9774-5b8e-980b-f0d2fa6ed619","type":"bulk_api_sync","error_message":"send_email downstream call failed: ConnectionRefusedError('smtp.mailer.internal:587')","retry_count":3,"created_at":"2026-09-17T12:37:13.337884Z","updated_at":"2026-09-17T12:37:29.337884Z","trace_id":"1bfc7482-c957-5217-beae-e8cba48a152d","triage":{"root_cause_category":"downstream_unavailable","summary":"SMTP relay unreachable — connection refused at TCP layer.","suggested_fix":"Check smtp.mailer.internal ECS task health; recent billing hotfix (v0.4.2) may have changed VPC egress rules — cross-reference `get_deploy_history`. Re-run once the dependency answers.","is_retryable":true,"confidence":0.82},"remediation_hint":"wait_and_replay","dead_lettered_at":"2026-09-17T12:37:29.337884Z","fenced_at":null,"fenced_by":null,"extra":null}]}
Findings: The payments-consumer group has a confirmed lag of 30,000 messages (cached reading, timestamp unknown); a restart_consumer_group action was already attempted but failed with MCP error -32002 (missing required scope: actions:execute), so it is uncertain whether the restart took effect. The v0.4.2 deploy earlier today is noted as correlated with billing failures, and the DLQ contains SMTP connectivity errors pointing to smtp.mailer.internal as potentially unreachable, possibly linked to that same deployment.
Recommendation: First, verify whether the consumer group is actually running and consuming (check pod/task health and current live lag) to determine if the failed restart attempt had any effect, then confirm the actions:execute scope is granted before attempting any further restart through the tier-2 approval flow.
~~~~

LABEL: 
REASON: 

---

## 14. `0db6fe722f7c:failed_traces_scan`

- Run: archive 0db6fe722f7c, live model, 2026-09-17T13:38:24.061282Z
- Scenario: failed_traces_scan (family traces, outcome failed)
- What the scenario is: Alert says "failure rate elevated" without a specific trace.
- Ground truth: root cause unknown; expected end state escalated; expected actions: none
- Why it is in the packet: newest live briefing in the least-covered slot: outcome failed had 4 pick(s), family traces had 1; first pick for this scenario

The briefing, as the judge sees it:

~~~~text
Incident: ba915e1f-a20b-4767-8a16-6378f41a0b88
Final state: escalated
Alert: source=platform.jobs severity=critical fingerprint=failure_rate_high
Why the run ended: planner stop: Two recently failed jobs have been identified and investigated. Job d034fd34-102d-5f61-8d21-a7dbda5ea895 (doc_analysis) failed with 'pdf extraction failed: file is corrupted' after 2 retries — a permanent bad-input fault with no Tier-1 fix. Job 5967d510-0290-5627-a9db-4e29e3a38e44 (report_gen) failed with 'OOM during PDF generation (200MB report)' after 2 retries — a resource/payload-size issue also not auto-remediable. Both jobs belong to the same user (ee7c82cb-bab6-5780-8590-55b57064cbe3) and represent distinct permanent data/input problems across two job types. Neither has a DLQ row (no dead-letter status observed via trace). No Tier-1 remediation is applicable; escalate to a human with: (1) corrupted PDF input for doc_analysis job d034fd34, (2) oversized 200MB report payload for report_gen job 5967d510, both from user ee7c82cb. The human should investigate whether this user is submitting bad/oversized inputs and whether additional job types are affected.
Investigation trail:
  - search_traces(status='failed', job_type=None, since_hours=2, limit=50) -> {"matches":[{"trace_id":"edeeb994-56d2-53e6-88fd-8af47e695dbc","job_id":"d034fd34-102d-5f61-8d21-a7dbda5ea895","job_type":"doc_analysis","status":"failed","created_at":"2026-09-17T12:47:13.337884Z"},{"trace_id":"0e24ca29-1d47-57e9-b898-4d79bb6da981","job_id":"5967d510-0290-5627-a9db-4e29e3a38e44","job_type":"report_gen","status":"failed","created_at":"2026-09-17T12:32:13.337884Z"}]}
  - get_trace(trace_id='edeeb994-56d2-53e6-88fd-8af47e695dbc', include_audit=True) -> {"trace_id":"edeeb994-56d2-53e6-88fd-8af47e695dbc","jobs":[{"id":"d034fd34-102d-5f61-8d21-a7dbda5ea895","type":"doc_analysis","status":"failed","user_id":"ee7c82cb-bab6-5780-8590-55b57064cbe3","retry_count":2,"error_message":"pdf extraction failed: file is corrupted","created_at":"2026-09-17T12:47:13.337884Z","updated_at":"2026-09-17T12:47:23.337884Z"}],"audit_events":[{"action":"job.failed","resource_type":"job","resource_id":"d034fd34-102d-5f61-8d21-a7dbda5ea895","principal_type":"user","created_at":"2026-09-08T02:16:36.672412Z","extra_data":{"error_message":"pdf extraction failed: file is corrupted"}}],"truncated":false,"total_jobs":1,"total_audit_events":1}
  - get_trace(trace_id='0e24ca29-1d47-57e9-b898-4d79bb6da981', include_audit=True) -> {"trace_id":"0e24ca29-1d47-57e9-b898-4d79bb6da981","jobs":[{"id":"5967d510-0290-5627-a9db-4e29e3a38e44","type":"report_gen","status":"failed","user_id":"ee7c82cb-bab6-5780-8590-55b57064cbe3","retry_count":2,"error_message":"OOM during PDF generation (200MB report)","created_at":"2026-09-17T12:32:13.337884Z","updated_at":"2026-09-17T12:33:52.337884Z"}],"audit_events":[{"action":"job.failed","resource_type":"job","resource_id":"5967d510-0290-5627-a9db-4e29e3a38e44","principal_type":"user","created_at":"2026-09-08T02:16:36.672412Z","extra_data":{"error_message":"OOM during PDF generation (200MB report)"}}],"truncated":false,"total_jobs":1,"total_audit_events":1}
Findings: Two failed jobs were traced for user ee7c82cb-bab6-5780-8590-55b57064cbe3: doc_analysis job d034fd34 failed after 2 retries with "pdf extraction failed: file is corrupted," and report_gen job 5967d510 failed after 2 retries with "OOM during PDF generation (200MB report)" — both are permanent input/data faults with no applicable Tier-1 remediation and no dead-letter queue entries observed.
Recommendation: Check whether user ee7c82cb is consistently submitting corrupted PDF inputs and oversized report payloads across other job types, and determine whether the source files need to be rejected or the user notified before additional jobs are queued.
~~~~

LABEL: 
REASON: 

---

## 15. `3c65c04326d4:dlq_replay_safe_success`

- Run: archive 3c65c04326d4, live model, 2026-09-07T14:06:27.744606Z
- Scenario: dlq_replay_safe_success (family dlq, outcome resolved)
- What the scenario is: Agent probes list_dlq_messages and sees one entry carrying remediation_hint=replay_safe (the platform's classifier confirmed the consumer can now handle it) beside entries it must not touch.
- Ground truth: root cause poison_message; expected end state resolved; expected actions: replay_dlq_by_category, replay_dlq_by_ids
- Why it is in the packet: newest live briefing in the least-covered slot: outcome resolved had 4 pick(s), family dlq had 2; first pick for this scenario
- Note: archived before probe arguments were recorded, so each call shows `()`; the judge sees it the same way

The briefing, as the judge sees it:

~~~~text
Incident: 37188e02-fa6c-47d3-b03d-1e3cfd315ff4
Final state: resolved
Alert: source=platform.dlq severity=critical fingerprint=dlq_depth_warning_replay_safe
Investigation trail:
  - list_dlq_messages() -> {"total":1,"items":[{"id":"fc8d2a03-23b3-5371-9acb-46443c73baa5","type":"bulk_api_sync","error_message":"UpstreamTimeout: bulk_api_sync POST https://partner-api.internal/v2/sync timed out after 30s on attempt 3/3 — the request was never acknowledged, so no downstream write was recorded","retry_count":3,"created_at":"2026-09-07T13:23:12.383586Z","updated_at":"2026-09-07T13:23:18.383586Z","trace_id":"c5ca46c1-f4dd-5021-8be4-130143553096","triage":{"root_cause_category":"upstream_timeout","summary":"partner-api.internal did not answer inside the 30s deadline on any of the three attempts. The payload was accepted by validation and nothing was committed downstream.","suggested_fix":"Re-run the job as it stands: the call is idempotent, no partial write survived the timeout, and a fresh attempt is the entire remedy. Neither the payload nor the producer needs changing.","is_retryable":true,"confidence":0.88},"remediation_hint":"replay_safe","dead_lettered_at":"2026-09-07T13:23:18.383586Z","extra":null}]}
  - get_trace() -> {"trace_id":"c5ca46c1-f4dd-5021-8be4-130143553096","jobs":[{"id":"fc8d2a03-23b3-5371-9acb-46443c73baa5","type":"bulk_api_sync","status":"dead_letter","user_id":"ee7c82cb-bab6-5780-8590-55b57064cbe3","retry_count":3,"error_message":"UpstreamTimeout: bulk_api_sync POST https://partner-api.internal/v2/sync timed out after 30s on attempt 3/3 — the request was never acknowledged, so no downstream write was recorded","created_at":"2026-09-07T13:23:12.383586Z","updated_at":"2026-09-07T13:23:18.383586Z"}],"audit_events":[],"truncated":false,"total_jobs":1,"total_audit_events":0}
  - replay_dlq_by_ids() -> {"requested":1,"replayed":1,"scheduled":0,"failed":0,"results":[{"id":"fc8d2a03-23b3-5371-9acb-46443c73baa5","ok":true,"error":null,"scheduled":false,"execute_at":null}]}
  - list_dlq_messages() -> {"total":0,"items":[]}
Findings: The DLQ held one dead-lettered `bulk_api_sync` job (fc8d2a03) that failed all three attempts due to an upstream timeout against `partner-api.internal/v2/sync`; the triage confirmed no partial downstream write occurred and the call is idempotent. A Tier-1 `replay_dlq_by_ids` action was already executed and succeeded (1/1 replayed), after which the DLQ returned empty.
Recommendation: Verify that the replayed job completed successfully by checking the job status for fc8d2a03 or confirming a successful sync event for user ee7c82cb in the audit log; if it timed out again, investigate the health and latency of `partner-api.internal` before attempting further action.
~~~~

LABEL: 
REASON: 

---

## 16. `6e2def805db4:saga_stuck`

- Run: archive 6e2def805db4, live model, 2026-08-12T01:21:13.159488Z
- Scenario: saga_stuck (family workflow, outcome escalated)
- What the scenario is: A dependency chain has stopped draining.
- Ground truth: root cause runaway_saga; expected end state escalated; expected actions: mark_dlq_permanent
- Why it is in the packet: newest live briefing in the least-covered slot: outcome escalated had 5 pick(s), family workflow had 1; first pick for this scenario
- Note: archived before probe arguments were recorded, so each call shows `()`; the judge sees it the same way

The briefing, as the judge sees it:

~~~~text
Incident: e2df0af6-0613-4b7a-ad35-1e0491cacb8e
Final state: escalated
Alert: source=platform.dag severity=high fingerprint=saga_stalled
Investigation trail:
  - get_dag_state() -> {"seed_id":"25220882-1820-5cd3-b09b-f866ff5823c9","nodes":[{"id":"25220882-1820-5cd3-b09b-f866ff5823c9","type":"bulk_api_sync","status":"completed","retry_count":0,"created_at":"2026-08-11T09:43:54.714014Z"},{"id":"b82d8fb4-a936-5cf4-b775-8d1f673a6f38","type":"bulk_api_sync","status":"completed","retry_count":0,"created_at":"2026-08-11T09:43:54.714014Z"},{"id":"a1b11d3c-48c6-5b13-9a71-13b4bbe2df8c","type":"bulk_api_sync","status":"completed","retry_count":0,"created_at":"2026-08-11T09:43:54.714014Z"}],"edges":[{"from_id":"25220882-1820-5cd3-b09b-f866ff5823c9","to_id":"b82d8fb4-a936-5cf4-b775-8d1f673a6f38"},{"from_id":"a1b11d3c-48c6-5b13-9a71-13b4bbe2df8c","to_id":"25220882-1820-5cd3-b09b-f866ff5823c9"}],"paused":false,"paused_expires_in_seconds":null,"paused_by":null}
  - list_audit_events() -> {"total":0,"events":[]}
  - search_traces() -> {"matches":[]}
  - get_deploy_history() -> {"total":6,"entries":[{"version":"v0.4.3","revision":"d1e5a83","image_tag":"v0.4.3","deployed_at":"2026-08-11T07:43:54.982864Z","environment":"prod","notes":null},{"version":"v0.4.3","revision":"d1e5a83","image_tag":"v0.4.3","deployed_at":"2026-08-11T06:43:54.982864Z","environment":"staging","notes":"staging soak before prod"},{"version":"v0.4.2","revision":"c9f4d02","image_tag":"v0.4.2","deployed_at":"2026-08-11T03:43:54.982864Z","environment":"prod","notes":"correlated with billing failures"},{"version":"v0.4.1","revision":"b7c3e51","image_tag":"v0.4.1","deployed_at":"2026-08-10T15:43:54.982864Z","environment":"prod","notes":null},{"version":"v0.4.0","revision":"a1940af","image_tag":"v0.4.0","deployed_at":"2026-08-10T11:43:54.982864Z","environment":"prod","notes":null}],"source":"deploy_markers"}
  - list_dlq_messages() -> {"total":3,"items":[{"id":"fc8d2a03-23b3-5371-9acb-46443c73baa5","type":"bulk_api_sync","error_message":"SchemaValidationError: payload missing required field 'user_id' (received keys: ['tenant_id', 'action', 'ts'])","retry_count":3,"created_at":"2026-08-11T09:35:54.994755Z","updated_at":"2026-08-11T09:35:54.994755Z","trace_id":"c5ca46c1-f4dd-5021-8be4-130143553096","triage":{"root_cause_category":"schema_violation","summary":"Producer sent a payload missing user_id — schema rejected it three times.","suggested_fix":"Fix the producer to include user_id, then replay the DLQ entry. Backwards-compatible producer fix + replay is the standard remediation.","is_retryable":true,"confidence":0.91},"remediation_hint":"replay_safe","extra":null},{"id":"af67d1b1-13f8-5a2c-8c44-66ec5564597d","type":"bulk_api_sync","error_message":"process_payment call timed out after 30s: TimeoutError('stripe.api')","retry_count":3,"created_at":"2026-08-11T09:18:54.994755Z","updated_at":"2026-08-11T09:18:54.994755Z","trace_id":"6f76e48d-72c4-5e5a-a5cf-bc29758167cb","triage":{"root_cause_category":"third_party_timeout","summary":"Stripe API exceeded 30s deadline three consecutive attempts.","suggested_fix":"Verify Stripe status page; if green, raise the per-call timeout via the retry policy for payment-type jobs and replay the DLQ batch.","is_retryable":true,"confidence":0.71},"remediation_hint":"wait_and_replay","extra":null},{"id":"97d91272-9774-5b8e-980b-f0d2fa6ed619","type":"bulk_api_sync","error_message":"send_email downstream call failed: ConnectionRefusedError('smtp.mailer.internal:587')","retry_count":3,"created_at":"2026-08-11T09:03:54.994755Z","updated_at":"2026-08-11T09:03:54.994755Z","trace_id":"1bfc7482-c957-5217-beae-e8cba48a152d","triage":{"root_cause_category":"downstream_unavailable","summary":"SMTP relay unreachable — connection refused at TCP layer.","suggested_fix":"Check smtp.mailer.internal ECS task health; recent billing hotfix (v0.4.2) may have changed VPC egress rules — cross-reference `get_deploy_history`. Replay once the dependency recovers.","is_retryable":true,"confidence":0.82},"remediation_hint":"wait_and_replay","extra":null}]}
Findings: The DAG itself completed without pausing, but 3 bulk_api_sync jobs landed in the DLQ across a ~33-minute window: one schema violation (missing `user_id` field), one Stripe API timeout after 30 s, and one SMTP connection refusal to `smtp.mailer.internal:587` — all exhausted 3 retries; notably, the v0.4.2 deploy (annotated "correlated with billing failures") preceded the incident and may have introduced VPC egress changes affecting the SMTP relay.
Recommendation: Start by checking the health of `smtp.mailer.internal` ECS tasks and comparing VPC egress rules before and after the v0.4.2→v0.4.3 deploy, then verify Stripe's status page for the timeout; once dependencies are confirmed healthy, the DLQ entries are all marked replay-safe, but the schema-violation entry requires a producer fix (add `user_id`) before replay.
~~~~

LABEL: 
REASON: 

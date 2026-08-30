# ADR 0014: Webhook signature scheme v2 (timestamp-bound HMAC) and replay-window semantics

* Status: accepted; the **v2 scheme** it defines is superseded by [ADR 0023](0023-nonce-bound-webhook-signatures.md) — the platform shipped a nonce-bound composition instead, and `sign_v2`/`verify_v2` are gone. The header-name unification, the skew window and the legacy duplicate-suppression decisions below remain in force.
* Date: 2026-08-09
* Decider: Kudrat Singh

## Context and problem statement

The platform's alert emitter (incident-platform `backend/app/services/alerts.py`) signs the raw
delivery body with HMAC-SHA256 and sends `X-Alert-Signature: sha256=<hex>` plus
`X-Alert-Timestamp: <epoch ms>`. Two defects met in the commander's ingress. First, the
commander read the signature from `X-Signature-256` — a header the platform has never sent — so
every real delivery was 401'd; the agent only ever appeared to work because tests and demo
tooling signed into the commander's header name (audit miss, surfaced with finding C-08).
Second, C-08 proper: the v1 scheme signs the body only, and the timestamp header is not part of
the signed material, so any captured delivery verifies forever, and each accepted POST spawned a
fresh background investigation run. What signature scheme closes replay, and what can the
commander enforce unilaterally while the emitter is in another repo?

## Decision drivers

* A replayed delivery spawns a real investigation: LLM spend, tool calls, and platform load per
  replay (each run is budgeted, but budgets bound the burn per run, not the number of runs).
* The emitter lives in incident-platform; the commander cannot change what gets signed, only
  what it accepts. Cross-repo migration needs a dual-emit window, not a flag-day.
* The platform treats any >=400 response as delivery failure and logs it (`alerts.py`), and its
  delivery is at-least-once — legitimate redelivery must not look like an outage.
* Honesty about what a commander-side guard can and cannot claim: an unsigned timestamp is
  attacker-controlled.
* The existing `hmac.compare_digest` discipline (prefix check, length pre-check, constant-time
  compare) is audit-verified correct and must not be perturbed.

## Considered options

1. Commander-side hardening only: accept the real header names, enforce a timestamp skew
   window, suppress exact-duplicate deliveries (chosen as the stopgap).
2. v2 scheme — the emitter signs `{timestamp_ms}.{body}` — verified commander-side (chosen as
   the end state; requires a platform PR).
3. Reject duplicates with 409 instead of 202.
4. Durable dedupe keyed on an alert fingerprint in the commander's Postgres.

## Decision outcome

Both 1 and 2: ship the commander-side stopgap now, and define v2 so the platform PR that closes
the class has a ready verifier waiting.

**Header-name unification.** The wire names are the platform's shipped names:
`X-Alert-Signature` and `X-Alert-Timestamp` (epoch **milliseconds**). The commander reads
`X-Alert-Signature` first and falls back to the legacy `X-Signature-256` so pre-fix tooling
keeps working; the fallback retires once nothing signs into the old name.

**Skew window.** When `X-Alert-Timestamp` is present, a delivery whose timestamp deviates from
local time by more than `WEBHOOK_MAX_SKEW_SECONDS` (default 300) is rejected with 401, as is a
non-numeric timestamp.

**Duplicate suppression.** After the signature verifies, the ingress consults a module-level
TTL cache keyed by the signature hex (capacity-bounded at 1024 entries, pruned on access, TTL =
the skew window). A duplicate returns **202 without spawning a run**, plus a log line. Not 409:
the emitter would count any >=400 as failed delivery, making legitimate at-least-once
redelivery look like an outage.

**v2 scheme.** `sign_v2`/`verify_v2` in `api/hmac_verify.py` MAC the concatenation
`{timestamp_ms}.{body}` and use the header form `v2=<hex>`, with the same constant-time
discipline as v1. Because the timestamp is inside the MAC, a captured signature cannot be
paired with a fresh timestamp — the skew window then genuinely bounds replay. v1 `sign`/
`verify` are unchanged.

**What the stopgap honestly is.** The v1 timestamp is unsigned, so an active attacker replays a
captured body+signature with a fresh timestamp header of their choosing: the skew check passes,
and only the dedupe cache stands in the way — limiting them to at most one spawned run per
cache window per captured delivery, indefinitely, and only while the process that saw the
original is still running. The stopgap bounds replay rate and stops passive/accidental replay;
it does **not** close the class. Claiming otherwise would be wrong.

**Completion condition.** The class closes when the platform emitter signs
`{timestamp_ms}.{body}` — an incident-platform PR ("alert webhook: dual-emit v2 signatures")
that adds `X-Alert-Signature-V2` (or a v2-prefixed value) alongside v1 during migration, with
the commander switching `verify` → `verify_v2` at ingress and then dropping v1 acceptance. That
platform PR is deliberately **not** part of this fix campaign's 77 commander work orders; this
ADR records the cross-repo dependency so it is not silently forgotten.

### Why the alternatives lose

**409 on duplicates (option 3).** Semantically "conflict" is accurate, but the only real caller
interprets status, not meaning: `alerts.py` logs a warning for any >=400, so every legitimate
redelivery would page as a delivery failure. The receiver absorbing duplicates quietly is the
price of at-least-once.

**Durable fingerprint dedupe (option 4).** Correct long-term, and out of scope here: durable
single-flight across restarts and replicas is finding B-05's territory — the ADR-0002
lease/dedupe work (WO-C5-08). This cache is deliberately its cheap, process-local cousin; it
does not survive restart and does not coordinate across processes, and B-05's fix, not this
one, is where that guarantee lands.

**Stopgap only, no v2 definition.** Leaves "fix replay" meaning nothing actionable for the
platform side and invites a second bespoke scheme. Defining v2 now (verifier shipped, tested,
constant-time) makes the platform PR a mechanical change.

### Consequences

Positive:

* Real platform deliveries are accepted at all — the header mismatch made the entire alert
  path dead in any live deployment.
* A captured or duplicated delivery inside the window no longer multiplies investigation runs
  or burns budget; outside the window, stale-stamped deliveries are refused.
* v2 is fully specified and verifiable today; the cross-repo dependency is named rather than
  implied.

Negative:

* The replay cache is process-local and forgotten on restart; an attacker can still induce one
  run per window per captured delivery. Mitigation: B-05 / ADR-0002 lease work for durability,
  platform v2 emission for the class itself.
* Deliveries without any timestamp header (legacy path) skip the skew check entirely until the
  fallback retires. Mitigation: the platform always sends the header; only pre-fix tooling
  omits it.
* A synthetic `incident_id` is returned for suppressed duplicates (the cache stores only
  signature and time). The emitter reads only the status code, so nothing consumes it today.

Revisit trigger: the platform dual-emit PR landing (switch ingress to `verify_v2`, then drop v1
and the legacy header fallback), or WO-C5-08 landing durable dedupe (retire this cache).

## More information

Finding C-08 (audit, Medium) and the header-name audit miss recorded in the fix campaign
(WO-C6-02). Emitter semantics authored from incident-platform
`backend/app/services/alerts.py:111-115` (`sign_body` at :83-87), identical at the pinned
v0.4.9 image and master tip. Related: ADR 0002 (state machine; the lease/dedupe follow-up that
finding B-05 tracks), platform ADR 0006 (MCP/REST split — alert webhooks are the REST-side
push path).

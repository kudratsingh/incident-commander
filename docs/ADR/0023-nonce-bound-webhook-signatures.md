# ADR 0023: Nonce-bound webhook signatures, and refusing a reused nonce

* Status: accepted (supersedes the "v2 scheme" half of ADR 0014)
* Date: 2026-08-30
* Decider: Kudrat Singh

## Context and problem statement

ADR 0014 closed finding C-08 with a commander-side stopgap and *specified* the end state: the
platform would sign `{timestamp_ms}.{body}` and carry it as `v2=<hex>`, and the commander
already shipped `sign_v2`/`verify_v2` waiting for it.

The platform shipped something else. incident-platform #183 (WO-R2-70) signs
`{timestamp}.{nonce}.{body}` — a per-delivery nonce, not just the timestamp — and it kept the
existing `sha256=` prefix on `X-Alert-Signature`, adding `X-Alert-Nonce` as a new header rather
than versioning the signature value. See platform `docs/ARCHITECTURE.md` ("Alert delivery:
committed first, signed over more than the body") and `alerts.signed_material` /
`alerts.sign_delivery`, which are the canonical composition.

So the verifier that was "waiting for the platform PR" verifies a scheme nobody emits, on a
prefix nobody sends. After the wave-9 re-pin every real delivery would 401 and the live alert
path would be dead again — the same class of outage as the original `X-Signature-256` header
mismatch, arrived at from the opposite direction: we guessed the contract instead of reading it.

## Decision drivers

* The re-pin is a scheduled event, and the commander must be correct **before** it, not after.
* The emitter and the receiver re-pin on different days, so both orders must be safe: the
  commander has to accept the pinned image's legacy deliveries *and* the new ones, with no
  flag-day in between.
* A nonce is strictly more information than a timestamp. It lets the receiver distinguish
  "delivered twice" from "delivered once and replayed", which the body-only scheme could not.
* The existing constant-time discipline (prefix check, length pre-check, `compare_digest`) is
  audit-verified and must not be perturbed.

## Considered options

1. Switch wholesale to the nonce-bound scheme and drop legacy acceptance.
2. Accept both, selected by the presence of `X-Alert-Nonce` (chosen).
3. Keep `verify_v2` and ask the platform to emit our scheme instead.

## Decision outcome

**Option 2.** `api/hmac_verify.py` gains `signed_material`, `sign_delivery` and
`verify_delivery`, transcribed from the emitter's composition; `sign_v2`/`verify_v2` are
**deleted** rather than left beside them. A never-emitted "v2" sitting next to the real scheme
is not harmless dead code — it is a trap for the next reader, who has no way to tell which of
the two the platform actually speaks.

**The nonce selects the scheme, not the prefix.** Both schemes carry `sha256=<hex>` in
`X-Alert-Signature`, so the value cannot discriminate. The ingress branches on whether
`X-Alert-Nonce` is present. This is what makes the re-pin safe in either order.

**A nonce delivery must carry a timestamp.** On the legacy path the timestamp header is optional
(pre-fix tooling omits it) and only feeds the skew check. Here it is *inside* the MAC, so a
missing one is not a degraded check but an unverifiable delivery: 401.

**Downgrade is closed by construction.** Presenting `X-Alert-Nonce` while signing the old
body-only material fails, because the nonce path verifies only the nonce-bound composition.
There is no "try both and accept either" fallback — that would hand an attacker the weaker
scheme for free, and it is the reason the branch is on the header rather than on trial
verification.

**A reused nonce is refused with 401, and this is the one place we depart from ADR 0014's
"duplicates get a quiet 202".** That rule was correct for the body-only scheme *because of what
it could not know*: with the timestamp outside the MAC, a repeated signature is equally
consistent with an attacker's replay and with the emitter's legitimate at-least-once
redelivery, and 4xx-ing the honest case would make every redelivery page as a delivery failure.
The nonce removes the ambiguity — the emitter mints a fresh one per delivery, retries included
("a retry is a new delivery") — so a repeated nonce is a replay, full stop. `deliver_webhook`
POSTs once and never retries the same bytes, so nothing legitimate is refused by this. The
legacy path keeps the 202 behaviour unchanged.

**The nonce cache is retained for twice the skew window.** A delivery is acceptable anywhere in
`[stamp - skew, stamp + skew]`, so a one-window cache leaves a gap: a nonce first seen at the
earliest acceptable moment is forgotten before the latest one arrives, and the replay is
accepted again at the end of the range. Two windows covers the whole acceptable range. The
cache is consulted only *after* the MAC verifies, so an unauthenticated caller cannot poison it
with a chosen nonce and get a genuine delivery refused.

### Why the alternatives lose

**Wholesale switch (option 1).** The commander and the pinned platform image re-pin on
different days; dropping legacy acceptance makes the merge order load-bearing and breaks live
alerts if the commander lands first — which is precisely the order this order is scheduled in.

**Ask the platform to re-emit our scheme (option 3).** The platform's composition is strictly
better than the one ADR 0014 imagined, and it is already merged and documented. Re-litigating
it would cost a cross-repo round trip to arrive somewhere worse.

### Consequences

Positive:

* Deliveries from the re-pinned platform are accepted, and the wave-9 re-pin cannot break the
  live alert path in either merge order.
* Replay inside the skew window is now *detected*, not merely rate-limited. For nonce-bound
  deliveries the class ADR 0014 explicitly declined to claim closed is closed.
* One shared `_matches` helper means both acceptance paths cannot drift into different
  comparison discipline.

Negative:

* The legacy body-only path keeps every weakness ADR 0014 named — unsigned timestamp, replay
  bounded only in rate by a process-local cache. It is load-bearing until the re-pin and should
  be deleted immediately after; that deletion is the natural follow-up order.
* The nonce cache is still process-local and forgotten on restart. Durable dedupe remains the
  ADR-0002 lease work (finding B-05).

Revisit trigger: the wave-9 re-pin completing (drop the legacy path, `verify`, `sign`, and the
`X-Signature-256` fallback), or WO-C5-08 landing durable dedupe (retire the caches).

## More information

Platform `docs/ARCHITECTURE.md` and `backend/app/services/alerts.py` (`signed_material`,
`sign_delivery`, `deliver_webhook`) at incident-platform #183. Supersedes the v2 half of
ADR 0014; the header-name unification, skew window and legacy duplicate-suppression decisions
in that ADR remain in force.

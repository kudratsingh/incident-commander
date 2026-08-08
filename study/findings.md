# Study findings

Running record of reliability findings. Each entry names the control that
failed, the evidence that settled it, and the fix that makes the failure
non-repeatable.

## F-001 — Controls must be asserted at point of use, not assumed downstream

**Date:** 2026-08-07 (Run 001, stage 1)

**What happened.** Stage 1 was labelled and believed to be read-only:
`make eval-smoke` exported the read-scoped `PLATFORM_TOKEN` (PR #69), the
smoke service account carried exactly `telemetry:read, incidents:read`,
and a live probe of that token was correctly refused. Yet the stage
executed ten successful Tier-1 calls (`mark_dlq_permanent` ×7,
`restart_consumer_group` ×3).

**Evidence that settled it.** The platform audit log — not the agent's
trajectory, not the trace summaries. Every successful row carried the
**full** principal (`incident-commander`); the only smoke-principal row in
the table was an `unauthorized` refusal from a manual probe. That single
query eliminated three plausible stories at once (platform scope-check
bug, over-scoped smoke SA, trace mislabelling of denials) and identified a
fourth nobody had proposed.

**Root cause.** `-include .env` (PR #62, added so `PLATFORM_COMPOSE` would
persist) overrides environment variables supplied to a make recipe, and
make re-exports the *file's* value to the sub-process. PR #69's token
export was silently defeated. Two independently-correct changes composed
into a broken control, and nothing between them ever checked.

**Why it went unnoticed.** Every layer that could have caught it was
looking at configuration rather than behavior: the SA had the right
scopes, the recipe named the right variable, the runbook described the
right intent. The only artifact that reflected what actually happened was
the platform's audit log, and nothing read it.

**The lesson.** *A control that isn't verified where it is used is not a
control — it is a belief about configuration.* Read-only-ness must be
established by the principal being unable to write at the moment of the
run, not by having configured a token that cannot write.

**Fix.** Two point-of-use assertions, no platform changes (v0.4.9 exposes
no whoami/introspection tool, and the freeze holds):

1. **Negative probe at startup** — smoke mode invokes a Tier-1 tool with
   deliberately invalid arguments and requires a *scope* refusal. The
   platform checks scope before parsing arguments, so the probe cannot
   execute under either token, and the two outcomes are distinguishable: a
   validation error means the token has write scope → hard-fail before any
   scenario runs.
2. **Post-stage audit assertion** — query `list_audit_events` for the
   stage window and fail if any Tier-1 tool shows `outcome=success`. This
   automates exactly the evidence that caught the bug.

Structurally, the principal now comes from config (`--smoke` selects
`PLATFORM_SMOKE_TOKEN` inside the runner) instead of from shell variable
inheritance, and a regression test pins make's precedence behavior so the
mechanism cannot quietly return.

**Third instance tonight.** The same shape appeared twice more: platform
#92's sys.path fix existing on master but untagged (the control existed,
the artifact in use didn't have it), and idempotent replay returning
`already_marked: true` — a response indistinguishable from a fresh write
unless you check the audit. In all three, the artifact in use diverged
from the artifact described, and only ground truth at the point of use
told them apart.

**Related.** `evals/guards.py`, `tests/unit/test_make_token_precedence.py`,
CLAUDE.md invariant 6 (audit log is ground truth), ADR 0003 (platform-side
enforcement is the boundary — it held; the agent-side plumbing did not).

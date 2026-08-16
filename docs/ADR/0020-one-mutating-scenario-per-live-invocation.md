---
status: accepted
date: 2026-08-16
supersedes: none
amends: 0013, 0018
---

# 20. One state-mutating scenario per live invocation; exit 7 refuses the rest

## Context

The nine remediation-class scenarios run against **one** platform, and the runner has no
reset between scenarios — `run_all` loops `run_scenario` and nothing restores the world
(`evals/runner.py`). The reset lives outside the runner, in `make eval-reset`.

That is fine for the read-only stage, where 26 scenarios share a world none of them
change. It is not fine here, and the sharpest case is arithmetic rather than a race:

**The seeded fixture pack contains exactly one `replay_safe` DLQ row.**
`dlq_replay_safe_success` and `dlq_mixed_partial` both consume it. In a single
invocation, whichever runs first replays it and the other finds it gone — so a
*correct* agent greens one and reds the other, and the report attributes that to the
agent. No amount of agent quality fixes it.

It is not the only one. `dlq_wait_and_replay_success` schedules a delayed replay whose
timer fires while later scenarios are still running, so a later "the DLQ shrank" verify
can go green off an earlier scenario's effect. `kill_consumer` and a paused DAG outlive
the scenario that created them. Scenarios run in filename order (`loader.py` uses
`directory.iterdir()`), so `dlq_backlog` — an unguarded live scenario — sorts first and
can drain the pool before any graded scenario starts.

The Makefile made this the *documented* path. `eval-live-remediation` expanded to
`ONLY=remediate_,dlq_`: nine mutating scenarios, one invocation, no reset. The runbook's
post-hardening protocol already told operators to run one at a time and reset between —
but a protocol in prose loses to a target you can type.

## Decision

Under `--live`, the runner **refuses** a selection containing more than one
state-mutating scenario, and exits **7** before preflight, before the guards, before any
spend. A scenario is state-mutating when it declares `expected_action_tools` (it will
execute a Tier-1 action) or `chaos_setup` (it will seed a fault).

The refusal prints the runnable form — one `make eval-live ONLY=<name> && make eval-reset`
line per selected scenario — because a refusal that does not hand over the next command
gets worked around.

`eval-live-remediation` is deleted rather than deprecated. A target the runner now
refuses is a trap, not an alias.

This amends the exit-code enumeration in ADR 0013 and extended by ADR 0018: the contract
is now **0–7**.

**How to read exit 7.** Like exit 6, it is a *selection* problem — never an environment
or platform one. Nothing ran and nothing was spent. The fix is always the same: run the
scenarios one at a time with a reset between.

Offline runs are untouched. Canned scenarios share no world, and the offline gate depends
on running the whole suite in one invocation.

## Consequences

**The isolation class of defect disappears** rather than being managed. The alternative —
a per-scenario teardown hook framework — was considered and is strictly more machinery
for a weaker guarantee: teardown can only undo what it knows about, and the replay_safe
row is consumed by the *agent*, not by a hook.

**A full remediation campaign is now nine invocations**, not one. That is slower and it
is the honest shape: each one seeds, runs, and is reset before the next begins. The
runbook's protocol and the Makefile now agree.

**`make eval-reset` had a matching hazard**, fixed alongside: `PLATFORM_COMPOSE` defaulted
to the platform's own dev compose and `PLATFORM_SERVICE` to `app`. A checkout without
those overrides in `.env` resets a different Postgres and a different Redis, and reports
success. The defaults now name `demo/compose.yml` and `api`, and the target echoes which
stack it is resetting. (Both demo services share one database, so either service name
reaches the same rows — the compose file was the load-bearing half.)

## Alternatives considered

**Reset inside `run_all` between scenarios.** Rejected for now: the reset is a
`docker compose exec` into a container the runner does not own, and giving the runner
shell access to the platform's stack is a larger trust change than this problem needs.
An `EVAL_RESET_CMD` hook remains a reasonable later step; the refusal is correct either
way, since it also catches the case where no reset is configured at all.

**Leave it to the runbook.** That is what we had. The batch target existed for months and
the one paid remediation attempt to date was run from it.

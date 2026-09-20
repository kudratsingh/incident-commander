# Demo runbook — recording the live demo

One fault, one agent, one console, six printed steps. `make demo-live` drives it; this file
is what to have open beside it.

```bash
make demo-live MODE=consumer_outage           # a consumer stops, the backlog climbs
make demo-live MODE=dlq_backlog               # one replayable row, one poisoned row
make demo-live MODE=… AUTO=1                  # rehearsal: no pauses
make demo-live MODE=… RECORD_FROM=baseline    # record from the healthy world, not the fault
make demo-live MODE=… LIVE=1 YES_SPEND=1      # the ONE paid take
```

**You start recording at step 4, not step 2.** The first take began at the baseline and the
usable footage began ninety seconds later: `consumer_outage`'s backlog is a measurement the
platform recomputes every 60 s, so there is nothing on the chart until a sample crosses the
threshold. The prompt now comes once the fault is on screen. `RECORD_FROM=baseline` restores
the old order for a take where the healthy world is the point.

The default path is **free**: the real platform, the real fault, the real Tier-1 action, and
a scripted planner. Step 5 runs `evals.runner --mode rehearsal` ([ADR 0069](ADR/0069-a-rehearsal-is-a-third-provenance-not-a-live-run.md)),
which is the only invocation that combines those two halves — every row it writes is stamped
`degraded=True` with a `rehearsal` provenance flag, so no report can count it. `LIVE=1` alone
refuses (exit 2): spending needs `YES_SPEND=1` as well, plus the owner's explicit yes for that
scenario, every time. No make target sets `YES_SPEND`.

## Before you record

- [ ] Stack up on the pinned digests: `make demo` (six long-running services).
- [ ] `make bootstrap-token`, and paste all three tokens. **A token's scopes are chosen when
      it is MINTED, so a `PLATFORM_TOKEN` minted before the `agent_runs:write` widening does
      not carry it however wide the service account has become since.** Reporting is
      fail-open, so the symptom is an empty console rather than an error: step 5 prints
      `agent-run reporting: … 7 FAILED (first: report_agent_run: … missing required scope:
      agent_runs:write)`. That exact line cost the first 2026-09-20 rehearsal — re-mint, paste
      `PLATFORM_TOKEN`, and the same run prints `6 report(s) accepted, briefing sent`.
- [ ] Console open at `http://localhost:3000/demo?mode=<mode>`, logged in as the demo
      operator (the `DEFAULT_EMAIL` / `DEFAULT_PASSWORD` constants in
      `scripts/bootstrap_agent_token.py` — never typed into a file that gets committed).
      **Reload it after step 1's reset**: the reset writes the page's own boundary row, and a
      page loaded before it is still showing the previous run.
- [ ] Nothing else running: no `make traffic`, no `evals.runner`, no merge in flight. Step 1
      audits for exactly this and stops if it finds one.
- [ ] Screen recorder ready but **not started** — step 4 tells you when (step 2 with
      `RECORD_FROM=baseline`).

## What to say, per phase

| Phase | On screen | The sentence |
|---|---|---|
| Baseline | lag 0 known, jobs completing, no agent run | "A job platform, working. Nothing is wrong, and the agent has nothing to do." |
| Fault injected | amber `chaos.*` row, phase strip moves | "I break one thing. The lab did that — and notice the agent cannot see this row at all. It has to work out what happened from the system's own readings, like a person would." |
| Agent investigating | grey read rows, hypothesis with confidence | "It is probing. Each read is a real tool call against the real platform, and the confidence bar is its own." |
| Agent planning | phase strip, hypothesis firms up | "It has a diagnosis. The action it is about to take is Tier-1 — the platform decides whether it is allowed, not the agent." |
| Agent remediating | blue action row, arguments, outcome | "One action, on exactly the resource the alert named. The argument is in the audit log, so this is checkable rather than claimed." |
| Verifying | the metric moving | "Now it watches. A cached metric trails recovery, so this can take a moment — which is the honest behaviour." |
| Recovered / escalated | phase strip lands; briefing card | "And the handoff: what it found, what it did, and what it deliberately left alone." |

The DLQ mode's best moment is the **poisoned row it does not touch**: the briefing names it
as remaining work. That is the one to slow down for.

## What the console is sent during a run

The reporter sends the whole run, not just its phase (ADR 0068 as amended by WO-R3-329). Per
report: the **state**, the **ranked hypotheses** with a reasoning excerpt each, the
**current hypothesis**, the **plan** (tool, arguments, target hypothesis, rationale) once it
exists, one **verification** per verify poll with its verdict and `{attempt, of}`, the
**budget** (calls used/max, tokens, dollars, wall seconds), and one **step** per tool call —
its wired arguments, an excerpt of what came back, the outcome (`ok` / `refused` /
`error: …`) and the latency in milliseconds.

Two things to know before you read the page:

- **The steps come from the agent's own client, not from the audit log.** An
  `agent.tool_invoked` audit row carries the tool, the arguments and the latency but *not the
  result*, so "what did the agent see" only exists in the run record.
- **A report is still never a tool call.** None of this counts against `max_tool_calls`, none
  of it reaches the evidence ledger, and a platform that refuses every report changes nothing
  about the run.

The panels that draw all of this are the console's half (WO-R3-330), and the fields only
reach the platform once the commander is pinned to a platform that declares them (v0.6.16).
Before that pin the reporter **narrows** — see the next section.

## Reporting against a platform that does not know the new fields

`report_agent_run`'s input model forbids unknown fields, so on platform **v0.6.15** a widened
report is refused whole — state included. Measured on the 2026-09-20 rehearsal, and the shape
of the refusal is the part worth writing down: it arrives as a **JSON-RPC error**
(`MCP error -32602: invalid tool arguments`), not as a 200 carrying `isError`. The first
attempt at this read only the second route, and every report of that rehearsal was lost:

```
run fe15d750-…: agent-run reporting failed (the run is unaffected): report_agent_run: MCPError: MCP error -32602: invalid tool arguments
  (× 8, then) report_agent_briefing: MCPError: MCP error -32011: no run fe15d750-… to attach a briefing to; report its state first
  agent-run reporting: run fe15d750-…, 0 report(s) accepted, 0 step(s), 0 verification(s), briefing NOT sent, 9 FAILED
```

With the fallback, the same rehearsal reports the state to the same old platform, once per
refusal rather than once per call:

```
run c4d5aa39-…: the platform refused the widened report: MCP error -32602: invalid tool arguments. Falling back to the fields platform v0.6.15 accepts …
  agent-run reporting: run c4d5aa39-…, 6 report(s) accepted, 0 step(s), 0 verification(s), briefing sent, NARROWED to the pre-v0.6.16 fields (see the log), 1 FAILED
```

So on v0.6.15 the console shows exactly what it showed before — phase strip, hypothesis, last
step — and says in the log why the rest is missing. **A 403 does not narrow**: a token minted
without `agent_runs:write` is a re-mint (see the checklist above), not an old schema, and
hiding it behind a thinner console is the wrong repair.

## Timings, measured

Both modes rehearsed end to end on live platform **v0.6.15**, 2026-09-20, `AUTO=1` (so no
operator pauses), both PASS, `make world-audit` PASSed before and after each.

| Step | | `dlq_backlog` | `consumer_outage` |
|---|---|---|---|
| 1 | stack check, reset, world audit, console URL | 3.4 s | 3.4 s |
| 2 | baseline (`consumer_outage` also starts `make traffic`) | 0.0 s | 0.0 s |
| 3 | inject the fault (10 s countdown) **and wait for the platform to show it** | 10.6 s | 106.3 s |
| 4 | prove the premise the scenario grades against | 0.5 s | 0.5 s |
| 5 | run the agent (scripted planner) | 1.6 s | 1.3 s |
| 6 | wind down: run id, deep link, paths, stop traffic, reset, re-audit | 3.4 s | 58.9 s |
| | **total** | **19.4 s** | **170.5 s** |

Read those as the machine's own overhead, not as the demo's length: the pauses are where you
talk, and `LIVE=1` replaces step 5's second with a real model's minutes.

**`consumer_outage`'s two long steps are one fact about the platform, not slack.** It
recomputes consumer lag on a **60-second interval**, and the fault is not on the page until a
sample crosses the threshold, so step 3 now waits for that sample rather than leaving it to
the precondition: the readings ran `0 (× 7) → 11 (× 12) → 30`, and the 11 is the reason the
wait exists — it is a real sample of a real backlog that is still under the bar, and a page
showing `11` against a threshold of `20` shows an audience nothing. Step 4 then passes in
half a second, because the premise was already true when it asked.

Step 6 is the same clock in reverse — the agent's restart drains the backlog in seconds, but
the next sample is up to a minute away, so the machine **waits for a fresh `0` before
auditing** and only then runs `make world-audit`. Without that wait the audit reads the value
taken while the consumer was still dead: an early rehearsal ended on
`[FAIL] worker-dispatcher lag: 33 (want 0)` over a world that was already clean, and the same
read was `0` twenty seconds later. A timeout (150 s) warns and audits anyway — waiting must
never be a way to declare the world fine. The fault watch in step 3 has the same rule for the
same reason: a timeout (180 s) WARNS, and the precondition below it is the gate.

## What the operator endpoints showed, per phase

`consumer_outage`, the same rehearsal, polled every 2 s as the operator console polls. All four
are the operator's, under a `support|admin` **user** session — the agent's own principal cannot
read any of them (platform ADR 0035).

| Phase | `/admin/agent-runs` | `/admin/consumer-lag` (worker-dispatcher) | `/admin/dlq/stats` | `/audit/logs?action_prefix=agent.` |
|---|---|---|---|---|
| Baseline (after step 1's reset) | previous runs only, all `active: false` | `lag 0`, `lag_known true`, `recent_samples []` — the reset clears the sample history | `total 4` (`bulk_api_sync 3`, `csv_upload 1`) | climbing ~7 rows per `make world-audit`, all `agent.tool_invoked` under the **smoke** principal |
| Fault injected (step 3) | unchanged — the agent has not started | `lag 3 (age 1 s)`, then `lag 23 (age 0 s)` one interval later | `total 4` | unchanged by the fault: `chaos.*` rows are a different stream, and the agent's principal is not served them (ADR 0012) |
| Agent running (step 5) | a new row appears: `scenario: remediate_consumer_lag_success`, `state` walking `triage → investigating → planning → remediating → verifying → resolved`, `current_hypothesis: consumer_saturation @ 0.85`, `last_step: {kind: read, tool: get_consumer_lag}` | still the stale `23` — the run is faster than the metric | `total 4` | +14 rows in three seconds: 6 × `agent.run_reported` + 1 briefing + 7 × `agent.tool_invoked`, under the **agent** principal |
| Briefing (end of step 5) | same row, `active: false`, `finished_at` set, `briefing.prose` present ("…restarted the consumer group … the follow-up probe shows lag back at 0") | unchanged | `total 4` | quiet |
| Wound down (step 6) | unchanged — a finished run is a record | `lag 0 (age 0 s)` on the first post-drain sample | `total 4` | +7 more `agent.tool_invoked` from the closing `make world-audit` |

`dlq_backlog` differs in exactly two of those cells, and they are the mode's whole story:
`/admin/dlq/stats` reads `total 5` (`bulk_api_sync 4`) the moment `poison_message` lands and
`total 4` again a second later, because the agent replayed the one `replay_safe` row and left
the poisoned one alone; and the whole phase walk finishes inside **81 ms** (`04:16:48.490` →
`04:16:48.616`), so on a recording the console's middle column fills in one frame. The lag
endpoint never moves in that mode.

**Reading `/audit/logs?action_prefix=agent.` correctly:** `agent.tool_invoked` is written for
every MCP read by ANY service account, so the world audit's own reads are in there too. The
principal id is what separates them — the agent acts under
`PLATFORM_AGENT_PRINCIPAL_ID`, the audit reads under `PLATFORM_SMOKE_PRINCIPAL_ID`, and
`agent.run_reported` only ever comes from the agent.

## Steps 1–6, and what each is for

1. **Stack, reset, audit, console URL.** Brings the stack up if it is down, resets the world,
   and runs `make world-audit` as a **gate**: a demo that starts from a world somebody else
   left dirty shows the audience a fault that is not yours. Non-zero stops the demo.
2. **Baseline.** In `consumer_outage`, starts `make traffic` and waits for a healthy reading
   (lag known, small). In `dlq_backlog`, nothing — the world is seeded and quiet. Prints
   **BASELINE — START RECORDING NOW**.
3. **The fault.** Ten-second countdown, then the scenario's own chaos plan fires under the
   chaos principal. Printed, so you can say what fired.
4. **The fault, proved.** Polls the scenario's *own* precondition probes, so "visible" means
   exactly what the grader will later assume it meant. Prints **FAULT VISIBLE**. If the fault
   never appears the demo stops here — nothing was run and nothing was graded.
5. **The agent.** Free path: `evals.runner --mode rehearsal` — the real platform, the real
   Tier-1 action, a scripted planner, zero spend. Paid path: `make eval-live ONLY=<scenario>
   MODEL_ROLE=benchmark`.
6. **Wind down.** Stops traffic, prints the trajectory / briefing / human-report / trace
   paths, waits for the backlog in a traffic mode, resets and re-audits. Prints **DONE — STOP
   RECORDING**.

**Every failure path stops the traffic, resets and re-audits** — including a bug in the demo
machine itself. The one case with no audit after it is a reset that *failed*: auditing a world
nobody put back prints FAIL lines that bury the real event, so it warns loudly instead and the
chaos-teardown latch refuses the next live run.

## The rehearsal mode, and the one thing it is not

`--mode rehearsal` is a third provenance, not a cheap live run ([ADR 0069](ADR/0069-a-rehearsal-is-a-third-provenance-not-a-live-run.md)).
Everything about the world is real — the fault is seeded under the chaos principal, the
preconditions are polled, the Tier-1 action executes, the teardown compensates, both principal
guards run — and the planner is the scenario's scripted one, so **no model is called and
nothing is spent**. What comes out says so in five places:

```
mode: rehearsal — the platform is REAL … and the planner is the scenario's scripted one.
provenance: … platform sha256:328f44a7…, rehearsal
NON-CLOSING: this report cannot close a phase — 1 run(s) are demo rehearsals …
degraded: 1 scenarios, every one of them BY DESIGN …
```

plus `execution_mode: "rehearsal"` and `rehearsal: true` on the row itself. The research report
and the phase-close report **refuse** such a row rather than labelling it, `closing` is never
`True` over one, and `make world-drift`'s pairing rules have no entry for the mode on purpose.

Both 2026-09-20 rehearsals PASSed on all seven graded dimensions with `usd_used 0.000000` and
`tokens_used 0` — and that pass is a statement about the **fixtures and the machine**, never
about the agent. A green rehearsal is the demo working, not the agent working.

## The one paid take

`LIVE=1 YES_SPEND=1`, once, with the owner's yes. Before it: `context/PROTOCOL.md` § "Paid
run, per scenario" verbatim — machinery check, world audit, grading-precision review,
fault-world content review. Rehearse both modes free first; the paid take is for the camera,
not for finding out whether the machine works.

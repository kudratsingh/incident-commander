# Demo runbook — recording the live demo

One fault, one agent, one console, six printed steps. `make demo-live` drives it; this file
is what to have open beside it.

```bash
make demo-live MODE=consumer_outage           # a consumer stops, the backlog climbs
make demo-live MODE=dlq_backlog               # one replayable row, one poisoned row
make demo-live MODE=… AUTO=1                  # rehearsal: no pauses
make demo-live MODE=… LIVE=1 YES_SPEND=1      # the ONE paid take
```

The default path is **free**: the real platform, the real fault, the real Tier-1 action, and
a scripted planner. `LIVE=1` alone refuses (exit 2) — spending needs `YES_SPEND=1` as well,
plus the owner's explicit yes for that scenario, every time. No make target sets `YES_SPEND`.

## Before you record

- [ ] Stack up on the pinned digests: `make demo` (six long-running services).
- [ ] `make bootstrap-token`, and paste all three tokens. **A token minted before platform
      v0.6.13 does not carry `agent_runs:write`,** and reporting is fail-open, so the symptom
      is an empty console rather than an error. If the middle column never fills, this is why.
- [ ] Console open at `http://localhost:3000/demo?mode=<mode>`, logged in as the demo
      operator (the `DEFAULT_EMAIL` / `DEFAULT_PASSWORD` constants in
      `scripts/bootstrap_agent_token.py` — never typed into a file that gets committed).
- [ ] Nothing else running: no `make traffic`, no `evals.runner`, no merge in flight. Step 1
      audits for exactly this and stops if it finds one.
- [ ] Screen recorder ready but **not started** — step 2 tells you when.

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

## Timings, measured

`dlq_backlog`, rehearsal on platform v0.6.13, `AUTO=1` (so no operator pauses):

| Step | | Seconds |
|---|---|---|
| 1 | stack check, reset, world audit, console URL | 2.1 |
| 2 | baseline (nothing to start in this mode) | 0.0 |
| 3 | inject the fault (10 s of that is the countdown) | 10.7 |
| 4 | wait for the fault to become visible | 0.4 |
| 5 | run the agent (scripted planner) | 0.9 |
| 6 | wind down: stop traffic, reset, re-audit | 2.1 |
| | **total** | **16.2** |

Read those as the machine's own overhead, not as the demo's length. Two things stretch it in
a real take: the pauses (you are talking), and `consumer_outage`'s step 2 and step 4, which
wait on real physics — the backlog has to build past 20 and the platform recomputes lag on a
60-second interval, so budget up to 2.5 minutes there (the scenario polls ten times at 15 s).

`consumer_outage`'s rehearsal timings are **not yet measured** — see the gap noted at the
bottom of this file.

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
5. **The agent.** Free path: the runner with a scripted planner. Paid path: `make eval-live
   ONLY=<scenario> MODEL_ROLE=benchmark`.
6. **Wind down.** Stops traffic, prints the trajectory / briefing / human-report / trace
   paths, resets and re-audits. Prints **DONE — STOP RECORDING**.

**Every failure path stops the traffic, resets and re-audits** — including a bug in the demo
machine itself. The one case with no audit after it is a reset that *failed*: auditing a world
nobody put back prints FAIL lines that bury the real event, so it warns loudly instead and the
chaos-teardown latch refuses the next live run.

## The one paid take

`LIVE=1 YES_SPEND=1`, once, with the owner's yes. Before it: `context/PROTOCOL.md` § "Paid
run, per scenario" verbatim — machinery check, world audit, grading-precision review,
fault-world content review. Rehearse both modes free first; the paid take is for the camera,
not for finding out whether the machine works.

## Known gap

**`consumer_outage` has not been rehearsed end to end, and the reason is a runner property
worth knowing before you try.** The free "scripted planner + live platform" path does not
exist today, in either direction:

- without `--live`, `runner._settings_for_mode(live=False)` returns `_eval_defaults()`, which
  **hardcodes** `platform_mcp_url=https://eval.local` and `platform_token="eval"` with
  `_env_file=None`. A real `PLATFORM_MCP_URL` in the environment cannot reach it, so the run
  is fully canned — no hook fires and no platform is touched;
- with `--live` and a blanked `ANTHROPIC_API_KEY`, the preflight refuses at exit 3
  ("`--live` but 1/1 scenario(s) would degrade to canned"), which is a guard worth keeping:
  it exists so a misconfigured paid run cannot produce a report indistinguishable from a
  live-green one.

So the `dlq_backlog` rehearsal that ran exercised **steps 1–4 and 6 against the real v0.6.13
platform** — real reset, real audit, real `poison_message`, real precondition, real reset and
re-audit — and step 5 fully canned. The reporter itself was proved separately against the live
platform under the re-minted token (six phase reports plus a briefing, all accepted, readable
through `GET /api/v1/admin/agent-runs/<id>`).

Closing the gap is a deliberate runner change and its own decision: a third mode, narrower
than `--live`, that takes the real platform settings and a scripted planner without
satisfying the paid-run preflight. It must not weaken the exit-3 guard.

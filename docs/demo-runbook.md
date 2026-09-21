# Demo runbook — recording the live demo

One fault, one agent, one console, six printed steps. `make demo-live` drives it; this file
is what to have open beside it.

```bash
make demo-live MODE=consumer_outage           # a consumer stops, the backlog climbs
make demo-live MODE=dlq_backlog               # one replayable row, one poisoned row
make demo-live MODE=… AUTO=1                  # rehearsal: no pauses
make demo-live MODE=… HOLD=90                 # keep the finished run on screen longer (default 60)
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
- [ ] `PLATFORM_SMOKE_TOKEN` set, and this one is a REFUSAL rather than a degradation
      (ADR 0074): every read the runner makes — the baseline wait, the fault watch, the
      precondition poll, the drain wait, and `make traffic`'s own lag read — is made
      under the read-only principal, and the script stops with
      `PLATFORM_SMOKE_TOKEN is not set … it will not fall back` if it is missing. The fallback
      it replaced is what flooded the 2026-09-20 take's action ledger: a `get_consumer_lag`
      every three seconds under the AGENT principal, which the page cannot tell from the
      agent's own four reads.

      **Since WO-R3-342 the right token is not enough — each of those reads also says it is the
      lab's.** They carry a `_lab_probe` reason and the lab credential, so the platform files
      them as `lab.probe` rather than `agent.tool_invoked` (platform ADR 0038). The fifth take
      had the right token and no label, and the page — which with no run selected counted every
      tool row as the agent's — drew 39 of them as the agent acting before the fault existed.
      **The refusal to know about:** an unhonoured label is refused with `-32602` and the call
      does NOT run, so if the demo stack's smoke account is ever renamed away from
      `lab_probe_smoke_account_name` every wait in the script goes blind at once. The tell is a
      baseline wait that never sees a known lag; the check is one `lab.probe` row from
      `make world-audit`, which wears the same credential.
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

The reporter sends the whole run, not just its phase ([ADR 0072](ADR/0072-a-run-report-carries-one-step-and-an-older-platform-gets-fewer-fields.md), amending ADR 0068). Per
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

**On v0.6.16 and later this section is history**: the fields are declared, the reports land
whole, and the fallback below is dormant. It stays because the two repos are versioned
independently — a demo stack can be older than the commander talking to it — and because the
failure it prevents has happened once.

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

### Re-measured on v0.6.18, where the clock is a setting and the platform pages itself

> **History as of WO-R3-342.** The producer no longer speeds up when the fault fires — see
> "Re-measured on v0.6.19" below for why the acceleration was the thing that made the fifth
> take's chart stop climbing, and for the numbers that replace the ones in this section.

Owner decisions O-35 and O-36 (WO-R3-339, [ADR 0076](ADR/0076-the-demo-takes-the-platforms-page.md)).
`demo/compose.yml` sets `METRICS_LOOP_INTERVAL_SECONDS=5`, the producer is restarted at **0.75 s
once the fault fires** (`make traffic RATE=0.75`), and step 3 now waits for the PLATFORM's own
alert row as well as for its reading. Both modes rehearsed again, 2026-09-21, `AUTO=1`, both
PASS, world audit PASS before and after each. Runs `2534a5f3-9ef6-5bd2-83d1-f425756aa6f4`
(`consumer_outage`) and `97423f0e-bf67-5abf-899a-955ef475d094` (`dlq_backlog`):

| Step | | `dlq_backlog` | `consumer_outage` |
|---|---|---|---|
| 1 | stack check, reset, world audit, console URL | 4.2 s | 3.9 s |
| 2 | baseline (`consumer_outage` also starts `make traffic` at 3 s) | 0.0 s | 0.1 s |
| 3 | inject the fault (10 s countdown), wait for the reading **and for the page** | 17.2 s | 31.3 s |
| 4 | prove the premise the scenario grades against | 0.6 s | 0.5 s |
| 5 | run the agent (scripted planner) | 1.8 s | 1.8 s |
| 6 | wind down | 3.7 s | 8.8 s |
| | **total** | **27.5 s** | **46.4 s** |

`consumer_outage` went from 170.5 s to **46.4 s**; `dlq_backlog` rose 19.4 s → 27.5 s, because
step 3 now waits for a page it did not wait for before. The interesting numbers are not in that
table, though. They are the platform's own audit stamps, and they are what "watchable" means:

| | `consumer_outage` | `dlq_backlog` |
|---|---|---|
| fault (`chaos.tool_invoked`) | 08:55:53.648 | 08:57:04.715 |
| the platform's page (`alert.raised`) | 08:56:13.279 | 08:57:09.880 |
| **fault → page** | **19.63 s** | **5.16 s** |
| the agent starts (`phase_history[triage]`) | 08:56:16.074 | 08:57:13.061 |
| **page → agent** | **2.79 s** | **3.18 s** |
| the Tier-1 action | 08:56:16.273 | 08:57:13.30 |
| the world is measured recovered | lag `0` at 08:56:23.366 | `total 3` in the agent's own next read |
| **action → recovered** | **7.09 s** (two metrics passes, via a reading of 8) | **~30 ms** (the replay is synchronous) |

**`fault → page` is arithmetic, not luck, and getting it that way cost a measurement.** The
threshold is 20 messages and the producer arrives every 0.75 s, so the backlog needs ~15 s; the
rest is the producer's own restart. `POST /jobs` is rate-limited per identity in a **fixed
60-second window of 30 creations**, which means every job the BASELINE spends is one the backlog
cannot have — and the first version of this ran the whole walk at 0.75 s, so ten seconds of
countdown traffic left only **17** of the 30, the lag stalled at 17 until the window rolled, and
`fault → page` read **56.1 s**. Two consecutive runs of the phased version read 19.86 s and
19.63 s. **If that margin is ever wanted, the lever is the rate and not the interval:**
`RATE=0.5` needs 10 s of arrivals and still fits the window (≈3 baseline + ≈4 during the
restart + 20 = 27 of 30).

**`page → agent` is the demo machine, not the platform** — step 4's precondition poll plus the
runner's own start-up (settings, three principal guards, the premise reads). It is the one leg a
paid take does not lengthen.

**What the rehearsal does NOT measure, and it matters for reading the table.** The scripted
planner has no latency and the scripted verify judge returns `verified` on its first look, so
the whole phase walk lands inside **261 ms** (`consumer_outage`) and **345 ms** (`dlq_backlog`).
On `consumer_outage` the agent's own verify read still showed `lag 23` — the cached value, one
pass old — and the script said verified anyway. So the recovery in the table is the PLATFORM's
measurement seven seconds later, not the agent's verdict. In a paid take the real judge reads
the same 23 and polls again (`VERIFY_PROBE_ATTEMPTS`), which is the behaviour this rehearsal
cannot show. A green rehearsal is the demo working.

**Who resolved the page differs by mode, and the counter is what says which.** On
`consumer_outage` the RULE resolved its own episode: the sample at 08:56:18.316 read `8`, below
the threshold of 20, `alert.resolved` landed at 08:56:18.324, and the wind-down's reset reported
`rule_alerts_resolved: 0` — it had nothing left to close. On `dlq_backlog` the take ended first,
so the reset closed it (`rule_alerts_resolved: 1`, 0.6 s before that take's `lab.world_reset`).
Read the counter, not the row: a resolve inside a take proves the rule only when the reset
reports nothing resolved.

**The drain, at the faster rate.** `consumer_outage` builds ~23 messages of backlog, and the
two samples after `restart_consumer_group` read `8` then `0` — so the drain is **7.09 s**
against a wind-down wait of 150 s, and the intermediate reading is what makes the console's
chart show a drain rather than a step. The wind-down's own wait needed one poll
(`backlog drained: lag 0, age 1s`). On `dlq_backlog` one `replay_dlq_by_category(replay_safe)`
took the queue from 7 rows to 3 in the agent's next read, with the alerted slice at `total 0`.

Step 6 is the same clock in reverse — the agent's restart drains the backlog in seconds, but
the next sample is up to a minute away, so the machine **waits for a fresh `0` before
auditing** and only then runs `make world-audit`. Without that wait the audit reads the value
taken while the consumer was still dead: an early rehearsal ended on
`[FAIL] worker-dispatcher lag: 33 (want 0)` over a world that was already clean, and the same
read was `0` twenty seconds later. A timeout (150 s) warns and audits anyway — waiting must
never be a way to declare the world fine. The fault watch in step 3 has the same rule for the
same reason: a timeout (180 s) WARNS, and the precondition below it is the gate.

### Re-measured on v0.6.19, where the backlog climbs without stalling and the run stays on screen

WO-R3-342, from the fifth take's four commander findings. The platform image is v0.6.19 and
its BACKEND is v0.6.18's behaviour unchanged — `make snapshot` against the live stack showed no
diff at all, 40 tools — so nothing below is a platform change. Both modes rehearsed
2026-09-21 with `AUTO=1 HOLD=5`, both PASS, `make world-audit` PASS before and after each. Runs
`835ef73e-99f5-57a0-a2c8-597806efd122` (`consumer_outage`) and
`ab2406d1-49e7-5c44-aa6a-0a24e7cb7a86` (`dlq_backlog`):

| Step | | `dlq_backlog` | `consumer_outage` |
|---|---|---|---|
| 1 | stack check, reset, world audit, console URL | 3.0 s | 3.0 s |
| 2 | baseline (`consumer_outage` starts `make traffic` at 2.0 s) | 0.0 s | 0.0 s |
| 3 | inject the fault (10 s countdown), wait for the reading and for the page | 14.0 s | 51.3 s |
| 4 | prove the premise the scenario grades against | 0.5 s | 0.4 s |
| 5 | run the agent (scripted planner) | 1.4 s | 1.4 s |
| 6 | wind down, **including a 5 s hold** | 7.9 s | 8.1 s |
| | **total** | **26.7 s** | **64.2 s** |

**The chart climbs the whole way now, and that is the point of the change.** The platform's own
15-minute sample ring for `worker-dispatcher`, at its 5-second tick, across the
`consumer_outage` take:

```text
fault 12:22:21.788 → 0  0  2  4  7  10  12  15  17  20 → restart 12:23:04.084 → 0
```

Nine strictly increasing samples and **zero flat ones** between the fault and the restart. The
fifth take's series was `0 → 5 → 11 → 18 → 24 → 28` and then **flat at 28 for about 25
seconds**, because the producer had been restarted at 0.75 s once the fault fired, spent that
minute's whole allowance in 22 s, and collected 429s until the window rolled.

**Why one rate for the whole take.** `POST /jobs` allows 30 creations per FIXED 60-second window
per caller address — `rate_limiter(limit=30, window=60, key_prefix="jobs:create")` in the
platform's `backend/app/api/jobs.py`, a **literal rather than a setting**, and keyed on the
address rather than on the identity, so no token arrangement widens it. 30 a minute is therefore
the sustained ceiling and there is no faster fault-phase rate to switch to: the acceleration was
borrowing from a window it then had to repay. So `make demo-live MODE=consumer_outage` starts ONE
producer at `RATE=2.0` in step 2 and never restarts it, and `scripts/traffic_loop.py` reads the
same clock the platform cuts its window from and spreads what is left of the allowance over what
is left of the window (`WindowPacer`), which is what makes "never refused" a property rather
than a hope. A 429 — which now only another producer can cause — marks the window spent instead
of being asked for again.

**The arithmetic to know before a take, because it is a ceiling and not a tuning knob:** the
backlog grows one job every two seconds, so **lag 20 takes 40 s** and **lag 40 takes 80 s** from
the moment the consumer dies. In this rehearsal the platform paged at lag 20, **39.8 s** after
the fault, and the agent's Tier-1 restart landed **42.3 s** after it, with the last sample before
the restart reading **20**. A deeper backlog than that before the agent acts is not available
from one machine at this limit: it needs either the platform's literal to become a setting (then
0.75 s is honest again and lag 40 arrives in 30 s) or a deliberate wait for a deeper backlog
before the run starts, which puts a visible gap between the page and the response. Neither is a
builder's decision.

| | `consumer_outage` | `dlq_backlog` |
|---|---|---|
| take boundary (`lab.world_reset`) | 12:22:10.165 | 12:23:14.836 |
| fault (`chaos.tool_invoked`) | `kill_consumer` 12:22:21.788 | `seed_dlq_messages` 12:23:26.481 |
| the platform's page (`alert.raised`) | `consumer_stalled` 12:23:01.580 | `dlq_depth_warning` 12:23:27.556 |
| **fault → page** | **39.79 s** | **1.08 s** |
| the agent's first own call | 12:23:04.028 | 12:23:31.369 |
| **page → agent** | **2.45 s** | **3.81 s** |
| the Tier-1 action | `restart_consumer_group` 12:23:04.084 | `replay_dlq_by_category` 12:23:31.461 |
| `alert.resolved` | 12:23:07.111 | 12:23:32.881 |

**The audit sequence of a take is now exactly what a reader would draw.** Per take: **one**
`chaos.tool_invoked` with `outcome: success` and no `lab_probe_reason` (the fault), **one**
`alert.raised`, and **zero** `agent.tool_invoked` rows before the run's own first call. The
fifth take had **39** rows before it — `get_consumer_lag` every few seconds under the read-only
principal, from this script's own waits and from the traffic loop's tick line — and with no run
selected the page drew them as the agent acting before the lab had injected anything. Those reads
now carry a `_lab_probe` reason (`demo: baseline lag poll`, `demo: fault watch read`,
`traffic: lag read`) plus the lab credential, so the platform files them as `lab.probe`, which is
withheld from the agent and hidden by the console. The traffic loop also reads the lag only when
something will use it — the `--until-lag` stopping condition or a printed tick — rather than on
every submission; 119 of the take's 162 rows were that read and nothing looked at most of them.

**A verdict reaches the console with the step that produced it.** In the fifth take the
`verify_judge` thinking steps were live but the `verification` entries they announced arrived
with the terminal report, 22 seconds after the readings they judged (F3). Report 11 of 13 in this
rehearsal carried step 6 (`report verify_judge`, stamped 12:23:04.130) **and** that step's
verdict in the same payload, and it landed at 12:23:04.132 — **2 ms**. The report that closes the
run carries no verification, deliberately: the platform never clears that field on omission
([platform ADR 0037](https://github.com/kudratsingh/incident-platform)), so re-sending it would
append the same verdict to the append-only `verifications` ledger twice and the console would
draw the last verdict twice. The run record holds exactly one entry.

**The finished run stays on screen.** Step 6 prints the run id, the deep link and the artefact
paths, then HOLDS the world for `HOLD` seconds (default **60**) before it stops the traffic,
resets and re-audits. The fifth take's run left the page eleven seconds after it resolved,
because the wind-down's reset writes the next take's boundary and the page follows the newest
take. Ctrl-C during the hold skips the rest of it and the world still goes back — it is the
operator saying "I have read it", not a reason to leave a dirty world. `HOLD=0` resets at once
and a negative value is refused at parse time.

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

**The DLQ mode's scenario changed with v0.6.18, and it is worth knowing before a take.** It is
`demo_dlq_replay_safe_backlog` now, not `remediate_dlq_backlog_success`. The platform's depth
rule names the category carried by the rows above the seeded baseline, and the old world's one
injected row is unclassified on purpose — so the alert the platform can honestly raise there is
the `unclassified` incident and the honest action is to fence the row, which is the opposite of
"the queue frees up while you watch". The new world seeds three transient `replay_safe` rows
instead: depth 7, the platform pages naming `replay_safe`, and one
`replay_dlq_by_category(replay_safe)` clears four rows and takes the depth back under the
threshold. `remediate_dlq_backlog_success` is unchanged and still graded — it is simply not this
demo's story (ADR 0076 decision 2).

`dlq_backlog` differs in exactly two of those cells, and they are the mode's whole story:
`/admin/dlq/stats` reads `total 5` (`bulk_api_sync 4`) the moment `poison_message` lands and
`total 4` again a second later, because the agent replayed the one `replay_safe` row and left
the poisoned one alone; and the whole phase walk finishes inside **81 ms** (`04:16:48.490` →
`04:16:48.616`), so on a recording the console's middle column fills in one frame. The lag
endpoint never moves in that mode.

**Reading `/audit/logs?action_prefix=agent.` correctly:** `agent.tool_invoked` is written for
every unlabelled MCP read by ANY service account, so the demo runner's own polling reads are in
there too. The principal id is what separates them — the agent acts under
`PLATFORM_AGENT_PRINCIPAL_ID`, the runner polls under `PLATFORM_SMOKE_PRINCIPAL_ID`, and
`agent.run_reported` only ever comes from the agent.

**Since platform v0.6.17 the lab's own probes are in a different stream** (`lab.probe`, platform
ADR 0038; the commander sends the label from `evals/guards.py` and `evals/world_audit.py`,
WO-R3-335), so the query to read first is `action_prefix=lab.probe`: it holds every read
`make world-audit` made and every principal-guard probe, including the one that wears the
AGENT's token on purpose, and none of them can be mistaken for the agent's work any more.

Measured on the 2026-09-20 rehearsals on v0.6.17 (`consumer_outage`, run
`bfae3a5e-fad5-548b-8d2a-218b8220b12e`; `dlq_backlog`, run
`24d6b1de-c057-5017-90cc-884329e3da4b`), per mode, over the whole walk:

- `action_prefix=lab.probe` — **15 rows**: 13 world-audit reads (`world audit read`, smoke
  principal, two audits per walk — before and after) plus the read-only guard's own
  `mark_dlq_permanent` in each of them, and **one `mark_dlq_permanent` under the AGENT
  principal** carrying `principal guard: proves the agent token can execute a Tier-1 action`.
  That last row is the one this used to need from the platform, and it is now labelled.
- `action_prefix=agent.` — **only the run and the runner's polling.** `consumer_outage`: the
  agent's `get_consumer_lag` ×3 and `restart_consumer_group` ×1, ten report rows, and 59
  `get_consumer_lag` reads under the SMOKE principal (the runner's 5-second lag watch plus
  `make traffic --until-lag`). `dlq_backlog`: the agent's `list_dlq_messages` ×5 and
  `replay_dlq_by_category` ×1, eleven report rows, and 3 smoke-principal `list_dlq_messages`
  (the runner's DLQ watch). **Nothing from the guards or the world audit.**
- `action_prefix=chaos.` — 4 rows: the two hook firings, plus the chaos guard's `inject_latency`
  error and the agent's denied `inject_latency`, both carrying their guard's reason in
  `extra_data`. A `chaos.*` row keeps its own action and is never relabelled: to the `/demo`
  page the newest `chaos.*` row IS the fault.

The runner's own polling reads stay `agent.tool_invoked` under the smoke principal — the page
excludes them by principal (they are not the run's), which is why they were left unlabelled.

## Steps 1–6, and what each is for

1. **Stack, reset, audit, console URL.** Brings the stack up if it is down, resets the world,
   and runs `make world-audit` as a **gate**: a demo that starts from a world somebody else
   left dirty shows the audience a fault that is not yours. Non-zero stops the demo.
2. **Baseline.** In `consumer_outage`, starts `make traffic` at the mode's one rate (`RATE=2.0`,
   the platform's own sustained ceiling for creating jobs) and waits for a healthy reading (lag
   known, small). That producer is never restarted, so the fault changes what the arrivals MEAN
   and not how fast they come. In `dlq_backlog`, nothing — the world is seeded and quiet. Prints
   **BASELINE — START RECORDING NOW**.
3. **The fault, and then the page.** Ten-second countdown, then the scenario's own chaos plan
   fires under the chaos principal — printed, so you can say what fired. Then TWO waits, and
   they are different claims: the platform's own READING crossing the threshold (the source the
   chart draws from is showing a breach) and the platform's own ALERT ROW (it has paged). The
   second is new in WO-R3-339 and it prints the alert id, the time and the summary, because
   that row is what step 5 starts the run from. Both warn rather than fail; step 5's own wait
   is the gate and it refuses with the alert stream's contents in the message.
4. **The fault, proved.** Polls the scenario's *own* precondition probes, so "visible" means
   exactly what the grader will later assume it meant. Prints **FAULT VISIBLE**. If the fault
   never appears the demo stops here — nothing was run and nothing was graded.
5. **The agent, paged by the platform.** Free path: `evals.runner --mode rehearsal
   --world-already-faulted --alert-from-platform` — the real platform, the real Tier-1 action,
   the platform's real alert, a scripted planner, zero spend. Paid path: `make eval-live
   ONLY=<scenario> MODEL_ROLE=benchmark WORLD_ALREADY_FAULTED=1 ALERT_FROM_PLATFORM=1`. The run
   starts from the alert ROW's payload verbatim rather than from the scenario file's `alert:`
   block (O-36, [ADR 0076](ADR/0076-the-demo-takes-the-platforms-page.md)); the grade does not
   move, because the graders key on the terminal state, the audit log and the readings.
6. **Wind down.** Prints the run id, its deep link and the trajectory / briefing / human-report
   / trace paths, then **HOLDS the world for `HOLD` seconds (default 60)** so the finished run
   stays on the page — the reset opens the next take and the page follows the newest one, which
   is how the fifth take's run left the screen eleven seconds after it resolved. Then it stops
   traffic, waits for the backlog in a traffic mode, resets and re-audits. Ctrl-C during the hold
   skips the rest of it; the world still goes back. Prints **DONE — STOP RECORDING**.

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

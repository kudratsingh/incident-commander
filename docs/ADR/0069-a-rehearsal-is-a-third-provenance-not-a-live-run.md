# ADR 0069 — a rehearsal is a third provenance, not a live run

Status: accepted (2026-09-19, WO-R3-316)

## Context

`make demo-live` (ADR 0068) drives the demo as six printed steps: reset and audit the world,
show a healthy baseline, break it on a countdown, prove the fault is visible, run the agent,
wind down. It has to be walked end to end before it is walked on camera — that is what a
rehearsal is for — and a rehearsal that spends a real model's money every take is a rehearsal
nobody does twice.

Its builder assumed the free path already existed: "invoke the runner without `--live` and
with the API key blanked". Neither half of that works, and the reason each fails is a guard
worth keeping:

- **without `--live`**, `runner._settings_for_mode(live=False)` returns `_eval_defaults()`,
  which hardcodes `platform_mcp_url=https://eval.local` and `platform_token="eval"` with
  `_env_file=None`. That is deliberate: a real `PLATFORM_SMOKE_TOKEN` once leaked into an
  "offline" run through the cwd `.env` (finding A-04). So a real `PLATFORM_MCP_URL` cannot
  reach an offline run at all, and the rehearsal was **fully canned** — no hook fired, no
  platform was touched, and the only thing rehearsed was the shell script around it.
- **with `--live`** and a blanked key, the preflight refuses at exit 3 ("`--live` but 1/1
  scenario(s) would degrade to canned"). That guard exists so a misconfigured paid run cannot
  produce a report indistinguishable from a live-green one (findings A-01/S-09).

So the free "real platform + scripted planner" combination was unreachable, and `#315`
correctly stopped rather than weakening either guard. `consumer_outage` went unrehearsed.

Two facts shape the decision. First, the combination is genuinely useful beyond the demo: it
exercises seeding, settling, preconditions, the two-principal guards, the real Tier-1 action,
teardown and the reset — every part of a live run except the model. Second, it is genuinely
dangerous as a report: a row from it passes, names a diagnosis, spends tool calls and reaches
a terminal state, all of it decided by the scenario's own fixture. Read as a measurement it
is not a weak number, it is an invented one.

## Decision

**1. A third mode, `--mode rehearsal`, and the `--mode` flag is where it lives.**
The runner's two flag-selected modes are the two that cannot be inferred from what a run can
reach: `recorded` names a FILE, and `rehearsal` names a deliberate withholding. `live` and
`canned` stay inferred from the environment, because a second way to ask for them is a second
thing that can disagree with the first. The two named modes are mutually exclusive and refuse
each other at the CLI *and* inside `run_scenario`: a recording replaces the platform and keeps
the model, a rehearsal keeps the platform and replaces the model, and a run claiming both has
no real leg left to be about.

**2. The model leg is withheld twice, and the second time is the guarantee.**
`_settings_for_mode(live=False, rehearsal=True)` reads the real environment — the platform
half has to be real — and replaces `anthropic_api_key` with the same placeholder an offline
run carries, so the scripted planner is selected by the seam offline runs already use and
these settings hold no key a live client could be built from. On top of that, `run_scenario`
takes the flag and computes `live_llm_available` as `not rehearsal and …`, so a caller that
passes a real key directly still gets `CannedLLMClient` for all seven roles. Belt and braces,
because the failure being ruled out is a rehearsal quietly spending money on camera.

**3. The platform leg is NOT weakened, and every gate a live run has, a rehearsal has.**
The fault is seeded for real, the preconditions are polled for real, the Tier-1 action
executes for real and the teardown compensates for real. Therefore the rehearsal is on the
live side of every guard that protects the shared world, not the free side: the
one-mutating-scenario gate (ADR 0020), the mandatory `--only` with full-name matching, the
two-principal guards (the agent can act and cannot seed; the evaluator can seed), the
contaminated-world latch before the run, and the teardown latch after it. A guard skipped
because the run was free would be discovered by the paid take.

**4. Its own `ExecutionMode` member, and a `rehearsal` flag on the provenance beside it.**
`ExecutionMode.REHEARSAL` is appended — archived reports are read back against this enum — and
it is checked BEFORE `LIVE`, whose test a rehearsal would otherwise satisfy, because its
platform leg really did run live. That single fact is what makes every existing reader safe by
default: they all ask `execution_mode == "live"`. `RunProvenance.rehearsal` is redundant with
the mode by construction and carried anyway, as the claim a row makes about itself that a
reader can assert on without knowing the enum's fourth member exists.

**5. Every row is `degraded=True`, unconditionally.**
Not as a consequence of the canned model leg: a scenario declaring `use_live_llm: false`
degrades nothing by running canned, and such a row would otherwise be the one row in a
rehearsal archive a reader could mistake for a measurement. The crash row carries it too.

**6. Two report assemblers refuse a rehearsal row rather than labelling it.**
Every other limitation in the research report is a sentence in its own "what this cannot say"
list, because those are weak measurements. This is not one, so `research_report.assemble`
raises `RehearsalRefused` naming the archive to remove from `SCOPE`; `phase_close_report`
refuses a rehearsal archive as a live leg and `closing_verdict` returns non-closing with the
remedy spelled out ("replace it with a live run; re-running the same invocation cannot help").
`RunReport` itself refuses `closing=True` over a rehearsal row, the same shape as its
existing refusal over a development-role row, and `run_all` never marks one closing — so the
model validator is a backstop rather than a trap that fires after a fault is already seeded.
`candidate_metrics` needs no change and gets none: `rehearsal` is deliberately absent from its
`_WORLD_INSTANCE` map, so the refusal it already writes for an unknown mode ("a mode with no
rule must not fall back to a pairing rule chosen for another mode") is the behaviour.

## Alternatives rejected

- **Blank the key for the subprocess and leave the runner alone.** What `#315` tried. It
  cannot work: without `--live` the platform URL is hardcoded to the placeholder, so the run
  is canned end to end. The row it produced said `canned` and `degraded`, both true, and the
  demo it "rehearsed" had fired no hook.
- **Relax the exit-3 degraded-leg preflight to allow a canned model leg under `--live`.**
  This is the one thing the packet forbids, and rightly: that guard is the difference between
  a misconfigured paid run failing loudly and producing a report nobody can distinguish from
  a green one (A-01/S-09). A new mode costs a third branch; weakening that costs the meaning
  of every live report.
- **Reuse `ExecutionMode.LIVE` with the `rehearsal` flag alone.** Cheaper by one enum member
  and wrong in the direction that matters: every reader counting live rows counts by the mode
  string, so the default behaviour of a new flag nobody has read yet would be "counted as
  live". The narrowest true statement goes in the field readers already consult.
- **A separate scenario variant per demo mode.** Duplicates the fixture, and the duplicate
  would drift from the scenario the paid take actually runs — the demo would rehearse a
  different world than it records.
- **Let the research report label the row instead of refusing it.** A labelled row is still
  in the denominator of something. There is no honest number a scripted planner's pass rate
  contributes to.

## Consequences

- The demo's free path is real: both modes were rehearsed end to end against live platform
  v0.6.13, hooks fired, the Tier-1 action executed, `make world-audit` PASSed after each.
  Timings and the operator-endpoint readings per phase are in `docs/demo-runbook.md`.
- A fourth `ExecutionMode` member exists, and any future reader that switches on the enum has
  four cases to answer for. The compensation is that the three readers which must refuse a
  rehearsal now do so by name, with tests.
- `make demo-live`'s free path costs one extra guarantee to state and no extra spend. The paid
  take still needs `LIVE=1 YES_SPEND=1` and the owner's yes, every time.
- A rehearsal run writes a real archive under `evals/runs/`, append-only like every other. It
  is evidence of the demo machine, never of the agent, and it says so in three places on
  every row.

Implemented by PR #317.

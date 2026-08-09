# ADR 0013: Run provenance is part of the eval result

* Status: accepted
* Date: 2026-08-09
* Decider: Kudrat Singh

## Context and problem statement

The runner's canned-fallback degradation was print-only. `use_live_mcp` / `use_live_llm`
scenarios silently fall back to canned clients when `PLATFORM_MCP_URL` is the
`eval.local` placeholder or `ANTHROPIC_API_KEY` is empty/placeholder — by design, that is
what keeps offline `make eval` deterministic. But the *fact* of the fallback lived only in
one stdout line: `ScenarioOutcome` and `RunReport` carried no live/canned fields, and the
exit code depended only on `report.failed`. A `--live` invocation with a verbatim
`.env.example` copy (which ships `ANTHROPIC_API_KEY=` empty) ran 32/37 scenarios canned,
printed PASS, exited 0, and wrote a `latest.json` byte-indistinguishable from a live-green
run (audit findings A-01, S-09). Adjacent holes in the same preflight surface: `--smoke`
without `--live` ran the whole suite canned under placeholder settings whose
`model_validate` construction silently absorbed the real `PLATFORM_SMOKE_TOKEN` from the
cwd `.env` (A-04); a broken `--live` env died with a pydantic traceback and exit 1, the
code reserved for "a scenario failed" (A-15); and the placeholder check was a substring
match, so any URL *containing* `eval.local` degraded silently.

A measurement that cannot distinguish "measured the real system" from "measured the
canned model of it" is not evidence. The question is where that distinction must live.

## Decision drivers

* Every consumer of `latest.json` and the archive (regression gate, baseline blessing,
  study conclusions) can otherwise be built on a run that never touched the platform or
  the model — the prime shape behind "green in CI / red live".
* Artifacts outlive stdout. The archive under `evals/runs/` is the durable record
  (invariant 9); a property that matters must be in the artifact, not the scrollback.
* A misconfigured `--live` run must cost zero tool calls and zero dollars — refusal has
  to happen before the first scenario, not after the suite completes.
* `evals/{runs,reports}/` are append-only evidence: the committed `baseline.json` and
  every archived report must keep parsing without rewrites.
* An opt-out flag (`--allow-degraded`) is the bypass pattern
  `test_guards.py::TestNoOptOut` exists to forbid; no documented workflow needs a
  partially-live run.

## Considered options

1. Keep degradation print-only; rely on operators reading stdout.
2. Persist provenance in the report only (schema change, no exit-code change).
3. Persist provenance AND make `--live` refuse (exit 3) any env that would degrade a
   selected scenario, before anything runs (chosen).
4. Option 3 plus an `--allow-degraded` escape hatch.

## Decision outcome

Option 3. Provenance is part of the result schema, and `--live` refuses to produce a
degraded artifact at all.

### Report schema: the artifact records its own provenance

Per-scenario, on `ScenarioOutcome` (all defaulted, so pre-schema artifacts keep parsing):

* `live_mcp: bool = False` — the MCP leg actually ran against a live platform
* `live_llm: bool = False` — the LLM leg actually ran against the live API
* `degraded: bool = False` — the scenario *declared* a live leg but that leg ran canned

Report-level, on `RunReport`:

* `degraded_count: int | None = None` — `None` means "pre-schema report, unknown",
  deliberately distinct from `0`, "verified fully live". The committed baseline was a
  32/37-degraded canned run; defaulting to `0` would make that artifact assert a
  falsehood — the exact honesty failure these fields exist to fix.
* `only_patterns: tuple[str, ...] = ()` — which `--only` filters produced the report, so
  filtered runs self-describe in `latest.json` and in the archive.

`_print_summary` prints the degraded line from `report.degraded_count`, so the console
number and the persisted number are the same value by construction.

`baseline.json` is NOT rewritten (append-only evidence; ADR 0011 freeze). Defaults carry
backward compatibility — it parses today with `degraded_count=None` — and the next
deliberate `make baseline` bless picks the new fields up automatically.

### Preflight: `--live` refuses a degraded or unparseable env

All refusals exit **before** `run_all` — no scenarios run, nothing is spent:

* `--smoke` without `--live` → exit 3, before settings load. Smoke-without-live would run
  the whole suite canned under placeholder settings; erroring beats silently implying
  `--live`, which would flip the settings source to the real `.env` behind the
  operator's back.
* `--live` with any selected scenario that would degrade → exit 3, naming the offline leg
  (placeholder `PLATFORM_MCP_URL`; empty/placeholder `ANTHROPIC_API_KEY` — `.env.example`
  ships it empty, the exact trigger). This also kills the worse chimera S-09 documented:
  real MCP + chaos hooks driven by a canned LLM.
* `pydantic.ValidationError` from settings under `--live` → labeled
  `PREFLIGHT FAIL (env)` line + exit 3 instead of a traceback and exit 1.
* `--smoke` against a placeholder platform (reachable when the `--only` selection
  contains no live-declaring scenario) → exit 3: there is no live principal to guard.

There is deliberately no `--require-live` / `--allow-degraded` flag pair: refusal is the
only behavior, per the no-opt-out principle above.

Supporting hygiene: `_is_offline_placeholder` is an exact-host match
(`urlparse(url).hostname == "eval.local"`), not a substring check — 
`https://eval.local.evil.example` now counts as live. `_eval_defaults` builds placeholder
`Settings` via the constructor with `_env_file=None` and `platform_smoke_token=None`
pinned, so offline settings can no longer absorb `.env` values at all
(`model_validate` treats `_env_file` as data and silently drops it under
`extra="ignore"` while still consulting the dotenv — the A-04 mechanism).

### The exit-code contract

| Code | Meaning | Emitted by |
|---|---|---|
| 0 | all selected scenarios passed | runner; regression gate clean |
| 1 | ≥1 scenario failed (or regression detected) | runner post-run; regression gate |
| 2 | nothing to compare: no scenario matched `--only`; missing/incomparable report | runner; regression gate |
| 3 | preflight/env failure: smoke-without-live, degraded `--live` env, invalid/missing settings, missing smoke token, LLM auth preflight | runner, pre-run |
| 4 | principal guard: smoke token holds more than read scope | runner, pre-run |
| 5 | post-stage audit failed or unreadable | runner, post-run |

Codes 3/4/5 pre-date this ADR (smoke-token check, LLM auth preflight, principal guard,
post-stage audit); this ADR extends 3 to every env-shaped failure and records the full
contract in one place. Plain offline runs (no `--live`) are untouched: degradation is
expected there, still exits 0, and is now *recorded* (`degraded_count` in `latest.json`)
instead of only printed.

### Gate consumption (designed here, implemented in the companion PR)

The regression gate (WO-C1-02) consumes these fields: comparing a `latest.json` against
`baseline.json` can distinguish a fully-live run (`degraded_count == 0`), a degraded run
(`> 0`), and a pre-schema artifact (`None`), and can see whether a report covers the full
suite (`only_patterns == ()`) or a filtered slice. Provenance-*mismatch* gating (failing a
comparison because baseline and latest differ in liveness) is deliberately deferred to
warn-only: until the post-campaign re-bless, the committed baseline is pre-schema
(`None`), and a hard mismatch gate would either block every comparison or invite a
baseline rewrite — both worse than an honest warning. Expect that warning in CI until the
next deliberate `make baseline`.

### Verification under the eval freeze

Per [ADR 0011](0011-campaign-eval-freeze.md) the runner is not executed; every refusal
path and the schema round-trip are proven by unit tests (`tests/unit/test_runner.py`),
including a read-only parse of the committed `baseline.json` and a stubbed
`--live --smoke` preflight pass with a real-shaped env (the unit replacement for
observing `make eval-smoke`). ADR 0011 keeps the canned `evals.yml` CI job enabled
during the freeze; that job (plain offline, no `--live`) is unaffected by the new gate
by construction. The one thing unit tests cannot show — a genuine live invocation with
real credentials passing preflight end-to-end — is confirmed by the first post-campaign
live run.

### Consequences

Positive:

* A green `--live` exit now implies every selected scenario ran both legs live; a canned
  artifact can never impersonate a live one again.
* Misconfiguration is caught pre-spend, with the offending env var named.
* Wrappers and CI can key on exit codes without misclassifying env problems as scenario
  regressions.
* Archived reports self-describe (provenance + filter), so post-hoc analysis does not
  depend on reconstructing the invocation's environment.

Negative:

* Pre-schema artifacts report `degraded_count=None`, so consumers must handle the
  tri-state (the regression gate does, per the companion PR). Resolved naturally at the
  next baseline bless.
* A live scenario that *crashes* keeps `degraded=False` defaults, so the post-run count
  can undercount the pre-run estimate — acceptable: a crash is already a failure, and
  the `--live` gate is pre-run.
* Operators with a placeholder `.env` lose the (misleading) ability to "try" `--live`
  cheaply; the refusal message tells them exactly which var to fix.

## More information

* Extends [ADR 0004](0004-eval-first-development-and-regression-gating.md) (eval-first +
  regression gating): the baseline-as-truth mechanism assumed reports were comparable;
  this ADR makes their provenance explicit so comparison can be honest. Contradicts
  nothing accepted.
* [ADR 0011](0011-campaign-eval-freeze.md) — the freeze this change lands under, and the
  record that the canned `evals.yml` job stays enabled.
* Findings covered: A-01, S-09, A-04, A-15 (audit fix campaign, WO-C1-01); gate
  consumption is implemented by the companion WO-C1-02 PR.
* Exit-3-for-preflight convention first established for the smoke-token and LLM-auth
  checks — see `docs/lessons/live-campaign-2026-08-03.md`.

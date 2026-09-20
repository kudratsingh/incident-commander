.PHONY: traffic demo-destroy demo-live help setup check lint types test test-unit test-integration test-contract test-drift test-idempotency fixture-drift fixture-drift-bless test-e2e eval eval-live eval-smoke eval-reg eval-reset world-dossier world-record world-drift trace-report chaos-help chaos-kill-consumer chaos-poison chaos-saturate chaos-latency chaos-bad-deploy chaos-restore chaos-bad-data-job demo demo-down bootstrap-token snapshot baseline clean

# Make does not read .env on its own — only the Python side does, via
# dotenv. Without this include, a make-level var like PLATFORM_COMPOSE
# has to be re-exported on every single invocation, and forgetting it
# fails `eval-reset` on its exit-2 guard. Optional (`-include`) so a
# fresh checkout with no .env still runs every offline target.
#
# Must precede any `?=` default that .env is expected to win over.
# A command-line `VAR=...` still overrides both.
#
# Deliberately no blanket `export`: the vars make actually consumes are
# expanded by make inside the recipe, so exporting would only widen
# ANTHROPIC_API_KEY and PLATFORM_TOKEN into every subprocess of every
# target for no benefit.
#
# Caveat: make parses .env more naively than dotenv — it keeps surrounding
# quotes and treats `#` as a comment. Keep make-consumed values unquoted.
# The secrets above are read by Python and never expanded by make, so
# their formatting is unaffected either way.
-include .env

MODEL_ROLE ?= development

help:
	@echo "Targets:"
	@echo "  setup            uv sync + install dev dependencies"
	@echo "  check            ruff lint + mypy --strict"
	@echo "  test             unit + integration tests"
	@echo "  test-unit        unit tests only"
	@echo "  test-integration integration tests only"
	@echo "  test-contract    diff platform tool schemas against snapshot"
	@echo "  test-drift       diff canned fixture VALUES against the pinned platform"
	@echo "  test-idempotency wire-level idempotency contract (MUTATES; run last)"
	@echo "  fixture-drift    the same walk, as a human-readable report"
	@echo "  test-e2e         full compose end-to-end (spends tokens)"
	@echo "  eval             full eval suite offline (writes report)"
	@echo "  eval-live        run named scenario(s) against live platform (needs .env);"
	@echo "                   ONLY=<name[,name...]> REQUIRED, full scenario names (e.g. ONLY=remediate_consumer_lag_success)"
	@echo "  eval-smoke       read-only smoke pass under the read-scoped smoke token"
	@echo "  world-audit      FREE (zero-LLM, read-only) audit of the seeded world against"
	@echo "                   the runbook baseline; exits non-zero on any FAIL."
	@echo "                   ROOTS=<job id[,id...]> also checks those chains are unpaused"
	@echo "  baseline-report  assemble the Phase 0 baseline from the committed archives;"
	@echo "                   reads only, spends nothing, writes nothing. FORMAT: --format"
	@echo "  phase-close-report  assemble the phase-close report (plan 03 section 14) from the"
	@echo "                   committed archives; reads only, spends nothing. PHASE=<n> picks"
	@echo "                   the phase (default: the latest declared). WRITE=1 persists it"
	@echo "  research-report  assemble the aggregate research report (plan 03 section 15) from the"
	@echo "                   committed archives: one leaderboard per model, grouped by the seven"
	@echo "                   WP-2.5 keys, every difference beside its paired-trial count."
	@echo "                   Reads only, spends nothing. --write persists it; --scan lists scope"
	@echo "  judge-calibration  put each judge's trap set to it and report trap agreement,"
	@echo "                   stability over N=5 and its track record (plan 03 section 9)."
	@echo "                   FREE by default (scripted fake judge, spends nothing)."
	@echo "                   JUDGE=<role> picks one; WRITE=1 persists; SCAN=1 asks nothing."
	@echo "                   LIVE=1 asks the real JUDGE_MODEL and SPENDS MONEY: it also"
	@echo "                   needs YES_SPEND=1 and the owner's explicit yes for that run"
	@echo "  regrade-archive  re-grade one locked run archive under today's rules from its own"
	@echo "                   trajectories; ARCHIVE=<run id> REQUIRED. Reads only, spends"
	@echo "                   nothing, never touches the archive. WRITE=1 persists the report"
	@echo "  world-dossier    FREE (zero-LLM) pre-run reading of one scenario's fault world;"
	@echo "                   ONLY=<name> REQUIRED, full scenario name. Seeds chaos, reads"
	@echo "                   every probe the agent will make, lints, resets, re-audits."
	@echo "  world-record     FREE (zero-LLM) recording of one scenario's fault world for"
	@echo "                   replay; ONLY=<name> REQUIRED, full scenario name. Same seeding"
	@echo "                   and lints as world-dossier, and KEEPS every answer keyed by the"
	@echo "                   wired arguments. Writes evals/recorded_worlds/, then resets"
	@echo "  world-drift      FREE (zero-LLM) check that a RECORDED world still matches the"
	@echo "                   live one; WORLD=<recording id|scenario> REQUIRED. Re-reads the"
	@echo "                   recording's own calls and diffs them with the fixture-drift"
	@echo "                   walk. Run it before reporting any recorded result. Exit 1 = drift"
	@echo "  trace-report     render evals/traces/*.jsonl → readable txt files"
	@echo "  training-export  FREE (reads only) JSONL export of traced trajectories for a"
	@echo "                   later training stage; TRACE_DIR= picks the trace store,"
	@echo "                   ONLY=<name> one scenario, WRITE=1 persists. REFUSES a"
	@echo "                   holdout template by name and never touches the traces"
	@echo "  dataset-checks   FREE (reads only) quality gate over a training export;"
	@echo "                   MANIFEST= picks one (default: the newest), TRACE_DIR= turns on"
	@echo "                   the boundary re-check, AUDIT=<json> the action check."
	@echo "                   Exit 1 = a blocking or unclassified finding"
	@echo "  chaos-help       list chaos setup subcommands (kill-consumer, etc.)"
	@echo "  eval-reg         full offline eval + regression gate vs baseline (refuses ONLY=)"
	@echo "  eval-reset       clear leftover chaos state between live scenarios;"
	@echo "                   also clears the chaos teardown latch on success"
	@echo "  demo             compose up only (platform + console pinned by digest); no eval runs"
	@echo "  demo-live        drive the recorded demo as six printed steps;"
	@echo "                   MODE=consumer_outage|dlq_backlog REQUIRED. FREE by default"
	@echo "                   (real platform, real fault, scripted planner). AUTO=1 skips the"
	@echo "                   pauses. RECORD_FROM=fault|baseline picks where the recording"
	@echo "                   starts (default fault). LIVE=1 is the PAID take and also needs"
	@echo "                   YES_SPEND=1"
	@echo "  demo-down        stop demo compose services"
	@echo "  bootstrap-token  mint a service-account token against a running platform"
	@echo "  snapshot         regenerate contracts/platform-tools.snapshot.json from live"
	@echo "  baseline         recompute and commit eval baseline (refuses ONLY=)"
	@echo "  clean            remove build artifacts and caches"

.PHONY: inventory
inventory:
	uv run python -m evals.inventory

.PHONY: world-audit baseline-report phase-close-report research-report regrade-archive judge-calibration
world-audit:
	PLATFORM_COMPOSE="$(PLATFORM_COMPOSE)" uv run python -m evals.world_audit --roots "$(ROOTS)"

baseline-report:
	uv run python -m evals.baseline_report

# PHASE= selects which phase's scope to assemble; it defaults to the latest one
# declared in evals/phase_close_report.py::SCOPES, which is the phase being
# closed. WRITE=1 persists the versioned JSON + Markdown pair — and because the
# filename carries the newest archive in scope, adding a pending re-run's
# archive to that scope writes the final version BESIDE the draft rather than
# over it (invariant 9).
phase-close-report:
	uv run python -m evals.phase_close_report \
		$(if $(PHASE),--phase $(PHASE),) $(if $(WRITE),--write,)

research-report:
	uv run python -m evals.research_report

# Judge calibration (plan 03 section 9, WP-6.3). FREE by default: no flags means
# the scripted fake judge, which proves the harness end to end and spends
# nothing. LIVE=1 asks the real pinned JUDGE_MODEL and needs YES_SPEND=1 as well
# — the module refuses one flag on its own, because PROTOCOL step 0 is that
# readiness is not authorization.
judge-calibration:
	uv run python -m evals.judge_calibration \
		$(if $(SCAN),--scan,) $(if $(JUDGE),--judge $(JUDGE),) \
		$(if $(REPS),--reps $(REPS),) $(if $(WRITE),--write,) \
		$(if $(LIVE),--live,) $(if $(YES_SPEND),--yes-spend,)

# Re-grade one locked archive under today's rules (WO-R3-265, INC-003). Reads
# only: no model call, no platform, nothing spent, and the archive itself is
# sha256-verified unchanged. ARCHIVE= is required — there is no "re-grade
# everything" default, because a re-grade is a statement about one run.
# RUNS_DIR= points at another checkout's evals/runs when the locked original
# lives there; WRITE=1 persists the versioned JSON + Markdown pair.
ifdef ARCHIVE
regrade-archive:
	PYTHONPATH=. uv run python scripts/regrade_archive.py $(ARCHIVE) \
		$(if $(RUNS_DIR),--runs-dir $(RUNS_DIR),) $(if $(WRITE),--write,)
else
regrade-archive:
	$(error 'make regrade-archive' needs ARCHIVE=<run id>, e.g. ARCHIVE=0db6fe722f7c)
endif

setup:
	uv sync --all-groups

check: lint types

lint:
	uv run ruff check .
	uv run ruff format --check .

types:
	uv run mypy

test: test-unit test-integration

test-unit:
	uv run pytest tests/unit

test-integration:
	uv run pytest tests/integration

test-contract:
	uv run pytest tests/integration/test_contract_snapshot.py -v

# The value-level sibling of test-contract: same pinned stack, same
# read-scoped principal, but it asks whether the canned fixture VALUES are
# ones the platform can produce rather than whether the schemas match.
test-drift:
	uv run pytest tests/integration/test_canned_fixtures_match_live.py -v

# The wire-level idempotency contract, against the same pinned stack.
# ADR 0008 removed the client-side execute-once guard on the strength of
# this test, so it is the sole automated defence for Tier-1 crash-resume —
# and it ran in no CI job until WO-R2-43 wired it here.
#
# Its own target, and NOT folded into test-contract, for two reasons that
# are both about ordering: it needs BOTH write credentials — the agent's
# PLATFORM_TOKEN for the Tier-1 calls it replays and the evaluator's
# PLATFORM_CHAOS_TOKEN for the chaos hook that sets up the world (one token
# carried both until platform v0.6.5; test-drift deliberately holds the
# read-only smoke token) —
# and it MUTATES the world it runs against — it fires the kill_consumer
# chaos hook and restarts worker-dispatcher several times. test-contract
# and test-drift both READ that world, and test-drift compares canned
# fixture VALUES against it, so running this first would report the
# mutations as fixture drift. Keep it last in the contract job.
test-idempotency:
	uv run pytest tests/integration/test_idempotency_contract.py -v

# Human-readable version of the same walk, for working on a fixture.
# PYTHONPATH=. because `python scripts/x.py` puts scripts/ on sys.path[0],
# not the repo root, so `import evals` fails. pytest does not need it
# (pyproject sets pythonpath), which is why `make test-drift` does not.
fixture-drift:
	PYTHONPATH=. uv run python scripts/fixture_drift.py

# Re-record the known-drift ledger. A DELIBERATE act: it accepts the current
# fixture state as the new floor, so it belongs in its own commit with the
# reason in the message, exactly like `make baseline`.
fixture-drift-bless:
	PYTHONPATH=. uv run python scripts/fixture_drift.py --bless
	@echo "Ledger rewritten. git add + commit evals/fixture-drift-ledger.json to bless."

test-e2e:
	@echo "TODO(phase-0+): compose up incident-platform + agent, inject scenario, assert audit"

eval:
	uv run python -m evals.runner --model-role "$(MODEL_ROLE)" $(if $(ONLY),--only $(ONLY))

# ONLY=<name>[,<name>...] names the scenario(s) to run, by FULL NAME — a live
# --only pattern is matched exactly (see the exact-match block in
# evals/runner.py), so ONLY=dlq_backlog runs that one scenario and not
# remediate_dlq_backlog_success alongside it.
#
# ONLY is REQUIRED here. Without it the recipe used to hand the runner a bare
# --live, i.e. the whole suite against one shared platform, and the only thing
# standing in the way was the runner's exit-8 canned-only gate — which refuses
# for a different reason (some scenarios have no live leg) and would stop
# refusing the moment they all gained one. Same parse-time `$(error)` shape as
# the `ifdef ONLY` guards on eval-reg and baseline below, pointing the other
# way: no prerequisites on the refusing rule, so it fires before anything runs,
# and make exits 2. The runner carries the same refusal (also exit 2) because
# `python -m evals.runner --live` never comes through here.
#
# Traced by construction — EVAL_TRACE_DIR is set inline so the human report is
# always produced, matching the post-hardening one-scenario protocol
# in docs/runbook.md.
# The trace render runs whether or not the suite passed, then the recipe
# exits with the runner's own code. Make aborts a recipe on the first
# non-zero line, so a FAILING run — the one whose traces you actually need —
# used to skip format_traces.py and leave only raw JSONL behind.
# The render is INCREMENTAL since WO-R3-257: it writes the report for the
# invocation this target just produced and skips the 38 scenarios nobody
# re-ran. `--force` re-renders everything, deliberately.
ifndef ONLY
eval-live:
	$(error 'make eval-live' without ONLY= would select the whole suite for a live, paid run; name exactly one scenario: make eval-live ONLY=<scenario_name>)
else
eval-live:
	@EVAL_TRACE_DIR=evals/traces uv run python -m evals.runner --model-role "$(MODEL_ROLE)" --live --only $(ONLY); \
	code=$$?; \
	PYTHONPATH=. uv run python scripts/format_traces.py || true; \
	echo "JSONL traces: evals/traces/*.jsonl"; \
	echo "Human-readable trajectories: evals/reports/human/<scenario>/*.txt"; \
	exit $$code
endif

# `eval-live-remediation` is gone. It selected `remediate_,dlq_` — nine
# state-mutating scenarios in one invocation, against one shared platform,
# with no reset between them. The runner now refuses that selection (exit 7,
# ADR 0020). Run them one at a time:
#   make eval-live ONLY=remediate_consumer_lag_success && make eval-reset
# It also swept in dlq_backlog, which sorts first and drains the DLQ pool
# before any graded scenario starts.

# Read-only smoke pass, structurally: runs under PLATFORM_SMOKE_TOKEN
# (telemetry:read + incidents:read only, minted by `make bootstrap-token`),
# so a Tier-1 attempt 403s at the platform, wraps as MCPError, and grades
# as an escalation instead of mutating state. The 2026-08-03 campaign's
# "read-only" pass fired a real DLQ replay; this target closes that door.
# WHICH scenarios it runs is no longer written here (WO-R2-123). The pass
# derives itself from the scenario directory: a scenario is in it when it
# declares no chaos_setup (else --smoke refuses the run outright, exit 6 /
# S-03) and no expected_action_tools (a Tier-1 write the read-scoped token
# 403s by design), unless its YAML carries a `smoke_exclusion:` reason.
# `Scenario.in_smoke_pass` is that predicate; the runner applies it to a
# bare `--smoke` and prints both the count and every hold-back it honoured.
#
# Two hand-maintained lists used to live here, SMOKE_ONLY and SMOKE_EXCLUDE.
# They could rot in three ways (WO-R2-41 caught two of them the hard way):
# a RENAMED scenario left its pattern behind matching nothing; a NEW
# read-only scenario that nobody added just never ran
# (consumer_lag_null_unknown_state had already dropped out that way); and an
# exclusion could go on naming a scenario that no longer existed. #151 added
# tests that CAUGHT all three — but catching drift after the fact needs the
# check to be kept in step with the list, and a derivation cannot fall out
# of step with the tree it derives from. dlq_human_required_escalates is held
# out by the predicate rather than by hand, and dlq_backlog now carries its
# own reason in evals/scenarios/dlq_backlog.yaml.
#
# SMOKE_ONLY survives as the OPERATOR OVERRIDE, unset by default: set it on
# the command line (`make eval-smoke SMOKE_ONLY=consumer_lag_`) or in .env to
# run a subset, e.g. when re-checking one scenario against a new pin. The
# override can only NARROW the derived selection, never widen it: it reaches
# the runner as --only, so the dead-pattern refusal (exit 2) and the
# outside-the-derived-set refusal (exit 6 — chaos_setup, expected_action_tools,
# or a declared smoke_exclusion, each named with its reason) both still apply
# to it. An override cannot smuggle a chaos-seeding or a write-declaring
# scenario in, and it cannot silently match nothing.
eval-smoke:
	@if [ -z "$(PLATFORM_SMOKE_TOKEN)" ]; then \
		echo "ERROR: PLATFORM_SMOKE_TOKEN not set. Run 'make bootstrap-token' and add it to .env" >&2; exit 2; \
	fi
	# The runner reads PLATFORM_SMOKE_TOKEN from Settings under --smoke and
	# asserts the principal against the live platform before any scenario.
	# Do NOT reintroduce `PLATFORM_TOKEN=... $(MAKE) ...` here: `-include .env`
	# above overrides recipe-exported values, which is exactly how every
	# "read-scoped" smoke run before 2026-08-07 silently held write scope.
	# The @ on the runner line also keeps tokens out of the log.
	# Traces are rendered whether or not the pass succeeded, then the recipe
	# exits with the runner's own code — the same shape as eval-live, and for
	# the same reason: make aborts a recipe on the first non-zero line, so a
	# FAILING smoke run (the one whose trajectories you actually need) used to
	# leave only raw JSONL behind. eval-live was fixed; this was not.
	@EVAL_TRACE_DIR=evals/traces uv run python -m evals.runner --model-role "$(MODEL_ROLE)" --live --smoke \
		$(if $(SMOKE_ONLY),--only "$(SMOKE_ONLY)"); \
	code=$$?; \
	PYTHONPATH=. uv run python scripts/format_traces.py || true; \
	echo "JSONL traces: evals/traces/*.jsonl"; \
	echo "Human-readable trajectories: evals/reports/human/<scenario>/*.txt"; \
	exit $$code

# Fault-world content review, free and zero-LLM (evals/dossier.py).
#
# PROTOCOL step 5 made mechanical. Seeds the named scenario's chaos hook
# through the runner's own chaos path, runs the scenario's preconditions and
# every READ probe the agent is expected to make (derived from
# ALERT_SUBJECT_PROBES / SOURCE_ROW_FOR_ACTION / VERIFY_PROBE_FOR_ACTION and
# the scenario's own claims) under the READ-SCOPED smoke token, prints every
# output in full, lints what it read for internal coherence, then
# `make eval-reset PURGE_IDEMPOTENCY=1` and re-audits the seeded baseline.
#
# It exists because rem 4 run A (2026-09-07, ≈$0.15, archive efdc3b2a9864)
# was lost to a world that contradicted itself — a `replay_safe` hint on a
# permanent data-bug error text — and two readiness sweeps missed it because
# both checked mechanics and neither READ the fault as the agent would.
#
# ONLY is REQUIRED and must be ONE full scenario name, guarded the same way
# `eval-live` is: a parse-time `$(error)` here (make exits 2, before anything
# runs) plus the same refusal inside evals/dossier.py, because
# `python -m evals.dossier` never comes through make. This target SEEDS CHAOS
# into the shared eval world, so a widened selection is not merely wasteful,
# it interleaves two faults and makes both readings meaningless.
#
# No `@` on the recipe line: the whole document goes to stdout on purpose —
# the coordinator pastes it into the readiness note. It is also written to
# evals/reports/dossiers/<scenario>/<scenario>.<stamp>.<invocation_id>.md
# (create-only, cmd #185's convention; per-scenario folder since WO-R3-257).
ifndef ONLY
world-dossier:
	$(error 'make world-dossier' without ONLY= has no meaning: a dossier seeds ONE scenario's fault into the shared world and then resets it; name exactly one scenario: make world-dossier ONLY=<scenario_name>)
else
# PLATFORM_COMPOSE is passed EXPLICITLY rather than left to the environment.
# Make resolves it from `-include .env` (or a command-line override) and the
# dossier needs the same file for its `chaos:*` key scan, but make exports
# nothing by default — so a checkout that points PLATFORM_COMPOSE at a sibling
# stack would have reset one world through make and scanned another through
# Python. Explicit beats ambient, the same reason `make_client` takes an
# explicit token (mcp_client.py: threading it through make's environment lost
# it to `-include .env`).
world-dossier:
	PLATFORM_COMPOSE="$(PLATFORM_COMPOSE)" uv run python -m evals.dossier --only $(ONLY)
endif

# Record one scenario's fault world for replay, free and zero-LLM
# (evals/recorder.py, WP-3.1).
#
# Same three writes as `world-dossier` and no others — the scenario's own chaos
# hooks and `make eval-reset PURGE_IDEMPOTENCY=1` — and every read is under the
# read-scoped smoke principal. What it adds is that the answers are KEPT, keyed
# by the WIRED arguments the agent's own client sends (tools/wire.py), so a
# replay platform can answer the agent identically as many times as wanted, in
# parallel, for the price of one seeding.
#
# ONLY is REQUIRED and must be ONE full scenario name, guarded exactly as
# `world-dossier` and `eval-live` are: a parse-time `$(error)` here plus the
# same refusal inside the module, because `python -m evals.recorder` never
# comes through make. This target SEEDS CHAOS into the shared eval world.
#
# Output (create-only, invariant 9, never overwritten):
#   evals/recorded_worlds/<scenario>/<scenario>.<stamp>.<invocation_id>.json
#   evals/recorded_worlds/<scenario>/<scenario>.<stamp>.<invocation_id>.truth.json
# The second is the evaluator's answer key for that world (ADR 0040) and the
# replay path has no way to load it.
ifndef ONLY
world-record:
	$(error 'make world-record' without ONLY= has no meaning: a recording seeds ONE scenario's fault into the shared world and then resets it; name exactly one scenario: make world-record ONLY=<scenario_name>)
else
# PLATFORM_COMPOSE passed explicitly, for the reason world-dossier states: the
# post-reset baseline re-audit scans redis for `chaos:*` keys through this
# compose file, and make exports nothing by default.
world-record:
	PLATFORM_COMPOSE="$(PLATFORM_COMPOSE)" uv run python -m evals.recorder --only $(ONLY)
endif

# Is a recorded world still the world it was? (evals/world_drift.py, WP-3.3)
#
# Re-reads the RECORDING's OWN calls against the live platform under the
# read-scoped smoke principal and diffs the two with `evals/fixture_drift.py`'s
# walk, so the fields that legitimately move between two honest observations
# (`_VOLATILE`: the DLQ clocks, the lag reading's freshness metadata, the redis
# gauges, the cache TTL) are checked for type and not for value. Zero LLM calls.
#
# WORLD is REQUIRED and is a recording's invocation id — the last segment of a
# filename under evals/recorded_worlds/ — or a scenario's full name for its
# newest recording. Guarded at parse time here AND inside the module, like
# `world-record`, because `python -m evals.world_drift` never comes through make.
#
# It SEEDS CHAOS when the recording is of a seeded world, and resets afterwards:
# the live platform does not hold the scenario's fault until its hooks fire, so a
# check that skipped the seeding would report the whole fault as drift every
# time. That makes it an operation with a go behind it, never something CI runs.
#
# ORDERING, inherited from `test-drift` and for the same reason (see its comment
# above): a drift check must not run AFTER a mutating check in the same sequence,
# or it reports that check's mutations as drift. `make test-idempotency` last,
# always.
#
# Exit 1 means the world moved — not that the check failed. Read
# docs/runbook.md's recorded-mode section before re-recording: the three
# readings are a platform release, a fixture-pack change, and a dirty world.
ifndef WORLD
world-drift:
	$(error 'make world-drift' without WORLD= has no meaning: a drift check compares ONE recorded world against the live one; name it: make world-drift WORLD=<recording invocation id or scenario name>)
else
# PLATFORM_COMPOSE passed explicitly, for the reason world-dossier states: the
# post-reset baseline re-audit scans redis for `chaos:*` keys through this
# compose file, and make exports nothing by default.
world-drift:
	PLATFORM_COMPOSE="$(PLATFORM_COMPOSE)" uv run python -m evals.world_drift --world $(WORLD)
endif

# Renders what is not yet rendered (WO-R3-257): a scenario whose newest
# traced attempt no existing report covers. `make trace-report ARGS=--force`
# re-renders every scenario, which is a deliberate act — each render is a
# permanent file (invariant 9).
trace-report:
	PYTHONPATH=. uv run python scripts/format_traces.py $(ARGS)

# The trajectory export a later training stage reads (WP-15.1, evals/export.py).
# Zero LLM calls, no platform, no money: it READS the append-only trace store and
# writes three files beside each other (invariant 9 — it never consumes evidence).
#
# Dry by default: it says what the export would contain and stops. WRITE=1 persists
#   evals/exports/training_export.<stamp>.<invocation_id>.jsonl           (training data)
#   evals/exports/training_export.<stamp>.<invocation_id>.labels.jsonl    (EVALUATOR ONLY)
#   evals/exports/training_export.<stamp>.<invocation_id>.manifest.json   (what it covers)
# Exclusive-create, so a re-export lands beside the last one.
#
# TRACE_DIR= names the trace store (default $$EVAL_TRACE_DIR or evals/traces); point it
# at evals/runs/<id>/traces/ to export one archived run. ONLY=<scenario> narrows it.
# It REFUSES, by name, any scenario whose template is held out, and the refusal is the
# whole export rather than a filter (plan 03 § 4, plan 06 D7).
.PHONY: training-export
training-export:
	uv run python -m evals.export \
		$(if $(TRACE_DIR),--trace-dir $(TRACE_DIR),) \
		$(if $(ONLY),--only $(ONLY),) \
		$(if $(WRITE),--write,)

# The quality gate over that export (WP-15.3, evals/dataset_checks.py). Reads only, and
# it can FAIL a dataset: exit 1 on any blocking finding and on any it cannot classify,
# because "nobody could classify it" is not evidence that it is harmless.
#
# MANIFEST= names the export to check (default: the newest under evals/exports).
# TRACE_DIR= turns on the second layer for incompleteness — the line's boundary claim is
# re-derived from the append-only trace store instead of trusted.
# AUDIT=<json> is {trajectory_id: audit window}; without it the action/result check
# against the platform log does not run, and the report says so rather than passing quietly.
.PHONY: dataset-checks
dataset-checks:
	uv run python -m evals.dataset_checks \
		$(if $(MANIFEST),--manifest $(MANIFEST),) \
		$(if $(TRACE_DIR),--trace-dir $(TRACE_DIR),) \
		$(if $(AUDIT),--audit $(AUDIT),) \
		$(if $(JSON),--json,)

# --- Chaos setup helpers (live-eval prep) -------------------------------
# All wrap scripts/chaos_setup.py. Effects self-clean on TTL. Requires
# PLATFORM_MCP_URL + PLATFORM_CHAOS_TOKEN — the EVALUATOR's principal
# (incident-commander-chaos, chaos:invoke), not the agent's PLATFORM_TOKEN,
# which since platform v0.6.5 does not carry that scope. These recipes hand
# the credential to the child process themselves — see below.
# See docs/runbook.md for the full workflow.

# The credentials the chaos and traffic scripts read from os.environ.
#
# `-include .env` at the top of this file puts them in MAKE's variables, not
# in the child environment, and there is deliberately no blanket `export`
# (see the header). Nothing bridged that gap, so every documented `make
# chaos-*` aborted with "PLATFORM_MCP_URL and PLATFORM_TOKEN must be set
# (env or --flag)" and `make traffic UNTIL_LAG=N` could never read the lag it
# was waiting for — the seeding step the live-eval runbook depends on, broken
# for anyone who kept their credentials in .env like the runbook says to.
#
# Target-specific `export` with `:=` hands over exactly these variables to
# exactly these targets. The right-hand side is expanded once, from make's
# own variables, so .env and the ambient environment keep the precedence the
# rest of this file documents, and nothing widens into unrelated recipes.
# NOT `PLATFORM_CHAOS_TOKEN=$(PLATFORM_CHAOS_TOKEN) uv run ...`, which would
# print the credential to the terminal on every invocation.
#
# NOTE: this makes PLATFORM_MCP_URL/PLATFORM_CHAOS_TOKEN/PLATFORM_SMOKE_TOKEN
# make-consumed, so the header's caveat now applies to them: make parses
# .env more naively than dotenv does. Keep these values unquoted in .env.
#
# The chaos targets are handed PLATFORM_CHAOS_TOKEN and deliberately NOT
# PLATFORM_TOKEN: the agent's principal cannot fire a hook since v0.6.5, and
# handing it over anyway would only produce a -32002 whose cause reads like a
# broken stack. One credential per role, named at the point of use.
CHAOS_TARGETS = chaos-help chaos-kill-consumer chaos-poison chaos-saturate \
                chaos-latency chaos-bad-deploy chaos-restore chaos-bad-data-job
$(CHAOS_TARGETS): export PLATFORM_MCP_URL := $(PLATFORM_MCP_URL)
$(CHAOS_TARGETS): export PLATFORM_CHAOS_TOKEN := $(PLATFORM_CHAOS_TOKEN)
# traffic_loop.py is read-scoped by construction — it only ever reads lag —
# so it takes PLATFORM_SMOKE_TOKEN and must never see the write-scoped one.
traffic: export PLATFORM_MCP_URL := $(PLATFORM_MCP_URL)
traffic: export PLATFORM_SMOKE_TOKEN := $(PLATFORM_SMOKE_TOKEN)

# Consumer lag is arrival minus service, and the eval only ever had the
# service half. Run this in a second terminal BEFORE seeding kill_consumer
# for remediate_consumer_lag_success: with nothing arriving, a killed
# consumer builds no backlog and the scenario's precondition correctly
# refuses to run it. `--until-lag N` stops once the backlog is deep enough.
traffic:
	uv run python scripts/traffic_loop.py $(if $(UNTIL_LAG),--until-lag $(UNTIL_LAG)) $(if $(COUNT),--count $(COUNT))

chaos-help:
	PYTHONPATH=. uv run python scripts/chaos_setup.py --help

chaos-kill-consumer:
	PYTHONPATH=. uv run python scripts/chaos_setup.py kill-consumer

chaos-poison:
	PYTHONPATH=. uv run python scripts/chaos_setup.py poison-message

chaos-saturate:
	PYTHONPATH=. uv run python scripts/chaos_setup.py saturate-redis

chaos-latency:
	PYTHONPATH=. uv run python scripts/chaos_setup.py inject-latency

chaos-bad-deploy:
	PYTHONPATH=. uv run python scripts/chaos_setup.py bad-deploy

chaos-restore:
	PYTHONPATH=. uv run python scripts/chaos_setup.py restore-consumer

# Between live scenarios: full seed reset via the platform-owned
# scripts/reset_eval_state.py (shipped in platform v0.4.6). Clears
# chaos:* keys, re-seeds lag cache + DLQ fixture pool + hot_set, and
# optionally purges idempotency records. Runs inside the platform
# `app` container so it has DB/Redis credentials.
#
# What it does NOT clear, stated because a green exit reads like a clean
# world: reset undoes what the eval SEEDS. Anything the world grew on its
# own outlives it. Specifically it does NOT clear ORGANIC ALERTS — SLO and
# other non-chaos alerts raised by the platform's own loops — and it cannot:
# the SLO evaluator reads the seeded fixtures as real traffic (4 of 7 seeded
# jobs are dead-lettered = a fast burn), so re-seeding the fixtures RE-ARMS
# the alert instead of removing it. The fast-burn dedup key is bucketed by
# the hour, so it also re-fires hourly rather than staying suppressed.
# Audit against the seeded baseline before a paid run (docs/runbook.md,
# "Pre-run checklist") rather than trusting this target's exit code.
# WO-R2-131 (reset should sweep organic alerts) and WO-R2-132 (platform fix)
# are both CLOSED by platform v0.6.4 (plat #201): the evaluator skips rows
# carrying the seeded-fixture payload markers, and reset now resolves every
# active alert outside the five seeded ones. The stopgap that disabled the
# loop (SLO_EVALUATION_INTERVAL_SECONDS=0 on both demo services) was lifted
# with the v0.6.4 re-pin, so the paragraph above is history: audit the
# baseline anyway, because the general rule below it still holds.
#
# PLATFORM_COMPOSE defaults to this repo's demo stack (see below) — override
# to point at a sibling incident-platform checkout, either per-invocation or
# once in .env (see the `-include .env` note at the top of this file).
#
# Pass PURGE_IDEMPOTENCY=1 to also `DELETE` the idempotency_records rows
# (usually unnecessary thanks to the 24h TTL from platform ADR 0010, but
# useful when a scenario needs a guaranteed-fresh cache).
#
# Compared to the literal 1. The gate was `$(if $(PURGE_IDEMPOTENCY),...)`,
# and make's $(if) asks whether the value is a non-empty STRING, not whether
# it is true: PURGE_IDEMPOTENCY=0, =no and =false each turned the row
# deletion ON — every spelling an operator reaches for to turn something off,
# on the one flag here that destroys data. Only `1` enables it now.
ifeq ($(PURGE_IDEMPOTENCY),1)
PURGE_IDEMPOTENCY_FLAG := --purge-idempotency
else
PURGE_IDEMPOTENCY_FLAG :=
endif
# The stack the eval actually runs against. This defaulted to the platform's
# own dev compose, which is a different Postgres and a different Redis — so a
# checkout without these lines in .env resets a stack nobody is testing and
# reports success. Both demo services share one database, so either service
# name works; `api` is the REST app that owns seeding.
PLATFORM_COMPOSE ?= demo/compose.yml
# Compose service name running the platform app. The dev stack calls it
# `app`; the pinned demo stack may name it differently.
PLATFORM_SERVICE ?= api
# PYTHONPATH prepend below: reset_eval_state.py does
# `from scripts import seed_eval_fixtures`, which needs /app on the path
# while the image ships PYTHONPATH=/app/backend for the app process.
# The v0.6.0 image SHIPS the fix (platform #92): its
# `/app/scripts/reset_eval_state.py` inserts the path itself before
# importing. Verified in the image at the wave-9 re-pin. The override is
# kept as a harmless belt-and-braces for now and is a wave-10 removal
# candidate — dropping it needs one live `make eval-reset` against the
# v0.6.0 stack to confirm, which the re-pin PR deliberately does not run.
#
# The last line clears the chaos teardown latch (ADR 0037, exit 10). It is
# LAST on purpose: make aborts a recipe at the first non-zero line, so a reset
# that failed never reaches it and the latch correctly survives to refuse the
# next live run. The latch means "the shared world is dirty"; this reset is
# the thing that makes that untrue, so the assertion is made by what earns it
# rather than by a second gesture an operator has to remember at the moment
# they are least likely to. ADR 0037 named this as the expected follow-up and
# said it changes no decision; `--clear-chaos-block` on an unset latch is not
# an error, so an ordinary between-scenario reset stays quiet.
eval-reset:
	@echo "eval-reset: resetting $(PLATFORM_COMPOSE) service $(PLATFORM_SERVICE)"
	@if [ ! -f "$(PLATFORM_COMPOSE)" ]; then \
		echo "ERROR: $(PLATFORM_COMPOSE) not found; set PLATFORM_COMPOSE=..." >&2; exit 2; \
	fi
	@docker compose -f "$(PLATFORM_COMPOSE)" exec -T \
		-e PYTHONPATH=/app:/app/backend $(PLATFORM_SERVICE) \
		python /app/scripts/reset_eval_state.py \
		$(PURGE_IDEMPOTENCY_FLAG)
	@uv run python -m evals.runner --clear-chaos-block

chaos-bad-data-job:
	PYTHONPATH=. uv run python scripts/chaos_setup.py bad-data-job

# ONLY= must never reach the regression gate: `eval-reg: eval` forwards
# ONLY into the runner, so a filtered run would become the NEWEST report and
# the gate would compare a shrunken suite (A-03; study/runs.jsonl line 4
# records this class as real artifact loss). Reports are versioned now, so
# the earlier full-suite report survives — but the gate resolves newest-wins
# and would still be pointed at the filtered one, and `make baseline` would
# bless it. The guard is a parse-time conditional swapping in a
# prerequisite-free $(error) rule — it fires before the `eval` prerequisite
# could run, whereas a recipe-line check would fire only AFTER the filtered
# eval already wrote a report that outranks the full-suite one.
# `-include .env` above means an ONLY= line in .env trips this too —
# deliberate: a filtered gate is wrong no matter where the filter came
# from. regression.py's exit-2 refusal of only_patterns reports is the
# backstop.
ifdef ONLY
eval-reg:
	$(error 'make eval-reg ONLY=...' would gate on a filtered report; run 'make eval-reg' without ONLY)
else
eval-reg: eval
	uv run python -m evals.regression
endif

# Bring-up only. Deliberately does NOT run `evals.runner --live` — the
# previous embedded batch was untraced (~$4), ran against a healthy
# (no-chaos) platform, and produced escalations that were correct
# behaviour but read as failures in the summary. Land the first live
# LLM spend inside the deliberate read-only smoke pass instead — see
# docs/runbook.md for the protocol.
# --wait is scoped to the six long-running services: compose fails the
# wait when a one-shot (migrate, redpanda-init) exits during the watch
# window, which happens on every re-up. depends_on still runs both
# one-shots first; their failures surface through the services that
# gate on service_completed_successfully.
#
# `console` joined the list on the v0.6.13 pin. It is waited on like the
# rest because `make demo-live` prints its URL as the next thing the
# operator opens, and a URL printed before nginx is listening reads as a
# broken demo.
demo:
	docker compose -f demo/compose.yml up -d --wait \
		postgres redis redpanda platform api console
	@echo "Platform up. Console: http://localhost:$${DEMO_CONSOLE_HOST_PORT:-3000}/"
	@echo "Next: 'make bootstrap-token' + follow the protocol"
	@echo "in docs/runbook.md#live-eval-protocol-post-hardening."
	@echo "Stop with 'make demo-down'."

# Stops the stack and KEEPS the data. `-v` was here until 2026-08-08 and
# deleted the volumes outright — including the platform's audit log, which
# CLAUDE.md invariant 6 makes the ground truth for grading safety, plus the
# service accounts and eval fixtures. Stopping a stack must not be a
# destructive act; use `make demo-destroy` when you actually mean it.
demo-down:
	docker compose -f demo/compose.yml down

# Explicit, irreversible: removes containers AND the named data volumes.
demo-destroy:
	@echo "This DELETES the demo platform's Postgres + Redis data, including"
	@echo "the audit log used to grade safety. Re-run with CONFIRM=1 to proceed."
	@test "$(CONFIRM)" = "1" || exit 2
	docker compose -f demo/compose.yml down -v

# The live demo's step machine (ADR 0068). MODE is required and closed:
#   make demo-live MODE=consumer_outage
#   make demo-live MODE=dlq_backlog AUTO=1          # rehearsal, no Enter between steps
#   make demo-live MODE=… LIVE=1 YES_SPEND=1        # the one PAID take
#
# The default path is FREE: the real platform, the real hooks, the real Tier-1 action,
# and a scripted planner — step 5 runs `evals.runner --mode rehearsal` (ADR 0069), which
# is the only invocation that combines those two halves. LIVE=1 alone REFUSES (exit 2) —
# spending needs YES_SPEND=1 as well, and the owner's explicit yes for that scenario,
# every time (PROTOCOL step 0). YES_SPEND is deliberately NOT set by any target here: a
# make target that could grant its own spending authorization is the thing the two-flag
# gate exists to prevent.
#
# Same parse-time refusal shape as eval-live's ONLY guard, so a missing MODE fails before
# anything is started, seeded or spent rather than inside the script.
ifndef MODE
demo-live:
	$(error 'make demo-live' needs a MODE: make demo-live MODE=consumer_outage|dlq_backlog)
else
# PYTHONPATH=. because the script imports `evals` (the scenario loader, the seeding
# path, the artifact resolver). Only `incident_commander` is installed from src/; every
# other script that reaches into `evals` carries the same prefix. Without it the machine
# dies at step 3 with ModuleNotFoundError, AFTER the countdown has run on camera.
demo-live:
	PYTHONPATH=. uv run python scripts/demo_live.py --mode $(MODE) \
		$(if $(LIVE),--live) $(if $(YES_SPEND),--yes-spend) $(if $(AUTO),--auto) \
		$(if $(RECORD_FROM),--record-from $(RECORD_FROM))
endif

bootstrap-token:
	uv run python scripts/bootstrap_agent_token.py

snapshot:
	uv run python scripts/snapshot_platform_tools.py

# Same parse-time ONLY guard as eval-reg: `make baseline ONLY=x` would
# bless a filtered subset over the committed 41-scenario baseline (the
# study/runs.jsonl artifact-loss pattern). Must refuse before the `eval`
# prerequisite can write a filtered report; an ONLY= line in .env trips it
# too, deliberately.
#
# The source report is RESOLVED, never globbed: `python -m evals.artifacts
# newest report` applies the same ordering (stamp, then invocation_id) that
# evals/regression.py uses, so the blessed baseline is provably the report
# the gate just read. `ls -t | head -1` would order by mtime and could bless
# a restored or copied file.
ifdef ONLY
baseline:
	$(error 'make baseline ONLY=...' would bless a filtered baseline over the committed full suite; run 'make baseline' without ONLY)
else
baseline: eval
	@newest=$$(uv run python -m evals.artifacts newest report) && \
		echo "blessing $$newest" && \
		cp "$$newest" evals/reports/baseline.json
	@echo "Baseline updated. git add + commit evals/reports/baseline.json to bless."
endif

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

.PHONY: remediation-table
remediation-table:
	uv run python -m evals.remediation_table

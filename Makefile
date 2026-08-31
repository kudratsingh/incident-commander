.PHONY: traffic demo-destroy help setup check lint types test test-unit test-integration test-contract test-drift test-idempotency fixture-drift fixture-drift-bless test-e2e eval eval-live eval-smoke eval-reg eval-reset trace-report chaos-help chaos-kill-consumer chaos-poison chaos-saturate chaos-latency chaos-bad-deploy chaos-restore chaos-bad-data-job demo demo-down bootstrap-token snapshot baseline clean

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
	@echo "  eval-live        run eval suite against live platform (needs .env);"
	@echo "                   ONLY=<substr[,substr...]> to filter (e.g. ONLY=remediate_consumer_lag_success)"
	@echo "  eval-smoke       read-only smoke pass under the read-scoped smoke token"
	@echo "  trace-report     render evals/traces/*.jsonl → readable txt files"
	@echo "  chaos-help       list chaos setup subcommands (kill-consumer, etc.)"
	@echo "  eval-reg         full offline eval + regression gate vs baseline (refuses ONLY=)"
	@echo "  eval-reset       clear leftover chaos state between live scenarios"
	@echo "  demo             compose up only (platform pinned by digest); no eval runs"
	@echo "  demo-down        stop demo compose services"
	@echo "  bootstrap-token  mint a service-account token against a running platform"
	@echo "  snapshot         regenerate contracts/platform-tools.snapshot.json from live"
	@echo "  baseline         recompute and commit eval baseline (refuses ONLY=)"
	@echo "  clean            remove build artifacts and caches"

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
# are both about ordering: it needs the WRITE-scoped PLATFORM_TOKEN with
# chaos:invoke (test-drift deliberately holds the read-only smoke token),
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
	uv run python -m evals.runner $(if $(ONLY),--only $(ONLY))

# ONLY=<pattern>[,<pattern>...] filters to scenarios whose name matches
# any of the substrings. Runs the whole suite when unset. Traced by
# construction — EVAL_TRACE_DIR is set inline so the human report is
# always produced, matching the post-hardening one-scenario protocol
# in docs/runbook.md.
# The trace render runs whether or not the suite passed, then the recipe
# exits with the runner's own code. Make aborts a recipe on the first
# non-zero line, so a FAILING run — the one whose traces you actually need —
# used to skip format_traces.py and leave only raw JSONL behind.
eval-live:
	@EVAL_TRACE_DIR=evals/traces uv run python -m evals.runner --live $(if $(ONLY),--only $(ONLY)); \
	code=$$?; \
	uv run python scripts/format_traces.py || true; \
	echo "JSONL traces: evals/traces/*.jsonl"; \
	echo "Human-readable trajectories: evals/reports/human/*.txt"; \
	exit $$code

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
# run a subset, e.g. when re-checking one scenario against a new pin. It
# reaches the runner as --only, so the dead-pattern refusal (exit 2) and the
# chaos refusal (exit 6) both still apply to it — an override cannot smuggle
# a chaos scenario in, and it cannot silently match nothing.
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
	@EVAL_TRACE_DIR=evals/traces uv run python -m evals.runner --live --smoke \
		$(if $(SMOKE_ONLY),--only "$(SMOKE_ONLY)"); \
	code=$$?; \
	uv run python scripts/format_traces.py || true; \
	echo "JSONL traces: evals/traces/*.jsonl"; \
	echo "Human-readable trajectories: evals/reports/human/*.txt"; \
	exit $$code

trace-report:
	uv run python scripts/format_traces.py

# --- Chaos setup helpers (live-eval prep) -------------------------------
# All wrap scripts/chaos_setup.py. Effects self-clean on TTL. Requires
# PLATFORM_MCP_URL + PLATFORM_TOKEN (with chaos:invoke scope), which these
# recipes hand to the child process themselves — see below.
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
# NOT `PLATFORM_TOKEN=$(PLATFORM_TOKEN) uv run ...`, which would print the
# write-scoped token to the terminal on every invocation.
#
# NOTE: this makes PLATFORM_MCP_URL/PLATFORM_TOKEN/PLATFORM_SMOKE_TOKEN
# make-consumed, so the header's caveat now applies to them: make parses
# .env more naively than dotenv does. Keep these values unquoted in .env.
CHAOS_TARGETS = chaos-help chaos-kill-consumer chaos-poison chaos-saturate \
                chaos-latency chaos-bad-deploy chaos-restore chaos-bad-data-job
$(CHAOS_TARGETS): export PLATFORM_MCP_URL := $(PLATFORM_MCP_URL)
$(CHAOS_TARGETS): export PLATFORM_TOKEN := $(PLATFORM_TOKEN)
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
eval-reset:
	@echo "eval-reset: resetting $(PLATFORM_COMPOSE) service $(PLATFORM_SERVICE)"
	@if [ ! -f "$(PLATFORM_COMPOSE)" ]; then \
		echo "ERROR: $(PLATFORM_COMPOSE) not found; set PLATFORM_COMPOSE=..." >&2; exit 2; \
	fi
	@docker compose -f "$(PLATFORM_COMPOSE)" exec -T \
		-e PYTHONPATH=/app:/app/backend $(PLATFORM_SERVICE) \
		python /app/scripts/reset_eval_state.py \
		$(PURGE_IDEMPOTENCY_FLAG)

chaos-bad-data-job:
	PYTHONPATH=. uv run python scripts/chaos_setup.py bad-data-job

# ONLY= must never reach the regression gate: `eval-reg: eval` forwards
# ONLY into the runner, so a filtered run would overwrite latest.json and
# the gate would compare a shrunken suite (A-03; study/runs.jsonl line 4
# records this class as real artifact loss). The guard is a parse-time
# conditional swapping in a prerequisite-free $(error) rule — it fires
# before the `eval` prerequisite could run, whereas a recipe-line check
# would fire only AFTER the filtered eval already overwrote latest.json.
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
# --wait is scoped to the five long-running services: compose fails the
# wait when a one-shot (migrate, redpanda-init) exits during the watch
# window, which happens on every re-up. depends_on still runs both
# one-shots first; their failures surface through the services that
# gate on service_completed_successfully.
demo:
	docker compose -f demo/compose.yml up -d --wait \
		postgres redis redpanda platform api
	@echo "Platform up. Next: 'make bootstrap-token' + follow the protocol"
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

bootstrap-token:
	uv run python scripts/bootstrap_agent_token.py

snapshot:
	uv run python scripts/snapshot_platform_tools.py

# Same parse-time ONLY guard as eval-reg: `make baseline ONLY=x` would
# bless a filtered subset over the committed 37-scenario baseline (the
# study/runs.jsonl artifact-loss pattern). Must refuse before the `eval`
# prerequisite can overwrite latest.json; an ONLY= line in .env trips it
# too, deliberately.
ifdef ONLY
baseline:
	$(error 'make baseline ONLY=...' would bless a filtered baseline over the committed full suite; run 'make baseline' without ONLY)
else
baseline: eval
	cp evals/reports/latest.json evals/reports/baseline.json
	@echo "Baseline updated. git add + commit evals/reports/baseline.json to bless."
endif

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

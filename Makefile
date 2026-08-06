.PHONY: help setup check lint types test test-unit test-integration test-contract test-e2e eval eval-live eval-live-remediation eval-smoke eval-reg eval-reset trace-report chaos-help chaos-kill-consumer chaos-poison chaos-saturate chaos-latency chaos-bad-deploy chaos-restore chaos-bad-data-job demo demo-down bootstrap-token snapshot baseline clean

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
	@echo "  test-e2e         full compose end-to-end (spends tokens)"
	@echo "  eval             full eval suite offline (writes report)"
	@echo "  eval-live        run eval suite against live platform (needs .env);"
	@echo "                   ONLY=<substr[,substr...]> to filter (e.g. ONLY=remediate_consumer_lag_success)"
	@echo "  eval-live-remediation  DEPRECATED alias for 'eval-live ONLY=remediate_,dlq_'"
	@echo "  eval-smoke       read-only smoke pass under the read-scoped smoke token"
	@echo "  trace-report     render evals/traces/*.jsonl → readable txt files"
	@echo "  chaos-help       list chaos setup subcommands (kill-consumer, etc.)"
	@echo "  eval-reg         regression eval subset"
	@echo "  eval-reset       clear leftover chaos state between live scenarios"
	@echo "  demo             compose up only (platform pinned by digest); no eval runs"
	@echo "  demo-down        stop demo compose services"
	@echo "  bootstrap-token  mint a service-account token against a running platform"
	@echo "  snapshot         regenerate contracts/platform-tools.snapshot.json from live"
	@echo "  baseline         recompute and commit eval baseline"
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

test-e2e:
	@echo "TODO(phase-0+): compose up incident-platform + agent, inject scenario, assert audit"

eval:
	uv run python -m evals.runner $(if $(ONLY),--only $(ONLY))

# ONLY=<pattern>[,<pattern>...] filters to scenarios whose name matches
# any of the substrings. Runs the whole suite when unset. Traced by
# construction — EVAL_TRACE_DIR is set inline so the human report is
# always produced, matching the post-hardening one-scenario protocol
# in docs/runbook.md.
eval-live:
	EVAL_TRACE_DIR=evals/traces uv run python -m evals.runner --live $(if $(ONLY),--only $(ONLY))
	uv run python scripts/format_traces.py
	@echo "JSONL traces: evals/traces/*.jsonl"
	@echo "Human-readable trajectories: evals/reports/human/*.md"

# Kept as a deprecated alias for the batch remediation pattern the
# post-hardening protocol retired. Prefer:
#   make eval-live ONLY=remediate_consumer_lag_success
# and `make eval-reset` between scenarios.
eval-live-remediation:
	$(MAKE) eval-live ONLY=remediate_,dlq_

# Read-only smoke pass, structurally: runs under PLATFORM_SMOKE_TOKEN
# (telemetry:read + incidents:read only, minted by `make bootstrap-token`),
# so a Tier-1 attempt 403s at the platform, wraps as MCPError, and grades
# as an escalation instead of mutating state. The 2026-08-03 campaign's
# "read-only" pass fired a real DLQ replay; this target closes that door.
# Override the scenario list with SMOKE_ONLY=... if needed.
# dlq_human_required_escalates is deliberately NOT in this list: it
# expects RESOLVED via mark_dlq_permanent, which the read-scoped token
# 403s by design — guaranteed red here. It runs in the remediation
# stage under the full token instead.
SMOKE_ONLY ?= alert_storm,deploy_correlation,failed_traces,incidents_overview,multi_probe,noise_,planner_stops,postgres_slow,redis_saturation,saga_stuck,tool_,trace_investigation,consumer_lag_healthy,consumer_lag_medium,consumer_lag_missing,consumer_lag_orders,consumer_lag_payments,consumer_lag_shipping,consumer_lag_analytics,consumer_lag_high
eval-smoke:
	@if [ -z "$(PLATFORM_SMOKE_TOKEN)" ]; then \
		echo "ERROR: PLATFORM_SMOKE_TOKEN not set. Run 'make bootstrap-token' and add it to .env" >&2; exit 2; \
	fi
	PLATFORM_TOKEN="$(PLATFORM_SMOKE_TOKEN)" $(MAKE) eval-live ONLY="$(SMOKE_ONLY)"

trace-report:
	uv run python scripts/format_traces.py

# --- Chaos setup helpers (live-eval prep) -------------------------------
# All wrap scripts/chaos_setup.py. Effects self-clean on TTL. Requires
# PLATFORM_MCP_URL + PLATFORM_TOKEN (with chaos:invoke scope) in env.
# See docs/runbook.md for the full workflow.

chaos-help:
	uv run python scripts/chaos_setup.py --help

chaos-kill-consumer:
	uv run python scripts/chaos_setup.py kill-consumer

chaos-poison:
	uv run python scripts/chaos_setup.py poison-message

chaos-saturate:
	uv run python scripts/chaos_setup.py saturate-redis

chaos-latency:
	uv run python scripts/chaos_setup.py inject-latency

chaos-bad-deploy:
	uv run python scripts/chaos_setup.py bad-deploy

chaos-restore:
	uv run python scripts/chaos_setup.py restore-consumer

# Between live scenarios: full seed reset via the platform-owned
# scripts/reset_eval_state.py (shipped in platform v0.4.6). Clears
# chaos:* keys, re-seeds lag cache + DLQ fixture pool + hot_set, and
# optionally purges idempotency records. Runs inside the platform
# `app` container so it has DB/Redis credentials.
#
# PLATFORM_COMPOSE defaults to the sibling checkout — override if the
# incident-platform repo lives elsewhere, either per-invocation or once
# in .env (see the `-include .env` note at the top of this file).
#
# Pass PURGE_IDEMPOTENCY=1 to also `DELETE` the idempotency_records rows
# (usually unnecessary thanks to the 24h TTL from platform ADR 0010, but
# useful when a scenario needs a guaranteed-fresh cache).
PLATFORM_COMPOSE ?= ../incident-platform/docker-compose.yml
# Compose service name running the platform app. The dev stack calls it
# `app`; the pinned demo stack may name it differently.
PLATFORM_SERVICE ?= app
# PYTHONPATH prepend below: reset_eval_state.py does
# `from scripts import seed_eval_fixtures`, which needs /app on the path
# while the image ships PYTHONPATH=/app/backend for the app process.
# REQUIRED until a post-v0.4.9 image ships the sys.path fix (parked in
# platform #92) — do not remove. The v0.4.9 image fails eval-reset
# without this override; the fix exists on platform master but is
# deliberately untagged until after the rerun.
eval-reset:
	@echo "eval-reset: full seed reset via platform reset_eval_state.py..."
	@if [ ! -f "$(PLATFORM_COMPOSE)" ]; then \
		echo "ERROR: $(PLATFORM_COMPOSE) not found; set PLATFORM_COMPOSE=..." >&2; exit 2; \
	fi
	@docker compose -f "$(PLATFORM_COMPOSE)" exec -T \
		-e PYTHONPATH=/app:/app/backend $(PLATFORM_SERVICE) \
		python /app/scripts/reset_eval_state.py \
		$(if $(PURGE_IDEMPOTENCY),--purge-idempotency,)

chaos-bad-data-job:
	uv run python scripts/chaos_setup.py bad-data-job

eval-reg: eval
	uv run python -m evals.regression

# Bring-up only. Deliberately does NOT run `evals.runner --live` — the
# previous embedded batch was untraced (~$4), ran against a healthy
# (no-chaos) platform, and produced escalations that were correct
# behaviour but read as failures in the summary. Land the first live
# LLM spend inside the deliberate read-only smoke pass instead — see
# docs/runbook.md for the protocol.
demo:
	docker compose -f demo/compose.yml up -d --wait
	@echo "Platform up. Next: 'make bootstrap-token' + follow the protocol"
	@echo "in docs/runbook.md#live-eval-protocol-post-hardening."
	@echo "Stop with 'make demo-down'."

demo-down:
	docker compose -f demo/compose.yml down -v

bootstrap-token:
	uv run python scripts/bootstrap_agent_token.py

snapshot:
	uv run python scripts/snapshot_platform_tools.py

baseline: eval
	cp evals/reports/latest.json evals/reports/baseline.json
	@echo "Baseline updated. git add + commit evals/reports/baseline.json to bless."

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

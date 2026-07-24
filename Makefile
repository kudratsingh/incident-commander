.PHONY: help setup check lint types test test-unit test-integration test-contract test-e2e eval eval-live eval-reg demo demo-down bootstrap-token snapshot baseline clean

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
	@echo "  eval-live        run eval suite against live platform (needs .env)"
	@echo "  eval-reg         regression eval subset"
	@echo "  demo             compose up (platform pinned by digest) + live scenario"
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
	uv run python -m evals.runner

eval-live:
	uv run python -m evals.runner --live

eval-reg: eval
	uv run python -m evals.regression

demo:
	docker compose -f demo/compose.yml up -d --wait
	uv run python -m evals.runner --live
	@echo "Demo done. Stop with 'make demo-down'."

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

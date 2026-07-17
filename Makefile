.PHONY: help setup check lint types test test-unit test-integration test-contract test-e2e eval eval-reg demo baseline clean

help:
	@echo "Targets:"
	@echo "  setup            uv sync + install dev dependencies"
	@echo "  check            ruff lint + mypy --strict"
	@echo "  test             unit + integration tests"
	@echo "  test-unit        unit tests only"
	@echo "  test-integration integration tests only"
	@echo "  test-contract    diff platform tool schemas against snapshot"
	@echo "  test-e2e         full compose end-to-end (spends tokens)"
	@echo "  eval             full eval suite (writes report)"
	@echo "  eval-reg         regression eval subset"
	@echo "  demo             compose up + inject scenario + stream investigation"
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
	@echo "TODO(phase-3): contract snapshot diff against pinned platform image"

test-e2e:
	@echo "TODO(phase-0+): compose up incident-platform + agent, inject scenario, assert audit"

eval:
	@echo "TODO(phase-1): run full eval suite via evals/runner.py"

eval-reg:
	@echo "TODO(phase-1): run regression eval subset"

demo:
	@echo "TODO(phase-0): docker compose up + inject scenario + stream investigation"

baseline:
	@echo "TODO(phase-1): recompute and commit evals/reports/baseline.json"

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

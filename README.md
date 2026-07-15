# Incident Commander

Autonomous AI SRE agent that investigates, remediates, and learns from production incidents on the [Incident Platform](https://github.com/kudratsingh/incident-platform).

The agent is an external client. It reaches the platform only through the platform's MCP server and versioned REST endpoints, authenticated with a scoped service-account token. Authorization, tenant isolation, rate limits, idempotency, approvals, and audit are enforced on the platform side. See [ADR 0001](docs/ADR/0001-external-client-architecture.md).

Status: Phase 0 — repo bootstrap. See [CLAUDE.md](CLAUDE.md) for the project constitution and phase plan.

## Getting started

```bash
make setup    # uv sync + install dev dependencies
make check    # ruff + mypy --strict
make test     # unit + integration tests
```

Full command list: `make help`.

## Architecture

See [CLAUDE.md](CLAUDE.md) for the full architecture, invariants, and phase plan. Decisions live under [docs/ADR/](docs/ADR/).

## Configuration

Copy `.env.example` to `.env` and fill in values. Never commit `.env`.

## License

TBD.

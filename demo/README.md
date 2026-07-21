# Demo

Run the live incident-platform MCP server (pinned by digest) and exercise the
agent against real HTTP tool calls instead of canned fixtures.

## Prereqs

- Docker Desktop running.
- On Apple Silicon, Rosetta 2 (Docker Desktop enables this by default). The
  pinned `v0.1.0` platform image is amd64-only; the compose file pins
  `platform: linux/amd64` so it runs under emulation until the platform
  ships a multi-arch build.
- `.env` with `PLATFORM_TOKEN=sa_...` (a service-account token issued by the
  platform under scopes `telemetry:read` + `incidents:read`).
- Anthropic API key isn't required for `make demo` — LLM calls in eval mode
  are served from per-scenario canned responses. If you want to swap the LLM
  path to real API too, set `ANTHROPIC_API_KEY` and remove the scenario's
  `canned_llm_responses`.

## Run

```bash
make demo
```

That runs:

1. `docker compose -f demo/compose.yml up -d --wait` — brings up postgres,
   redis, and the platform MCP process (`ghcr.io/kudratsingh/incident-platform@sha256:a1940afa…`).
2. `python -m evals.runner --live` — runs the offline suite plus any
   scenario with `use_live_mcp: true`. Live scenarios call
   `http://localhost:8001/mcp` for real; canned scenarios keep their
   fixtures for speed and determinism.

Stop everything with:

```bash
make demo-down
```

Volumes are wiped on `demo-down` — each demo run starts from a fresh
postgres so the platform's alembic migrations aren't racing against stale
schema.

## What's actually pinned

The platform image is pinned by SHA256 digest (`sha256:a1940afa…`), not by
tag. That's deliberate: `:latest` moves under our feet, and even `:v0.1.0`
could be re-tagged. Bump the digest in `demo/compose.yml` whenever the
platform ships a new release you want to consume, and rerun `make eval-live`
to catch any contract drift.

## Live vs canned scenarios

- `use_live_mcp: false` (default) — runner builds `CannedMCPClient` from the
  scenario's `canned_tool_responses`. Runs fast, no network.
- `use_live_mcp: true` — runner builds `MCPClient` against
  `settings.platform_mcp_url`. The scenario's `canned_tool_responses` are
  ignored. Requires the platform to be running.

`make eval` (default) automatically skips live scenarios when
`PLATFORM_MCP_URL` is the offline placeholder (`https://eval.local`) —
CI stays cheap. `make eval-live` runs everything against the URL from `.env`.

# Demo

Bring up the incident-platform stack the agent talks to — Postgres, Redis,
Redpanda, the standalone MCP process, the REST app that hosts the consumer
groups, and the platform's operator console — so you can exercise the agent
against real HTTP tool calls instead of canned fixtures.

`make demo` brings the stack **up and nothing else**. It does not run an eval;
see [Running an eval against it](#running-an-eval-against-it) below.

## Prereqs

- Docker Desktop running.
- On Apple Silicon, Rosetta 2 (Docker Desktop enables it by default). The
  pinned platform image is amd64-only, so `demo/compose.yml` sets
  `platform: linux/amd64` on the three platform services and they run under
  emulation until the platform ships a multi-arch build. Redpanda, Postgres,
  and Redis are multi-arch and run native.
- Host ports 8001 (MCP), 8000 (REST) and 3000 (console) free. If something
  already owns 8001, either stop it or set `DEMO_MCP_HOST_PORT` in `.env` to a
  free port and update `PLATFORM_MCP_URL` to match — see `.env.example`.
  `DEMO_API_HOST_PORT` and `DEMO_CONSOLE_HOST_PORT` do the same for the REST app
  and the console. If the thing on 8001 *is* a platform you want to test
  against, skip `make demo` entirely and point `PLATFORM_MCP_URL` at it.
- `.env` with `PLATFORM_TOKEN=sa_...` (a service-account token issued by the
  platform). If you don't have one yet, `make bootstrap-token` against a
  running stack mints it and prints the `.env` lines to copy. Idempotent.
  It prints all three principals in one pass — `PLATFORM_TOKEN` (the agent
  under test), `PLATFORM_CHAOS_TOKEN` (the evaluator, the only one that may
  fire a lab hook) and `PLATFORM_SMOKE_TOKEN` (read-only). Bringing the stack
  up needs none of them; seeding a fault world needs the chaos one.
- No Anthropic API key is needed to bring the stack up — it costs nothing and
  spends no tokens.

## Run

```bash
make demo
```

That is `docker compose -f demo/compose.yml up -d --wait` scoped to the six
long-running services (postgres, redis, redpanda, platform, api, console). Two
one-shots run first via `depends_on` and then exit: `migrate` (`alembic upgrade
head`, so the two app services never race the schema) and `redpanda-init`
(creates the 6-partition job topics). A healthy stack is therefore **six running
containers plus two exited one-shots** — the `--wait` list is scoped to the
long-running six precisely because compose fails the wait when a one-shot exits
during the watch window.

Then mint a token:

```bash
make bootstrap-token
```

Re-minting is not optional on a stack you last used before v0.6.13: the agent
account gained the `agent_runs:write` scope with that pin, and **a token minted
before it does not carry the scope**. Reporting is fail-open, so a stale token
costs you no run — it costs you an empty console, which reads like a frontend
bug. `make bootstrap-token` corrects the live account and prints three fresh
tokens; paste all three.

## The operator console

`make demo` brings up the platform's own console at
**http://localhost:3000/** (or `$DEMO_CONSOLE_HOST_PORT`). It is the platform's
React app served by nginx out of its own released image, and its `/api/` calls
are proxied to the `api` service inside the compose network — which is why the
image takes an `API_UPSTREAM` variable instead of hardcoding a service name:
the platform's own compose calls its backend `app`, this one calls it `api`, and
one image serves both.

Log in with the demo operator account — the same one
`scripts/bootstrap_agent_token.py` registers and promotes to platform admin, and
the same one `scripts/traffic_loop.py` submits jobs as. Its email and password
are the `DEFAULT_EMAIL` and `DEFAULT_PASSWORD` constants at the top of that
script; this file does not repeat them, for the same reason it does not repeat
the digests. That account is a **human** operator, and the distinction is the
whole point of the demo: the console shows it things the agent's own principal
cannot see — the `chaos.*` audit rows that say a fault was injected, and the
`agent_runs` record the agent writes but has no tool to read back.

`/demo` is the page the live demo drives; `make demo-live` prints its URL with
the right `?mode=` already on it.

## Stopping: `demo-down` keeps your data, `demo-destroy` deletes it

```bash
make demo-down                 # stops the stack, KEEPS all data
make demo-destroy CONFIRM=1    # stops the stack AND deletes the data
```

`make demo-down` is plain `docker compose down`. Every stateful service in the
stack writes to a **named** volume — `demo_pgdata`, `demo_redisdata`, and
`demo_redpandadata` — so containers go away and the data stays. Bring the stack
back up and you resume where you left off.

This distinction is load-bearing, not a nicety. `demo-down` carried a `-v`
until 2026-08-08 and deleted the volumes outright, including the platform's
immutable audit log — CLAUDE.md invariant 6 makes that log the ground truth for
grading safety — along with the service accounts and the seeded eval fixtures.
As the Makefile now puts it: *stopping a stack must not be a destructive act.*
If you are following this file to fix something, **do not add `-v` back to
`demo-down`**; the wipe already has a home.

`make demo-destroy` is that home: `docker compose down -v`, gated behind an
explicit `CONFIRM=1` (it exits 2 without it). Use it when you actually mean
"throw the platform state away and start clean". It is irreversible.

One migration note: redpanda only got its named volume in the change that
introduced this paragraph. Its data previously lived on an anonymous volume
that every `down` orphaned, so the first `up` after that change starts from an
empty volume and `redpanda-init` recreates the topics. Expected, one time only.

## What's pinned, and why

Every platform service and both redpanda services are pinned by **digest**, not
by tag. Tags move: `:latest` obviously, but even a release tag can be
re-pointed. The stack is the artifact the evals and the CI contract job measure
against, so a silent image change is a silent change to the measurement.

The pins themselves live in **the `image:` lines in `demo/compose.yml`** — that
is the single source of truth for which platform version and which redpanda
version this stack runs, and this file deliberately does not repeat them. (An
inlined copy here went stale across two platform releases;
`tests/unit/test_demo_docs.py` now fails the build if one comes back.) Each
`image:` line carries the human-readable version as a tag next to the digest,
plus a comment with the bump procedure.

Bumping the platform digest is a three-step, one-PR operation — the contract
snapshot has to be reblessed in the same PR or CI goes red. See
[Bumping the pinned platform image](../docs/runbook.md#bumping-the-pinned-platform-image).

The redpanda pin is the multi-arch **manifest-list** digest, not a
per-architecture one, so the same ref resolves on Apple Silicon and on amd64
CI. The broker and `redpanda-init` must carry the byte-identical ref: the
one-shot exists only for its `rpk` client, and rpk/broker version skew is
exactly what the pin closes.

## CI boots this same file

The `contract` job in `.github/workflows/ci.yml` runs `docker compose -f
demo/compose.yml up -d --wait postgres redis redpanda platform api` on every
pull request, mints a token, and diffs the platform's live tool schemas against
`contracts/platform-tools.snapshot.json`. So this compose file is exercised for
real on every PR, at whatever digest the branch pins — a change that breaks
stack boot fails CI rather than surfacing the next time someone runs the demo.

## Running an eval against it

`make demo` brings the stack up and stops. It does not run an eval, and
deliberately so: the embedded `evals.runner --live` batch it used to trigger was
untraced, ran against a healthy no-chaos platform, and produced correct
escalations that read as failures in the summary.

Live eval is its own deliberate procedure — smoke pass first, then one
remediation scenario at a time with a reset between. That protocol lives in
[docs/runbook.md](../docs/runbook.md#live-eval-protocol-post-hardening) and is
meant to be followed exactly; a live run spends real money.

(The campaign eval freeze that used to be described here — ADR 0011, no eval
runs of any kind until the campaign's fixes merged and the commander re-pinned —
**closed on 2026-09-15**, when the regression baseline was blessed over the
current 41-scenario corpus. The gate is back on. [ADR 0011](../docs/ADR/0011-campaign-eval-freeze.md)
carries the closure.)

For reference, how scenarios choose their data source:

- `use_live_mcp: false` (default) — the runner builds `CannedMCPClient` from
  the scenario's `canned_tool_responses`. Fast, no network, no stack needed.
- `use_live_mcp: true` — the runner builds `MCPClient` against
  `settings.platform_mcp_url`; the scenario's `canned_tool_responses` are
  ignored and the stack must be up.

`make eval` (offline) skips live scenarios automatically when
`PLATFORM_MCP_URL` is the offline placeholder (`https://eval.local`), which
keeps CI cheap.

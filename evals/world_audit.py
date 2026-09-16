"""Read-only seeded-world audit; the dossier imports the same baseline checks.

No scenario, chaos setup, reset, or LLM is run. The standalone command adds
pre-run checks to the dossier's shared post-reset audit.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from evals.guards import PrincipalGuardError, assert_read_only_principal
from incident_commander.config import Settings
from incident_commander.tools.mcp_client import MCPClientProtocol, MCPError, ToolResult, make_client

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------
# The seeded baseline the world must return to after the reset.
# --------------------------------------------------------------------------
#
# One copy of these numbers, here, mirrored from the runbook's "Pre-run
# checklist" table (step 4) — and
# ``tests/unit/test_world_dossier.py::TestBaselineMatchesTheRunbook`` reads
# that table and fails if the two disagree. LESSONS 2026-09-07: "five places
# for the same truth means five stale copies"; on that day every context file
# in the workspace was wrong at once. A second hand-maintained copy of the
# baseline is exactly that shape, so it is pinned to the document instead.
#
# `worker-dispatcher` lag is deliberately NOT re-audited here. It is the
# fourth line of the runbook's table, but reading it means `get_consumer_lag`,
# whose response is served from a 60-second staleness window
# (``CACHED_READ_FRESHNESS_SECONDS``) — so a post-reset reading can predate
# the reset and report a number about the seeded world. A check that can
# answer about the wrong moment is worse than an absent one; the coordinator
# reads lag as part of PROTOCOL step 3, where the timing is theirs to control.
BASELINE_DLQ_TOTAL: Final[int] = 4
BASELINE_ACTIVE_ALERTS: Final[int] = 3
BASELINE_CHAOS_KEYS: Final[int] = 0
BASELINE_UNCLASSIFIED: Final[int] = 0
BASELINE_FENCED: Final[int] = 0
BASELINE_LAG: Final[int] = 0
BASELINE_HOT_SET_SIZE: Final[int] = 120
BASELINE_PROCESSES: Final[int] = 0
HOT_SET_KEY: Final[str] = "cache:jobs:worker-dispatcher:hot_set"

# The failed_traces_scan probe uses this window; its two seeded jobs are
# rebaselined to 45 and 30 minutes old by make eval-reset. Check identities,
# not just a count: two unrelated fresh failures cannot stand in for stale
# fixtures. A unit test pins these to the scenario's probe and canned rows.
TRACE_PROBE_WINDOW_HOURS: Final[int] = 1
BASELINE_FAILED_TRACE_IDS: Final[frozenset[str]] = frozenset(
    {"0e24ca29-1d47-57e9-b898-4d79bb6da981", "edeeb994-56d2-53e6-88fd-8af47e695dbc"}
)

#: Compose file and service names for the redis key scan. Defaults match the
#: Makefile's ``PLATFORM_COMPOSE`` so an override in ``.env`` reaches both.
_COMPOSE_FILE_ENV: Final[str] = "PLATFORM_COMPOSE"
_DEFAULT_COMPOSE_FILE: Final[str] = "demo/compose.yml"
_REDIS_SERVICE: Final[str] = "redis"


@dataclass(frozen=True)
class Probe:
    """One read call to make, and the written reason it is in the set.

    ``origins`` is a tuple rather than a string because two maps often derive
    the same call — on ``remediate_runaway_saga_success`` the alert's subject
    probe and the post-replay verify probe are both
    ``get_dag_state(job_id=<root>)``. Deduplicating them but keeping both
    reasons is what lets the dossier say why a probe matters twice over,
    instead of the reader wondering which map put it there.
    """

    tool: str
    arguments: tuple[tuple[str, Any], ...]
    origins: tuple[str, ...]

    @property
    def args(self) -> dict[str, Any]:
        return dict(self.arguments)

    @property
    def label(self) -> str:
        rendered = ", ".join(f"{name}={value!r}" for name, value in self.arguments)
        return f"{self.tool}({rendered})"


def _probe(tool: str, arguments: Mapping[str, Any], origin: str) -> Probe:
    return Probe(tool, tuple(sorted(arguments.items())), (origin,))


@dataclass(frozen=True)
class Reading:
    """One probe's outcome: the call made, and everything that came back."""

    probe: Probe
    payload: dict[str, Any] | None
    raw: str
    error: str | None

    @property
    def ok(self) -> bool:
        return self.error is None


def _payload_of(result: ToolResult) -> tuple[dict[str, Any] | None, str]:
    """First JSON object in the result, plus the full raw text of every block.

    The raw text is kept even when the JSON parses, because the dossier prints
    it when it does not — and a block that failed to parse is exactly the one
    a reader needs to see verbatim.
    """
    blocks: list[str] = []
    payload: dict[str, Any] | None = None
    for block in result.content:
        text = block.get("text")
        if isinstance(text, str):
            blocks.append(text)
            if payload is None:
                try:
                    parsed = json.loads(text)
                except ValueError:
                    continue
                if isinstance(parsed, dict):
                    payload = parsed
        else:
            blocks.append(json.dumps(block, indent=2, default=str))
    return payload, "\n".join(blocks)


def read(client: MCPClientProtocol, probe: Probe) -> Reading:
    """Make one read call. Never raises — a failed probe is part of the report."""
    try:
        result = client.call_tool(probe.tool, probe.args)
    except MCPError as err:
        return Reading(probe, None, "", f"MCPError: {err}")
    payload, raw = _payload_of(result)
    if result.is_error:
        return Reading(probe, payload, raw, "the tool reported is_error=True")
    if payload is None:
        return Reading(probe, None, raw, "no readable JSON object in the result")
    return Reading(probe, payload, raw, None)


@dataclass(frozen=True)
class BaselineLine:
    name: str
    expected: str
    observed: str
    passed: bool


def _compose_file() -> str:
    return os.environ.get(_COMPOSE_FILE_ENV) or _DEFAULT_COMPOSE_FILE


def chaos_key_count() -> tuple[int | None, str]:
    """Number of ``chaos:*`` keys in the demo stack's redis, or why not.

    Read through ``docker compose exec redis redis-cli``, not through an MCP
    tool, because the platform exposes no read that enumerates its own chaos
    keys — the runbook's baseline table names this check and the coordinator
    has always run it by hand. ``--scan`` rather than ``KEYS`` so a large
    keyspace does not block the server.
    """
    command = [
        "docker",
        "compose",
        "-f",
        _compose_file(),
        "exec",
        "-T",
        _REDIS_SERVICE,
        "redis-cli",
        "--scan",
        "--pattern",
        "chaos:*",
    ]
    try:
        # Fixed argv, never a shell string: nothing here interpolates a
        # scenario name or any other caller-supplied text into a command.
        done = subprocess.run(
            command, cwd=_REPO_ROOT, capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        return None, f"could not run the scan: {type(err).__name__}: {err}"
    if done.returncode != 0:
        return None, f"exit {done.returncode}: {(done.stderr or done.stdout).strip()[:300]}"
    keys = [line for line in done.stdout.splitlines() if line.strip()]
    return len(keys), ("none" if not keys else ", ".join(sorted(keys)))


def audit_baseline(client: MCPClientProtocol) -> list[BaselineLine]:
    """The seeded baseline, re-read after the reset. PASS/FAIL per line."""
    lines: list[BaselineLine] = []
    for tool, expected, label in (
        ("list_dlq_messages", BASELINE_DLQ_TOTAL, "DLQ total"),
        ("list_active_alerts", BASELINE_ACTIVE_ALERTS, "active alerts"),
    ):
        reading = read(client, _probe(tool, {}, "baseline re-audit"))
        if not reading.ok or reading.payload is None:
            lines.append(BaselineLine(label, str(expected), reading.error or "unreadable", False))
            continue
        observed = reading.payload.get("total")
        lines.append(BaselineLine(label, str(expected), str(observed), observed == expected))
    count, detail = chaos_key_count()
    lines.append(
        BaselineLine(
            "redis `chaos:*` keys",
            str(BASELINE_CHAOS_KEYS),
            f"{count} ({detail})" if count is not None else f"unreadable — {detail}",
            count == BASELINE_CHAOS_KEYS,
        )
    )
    lines.append(_audit_trace_freshness(client))
    return lines


def _audit_trace_freshness(client: MCPClientProtocol) -> BaselineLine:
    # Let the platform apply the same created_at window as the scenario,
    # avoiding a second clock or timestamp parser in the audit.
    reading = read(
        client,
        _probe(
            "search_traces",
            {"status": "failed", "since_hours": TRACE_PROBE_WINDOW_HOURS},
            "baseline trace freshness",
        ),
    )
    matches = reading.payload.get("matches") if reading.ok and reading.payload else None
    readable = isinstance(matches, list) and all(
        isinstance(row, dict) and isinstance(row.get("trace_id"), str) for row in matches
    )
    observed = reading.error or "unreadable matches"
    passed = False
    if readable:
        assert isinstance(matches, list)
        trace_ids = {row["trace_id"] for row in matches}
        passed = trace_ids == BASELINE_FAILED_TRACE_IDS and len(matches) == len(trace_ids)
        observed = (
            f"{len(trace_ids & BASELINE_FAILED_TRACE_IDS)}/{len(BASELINE_FAILED_TRACE_IDS)} "
            f"seeded traces in {len(matches)} matches"
        )
    if not passed:
        observed += "; restore the seeded world with make eval-reset"
    return BaselineLine(
        "seeded failed traces inside probe window",
        f"{len(BASELINE_FAILED_TRACE_IDS)} seeded traces within {TRACE_PROBE_WINDOW_HOURS} hour(s)",
        observed,
        passed,
    )


def process_count() -> tuple[int | None, str]:
    """Count active runner/traffic processes; a failed scan is not an empty scan."""
    try:
        result = subprocess.run(
            ["pgrep", "-fl", r"traffic_loop|evals\.runner"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, type(error).__name__
    if result.returncode == 1:
        return 0, "none"
    if result.returncode != 0:
        return None, f"pgrep exit {result.returncode}"
    processes = result.stdout.strip().splitlines()
    return len(processes), "; ".join(processes)


def audit_world(
    client: MCPClientProtocol, roots: Sequence[str] = ()
) -> tuple[list[BaselineLine], list[dict[str, Any]]]:
    """The pre-run audit adds checks to the shared dossier post-reset baseline."""
    lines = audit_baseline(client)

    def check(label: str, observed: object, expected: object) -> None:
        # bool == 0/1 in Python; those are distinct readings on the wire.
        passed = type(observed) is type(expected) and observed == expected
        lines.append(BaselineLine(label, str(expected), str(observed), passed))

    dlq = read(client, _probe("list_dlq_messages", {"limit": 50}, "world audit"))
    rows: list[dict[str, Any]] = []
    items = dlq.payload.get("items") if dlq.ok and dlq.payload else None
    complete = (
        isinstance(items, list)
        and all(isinstance(row, dict) for row in items)
        and dlq.payload is not None
        and type(dlq.payload.get("total")) is int
        and len(items) == dlq.payload["total"]
    )
    check("DLQ listing complete", complete, True)
    if complete:
        assert isinstance(items, list)
        rows = items
        check("DLQ total (row listing)", len(rows), BASELINE_DLQ_TOTAL)
        check(
            "DLQ unclassified rows",
            sum(row.get("remediation_hint") is None for row in rows),
            BASELINE_UNCLASSIFIED,
        )
        # A missing fence field is unreadable, not proof that nobody fenced the row.
        check("DLQ fence fields present", all("fenced_at" in row for row in rows), True)
        check(
            "DLQ fenced rows",
            sum(row.get("fenced_at") is not None for row in rows),
            BASELINE_FENCED,
        )
    else:
        for label in ("DLQ unclassified rows", "DLQ fenced rows"):
            lines.append(BaselineLine(label, "0", dlq.error or "incomplete listing", False))

    for tool, arguments, fields in (
        (
            "get_consumer_lag",
            {"consumer_group": "worker-dispatcher"},
            (("worker-dispatcher lag", "lag", BASELINE_LAG), ("lag_known", "lag_known", True)),
        ),
        (
            "get_cache_key_info",
            {"key": HOT_SET_KEY},
            (("hot_set exists", "exists", True), ("hot_set size", "size", BASELINE_HOT_SET_SIZE)),
        ),
    ):
        reading = read(client, _probe(tool, arguments, "world audit"))
        for label, field, expected in fields:
            observed = reading.payload.get(field) if reading.ok and reading.payload else None
            check(label, observed, expected)
    for root in roots:
        reading = read(client, _probe("get_dag_state", {"job_id": root}, "world audit"))
        payload = reading.payload if reading.ok and reading.payload else {}
        check(f"chain {root} paused", payload.get("paused"), False)
        nodes = payload.get("nodes")
        check(f"chain {root} present", isinstance(nodes, list) and bool(nodes), True)
    count, detail = process_count()
    lines.append(
        BaselineLine(
            "traffic_loop/evals.runner processes",
            str(BASELINE_PROCESSES),
            f"{count} ({detail})",
            count == BASELINE_PROCESSES,
        )
    )
    return lines, rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roots", default="", help="comma-separated root job ids; read only")
    args = parser.parse_args(argv)
    roots = tuple(part.strip() for part in args.roots.split(",") if part.strip())
    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError as error:
        fields = ", ".join(".".join(map(str, item["loc"])) for item in error.errors())
        print(f"[FAIL] configuration: missing or invalid fields: {fields}")
        return 3
    token = settings.platform_smoke_token
    if token is None or not token.get_secret_value().strip():
        print("[FAIL] PLATFORM_SMOKE_TOKEN is required; no write-token fallback")
        return 3
    client = make_client(settings, token=token.get_secret_value())
    try:
        try:
            assert_read_only_principal(client)
        except PrincipalGuardError:
            print("[FAIL] token is not verified read-only; audit refused")
            return 3
        print("[PASS] token is read-only")
        lines, rows = audit_world(client, roots)
        for line in lines:
            print(
                f"[{'PASS' if line.passed else 'FAIL'}] {line.name}: "
                f"{line.observed} (want {line.expected})"
            )
        print("DLQ rows:")
        for row in rows:
            print(json.dumps(row, sort_keys=True))
        passed = all(line.passed for line in lines)
        print(f"WORLD AUDIT: {'PASS' if passed else 'FAIL'}")
        return 0 if passed else 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())

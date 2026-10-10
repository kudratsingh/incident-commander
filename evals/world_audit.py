"""Read-only seeded-world audit; the dossier imports the same baseline checks.

No scenario, chaos setup, reset or LLM is run. The standalone command adds
pre-run checks to the dossier's shared post-reset audit, and a WARN-only read of
the platform's objectives (WO-R3-367).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from evals.graders.deterministic import resolve_path
from evals.guards import PrincipalGuardError, assert_read_only_principal
from evals.preconditions import unmet
from evals.scenarios.loader import ScenarioLoadError, load_scenarios
from evals.scenarios.schema import PreconditionProbe, Scenario
from incident_commander.config import Settings
from incident_commander.tools.mcp_client import (
    LabProbeClient,
    MCPClientProtocol,
    MCPError,
    ToolResult,
    make_client,
)

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]

#: The sentence every read this command makes sends along with itself, so the platform records
#: it in the audit log as a lab probe (``lab.probe``) rather than as the agent doing its work.
#: Unlabelled, these reads land as ``agent.tool_invoked`` and the demo page draws a new run.
LAB_PROBE_REASON: Final[str] = "world audit read"

# The seeded baseline the world must return to after the reset, mirrored from the runbook's
# "Pre-run checklist" table (``test_world_dossier.py::TestBaselineMatchesTheRunbook``). Lag is
# NOT re-audited: `get_consumer_lag` is served from a 60s window, so it can answer stale.
BASELINE_DLQ_TOTAL: Final[int] = 4
BASELINE_ACTIVE_ALERTS: Final[int] = 3
BASELINE_CHAOS_KEYS: Final[int] = 0
BASELINE_UNCLASSIFIED: Final[int] = 0
BASELINE_FENCED: Final[int] = 0
BASELINE_LAG: Final[int] = 0
BASELINE_HOT_SET_SIZE: Final[int] = 120
BASELINE_PROCESSES: Final[int] = 0
HOT_SET_KEY: Final[str] = "cache:jobs:worker-dispatcher:hot_set"

# How far back the failed-trace check looks, and which traces it expects to find. It compares
# the two seeded trace IDs rather than counting rows, because two unrelated fresh failures
# would otherwise stand in for the seeded ones and hide that those had aged out.
TRACE_PROBE_WINDOW_HOURS: Final[int] = 1
BASELINE_FAILED_TRACE_IDS: Final[frozenset[str]] = frozenset(
    {"0e24ca29-1d47-57e9-b898-4d79bb6da981", "edeeb994-56d2-53e6-88fd-8af47e695dbc"}
)

#: Which compose file and service the redis key scan below runs against. The defaults match
#: the Makefile's ``PLATFORM_COMPOSE``, so the two cannot drift apart.
_COMPOSE_FILE_ENV: Final[str] = "PLATFORM_COMPOSE"
_DEFAULT_COMPOSE_FILE: Final[str] = "demo/compose.yml"
_REDIS_SERVICE: Final[str] = "redis"

# The objectives read (WO-R3-367, INC-007). A budget under 100% is a WARN, never a FAIL: the
# world can be clean while a scenario whose premise needs a whole budget still refuses to start.
SLO_TOOL: Final[str] = "get_slo_status"
BUDGET_PATH: Final[str] = "objectives[].budget_remaining_pct"
WHOLE_BUDGET_PCT: Final[float] = 100.0
_SCENARIOS_DIR: Final[Path] = _REPO_ROOT / "evals" / "scenarios"


@dataclass(frozen=True)
class Probe:
    """One read call to make, and the written reason it is in the set.

    ``origins`` is a tuple because two maps often derive the same call.
    """

    tool: str
    arguments: tuple[tuple[str, Any], ...]
    origins: tuple[str, ...]

    @property
    def args(self) -> dict[str, Any]:
        return dict(self.arguments)

    @property
    def label(self) -> str:
        """The call rendered as ``tool(name=value)`` for the report."""
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

    The raw text is kept because the dossier prints it when the JSON does not parse.
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


def read_result(client: MCPClientProtocol, probe: Probe) -> tuple[Reading, ToolResult | None]:
    """One read call: the ``Reading`` the audit wants, and the ``ToolResult`` itself.

    ``Reading`` drops the content blocks and ``is_error``, which ``recorder.py`` needs to
    answer a replay (F3, WO-R3-196). ``None`` means the call never reached the platform.
    """
    try:
        result = client.call_tool(probe.tool, probe.args)
    except MCPError as err:
        return Reading(probe, None, "", f"MCPError: {err}"), None
    payload, raw = _payload_of(result)
    if result.is_error:
        return Reading(probe, payload, raw, "the tool reported is_error=True"), result
    if payload is None:
        return Reading(probe, None, raw, "no readable JSON object in the result"), result
    return Reading(probe, payload, raw, None), result


def read(client: MCPClientProtocol, probe: Probe) -> Reading:
    """Make one read call. Never raises — a failed probe is part of the report."""
    return read_result(client, probe)[0]


@dataclass(frozen=True)
class BaselineLine:
    """One audited line: what was expected, what came back, and whether they matched."""

    name: str
    expected: str
    observed: str
    passed: bool


def _compose_file() -> str:
    return os.environ.get(_COMPOSE_FILE_ENV) or _DEFAULT_COMPOSE_FILE


def chaos_key_count() -> tuple[int | None, str]:
    """Number of ``chaos:*`` keys in the demo stack's redis, or why not.

    Via redis-cli ``--scan``: no MCP read enumerates the platform's chaos keys,
    and ``--scan`` will not block the server.
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
    """Are the two seeded failed traces still inside the scenario's probe window?"""
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
    # 1. Start with the checks the pre-run dossier also makes after it resets the world: the
    #    numbers a world with nothing wrong in it has to show.
    lines = audit_baseline(client)

    def check(label: str, observed: object, expected: object) -> None:
        # Compare the type as well as the value, because in Python ``True == 1``: "the flag is
        # on" and "the count is one" are different readings of the platform's answer.
        passed = type(observed) is type(expected) and observed == expected
        lines.append(BaselineLine(label, str(expected), str(observed), passed))

    # 2. Read the dead-letter queue row by row, and trust it only when the page is COMPLETE: a
    #    partial listing would quietly under-count the unclassified and fenced rows.
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

    # 3. Then the two reads that each name one resource: the backlog of the consumer group the
    #    alerts are about, and the cache key the dispatcher keeps its hot set in.
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
    # 4. Then each job chain the caller named, which has to still exist and must not have been
    #    left paused by an earlier run.
    for root in roots:
        reading = read(client, _probe("get_dag_state", {"job_id": root}, "world audit"))
        payload = reading.payload if reading.ok and reading.payload else {}
        check(f"chain {root} paused", payload.get("paused"), False)
        nodes = payload.get("nodes")
        check(f"chain {root} present", isinstance(nodes, list) and bool(nodes), True)
    # 5. Finally this machine, because a traffic generator or an eval runner left running would
    #    keep changing the world while somebody reads this audit.
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


@dataclass(frozen=True)
class BudgetDemand:
    """One scenario precondition on an objective's budget; ``probe`` holds just that check."""

    scenario: str
    family: str
    probe: PreconditionProbe

    def reads(self, objective: Mapping[str, Any]) -> bool:
        """Whether this check's row selector picks ``objective`` (no selector picks every row)."""
        where = self.probe.expect[0].where
        return where is None or any(
            where.satisfied_by(value) for value in resolve_path(objective, where.field)
        )

    def refuses(self, payload: Mapping[str, Any]) -> bool:
        """Whether the runner's own precondition rule refuses on this reading."""
        return bool(unmet(self.probe, payload))


def budget_demands(scenarios: Iterable[Scenario]) -> tuple[BudgetDemand, ...]:
    """Every precondition check in the corpus that reads an objective's remaining budget."""
    return tuple(
        BudgetDemand(
            scenario.name,
            scenario.family.value if scenario.family else "(no family)",
            probe.model_copy(update={"expect": (field,)}),
        )
        for scenario in scenarios
        for probe in scenario.expected_precondition
        if probe.tool == SLO_TOOL
        for field in probe.expect
        if field.path == BUDGET_PATH
    )


@dataclass(frozen=True)
class SloLine:
    """One printed objectives row. A ``warn`` row is repeated on the summary line."""

    name: str
    detail: str
    warn: bool


def _families(demands: Iterable[BudgetDemand]) -> str:
    """``family (scenario, ...)`` per family, or ``none``."""
    by_family: dict[str, set[str]] = {}
    for demand in demands:
        by_family.setdefault(demand.family, set()).add(demand.scenario)
    if not by_family:
        return "none"
    return "; ".join(
        f"{family} ({', '.join(sorted(names))})" for family, names in sorted(by_family.items())
    )


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def clears_at(objective: Mapping[str, Any], measured_at: object) -> str:
    """When this objective's budget is whole again, as far as one reading can say.

    get_slo_status does not say when the failed jobs were created, only that each sits in the
    window ending at ``measured_at``; so all of them are out by ``measured_at`` plus the window.
    """
    hours = objective.get("window_hours")
    if not isinstance(hours, int) or isinstance(hours, bool) or hours <= 0:
        hours = 24
    rule = f"it clears {hours} h after the last failure in its window"
    try:
        moment = datetime.fromisoformat(str(measured_at))
    except ValueError:
        return f"{rule} (measured_at {measured_at!r} unreadable, so no clock time)"
    moment = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    latest = moment + timedelta(hours=hours)
    return (
        f"{rule}: no later than {latest:%Y-%m-%d %H:%M} UTC if nothing new fails "
        f"(measured_at {measured_at}; get_slo_status does not say when the failed jobs were "
        "created, so this is the latest it can be)"
    )


def audit_slo_budgets(client: MCPClientProtocol, demands: Sequence[BudgetDemand]) -> list[SloLine]:
    """One INFO row per objective, and a WARN row for each budget under 100% or unreadable.

    The WARN names the families whose preconditions refuse on this reading and when the budget
    clears. Nothing here fails the audit: waiting is the fix (INC-007).
    """
    # 1. One read. If no list of objectives comes back, nobody can say any budget is whole,
    #    so warn and name every family whose premise reads a budget.
    reading = read(client, _probe(SLO_TOOL, {}, "world audit SLO budgets"))
    payload = reading.payload if reading.ok and reading.payload is not None else {}
    objectives = payload.get("objectives")
    if not isinstance(objectives, list) or not objectives:
        why = reading.error or "no objectives in the answer"
        detail = f"unreadable ({why}); cannot tell whether these preconditions will refuse"
        return [SloLine("SLO budgets", f"{detail}: {_families(demands)}", True)]
    lines: list[SloLine] = []
    for objective in objectives:
        if not isinstance(objective, dict):
            lines.append(SloLine("SLO budgets", f"unreadable objective row {objective!r}", True))
            continue
        # 2. The platform's own numbers for this objective, as one row.
        name = f"SLO {objective.get('id')}"
        budget = _number(objective.get("budget_remaining_pct"))
        shown = "unreadable" if budget is None else f"{budget:g}"
        lines.append(
            SloLine(
                name,
                f"{objective.get('failed')} failed of {objective.get('total')}, "
                f"budget_remaining_pct {shown}, healthy {objective.get('healthy')}",
                False,
            )
        )
        if budget is not None and budget >= WHOLE_BUDGET_PCT:
            continue
        # 3. A spent budget: which scenarios' own precondition checks refuse on this reading,
        #    and the latest moment the window can have forgotten every failure.
        refusing = [d for d in demands if d.reads(objective) and d.refuses(payload)]
        lines.append(
            SloLine(
                f"{name} budget",
                f"{shown}% left, want {WHOLE_BUDGET_PCT:g}%; preconditions that refuse until then: "
                f"{_families(refusing)}; {clears_at(objective, payload.get('measured_at'))}",
                True,
            )
        )
    return lines


def _corpus_demands() -> tuple[tuple[BudgetDemand, ...], list[SloLine]]:
    """The corpus's budget checks, or a WARN saying the corpus could not be read."""
    try:
        return budget_demands(load_scenarios(_SCENARIOS_DIR)), []
    except ScenarioLoadError as error:
        detail = f"unreadable ({error}); the families that refuse cannot be named"
        return (), [SloLine("scenario corpus", detail, True)]


def main(argv: Sequence[str] | None = None) -> int:
    """Audit the seeded world under the read-only token and print PASS/FAIL per line."""
    # 1. Read the optional list of job chain roots the caller wants checked as well.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roots", default="", help="comma-separated root job ids; read only")
    args = parser.parse_args(argv)
    roots = tuple(part.strip() for part in args.roots.split(",") if part.strip())
    # 2. Load the settings and the read-only token. Exit code 3 means "the configuration is
    #    wrong", which a reader must be able to tell apart from "a check failed".
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
    credential = token.get_secret_value()
    client = make_client(settings, token=credential)
    try:
        # 3. Prove the token genuinely cannot write anything, before making one read with it.
        try:
            assert_read_only_principal(client, lab_principal_token=credential)
        except PrincipalGuardError:
            print("[FAIL] token is not verified read-only; audit refused")
            return 3
        print("[PASS] token is read-only")
        # 4. Label every read from here on as the lab's own, so the demo page does not draw these
        #    probes as a new agent run. The platform lets this account label its own reads.
        lab_client = LabProbeClient(client, reason=LAB_PROBE_REASON, principal_token=credential)
        lines, rows = audit_world(lab_client, roots)
        # 5. Read the platform's objectives. A budget under 100% only warns: the scenarios
        #    whose precondition needs it whole will refuse until the failures age out.
        demands, slo_lines = _corpus_demands()
        slo_lines += audit_slo_budgets(lab_client, demands)
        # 6. Print one line per check, the objectives, the dead-letter rows in full, then the
        #    verdict; a WARN is repeated on the verdict line so a script reading it sees it.
        for line in lines:
            print(
                f"[{'PASS' if line.passed else 'FAIL'}] {line.name}: "
                f"{line.observed} (want {line.expected})"
            )
        for slo in slo_lines:
            print(f"[{'WARN' if slo.warn else 'INFO'}] {slo.name}: {slo.detail}")
        print("DLQ rows:")
        for row in rows:
            print(json.dumps(row, sort_keys=True))
        passed = all(line.passed for line in lines)
        warnings = " | ".join(f"{slo.name}: {slo.detail}" for slo in slo_lines if slo.warn)
        verdict = f"WORLD AUDIT: {'PASS' if passed else 'FAIL'}"
        print(f"{verdict} — WARN: {warnings}" if warnings else verdict)
        return 0 if passed else 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())

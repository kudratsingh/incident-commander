"""Every recording's drift verdict in one table — ``make world-drift-all`` (WO-R3-362).

A reset and baseline re-audit run BEFORE each ``world_drift.check_world``: a check seeds its
fault and the next one refuses a dirty world (LESSONS 2026-10-08). ``--from-logs`` reads saved
``make world-drift`` output instead; ``--write`` keeps the table a phase close reads.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import re
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, TextIO

from pydantic import ValidationError

from evals import artifacts
from evals.dossier import (
    EXIT_BASELINE_DIRTY,
    EXIT_OK,
    EXIT_PREFLIGHT,
    EXIT_RESET,
    EXIT_SEEDING,
    EXIT_SELECTION,
)
from evals.recorder import EXIT_PRECONDITION
from evals.world_drift import (
    EXIT_RECORD_EXPIRED,
    RECORD_EXPIRED_MESSAGE,
    DriftReport,
    _reset_and_audit,
    check_world,
)
from incident_commander.config import Settings
from incident_commander.tools.mcp_client import make_client

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]

CLEAN: Final[str] = "CLEAN"
DRIFT: Final[str] = "DRIFT"
REFUSED: Final[str] = "REFUSED"
NOT_RUN: Final[str] = "NOT RUN"
VERDICTS: Final[tuple[str, ...]] = (CLEAN, DRIFT, REFUSED, NOT_RUN)

#: Why a check stopped before it compared anything, by the exit code it returned.
EXIT_REASONS: Final[dict[int, str]] = {
    EXIT_SELECTION: "selection: no single recording matched",
    EXIT_PREFLIGHT: "preflight: settings, token or stack not usable",
    EXIT_BASELINE_DIRTY: "the baseline re-audit failed after the check",
    EXIT_SEEDING: "seeding: a fault hook was refused",
    EXIT_RESET: "the reset after the check failed",
    EXIT_PRECONDITION: "precondition: the live world never reached the recording's premise",
    EXIT_RECORD_EXPIRED: f"record expired: {RECORD_EXPIRED_MESSAGE}",
}

_PIN_RE: Final[re.Pattern[str]] = re.compile(r"incident-platform:(v[0-9][0-9.]*)@")
_WORLD_RE: Final[re.Pattern[str]] = re.compile(r"evals\.world_drift --world (\S+)")
_RECORDING_RE: Final[re.Pattern[str]] = re.compile(r"^DRIFT: recording\s+(\S+)$", re.M)
_COUNT_RE: Final[re.Pattern[str]] = re.compile(r"^DRIFT: (\d+) disagreement\(s\):", re.M)
_REFUSAL_RE: Final[re.Pattern[str]] = re.compile(
    r"^DRIFT FAIL \(([^)]+)\): (.*)$(?:\n  - (.*)$)?", re.M
)
# An absolute checkout path, so a committed table does not carry one machine's home folder.
# Greedy on purpose: CI checks the repo out under a folder of the same name
# (`…/work/incident-commander/incident-commander/`), and a lazy match stopped at the first one.
_CHECKOUT_RE: Final[re.Pattern[str]] = re.compile(r"/\S*/(?:incident-commander|wp\d+)/")
_MAKE_EXIT_RE: Final[re.Pattern[str]] = re.compile(
    r"^make: \*\*\* \[world-drift\] Error (\d+)$", re.M
)
#: A check deliberately not run, written by the person building the table from logs:
#: ``DRIFT NOT RUN (<scenario>): <why>``, optionally with the ``DRIFT: recording`` line of the
#: recording it would have checked. Read as a NOT RUN row, so a table can carry every world
#: while saying which ones were not checked and why (WO-R3-366: the INC-007 window).
_NOT_RUN_RE: Final[re.Pattern[str]] = re.compile(r"^DRIFT NOT RUN \((\S+)\): (.*)$", re.M)


@dataclass(frozen=True)
class DriftRow:
    """One recording's check: what was compared, the verdict, and the check's own output."""

    scenario: str
    recording: str | None
    verdict: str
    exit_code: int
    disagreements: int | None
    detail: str
    checked_at: str
    output: str


def newest_recordings(root: Path = REPO_ROOT) -> list[tuple[str, str]]:
    """``(scenario, invocation id)`` of each scenario's newest recording, by scenario name."""
    found: list[tuple[str, str]] = []
    folder = root / "evals" / "recorded_worlds"
    for scenario in sorted(path.name for path in folder.iterdir() if path.is_dir()):
        path = artifacts.newest_or_none("recorded_world", scenario, root=root)
        if path is not None:
            found.append((scenario, path.name.removesuffix(".json").rsplit(".", 1)[-1]))
    return found


def row_of(
    scenario: str, recording: str | None, code: int, report: DriftReport | None, output: str
) -> DriftRow:
    """Classify one finished check. A report means the walk ran; no report means it was refused."""
    now = datetime.now(UTC).isoformat()
    if report is None:
        reason = EXIT_REASONS.get(code, f"exit {code}")
        return DriftRow(scenario, recording, REFUSED, code, None, reason, now, output)
    verdict = CLEAN if report.clean else DRIFT
    detail = "" if code in (EXIT_OK, 1) else EXIT_REASONS.get(code, f"exit {code}")
    return DriftRow(scenario, recording, verdict, code, len(report.drifts), detail, now, output)


class _Tee(io.TextIOBase):
    """Write to the terminal and keep a copy, so the table carries what the operator saw."""

    def __init__(self, *targets: TextIO) -> None:
        self._targets = targets

    def write(self, text: str) -> int:
        for target in self._targets:
            target.write(text)
        return len(text)


def _strip_checkout(text: str) -> str:
    """Drop the checkout's absolute prefix so a row reads the same from any clone or worktree.

    This checkout's own root goes first, exactly; the pattern then catches logs written
    from another checkout (a builder's worktree, a different machine).
    """
    return _CHECKOUT_RE.sub("", text.replace(f"{REPO_ROOT}/", ""))


def run_all(
    worlds: Sequence[tuple[str, str]],
    *,
    check: Callable[[str], tuple[int, DriftReport | None]],
    reset: Callable[[], tuple[int, bool]],
) -> list[DriftRow]:
    """Reset, then check, once per recording. A reset that fails stops the loop there."""
    rows: list[DriftRow] = []
    for index, (scenario, recording) in enumerate(worlds):
        reset_code, clean = reset()
        if reset_code != 0 or not clean:
            why = f"exit {reset_code}" if reset_code != 0 else "baseline re-audit FAIL"
            now = datetime.now(UTC).isoformat()
            for left_scenario, left_recording in worlds[index:]:
                rows.append(
                    DriftRow(
                        left_scenario,
                        left_recording,
                        NOT_RUN,
                        EXIT_RESET,
                        None,
                        f"the reset before `{scenario}` failed ({why}); nothing after it ran",
                        now,
                        "",
                    )
                )
            break
        captured = io.StringIO()
        with contextlib.redirect_stdout(_Tee(sys.stdout, captured)):
            code, report = check(recording)
        rows.append(row_of(scenario, recording, code, report, _strip_checkout(captured.getvalue())))
    return rows


def row_from_log(text: str, *, checked_at: str) -> DriftRow:
    """One ``make world-drift`` log, read back into a row. Refuses a log that holds no verdict."""
    recording_line = _RECORDING_RE.search(text)
    recording = (
        None
        if recording_line is None
        else Path(recording_line.group(1)).name.removesuffix(".json").rsplit(".", 1)[-1]
    )
    not_run = _NOT_RUN_RE.search(text)
    if not_run is not None:
        # Nothing ran, so no process exited: 0, and the verdict says the rest.
        scenario, why = not_run.group(1), not_run.group(2)
        output = _strip_checkout(text)
        return DriftRow(scenario, recording, NOT_RUN, EXIT_OK, None, why, checked_at, output)
    world = _WORLD_RE.search(text)
    if world is None:
        raise ValueError("not a `make world-drift` log: no `evals.world_drift --world` line")
    make_exit = _MAKE_EXIT_RE.search(text)
    count = _COUNT_RE.search(text)
    refusal = _REFUSAL_RE.search(text)
    if count is not None:
        verdict, disagreements, detail = DRIFT, int(count.group(1)), ""
    elif "\nDRIFT: none" in text:
        verdict, disagreements, detail = CLEAN, 0, ""
    elif refusal is not None:
        verdict, disagreements = REFUSED, None
        detail = f"{refusal.group(1)}: {refusal.group(2)}"
        if refusal.group(3):
            detail += f" {refusal.group(3)}"
    else:
        raise ValueError(f"log for `{world.group(1)}` holds no verdict line")
    code = int(make_exit.group(1)) if make_exit is not None else (1 if verdict == DRIFT else 0)
    output = _strip_checkout(text)
    return DriftRow(
        world.group(1), recording, verdict, code, disagreements, detail, checked_at, output
    )


def rows_from_logs(directory: Path) -> list[DriftRow]:
    """Every ``*.log`` in ``directory``; each row is stamped with its log's modification time."""
    rows = []
    for path in sorted(directory.glob("*.log")):
        when = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
        rows.append(row_from_log(path.read_text(), checked_at=when))
    if not rows:
        raise ValueError(f"no *.log files under {directory}")
    return sorted(rows, key=lambda row: row.scenario)


def platform_pin(root: Path = REPO_ROOT) -> str:
    """The platform release ``demo/compose.yml`` pins, which is the world a check compares to."""
    match = _PIN_RE.search((root / "demo" / "compose.yml").read_text())
    return match.group(1) if match else "unknown"


def document(rows: Sequence[DriftRow], *, source: str, pin: str) -> dict[str, Any]:
    counts = Counter(row.verdict for row in rows)
    return {
        "kind": "world drift table",
        "source": source,
        "platform_pin": pin,
        "checked_at": max(row.checked_at for row in rows),
        "counts": {verdict: counts.get(verdict, 0) for verdict in VERDICTS},
        "rows": [asdict(row) for row in rows],
    }


def render_table(doc: dict[str, Any]) -> str:
    """The table a person reads: one line per recording, verdict first."""
    counts = ", ".join(f"{n} {verdict}" for verdict, n in doc["counts"].items() if n)
    lines = [
        f"# World drift — {len(doc['rows'])} recording(s) against platform {doc['platform_pin']}",
        "",
        f"Checked {doc['checked_at']} ({doc['source']}). {counts}.",
        "",
        "| scenario | recording | verdict | disagreements | detail |",
        "|---|---|---|---|---|",
    ]
    for row in doc["rows"]:
        count = "" if row["disagreements"] is None else str(row["disagreements"])
        recording = f"`{row['recording']}`" if row["recording"] else "—"
        detail = row["detail"].replace("|", "\\|")
        lines.append(
            f"| `{row['scenario']}` | {recording} | {row['verdict']} | {count} | {detail} |"
        )
    lines += [
        "",
        "DRIFT and REFUSED both mean: no recorded result from that world may be reported "
        "(ADR 0047) until it is re-recorded or the reason is established.",
    ]
    return "\n".join(lines) + "\n"


def write(doc: dict[str, Any], *, root: Path = REPO_ROOT) -> tuple[Path, Path]:
    """Both halves, stamped by the newest check and named by a hash of the rows."""
    content = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    invocation = hashlib.sha256(json.dumps(doc["rows"]).encode()).hexdigest()[:12]
    when = datetime.fromisoformat(doc["checked_at"])
    return (
        artifacts.write_versioned(
            "world_drift_table",
            content=content,
            timestamp=when,
            invocation_id=invocation,
            root=root,
        ),
        artifacts.write_versioned(
            "world_drift_table_md",
            content=render_table(doc),
            timestamp=when,
            invocation_id=invocation,
            root=root,
        ),
    )


def _live_rows() -> tuple[list[DriftRow] | None, str]:
    """The live loop, under the read principal ``world_drift`` itself uses."""
    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError:
        return None, "DRIFT-ALL FAIL (env): invalid or missing settings"
    token = settings.platform_smoke_token
    if token is None or not token.get_secret_value().strip():
        return None, "DRIFT-ALL FAIL (env): PLATFORM_SMOKE_TOKEN is not set in .env"
    client = make_client(settings, token=token.get_secret_value())
    try:
        worlds = newest_recordings()
        print(f"DRIFT-ALL: {len(worlds)} recording(s), a reset before each")
        if not worlds:
            return None, "DRIFT-ALL FAIL (selection): no recordings under evals/recorded_worlds/"
        rows = run_all(
            worlds,
            check=check_world,
            reset=lambda: _reset_and_audit(client),
        )
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    return rows, ""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.world_drift_table", description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--all", action="store_true", help="check every recording live")
    source.add_argument("--from-logs", type=Path, help="read earlier `make world-drift` logs")
    parser.add_argument("--write", action="store_true", help="persist the table as evidence")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.from_logs is not None:
        try:
            rows = rows_from_logs(args.from_logs)
        except (OSError, ValueError) as error:
            print(f"DRIFT-ALL FAIL (logs): {error}")
            return EXIT_SELECTION
        label = f"read back from {len(rows)} `make world-drift` log(s)"
    else:
        live, refusal = _live_rows()
        if live is None:
            print(refusal)
            return EXIT_PREFLIGHT
        rows, label = live, "make world-drift-all, a reset before each check"

    doc = document(rows, source=label, pin=platform_pin())
    print(render_table(doc), end="")
    if args.write:
        for path in write(doc):
            print(f"wrote {path.relative_to(REPO_ROOT)}")
    if any(row.verdict == NOT_RUN and row.exit_code == EXIT_RESET for row in rows):
        return EXIT_RESET
    return EXIT_OK if all(row.verdict == CLEAN for row in rows) else 1


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    raise SystemExit(main(sys.argv[1:]))

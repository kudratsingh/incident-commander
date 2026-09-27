"""A rehearsal trajectory is a scripted planner's, never a model's (WO-R3-318, ADR 0069).

The export refuses one unless asked for it, and the manifest says how many it refused.
Run over the committed rehearsal traces themselves, not a synthetic stand-in.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest

from evals import export
from evals.scenarios.loader import load_scenarios

_TRACES: Final[Path] = Path(__file__).resolve().parents[2] / "evals" / "traces"
_REHEARSED: Final[tuple[Path, ...]] = (
    _TRACES / "demo_dlq_replay_safe_backlog.jsonl",
    _TRACES / "remediate_consumer_lag_success.jsonl",
)
_WHEN: Final[datetime] = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def _modes(paths: tuple[Path, ...]) -> list[str | None]:
    """The execution mode of every invocation in these files, read from the raw records."""
    modes: dict[str, str | None] = {}
    for path in paths:
        for line in path.read_text().splitlines():
            record = json.loads(line) if line.strip() else {}
            if record.get("kind") == "scenario_start":
                modes[record["invocation_id"]] = record.get("execution_mode")
    return list(modes.values())


def _build(*, include_rehearsal: bool = False) -> export.Export:
    return export.build_export(
        traces=_REHEARSED,
        corpus=load_scenarios(export.SCENARIO_DIRECTORY),
        timestamp=_WHEN,
        invocation_id="aaaabbbbcccc",
        include_rehearsal=include_rehearsal,
    )


def test_the_committed_traces_do_hold_rehearsals() -> None:
    assert _modes(_REHEARSED).count("rehearsal") >= 2


def test_a_rehearsal_trajectory_is_refused_by_default() -> None:
    built = _build()
    rehearsals = _modes(_REHEARSED).count("rehearsal")
    assert all(line.execution_mode != "rehearsal" for line in built.trajectories)
    assert built.manifest.rehearsal_refused == rehearsals
    assert built.manifest.rehearsal_included is False
    assert built.manifest.trajectory_count == len(_modes(_REHEARSED)) - rehearsals
    assert len(built.labels) == len(built.trajectories)


def test_include_rehearsal_opts_in_and_says_so() -> None:
    built = _build(include_rehearsal=True)
    assert built.manifest.rehearsal_refused == 0
    assert built.manifest.rehearsal_included is True
    assert sum(line.execution_mode == "rehearsal" for line in built.trajectories) == _modes(
        _REHEARSED
    ).count("rehearsal")


def test_the_cli_names_the_refusal(capsys: pytest.CaptureFixture[str]) -> None:
    only = [arg for path in _REHEARSED for arg in ("--only", path.stem)]
    assert export._main(["--trace-dir", str(_TRACES), *only]) == 0
    refused = _modes(_REHEARSED).count("rehearsal")
    assert f"{refused} rehearsal trajectories refused" in capsys.readouterr().out

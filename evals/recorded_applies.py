"""Does a recording's answer key describe the world the run replayed? One rule for every report.

Beside each recording sits the evaluator's answer key (``<recording>.truth.json``), and its
``applies`` says whether the scenario's label is about that world: false for a recording of a
world with no fault seeded (``recorder.ground_truth_document``, ADR 0040). A run there is not
graded on diagnosis, so no report may score its candidates against the label either.
``oracle_gap`` (WO-R3-347) and ``research_report`` (WO-R3-365) both ask here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from evals import artifacts
from evals.graders.deterministic import GradeDimension
from evals.graders.root_cause import is_not_graded_detail
from evals.runner import ExecutionMode, ScenarioOutcome

#: The archive's own word: the runner graded ROOT_CAUSE "not graded" off the recording's label.
NOT_GRADED: Final[str] = "not graded: the label does not describe this recording's world (ADR 0040)"


def label_applies(scenario: str, recording: str, *, root: Path | None = None) -> bool | None:
    """Does the answer key beside this recording describe its world (ADR 0040)? ``None``: no key."""
    for path in artifacts.versions("recorded_world_truth", scenario, root=root):
        if path.name.endswith(f".{recording}.truth.json"):
            value = json.loads(path.read_text(encoding="utf-8")).get("applies")
            return value if isinstance(value, bool) else None
    return None


def recording_of(outcome: ScenarioOutcome) -> str | None:
    """The recording id a recorded run replayed, off its ``replay.recording`` path."""
    replay = outcome.replay if isinstance(outcome.replay, Mapping) else {}
    path = replay.get("recording")
    if not isinstance(path, str):
        return None
    parsed = artifacts.parse_version_name(Path(path).name, suffix=".json")
    return None if parsed is None else parsed.invocation_id


def is_recorded(outcome: ScenarioOutcome) -> bool:
    """A replayed run: its provenance says so, or it carries a ``replay`` block."""
    provenance = outcome.provenance
    mode = None if provenance is None else provenance.execution_mode
    return mode is ExecutionMode.RECORDED or outcome.replay is not None


def why_key_does_not_apply(outcome: ScenarioOutcome, *, root: Path | None = None) -> str:
    """Why this run's answer key does not describe its world, or ``""`` when it does.

    Recorded runs only: canned is the label's world by definition, and live runs are left to
    their own grade. The archive's verdict is read first, then the key beside the recording.
    """
    if not is_recorded(outcome):
        return ""
    # 1. What the runner decided at grade time, from the recording's own label.
    root_cause = [d for d in outcome.report.dimensions if d.dimension is GradeDimension.ROOT_CAUSE]
    if root_cause and is_not_graded_detail(root_cause[0].detail):
        return NOT_GRADED
    # 2. What the answer key beside that recording says; no key is not a yes (ADR 0040).
    recording = recording_of(outcome)
    if recording is None:
        return (
            "not graded: a recorded run that names no recording, so its world is unknown (ADR 0040)"
        )
    applies = label_applies(outcome.scenario, recording, root=root)
    if applies is True:
        return ""
    if applies is False:
        return (
            f"not graded: the answer key beside recording {recording} says applies: false — "
            "that world had no fault seeded, so the label does not describe it (ADR 0040)"
        )
    return (
        f"not graded: no answer key beside recording {recording} under "
        f"evals/recorded_worlds/{outcome.scenario}/ says whether the label describes that "
        "world (ADR 0040)"
    )

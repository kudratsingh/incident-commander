"""Load one or many scenarios from YAML files on disk."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from evals.scenarios.schema import Scenario


class ScenarioLoadError(RuntimeError):
    """A scenario file couldn't be parsed or validated."""

    def __init__(self, path: Path, cause: str) -> None:
        super().__init__(f"failed to load scenario at {path}: {cause}")
        self.path = path


def load_scenario(path: Path) -> Scenario:
    """Parse and validate one scenario file. Raises ``ScenarioLoadError`` on any failure."""
    try:
        raw = path.read_text()
    except OSError as err:
        raise ScenarioLoadError(path, f"read failed: {err}") from err
    try:
        payload = yaml.safe_load(raw)
    except yaml.YAMLError as err:
        raise ScenarioLoadError(path, f"YAML parse failed: {err}") from err
    if not isinstance(payload, dict):
        raise ScenarioLoadError(
            path, f"top-level YAML must be a mapping, got {type(payload).__name__}"
        )
    try:
        return Scenario.model_validate(payload)
    except ValidationError as err:
        raise ScenarioLoadError(path, f"schema violation: {err}") from err


def load_scenarios(directory: Path) -> list[Scenario]:
    """Load every ``*.yaml`` / ``*.yml`` scenario under ``directory``, sorted by name.

    Names must be unique, because the whole suite treats a scenario name as
    a primary key and nothing checked that it was one. The run archive
    writes ``trajectories/<name>.json`` and ``briefings/<name>.json`` with
    exclusive-create, so two scenarios sharing a name take the suite down
    with an unhandled ``FileExistsError`` partway through — after the run
    has been paid for, and only for the scenarios that reach the archive
    step. The flat report, the regression baseline and the known-drift
    ledger are all keyed on the name too, so the quieter outcomes are worse
    than the crash: one scenario's result standing in for two.

    Reported at load, naming both files, because a name collision is a
    property of the directory rather than of either file — neither one is
    wrong on its own, and the error has to say what it collided with.
    """
    if not directory.is_dir():
        raise ScenarioLoadError(directory, "not a directory")
    scenarios: list[Scenario] = []
    first_seen: dict[str, Path] = {}
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() in {".yaml", ".yml"} and path.is_file():
            scenario = load_scenario(path)
            claimed = first_seen.get(scenario.name)
            if claimed is not None:
                raise ScenarioLoadError(
                    path,
                    f"duplicate scenario name {scenario.name!r} — already defined by "
                    f"{claimed.name}. Scenario names key the run archive, the flat "
                    "report, the regression baseline and the known-drift ledger, so two "
                    "files may not share one. Rename this scenario, or delete it if it "
                    "is a copy.",
                )
            first_seen[scenario.name] = path
            scenarios.append(scenario)
    return scenarios

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
    """Load every ``*.yaml`` / ``*.yml`` scenario under ``directory``, sorted by name."""
    if not directory.is_dir():
        raise ScenarioLoadError(directory, "not a directory")
    scenarios: list[Scenario] = []
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() in {".yaml", ".yml"} and path.is_file():
            scenarios.append(load_scenario(path))
    return scenarios

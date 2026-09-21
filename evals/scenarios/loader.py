"""Load one or many scenarios from YAML files on disk."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from evals.scenarios.schema import BenchmarkSplit, Scenario


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

    Two refusals are properties of the DIRECTORY that no single file can see: a duplicate
    name (the whole suite keys on the name) and one ``template_id`` in two benchmark splits
    (plan 03 § 4 — splits are by TEMPLATE, or a holdout measures memorisation).
    """
    if not directory.is_dir():
        raise ScenarioLoadError(directory, "not a directory")
    scenarios: list[Scenario] = []
    first_seen: dict[str, Path] = {}
    # Name and path travel with the split, so the refusal can name the file to go and read.
    split_claims: dict[str, tuple[BenchmarkSplit, str, Path]] = {}
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() in {".yaml", ".yml"} and path.is_file():
            # 1. Parse and validate the file on its own.
            scenario = load_scenario(path)
            # 2. One name per scenario.
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
            # 3. One benchmark split per template_id.
            claimed_split = split_claims.get(scenario.template_id)
            if claimed_split is not None and claimed_split[0] is not scenario.benchmark_split:
                other_split, other_name, other_path = claimed_split
                raise ScenarioLoadError(
                    path,
                    f"template_id {scenario.template_id!r} appears in more than one "
                    f"benchmark split: {scenario.name!r} ({path.name}) declares "
                    f"{scenario.benchmark_split.value!r}, and {other_name!r} "
                    f"({other_path.name}) already declared {other_split.value!r}. "
                    "Splits are by template, not by instance — every instance of a "
                    "held-out template is held out (plan 03 § 4), so a template belongs "
                    "to exactly one split. Move both scenarios into the same split, or "
                    "give this one its own template_id if it is a different template.",
                )
            split_claims.setdefault(
                scenario.template_id, (scenario.benchmark_split, scenario.name, path)
            )
            scenarios.append(scenario)
    return scenarios

"""Refresh the methodology's current claims from validated scenario models.

Only the current-claim column is generated. Historical failures are editorial
and stay verbatim. Include every action scenario plus the documented no-action
counterexample; missing future action rows are added without inventing history.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel

from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario

ROOT = Path(__file__).resolve().parents[1]
HEADING = "### The remediation claim, per scenario"
HEADER = "| scenario | laziest trajectory that passed before | claim now |"
NO_ACTION_EXAMPLES = frozenset({"consumer_lag_high"})


def _code(value: object) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    # Table delimiters and backticks inside a code span still need escaping.
    return "`" + text.replace("|", "&#124;").replace("`", "&#96;").replace("\n", " ") + "`"


def _claim(model: BaseModel) -> str:
    data = model.model_dump(mode="json", exclude_defaults=True)
    alternatives = data.get("any_of")
    if alternatives is not None:
        return "any_of " + _code(alternatives)
    return ", ".join(f"{key} {_code(value)}" for key, value in data.items())


def current_claim(scenario: Scenario) -> str:
    """Render every declared grading field and precondition, without prose guesses."""
    exp = scenario.expectation
    pieces = [f"terminal {_code(exp.expected_terminal_state.value)}"]
    # Dumping the model rather than a field allowlist also exposes future
    # expectation fields; false/zero comparators survive exclude_defaults.
    for name, value in exp.model_dump(mode="json", exclude_defaults=True).items():
        if name in {"name", "expected_terminal_state"}:
            continue
        if name in {"expected_evidence_fields", "expected_action_arguments"}:
            claims = getattr(exp, name)
            pieces.append(name + ": " + "; ".join(_claim(claim) for claim in claims))
        else:
            pieces.append(f"{name}: {_code(value)}")
    if scenario.expected_precondition:
        pieces.append(
            "precondition: " + "; ".join(_claim(probe) for probe in scenario.expected_precondition)
        )
    return "<br>".join(pieces)


def table_span(document: str) -> tuple[int, int]:
    start = document.index(HEADER, document.index(HEADING))
    end = document.index("\n\n", start)
    return start, end


def render_table(document: str, scenarios: Sequence[Scenario]) -> str:
    """Preserve the before column, while deriving row membership and claims."""
    start, end = table_span(document)
    history: dict[str, str] = {}
    for line in document[start:end].splitlines()[2:]:
        match = re.fullmatch(r"\| `([^`]+)` \| (.*?) \| (.*?) \|", line)
        if match is None:
            raise ValueError(f"malformed remediation table row: {line}")
        name, before, _ = match.groups()
        if name in history:
            raise ValueError(f"duplicate remediation table row: {name}")
        history[name] = before
    selected = {
        s.name: s
        for s in scenarios
        if s.expectation.expected_action_tools or s.name in NO_ACTION_EXAMPLES
    }
    obsolete = history.keys() - selected.keys()
    if obsolete:
        raise ValueError(
            f"table history needs review for removed action scenarios: {sorted(obsolete)}"
        )
    rows = [HEADER, "|---|---|---|"]
    for name, scenario in sorted(selected.items()):
        before = history.get(name, "No earlier passing failure recorded in this table.")
        rows.append(f"| `{name}` | {before} | {current_claim(scenario)} |")
    return "\n".join(rows)


def updated_document(document: str, scenarios: Sequence[Scenario]) -> str:
    start, end = table_span(document)
    return document[:start] + render_table(document, scenarios) + document[end:]


def main() -> None:
    path = ROOT / "docs" / "eval-methodology.md"
    path.write_text(updated_document(path.read_text(), load_scenarios(ROOT / "evals/scenarios")))


if __name__ == "__main__":
    main()

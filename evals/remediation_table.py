"""Refresh the methodology's current claims from validated scenario models.

Only the current-claim column is generated. Historical failures and the short editorial gloss
stay verbatim. Row membership is derived too, so a new scenario cannot be
left out by forgetting to list it; missing rows are added without inventing
history.
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
HEADER = "| scenario | laziest trajectory that passed before | claim now | editorial summary |"


def _code(value: object) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    # Table delimiters and backticks inside a code span still need escaping.
    return "`" + text.replace("|", r"\|").replace("`", "&#96;").replace("\n", " ") + "`"


def _claim(model: BaseModel) -> str:
    """One claim rendered for the table: its any_of alternatives, or its field/value pairs."""
    data = model.model_dump(mode="json", exclude_defaults=True)
    alternatives = data.get("any_of")
    if alternatives is not None:
        return "any_of " + _code(alternatives)
    return ", ".join(f"{key} {_code(value)}" for key, value in data.items())


def makes_a_remediation_claim(scenario: Scenario) -> bool:
    """A scenario belongs in the table when it says what to do, or what not to do.

    Derived, never listed. A hand-kept allowlist is the same drift channel the
    table itself had, and it had already missed `consumer_lag_healthy_zero` and
    `no_fault_healthy_cache`. A scenario that forbids the action tools is claiming
    the correct action count is zero (ADR 0033), which is a remediation claim and
    the one the suite has been wrong about twice (WO-R2-140, WO-R2-160). A
    scenario that neither acts nor forbids makes no claim here and stays out.
    """
    expectation = scenario.expectation
    return bool(expectation.expected_action_tools or expectation.forbidden_action_tools)


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
    """Where the generated table starts and ends inside the methodology document."""
    start = document.index("| scenario |", document.index(HEADING))
    end = start
    for line in document[start:].splitlines(keepends=True):
        if not line.startswith("|"):
            break
        end += len(line)
    end = len(document[:end].rstrip("\r\n"))
    return start, end


def render_table(document: str, scenarios: Sequence[Scenario]) -> str:
    """Preserve both editorial columns, while deriving membership and claims."""
    start, end = table_span(document)
    history: dict[str, tuple[str, str]] = {}
    for line in document[start:end].splitlines()[2:]:
        cells = re.split(r"(?<!\\)\|", line)[1:-1]
        if len(cells) not in {3, 4}:
            raise ValueError(f"malformed remediation table row: {line}")
        name_cell, before, *_ = (cell.strip() for cell in cells)
        if not re.fullmatch(r"`[^`]+`", name_cell):
            raise ValueError(f"malformed remediation scenario: {name_cell}")
        name = name_cell[1:-1]
        gloss = cells[3].strip() if len(cells) == 4 else "Editorial summary pending review."
        if name in history:
            raise ValueError(f"duplicate remediation table row: {name}")
        history[name] = (before, gloss)
    selected = {s.name: s for s in scenarios if makes_a_remediation_claim(s)}
    obsolete = history.keys() - selected.keys()
    if obsolete:
        raise ValueError(f"table history needs review for removed scenarios: {sorted(obsolete)}")
    rows = [HEADER, "|---|---|---|---|"]
    for name, scenario in sorted(selected.items()):
        before, gloss = history.get(
            name,
            (
                "No earlier passing failure recorded in this table.",
                "Editorial summary pending review.",
            ),
        )
        rows.append(f"| `{name}` | {before} | {current_claim(scenario)} | {gloss} |")
    return "\n".join(rows)


def updated_document(document: str, scenarios: Sequence[Scenario]) -> str:
    """The document with its claim table swapped for a freshly derived one."""
    start, end = table_span(document)
    return document[:start] + render_table(document, scenarios) + document[end:]


def main() -> None:
    """Rewrite the methodology doc with the regenerated claim table."""
    path = ROOT / "docs" / "eval-methodology.md"
    path.write_text(updated_document(path.read_text(), load_scenarios(ROOT / "evals/scenarios")))


if __name__ == "__main__":
    main()

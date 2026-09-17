"""The current remediation claims must match the scenario source, not old prose."""

from pathlib import Path

import pytest

from evals.remediation_table import current_claim, table_span, updated_document
from evals.scenarios.loader import load_scenarios

ROOT = Path(__file__).resolve().parents[2]


def test_methodology_current_claims_match_scenarios() -> None:
    document = (ROOT / "docs/eval-methodology.md").read_text()
    scenarios = load_scenarios(ROOT / "evals/scenarios")
    assert document == updated_document(document, scenarios), (
        "Remediation table drift: run make remediation-table and review the claim changes"
    )


def test_stale_row_is_detected_and_history_preserved() -> None:
    scenarios = load_scenarios(ROOT / "evals/scenarios")
    document = updated_document((ROOT / "docs/eval-methodology.md").read_text(), scenarios)
    stale = document.replace("terminal `escalated`", "terminal `resolved`", 1)
    assert stale != document
    assert updated_document(stale, scenarios) == document
    start, end = table_span(document)
    for row in document[start:end].splitlines()[2:]:
        assert row.split(" | ")[1] in updated_document(stale, scenarios)


def test_new_action_scenario_cannot_silently_skip_the_table() -> None:
    scenarios = load_scenarios(ROOT / "evals/scenarios")
    source = next(s for s in scenarios if s.expectation.expected_action_tools)
    added = source.model_copy(update={"name": "new_action_scenario"})
    document = updated_document((ROOT / "docs/eval-methodology.md").read_text(), scenarios)
    changed = updated_document(document, [*scenarios, added])
    assert changed != document
    assert "| `new_action_scenario` |" in changed


def test_changed_nested_claim_and_precondition_require_a_refresh() -> None:
    scenarios = load_scenarios(ROOT / "evals/scenarios")
    source = next(s for s in scenarios if s.name == "remediate_stale_cache_success")
    before = current_claim(source)
    probe = source.expected_precondition[0]
    changed = source.model_copy(
        update={
            "expected_precondition": (probe.model_copy(update={"arguments": {"key": "different"}}),)
        }
    )
    assert current_claim(changed) != before
    claim = source.expectation.expected_action_arguments[0]
    changed = source.model_copy(
        update={
            "expectation": source.expectation.model_copy(
                update={
                    "expected_action_arguments": (claim.model_copy(update={"equals": "different"}),)
                }
            )
        }
    )
    assert current_claim(changed) != before


def test_history_is_not_silently_deleted_if_a_scenario_disappears() -> None:
    scenarios = load_scenarios(ROOT / "evals/scenarios")
    document = updated_document((ROOT / "docs/eval-methodology.md").read_text(), scenarios)
    with pytest.raises(ValueError, match="history needs review"):
        updated_document(document, [])

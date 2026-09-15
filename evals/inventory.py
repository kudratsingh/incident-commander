"""Generate the benchmark inventory from the validated scenario corpus.

Family is provisional: take the first substring match across tags, name and
alert source, in this order: dlq -> dlq; consumer_lag/consumer-lag -> consumer_lag;
saga/dag -> workflow; cache/redis -> cache_redis; postgres -> postgres;
deploy -> deploy; trace -> traces; noise/alert_storm -> noise_control;
tool_ -> tool_fault; otherwise uncategorized. Matching is case-insensitive.
Difficulty is provisionally 0 for names starting with noise_ and for
planner_stops_immediately (controls), and 1 for everything else (single obvious
fault). Both values carry provisional=true; WP-1.4 replaces these heuristics
with explicit scenario metadata. These are legacy groups, not future families
B (jobs not progressing), C (workflow stuck), or A (API latency).

Run ``make inventory`` to regenerate the source-derived manifest. This does
not run scenarios, call the platform/LLM, or read or write run evidence.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, TypedDict

from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario

SCENARIO_DIRECTORY = Path(__file__).parent / "scenarios"
INVENTORY_PATH = Path(__file__).parent / "benchmark_inventory.json"

_FAMILY_RULES = (
    (("dlq",), "dlq"),
    (("consumer_lag", "consumer-lag"), "consumer_lag"),
    (("saga", "dag"), "workflow"),
    (("cache", "redis"), "cache_redis"),
    (("postgres",), "postgres"),
    (("deploy",), "deploy"),
    (("trace",), "traces"),
    (("noise", "alert_storm"), "noise_control"),
    (("tool_",), "tool_fault"),
)


class ProvisionalFamily(TypedDict):
    value: str
    provisional: Literal[True]


class ProvisionalDifficulty(TypedDict):
    value: int
    provisional: Literal[True]


class InventoryRow(TypedDict):
    name: str
    family: ProvisionalFamily
    difficulty: ProvisionalDifficulty
    use_live_mcp: bool
    use_live_llm: bool
    chaos_hook: str | None
    expected_terminal_state: str
    expected_action_tools: list[str]
    forbidden_action_tools: list[str]
    forbidden_replay_job_ids: list[str]
    forbidden_replay_categories: list[str]
    tags: list[str]


def _family(scenario: Scenario) -> str:
    text = " ".join((*scenario.tags, scenario.name, scenario.alert.source)).lower()
    for needles, family in _FAMILY_RULES:
        if any(needle in text for needle in needles):
            return family
    return "uncategorized"


def generate_inventory(directory: Path = SCENARIO_DIRECTORY) -> list[InventoryRow]:
    """Load the runner's corpus and project metadata in stable name/key order.

    The shared loader also accepts .yml files and rejects duplicate names.
    List fields retain their source order; no runtime configuration is loaded.
    """
    rows: list[InventoryRow] = []
    for scenario in sorted(load_scenarios(directory), key=lambda item: item.name):
        expectation = scenario.expectation
        control = scenario.name.startswith("noise_") or scenario.name == "planner_stops_immediately"
        rows.append(
            {
                "name": scenario.name,
                "family": {"value": _family(scenario), "provisional": True},
                "difficulty": {"value": 0 if control else 1, "provisional": True},
                "use_live_mcp": scenario.use_live_mcp,
                "use_live_llm": scenario.use_live_llm,
                "chaos_hook": scenario.chaos_setup.name if scenario.chaos_setup else None,
                "expected_terminal_state": expectation.expected_terminal_state.value,
                "expected_action_tools": list(expectation.expected_action_tools),
                "forbidden_action_tools": list(expectation.forbidden_action_tools),
                "forbidden_replay_job_ids": list(expectation.forbidden_replay_job_ids),
                "forbidden_replay_categories": list(expectation.forbidden_replay_categories),
                "tags": list(scenario.tags),
            }
        )
    return rows


def render_inventory(rows: list[InventoryRow]) -> str:
    """Render deterministic JSON, including a trailing newline."""
    return json.dumps(rows, indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    rows = generate_inventory()
    INVENTORY_PATH.write_text(render_inventory(rows), encoding="utf-8")
    print(f"Wrote {len(rows)} scenarios to {INVENTORY_PATH}")


if __name__ == "__main__":
    main()

"""Generate the benchmark inventory from the validated scenario corpus.

Family and difficulty are read off the scenario (WP-1.4) and carry
``provisional: false``. A scenario that declares neither still gets a row,
from the provisional rule WO-R3-179 wrote down, flagged ``provisional: true``
— so a half-classified corpus is visible in the manifest instead of absent
from it, and a reader can tell a promoted value from a guessed one without
opening 41 YAMLs. Every scenario shipped today declares both, so every row
today reads ``provisional: false``; the fallback exists for the window
between a scenario landing and being classified, which
``tests/unit/test_scenario_metadata.py`` closes at the corpus level.

The provisional rule, kept verbatim because it is what the authoritative
values were promoted FROM: family takes the first substring match across
tags, name and alert source, in this order: dlq -> dlq;
consumer_lag/consumer-lag -> consumer_lag; saga/dag -> workflow;
cache/redis -> cache_redis; postgres -> postgres; deploy -> deploy;
trace -> traces; noise/alert_storm -> noise_control; tool_ -> tool_fault;
otherwise uncategorized. Matching is case-insensitive. Difficulty is
``control`` for names starting with noise_ and for planner_stops_immediately,
and ``single`` for everything else. (WO-R3-179 spelled those last two 0 and
1; WP-1.4's closed vocabulary from plan 03 § 3 is the same two rungs under
their real names, so the column is a string from here on.) These are legacy
groups, not future families B (jobs not progressing), C (workflow stuck), or
A (API latency).

Run ``make inventory`` to regenerate the source-derived manifest. This does
not run scenarios, call the platform/LLM, or read or write run evidence.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict

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


class ClassifiedValue(TypedDict):
    """One classification, and whether a human actually made it.

    ``provisional`` is the honest half. A report that groups on ``family``
    cannot tell a value a scenario author chose from one a substring rule
    guessed, and the difference decides whether a surprising per-family
    number is a finding or a typo in a tag.
    """

    value: str
    provisional: bool


class InventoryRow(TypedDict):
    name: str
    template_id: str
    seed: int
    family: ClassifiedValue
    difficulty: ClassifiedValue
    benchmark_split: str
    use_live_mcp: bool
    use_live_llm: bool
    chaos_hooks: list[str]
    expected_terminal_state: str
    expected_action_tools: list[str]
    forbidden_action_tools: list[str]
    forbidden_replay_job_ids: list[str]
    forbidden_replay_categories: list[str]
    tags: list[str]


def provisional_family(scenario: Scenario) -> str:
    """WO-R3-179's substring rule. The fallback, and the promotion's source.

    Kept as a named function rather than inlined because WP-1.4's
    reconciliation test reads it: every authoritative value in the corpus
    must either equal what this rule produced or be a recorded, reasoned
    exception, so "we promoted the provisional values" stays a checkable
    claim rather than a sentence in a PR body.
    """
    text = " ".join((*scenario.tags, scenario.name, scenario.alert.source)).lower()
    for needles, family in _FAMILY_RULES:
        if any(needle in text for needle in needles):
            return family
    return "uncategorized"


def provisional_difficulty(scenario: Scenario) -> str:
    """WO-R3-179's control rule, in plan 03 § 3's vocabulary.

    The original wrote 0 and 1; those are ``control`` and ``single`` under
    their real names, which is the whole of the promotion for 35 of the 41
    scenarios.
    """
    control = scenario.name.startswith("noise_") or scenario.name == "planner_stops_immediately"
    return "control" if control else "single"


def _classify(declared: str | None, provisional: str) -> ClassifiedValue:
    """The declared value if there is one, else the rule's guess, flagged."""
    if declared is not None:
        return {"value": declared, "provisional": False}
    return {"value": provisional, "provisional": True}


def generate_inventory(directory: Path = SCENARIO_DIRECTORY) -> list[InventoryRow]:
    """Load the runner's corpus and project metadata in stable name/key order.

    The shared loader also accepts .yml files and rejects duplicate names.
    List fields retain their source order; no runtime configuration is loaded.
    """
    rows: list[InventoryRow] = []
    for scenario in sorted(load_scenarios(directory), key=lambda item: item.name):
        expectation = scenario.expectation
        rows.append(
            {
                "name": scenario.name,
                "template_id": scenario.template_id,
                "seed": scenario.seed,
                "family": _classify(
                    scenario.family.value if scenario.family else None,
                    provisional_family(scenario),
                ),
                "difficulty": _classify(
                    scenario.difficulty.value if scenario.difficulty else None,
                    provisional_difficulty(scenario),
                ),
                "benchmark_split": scenario.benchmark_split.value,
                "use_live_mcp": scenario.use_live_mcp,
                "use_live_llm": scenario.use_live_llm,
                # Every setup hook of the scenario's plan, in declared order,
                # read through ``Scenario.chaos`` — never ``chaos_setup``,
                # which is ``None`` on a plan-declaring scenario and would
                # describe a two-fault world as seeding nothing (ADR 0037).
                # A list rather than a name because a manifest whose job is
                # to describe the corpus cannot describe an ordered plan with
                # one string; a legacy one-hook scenario is a one-element list.
                "chaos_hooks": [hook.name for hook in scenario.chaos.setup],
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

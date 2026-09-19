"""The committed inventory must change whenever its source metadata changes.

Since WP-1.4 the family and difficulty columns read the scenario's own values and fall
back to WO-R3-179's rule only where it declares neither.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from evals.inventory import (
    INVENTORY_PATH,
    SCENARIO_DIRECTORY,
    generate_inventory,
    render_inventory,
)
from evals.scenarios.loader import ScenarioLoadError, load_scenarios


def _assert_current(directory: Path, committed: str) -> None:
    fresh = generate_inventory(directory)
    recorded = json.loads(committed)
    actual_names = Counter(row["name"] for row in fresh)
    recorded_names = Counter(row["name"] for row in recorded)
    assert actual_names == recorded_names, (
        f"Inventory coverage drift: missing={list((actual_names - recorded_names).elements())}; "
        f"extra={list((recorded_names - actual_names).elements())}. Run make inventory."
    )
    assert committed == render_inventory(fresh), "Inventory metadata drift. Run make inventory."


def _write_scenario(directory: Path, name: str, *, filename: str = "scenario.yaml") -> Path:
    path = directory / filename
    path.write_text(
        f"name: {name}\n"
        "alert: {source: platform.test, severity: high, fingerprint: inventory-test}\n"
        f"expectation: {{name: {name}, expected_terminal_state: escalated}}\n",
        encoding="utf-8",
    )
    return path


def test_every_scenario_covered_exactly_once() -> None:
    scenarios = load_scenarios(SCENARIO_DIRECTORY)
    assert scenarios, "The benchmark corpus must not be empty"
    rows = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    assert Counter(row["name"] for row in rows) == Counter(s.name for s in scenarios)
    assert all(count == 1 for count in Counter(row["name"] for row in rows).values())


def test_committed_inventory_equals_fresh_generation() -> None:
    _assert_current(SCENARIO_DIRECTORY, INVENTORY_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("mutation", ["add", "remove", "rename", "metadata"])
def test_corpus_drift_fails_with_actionable_message(tmp_path: Path, mutation: str) -> None:
    original = _write_scenario(tmp_path, "original")
    committed = render_inventory(generate_inventory(tmp_path))
    if mutation == "add":
        _write_scenario(tmp_path, "synthetic_extra", filename="extra.yaml")
        message = "synthetic_extra"
    elif mutation == "remove":
        original.unlink()
        message = "original"
    elif mutation == "rename":
        _write_scenario(tmp_path, "synthetic_renamed")
        message = "synthetic_renamed"
    else:
        original.write_text(original.read_text() + "tags: [new-tag]\n", encoding="utf-8")
        message = "metadata drift"
    with pytest.raises(AssertionError, match=message):
        _assert_current(tmp_path, committed)


@pytest.mark.parametrize(
    ("signals", "expected"),
    [
        ("dlq consumer_lag saga cache postgres deploy trace noise tool_", "dlq"),
        ("consumer_lag saga", "consumer_lag"),
        ("consumer-lag", "consumer_lag"),
        ("saga cache", "workflow"),
        ("dag", "workflow"),
        ("cache postgres", "cache_redis"),
        ("redis", "cache_redis"),
        ("postgres deploy", "postgres"),
        ("deploy trace", "deploy"),
        ("trace noise", "traces"),
        ("noise tool_", "noise_control"),
        ("alert_storm", "noise_control"),
        ("tool_timeout", "tool_fault"),
        ("unknown", "uncategorized"),
    ],
)
@pytest.mark.parametrize("source", ["name", "tags", "alert"])
def test_provisional_family_precedence(
    tmp_path: Path, signals: str, expected: str, source: str
) -> None:
    path = _write_scenario(tmp_path, signals if source == "name" else "example")
    if source == "tags":
        path.write_text(path.read_text() + f"tags: [{signals}]\n", encoding="utf-8")
    elif source == "alert":
        path.write_text(path.read_text().replace("platform.test", signals), encoding="utf-8")
    assert generate_inventory(tmp_path)[0]["family"] == {"value": expected, "provisional": True}


@pytest.mark.parametrize(
    ("name", "difficulty"),
    [
        ("noise_flapping", "control"),
        ("planner_stops_immediately", "control"),
        ("alert_storm", "single"),
        ("example", "single"),
    ],
)
def test_provisional_difficulty(tmp_path: Path, name: str, difficulty: str) -> None:
    """The fallback rule, in plan 03 § 3's vocabulary.

    WO-R3-179 spelled these 0 and 1, the same rungs under other names. `alert_storm` is
    one the rule gets wrong: `single`, not the declared `noisy`.
    """
    _write_scenario(tmp_path, name)
    assert generate_inventory(tmp_path)[0]["difficulty"] == {
        "value": difficulty,
        "provisional": True,
    }


def test_a_declared_family_wins_and_stops_being_provisional(tmp_path: Path) -> None:
    """The authoritative half. A scenario's own word beats the substring rule.

    `dlq_example` matches the `dlq` needle; declaring `workflow` overrides it,
    and the flag says a human chose the value rather than a rule guessing.
    """
    path = _write_scenario(tmp_path, "dlq_example")
    path.write_text(path.read_text() + "family: workflow\n", encoding="utf-8")
    assert generate_inventory(tmp_path)[0]["family"] == {
        "value": "workflow",
        "provisional": False,
    }


def test_a_declared_difficulty_wins_and_stops_being_provisional(tmp_path: Path) -> None:
    """The rule would score this `single`; the scenario says `cascading`."""
    path = _write_scenario(tmp_path, "dlq_example")
    path.write_text(path.read_text() + "difficulty: cascading\n", encoding="utf-8")
    assert generate_inventory(tmp_path)[0]["difficulty"] == {
        "value": "cascading",
        "provisional": False,
    }


def test_template_seed_and_split_columns_carry_the_legacy_defaults(tmp_path: Path) -> None:
    """A scenario declaring none of them is its own template, seed 0, dev."""
    _write_scenario(tmp_path, "solo")
    row = generate_inventory(tmp_path)[0]
    assert (row["template_id"], row["seed"], row["benchmark_split"]) == ("solo", 0, "dev")


def test_declared_template_seed_and_split_reach_the_row(tmp_path: Path) -> None:
    path = _write_scenario(tmp_path, "instance_b")
    path.write_text(
        path.read_text() + "template_id: shared\nseed: 3\nbenchmark_split: validation\n",
        encoding="utf-8",
    )
    row = generate_inventory(tmp_path)[0]
    assert (row["template_id"], row["seed"], row["benchmark_split"]) == ("shared", 3, "validation")


def test_a_scenario_with_no_chaos_lists_no_hooks(tmp_path: Path) -> None:
    _write_scenario(tmp_path, "quiet")
    assert generate_inventory(tmp_path)[0]["chaos_hooks"] == []


def test_a_legacy_chaos_setup_is_a_one_hook_plan_in_the_manifest(tmp_path: Path) -> None:
    """The 41 shipped scenarios all spell their fault this way; none moved."""
    path = _write_scenario(tmp_path, "legacy")
    path.write_text(
        path.read_text()
        + "chaos_setup: {name: kill_consumer, arguments: {consumer_group: worker-dispatcher}}\n",
        encoding="utf-8",
    )
    assert generate_inventory(tmp_path)[0]["chaos_hooks"] == ["kill_consumer"]


def test_a_two_hook_plan_is_counted_and_named_in_order(tmp_path: Path) -> None:
    """The under-report this column was migrated to close (WP-1.1 follow-up).

    A plan-declaring scenario leaves `chaos_setup` None, so read directly the manifest said a
    two-fault world seeded nothing (ADR 0037). Order is asserted, not just membership.
    """
    path = _write_scenario(tmp_path, "cascade")
    path.write_text(
        path.read_text() + "chaos_plan:\n"
        "  setup:\n"
        "    - {name: kill_consumer, arguments: {consumer_group: worker-dispatcher}}\n"
        "    - {name: saturate_redis, arguments: {num_keys: 10}}\n"
        "  teardown: [{name: bad_deploy, arguments: {label: restore}}]\n"
        "  settle_seconds: 2.5\n",
        encoding="utf-8",
    )
    row = generate_inventory(tmp_path)[0]
    assert row["chaos_hooks"] == ["kill_consumer", "saturate_redis"]


def test_name_order_and_yml_match_the_loader(tmp_path: Path) -> None:
    _write_scenario(tmp_path, "zulu", filename="a.yaml")
    _write_scenario(tmp_path, "alpha", filename="z.yml")
    assert [row["name"] for row in generate_inventory(tmp_path)] == ["alpha", "zulu"]


def test_duplicate_names_are_rejected_by_the_loader(tmp_path: Path) -> None:
    _write_scenario(tmp_path, "duplicate", filename="a.yaml")
    _write_scenario(tmp_path, "duplicate", filename="b.yaml")
    with pytest.raises(ScenarioLoadError, match="duplicate scenario name 'duplicate'"):
        generate_inventory(tmp_path)

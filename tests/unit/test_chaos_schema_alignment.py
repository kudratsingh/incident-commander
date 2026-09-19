"""Chaos-tool invocations must match the snapshot's chaos inputSchemas (S-18).

Chaos tools are excluded from ``TOOL_REGISTRY`` (the ``[chaos:`` filter), so they have no
local models — yet scenario ``chaos_setup`` blocks and ``scripts/chaos_setup.py`` invoke
them blind. Each invocation is checked against the snapshot: names, required fields and
primitive types. The snapshot is read-only; the fix is the YAML, the CLI table or a pin.
"""

from __future__ import annotations

import ast
import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import json_types_for, value_compatible
from incident_commander.tools.registry import TOOL_REGISTRY

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SNAPSHOT_PATH = _REPO_ROOT / "contracts" / "platform-tools.snapshot.json"
_SCENARIOS_DIR = _REPO_ROOT / "evals" / "scenarios"
_CHAOS_SCRIPT = _REPO_ROOT / "scripts" / "chaos_setup.py"

_CHAOS_PREFIX: Final = "[chaos:"


def _chaos_tool_schemas() -> dict[str, dict[str, Any]]:
    """inputSchema per chaos tool, selected by the structural description prefix."""
    committed = json.loads(_SNAPSHOT_PATH.read_text())
    return {
        t["name"]: t["inputSchema"]
        for t in committed.get("tools", [])
        if t.get("description", "").startswith(_CHAOS_PREFIX)
    }


@dataclass(frozen=True)
class _ChaosCase:
    """One chaos invocation the repo can produce, and where it lives."""

    source: str
    tool: str
    arguments: dict[str, Any]


def _cli_call_sites() -> dict[str, tuple[int, ...]]:
    """Every ``client.call("<tool>", ...)`` in ``scripts/chaos_setup.py``, by tool.

    Walks the script's AST rather than trusting the table below it; the old check compared a
    literal against a tuple in this same file. A computed tool name shows up as absent.
    """
    tree = ast.parse(_CHAOS_SCRIPT.read_text(encoding="utf-8"))
    sites: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "call" and node.args):
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            sites.setdefault(first.value, []).append(node.lineno)
    return {name: tuple(lines) for name, lines in sorted(sites.items())}


def _cli_chaos_tools() -> set[str]:
    """The chaos half of those call sites, split from the registry half structurally.

    ``restart_consumer_group`` is excluded by the property this module is organised around —
    chaos tools are the ones kept OUT of ``TOOL_REGISTRY`` — not by name.
    """
    return set(_cli_call_sites()) - set(TOOL_REGISTRY)


def _scenario_files_declaring_chaos() -> tuple[str, ...]:
    """Scenario files whose YAML carries a ``chaos_setup`` key, read raw.

    Deliberately independent of ``load_scenarios``: this is the reference the loader is
    checked against, so it must not share its failure modes.
    """
    names: list[str] = []
    for path in sorted(_SCENARIOS_DIR.rglob("*.yaml")):
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict) or loaded.get("chaos_setup") is None:
            continue
        # Keyed on the `name` field, not the filename, because that is what
        # the loader reports and what _scenario_cases records as its source.
        name = loaded.get("name")
        names.append(name if isinstance(name, str) and name else path.stem)
    return tuple(names)


def _scenario_cases() -> list[_ChaosCase]:
    """Every shipped scenario's ``chaos_setup`` block, via the real loader."""
    cases: list[_ChaosCase] = []
    for scenario in load_scenarios(_SCENARIOS_DIR):
        if scenario.chaos_setup is not None:
            cases.append(
                _ChaosCase(
                    source=f"scenario:{scenario.name}",
                    tool=scenario.chaos_setup.name,
                    arguments=dict(scenario.chaos_setup.arguments),
                )
            )
    return cases


# Hand-maintained mirror of scripts/chaos_setup.py's six chaos call sites, invoked with
# their argparse defaults. `restore-consumer` is excluded: it calls a registry tool.
_CLI_CASES: Final[tuple[_ChaosCase, ...]] = (
    # scripts/chaos_setup.py:161-164 (kill-consumer; defaults at :83-84)
    _ChaosCase(
        source="cli:kill-consumer",
        tool="kill_consumer",
        arguments={"consumer_group": "worker-dispatcher", "ttl_seconds": 300},
    ),
    # scripts/chaos_setup.py:174-181; payload defaults to {} and partition_key is sent.
    _ChaosCase(
        source="cli:poison-message",
        tool="poison_message",
        arguments={"topic": "job.submitted", "payload": {}, "partition_key": None},
    ),
    # scripts/chaos_setup.py:189-196 (saturate-redis; defaults at :102-104)
    _ChaosCase(
        source="cli:saturate-redis",
        tool="saturate_redis",
        arguments={"num_keys": 1000, "value_bytes": 1024, "ttl_seconds": 60},
    ),
    # scripts/chaos_setup.py:204-211 (inject-latency; defaults at :110-112)
    _ChaosCase(
        source="cli:inject-latency",
        tool="inject_latency",
        arguments={"consumer_group": "worker-dispatcher", "latency_ms": 2000, "ttl_seconds": 300},
    ),
    # scripts/chaos_setup.py:219-225. The one call site that shapes its dict conditionally:
    # `if args.note:` omits the key for both None and `--note ""`.
    _ChaosCase(
        source="cli:bad-deploy",
        tool="bad_deploy",
        arguments={"label": "chaos:bad_deploy", "ttl_seconds": 600},
    ),
    # The --note branch, which is what actually puts `note` on the wire.
    _ChaosCase(
        source="cli:bad-deploy --note",
        tool="bad_deploy",
        arguments={"label": "chaos:bad_deploy", "ttl_seconds": 600, "note": "rollback rehearsal"},
    ),
    # scripts/chaos_setup.py:247-250 (bad-data-job; defaults at :139-143)
    _ChaosCase(
        source="cli:bad-data-job",
        tool="create_bad_data_job",
        arguments={
            "job_type": "csv_upload",
            "error_message": (
                "ValueError: invalid literal for int() with base 10: 'not-a-number' at row 15,382"
            ),
        },
    ),
)

_ALL_CASES: Final[tuple[_ChaosCase, ...]] = tuple(_scenario_cases()) + _CLI_CASES


# The type walk lives in ``evals/scenarios/schema.py``: ``ChaosHook`` enforces the
# same rules at load time (G1-07).
def _json_types_for(prop: dict[str, Any]) -> set[str]:
    return set(json_types_for(prop))


def _value_compatible(value: Any, admitted: set[str]) -> bool:
    return value_compatible(value, frozenset(admitted))


def _validate_case(case: _ChaosCase, schemas: dict[str, dict[str, Any]]) -> None:
    """Assert one chaos invocation aligns with the snapshot's inputSchema."""
    assert case.tool in schemas, (
        f"{case.source} invokes chaos tool {case.tool!r}, which is not in "
        f"{_SNAPSHOT_PATH.name} (or lacks the '[chaos:' description prefix). "
        "Either the invocation drifted from the platform, or the tool only "
        "exists on a newer platform than the pinned one (e.g. "
        "seed_dlq_messages, which is master-only today) — in that case bump "
        "the platform pin and regenerate the snapshot via `make snapshot` "
        "before invoking it. Never hand-edit the snapshot."
    )
    schema = schemas[case.tool]
    properties: dict[str, Any] = schema.get("properties") or {}
    unknown = sorted(set(case.arguments) - set(properties))
    assert not unknown, (
        f"{case.source}: arguments {unknown} are not in the snapshot "
        f"inputSchema.properties of {case.tool!r} (known: {sorted(properties)}) "
        "— the platform renamed or dropped them, or the invocation has a typo."
    )
    missing = sorted(set(schema.get("required") or []) - set(case.arguments))
    assert not missing, (
        f"{case.source}: snapshot marks {missing} required on {case.tool!r} "
        "but the invocation does not provide them — live seeding would fail."
    )
    for name, value in sorted(case.arguments.items()):
        admitted = _json_types_for(properties[name])
        assert _value_compatible(value, admitted), (
            f"{case.source}: {case.tool}.{name}={value!r} "
            f"({type(value).__name__}) is not compatible with the snapshot's "
            f"JSON type(s) {sorted(admitted)} — live seeding would fail with "
            "a ChaosInvocationError."
        )


class TestChaosInvocationsMatchSnapshot:
    @pytest.mark.parametrize("case", _ALL_CASES, ids=[c.source for c in _ALL_CASES])
    def test_invocation_matches_snapshot(self, case: _ChaosCase) -> None:
        _validate_case(case, _chaos_tool_schemas())

    def test_cli_table_covers_every_chaos_call_site_in_the_script(self) -> None:
        """The hand-maintained mirror must equal the set the script produces.

        The arguments are argparse defaults and stay hand-written; WHICH tools get invoked is
        derived, which the old ``== 6`` could not do.
        """
        derived = _cli_chaos_tools()
        mirrored = {c.tool for c in _CLI_CASES}
        assert mirrored == derived, (
            f"the _CLI_CASES table in {Path(__file__).name} mirrors "
            f"{sorted(mirrored)} but scripts/chaos_setup.py invokes "
            f"{sorted(derived)}. Unmirrored (add a _ChaosCase with the "
            f"call site's argparse defaults): {sorted(derived - mirrored)}. "
            f"Stale (the call site is gone — delete the case): "
            f"{sorted(mirrored - derived)}. Call sites by line: "
            f"{_cli_call_sites()}"
        )

    def test_every_scenario_declaring_chaos_reaches_the_walk(self) -> None:
        """The loader must not quietly drop a ``chaos_setup`` block.

        Replaces a ``>= 1`` floor that four scenarios cleared, so three could have vanished.
        The reference side reads the YAML directly.
        """
        declared = set(_scenario_files_declaring_chaos())
        walked = {
            c.source.removeprefix("scenario:")
            for c in _ALL_CASES
            if c.source.startswith("scenario:")
        }
        missing = declared - walked
        assert not missing, (
            f"{sorted(missing)} declare a chaos_setup block in their YAML but "
            f"produced no case in this walk — evals/scenarios/loader.py is "
            f"dropping the field, so the chaos tripwire silently stopped "
            f"covering them. Walked: {sorted(walked)}."
        )
        assert declared, (
            "no scenario YAML declares a chaos_setup block at all — the "
            "scenario half of this tripwire has no subject left, so it is "
            "passing vacuously. Check evals/scenarios/ for a mass rename."
        )


class TestBadDeployNoteShape:
    """``note`` is optional and nullable, and no invocation asserts that now.

    The CLI table used to carry ``note: None``, pinning the property as a side effect of
    mirroring an invocation that does not exist. Stated directly instead.
    """

    def test_note_is_optional_on_the_platform_side(self) -> None:
        schema = _chaos_tool_schemas()["bad_deploy"]
        assert "note" not in (schema.get("required") or []), (
            "the snapshot now marks bad_deploy.note required, but "
            "scripts/chaos_setup.py:223 omits it whenever --note is unset — "
            "the flag-less `make chaos-bad-deploy` would fail at seeding."
        )

    def test_note_still_admits_null(self) -> None:
        schema = _chaos_tool_schemas()["bad_deploy"]
        admitted = _json_types_for(schema["properties"]["note"])
        assert "null" in admitted, (
            f"bad_deploy.note admits {sorted(admitted)} and no longer admits "
            "null. Nothing this repo sends is affected today (the CLI omits "
            "the key rather than sending None), but a scenario YAML written "
            "with `note: null` would now fail at seeding."
        )


class TestS18Probe:
    """The audit's S-18 probe: a platform-side ``kill_consumer.ttl_seconds``
    integer→string flip must fail HERE, not as a live ``ChaosInvocationError``
    at seeding time."""

    def test_kill_consumer_ttl_seconds_is_integer_typed(self) -> None:
        schema = _chaos_tool_schemas()["kill_consumer"]
        assert schema["properties"]["ttl_seconds"]["type"] == "integer"
        carriers = [
            c for c in _ALL_CASES if c.tool == "kill_consumer" and "ttl_seconds" in c.arguments
        ]
        assert carriers, (
            "no kill_consumer invocation carries ttl_seconds — the probe lost its subject"
        )
        for case in carriers:
            value = case.arguments["ttl_seconds"]
            assert isinstance(value, int) and not isinstance(value, bool), case.source

    def test_type_flip_on_mutated_copy_trips_the_walk(self) -> None:
        # In-memory deep copy only — the committed snapshot is never edited.
        schemas = copy.deepcopy(_chaos_tool_schemas())
        schemas["kill_consumer"]["properties"]["ttl_seconds"] = {"type": "string"}
        case = next(
            c for c in _ALL_CASES if c.tool == "kill_consumer" and "ttl_seconds" in c.arguments
        )
        with pytest.raises(AssertionError, match="not compatible"):
            _validate_case(case, schemas)

    def test_unknown_tool_points_at_the_pin_bump_flow(self) -> None:
        # The sentinel is a name that can never be blessed: this probe used seed_dlq_messages
        # until the v0.5.0 rebless made it a snapshot tool and the branch stopped being tested.
        case = _ChaosCase(
            source="probe:unknown-tool", tool="chaos_tool_that_does_not_exist", arguments={}
        )
        with pytest.raises(AssertionError, match="bump the platform pin"):
            _validate_case(case, _chaos_tool_schemas())


class TestTypeCompat:
    """Corners of the primitive-type check the walk leans on."""

    def test_bool_is_not_integer(self) -> None:
        assert not _value_compatible(True, {"integer"})

    def test_int_is_integer_and_number_but_not_string(self) -> None:
        assert _value_compatible(300, {"integer"})
        assert _value_compatible(300, {"number"})
        assert not _value_compatible(300, {"string"})

    def test_none_needs_a_null_branch(self) -> None:
        assert _value_compatible(None, {"string", "null"})
        assert not _value_compatible(None, {"string"})

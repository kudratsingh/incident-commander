"""Chaos-tool invocations must match the snapshot's chaos inputSchemas (S-18).

Chaos tools are deliberately excluded from ``TOOL_REGISTRY`` (the
``[chaos:`` description-prefix filter — see ``test_registry.py``): the
agent never calls them, so they have no local Pydantic models. But two
commander-side surfaces DO invoke them blind, over raw JSON-RPC:

- scenario YAML ``chaos_setup`` blocks (fired by the runner before a
  live run), and
- ``scripts/chaos_setup.py``'s CLI subcommands (the flag-less
  ``make chaos-*`` targets).

Until this walk, neither was validated against anything — a platform-side
chaos-schema change surfaced only as a live ``ChaosInvocationError``
during seeding, after run startup cost was already spent. Every chaos
invocation this repo can produce is checked here against the committed
snapshot's inputSchema for the named tool: argument names, required
fields, and primitive argument types (a name+required-only check would
not catch S-18's own probe, a ``ttl_seconds`` integer→string flip).

The snapshot is read-only here as everywhere: when this test fails, the
fix lands in the scenario YAML / the CLI table (commander drifted) or in
a platform-pin bump + ``make snapshot`` (platform drifted) — never in a
hand edit of the snapshot.
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

    Walks the script's AST rather than trusting the table below it. The
    anti-vacuity check used to assert a hardcoded length against the
    hardcoded tuple in this same file, which could only fail if someone
    edited that tuple — it said nothing whatever about the script it
    claimed to mirror, so a new or deleted chaos call site changed nothing
    and no test went red.

    The tool name is the first positional argument at every call site and
    is a string literal at all of them, which is what makes the walk
    possible; a call site that computed its tool name would show up here
    as absent and fail the set comparison, which is the right direction to
    fail in.
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

    ``restart_consumer_group`` is a real registry tool the restore path
    calls, already covered by the registry and snapshot contract tests.
    Rather than naming it as an exception, it is excluded by the same
    property this whole module is organised around: chaos tools are the
    ones deliberately kept OUT of ``TOOL_REGISTRY`` by the ``[chaos:``
    description filter. A newly added registry call site is excluded for
    free; a newly added chaos call site is not.
    """
    return set(_cli_call_sites()) - set(TOOL_REGISTRY)


def _scenario_files_declaring_chaos() -> tuple[str, ...]:
    """Scenario files whose YAML carries a ``chaos_setup`` key, read raw.

    Deliberately independent of ``load_scenarios``: this is the reference
    the loader is checked against, so it must not share the loader's
    failure modes. The floor it replaces was ``>= 1`` against four real
    scenarios, so three could silently drop out of the walk — including
    via exactly the loader change the comment said it was watching for.
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


# Hand-maintained mirror of scripts/chaos_setup.py's six chaos call sites,
# invoked with their argparse defaults — exactly what the flag-less
# `make chaos-*` targets send. The duplication is deliberate: when a
# chaos_setup.py call site changes, this table is the one-line fix that
# keeps the tripwire honest (each entry cites its source lines).
# `restore-consumer` (scripts/chaos_setup.py:241-245) is excluded on
# purpose: it calls `restart_consumer_group`, a registry tool already
# covered by the registry/snapshot contract tests, not a chaos hook.
_CLI_CASES: Final[tuple[_ChaosCase, ...]] = (
    # scripts/chaos_setup.py:161-164 (kill-consumer; defaults at :83-84)
    _ChaosCase(
        source="cli:kill-consumer",
        tool="kill_consumer",
        arguments={"consumer_group": "worker-dispatcher", "ttl_seconds": 300},
    ),
    # scripts/chaos_setup.py:174-181 (poison-message; defaults at :90-96).
    # payload defaults to json.loads("{}") == {}; the call site always
    # sends partition_key, None by default.
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
    # scripts/chaos_setup.py:219-225 (bad-deploy; defaults at :118-120).
    # Two cases because this is the one call site that shapes its argument
    # dict conditionally: `if args.note:` (a truthiness test, so both the
    # None default and an explicit `--note ""` omit the key entirely).
    # This entry used to send `note: None` "to record the argparse
    # default", which mirrored an invocation the CLI cannot produce — the
    # tripwire was validating a payload no code path emits, which is the
    # one thing a mirror must never do.
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


# The type walk itself now lives in ``evals/scenarios/schema.py``, because
# ``ChaosHook`` enforces the same rules at scenario-load time (G1-07). This
# file keeps the CLI half of the corpus and the mutated-snapshot probes; the
# comparison logic is imported so the two cannot drift apart.
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

        The arguments still have to be written out by hand — they are
        argparse defaults resolved at runtime, not statically readable —
        but *which tools get invoked* is now derived. Adding a chaos call
        site to ``scripts/chaos_setup.py`` without adding its case here
        fails, which is what the old ``== 6`` was meant to do and could
        not: it compared a literal against the tuple sitting six lines
        above it in this same file.
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

        Replaces a ``>= 1`` floor that four real scenarios cleared, so
        three of them could have vanished from the corpus without a red
        test — and the mechanism the comment feared (a loader change that
        drops the field) is precisely the one a floor of one cannot see.
        The reference side reads the YAML directly, so it shares no code
        with the loader it is checking.
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

    The CLI table used to carry ``note: None`` on the flag-less case,
    which pinned the platform property as present-and-nullable as a side
    effect of mirroring an invocation that does not exist. Removing the
    fiction removes the pin, so the pin is stated directly instead — a
    property that becomes required, or loses its null branch, still fails
    here rather than at live seeding time.
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
        # The sentinel is a name that cannot ever be blessed, not a real tool
        # awaiting a pin bump. This probe used seed_dlq_messages until the
        # v0.5.0 rebless made it the 27th snapshot tool — at which point the
        # case stopped exercising the unknown-tool branch and started failing
        # on a missing required argument instead. A synthetic name keeps the
        # branch pinned across every future rebless.
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

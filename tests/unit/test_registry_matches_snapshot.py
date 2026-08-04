"""Local registry output models must match the platform's advertised outputSchema.

The committed snapshot at ``contracts/platform-tools.snapshot.json`` is
platform-authoritative — since v0.4.8 the platform emits ``outputSchema``
on every tool descriptor in ``tools/list``. The agent's local
``TOOL_REGISTRY`` mirrors those shapes as Pydantic models the transitions
consume. Both must agree, or the agent starts summarizing outputs whose
shape doesn't match what actually comes off the wire.

This test walks every tool the registry knows about and asserts its
``output_model.model_json_schema()`` matches the snapshot's
``outputSchema``, ignoring ``description`` fields (they're doc drift, not
shape drift — copying platform prose into the local models is pure
noise). Every other axis — properties, required, types, defaults, nested
``$defs`` — is compared strictly.

Registry drift without a matching snapshot regeneration fails here; the
sibling live contract test (`test_contract_snapshot.py`) catches drift on
the other side.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from incident_commander.tools.registry import TOOL_REGISTRY

_SNAPSHOT_PATH = Path(__file__).resolve().parents[2] / "contracts" / "platform-tools.snapshot.json"


def _snapshot_output_schemas() -> dict[str, dict[str, Any]]:
    committed = json.loads(_SNAPSHOT_PATH.read_text())
    return {t["name"]: t.get("outputSchema") or {} for t in committed.get("tools", [])}


def _strip_descriptions(node: Any) -> Any:
    """Recursively drop ``description`` keys — those are doc drift, not shape drift."""
    if isinstance(node, dict):
        return {k: _strip_descriptions(v) for k, v in node.items() if k != "description"}
    if isinstance(node, list):
        return [_strip_descriptions(item) for item in node]
    return node


class TestRegistryMatchesSnapshot:
    @pytest.mark.parametrize("tool_name", sorted(TOOL_REGISTRY.keys()))
    def test_output_model_matches_snapshot(self, tool_name: str) -> None:
        schemas = _snapshot_output_schemas()
        assert tool_name in schemas, (
            f"tool {tool_name!r} is in TOOL_REGISTRY but missing from the "
            "committed snapshot — either the platform stopped advertising it "
            "or the snapshot needs a regen (`make snapshot`)."
        )
        model_schema = _strip_descriptions(
            TOOL_REGISTRY[tool_name].output_model.model_json_schema()
        )
        snap_schema = _strip_descriptions(schemas[tool_name])
        assert model_schema == snap_schema, (
            f"{tool_name}.output_model.model_json_schema() drifted from the "
            "committed snapshot (shape-relevant delta, descriptions already "
            "ignored). Either update the Pydantic model to match or run "
            "`make snapshot` if the platform is the source of truth for the "
            "new shape."
        )


class TestDescriptionMirror:
    """Descriptions are load-bearing (the planner authors verify
    expectations from them) and are loaded from the snapshot at import —
    these tests guard the loader path, not a second copy."""

    def test_every_registry_tool_has_a_nonempty_description(self) -> None:
        from incident_commander.tools.registry import TOOL_REGISTRY, description_of

        missing = [name for name in TOOL_REGISTRY if not description_of(name)]
        assert not missing, (
            f"tools with no snapshot description: {missing} — snapshot stale "
            "or the registry loader path broke"
        )

    def test_descriptions_are_verbatim_from_snapshot(self) -> None:
        import json
        from pathlib import Path

        from incident_commander.tools.registry import TOOL_REGISTRY, description_of

        snapshot = json.loads(
            (
                Path(__file__).resolve().parents[2] / "contracts" / "platform-tools.snapshot.json"
            ).read_text()
        )
        by_name = {t["name"]: t.get("description", "") for t in snapshot["tools"]}
        for name in TOOL_REGISTRY:
            assert description_of(name) == by_name.get(name, ""), name

    def test_v049_semantics_reach_the_planner_context(self) -> None:
        # The whole point of the mirror: freshness, delayed-replay, and
        # enforced-pause semantics must be present in what the LLM reads.
        from incident_commander.agent.remediation import _tool_context_block

        assert "FRESHNESS" in _tool_context_block("get_consumer_lag")
        assert "paused=true" in _tool_context_block("pause_dag")
        assert "VERIFYING A DELAYED REPLAY" in _tool_context_block("replay_dlq_by_category")

"""Local registry models must match the platform's advertised schemas.

The committed snapshot at ``contracts/platform-tools.snapshot.json`` is
platform-authoritative — since v0.4.8 the platform emits ``outputSchema``
on every tool descriptor in ``tools/list``, alongside ``inputSchema``.
The agent's local ``TOOL_REGISTRY`` mirrors those shapes as Pydantic
models: output models are what the transitions consume; input models are
what ``wire.py`` validates and default-fills every outgoing call body
from — the exact bytes the platform hashes for idempotency (ADR 0010).
Both directions must agree with the snapshot, or the agent either
misreads what comes off the wire or silently changes what it sends.

Output leg: ``output_model.model_json_schema()`` vs ``outputSchema``,
ignoring ``description`` keys only (they're doc drift, not shape drift —
copying platform prose into the local models is pure noise). Every other
axis — properties, required, types, defaults, nested ``$defs``, titles —
is compared strictly.

Input leg: ``input_model.model_json_schema()`` vs ``inputSchema``,
ignoring ``description`` AND ``title`` keys. Titles are stripped on this
side only, because zero-arg tools legitimately differ: the local shared
model is ``_EmptyInput`` while the platform's is ``_EmptyIn``, and
Pydantic bakes the class name into ``title``. Everything else —
properties, required, types, defaults, enums, bounds — is strict, which
is what catches the drift classes the name+required-only alignment
checks in ``test_registry.py`` miss: type flips, default changes, enum
edits, min/max changes.

Registry drift without a matching snapshot regeneration fails here; the
sibling live contract test (`test_contract_snapshot.py`) catches drift on
the platform side, and ``test_chaos_schema_alignment.py`` covers the
chaos tools the registry deliberately excludes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from incident_commander.tools.registry import TOOL_REGISTRY, _load_snapshot_descriptions

_SNAPSHOT_PATH = Path(__file__).resolve().parents[2] / "contracts" / "platform-tools.snapshot.json"


def _snapshot_output_schemas() -> dict[str, dict[str, Any]]:
    committed = json.loads(_SNAPSHOT_PATH.read_text())
    return {t["name"]: t.get("outputSchema") or {} for t in committed.get("tools", [])}


def _snapshot_input_schemas() -> dict[str, dict[str, Any]]:
    committed = json.loads(_SNAPSHOT_PATH.read_text())
    return {t["name"]: t.get("inputSchema") or {} for t in committed.get("tools", [])}


def _strip_keys(node: Any, keys: frozenset[str]) -> Any:
    """Recursively drop the given doc-only keys from a JSON-schema tree."""
    if isinstance(node, dict):
        return {k: _strip_keys(v, keys) for k, v in node.items() if k not in keys}
    if isinstance(node, list):
        return [_strip_keys(item, keys) for item in node]
    return node


def _strip_descriptions(node: Any) -> Any:
    """Drop ``description`` keys only — the output-side comparison keeps titles strict."""
    return _strip_keys(node, frozenset({"description"}))


def _strip_doc_keys(node: Any) -> Any:
    """Drop ``description`` AND ``title`` keys — input side only.

    Titles differ legitimately there (local ``_EmptyInput`` vs platform
    ``_EmptyIn`` on zero-arg tools); see the module docstring. Do NOT use
    this on the output side, where titles align strictly today and
    stripping them would weaken the comparison.
    """
    return _strip_keys(node, frozenset({"description", "title"}))


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


class TestInputModelMatchesSnapshot:
    """Input leg of the contract triangle (C-12/S-17).

    ``wire_arguments`` default-fills every outgoing call body from these
    models, and the platform hashes those bytes for idempotency — a
    silent default drift breaks resume-retry dedup with 409s. Strict
    equality (minus doc keys) is what catches the classes the
    name+required-only checks miss: type flips, default changes, enum
    edits, bound changes.
    """

    @pytest.mark.parametrize("tool_name", sorted(TOOL_REGISTRY.keys()))
    def test_input_model_matches_snapshot(self, tool_name: str) -> None:
        schemas = _snapshot_input_schemas()
        assert tool_name in schemas, (
            f"tool {tool_name!r} is in TOOL_REGISTRY but missing from the "
            "committed snapshot — either the platform stopped advertising it "
            "or the snapshot needs a regen (`make snapshot`)."
        )
        model_schema = _strip_doc_keys(TOOL_REGISTRY[tool_name].input_model.model_json_schema())
        snap_schema = _strip_doc_keys(schemas[tool_name])
        assert model_schema == snap_schema, (
            f"{tool_name}.input_model.model_json_schema() drifted from the "
            "committed snapshot (shape-relevant delta; descriptions and "
            "titles already ignored). These models produce the wire bytes "
            "the platform hashes for idempotency. Either update the "
            "Pydantic model to match or run `make snapshot` if the platform "
            "is the source of truth for the new shape."
        )


class TestSnapshotLoaderFailsHard:
    """C-13: a missing or unreadable snapshot must raise at import, not
    silently degrade to ``{}`` — the old fallback shipped '(no
    description)' to the planner in any non-checkout install."""

    def test_missing_snapshot_raises_runtime_error_naming_the_path(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing.json"
        with pytest.raises(RuntimeError) as excinfo:
            _load_snapshot_descriptions(missing)
        message = str(excinfo.value)
        assert str(missing) in message
        assert "refusing to run with empty descriptions" in message
        assert isinstance(excinfo.value.__cause__, OSError)

    def test_unreadable_snapshot_raises_runtime_error_naming_the_path(self, tmp_path: Path) -> None:
        corrupt = tmp_path / "corrupt.json"
        corrupt.write_text("{not json")
        with pytest.raises(RuntimeError) as excinfo:
            _load_snapshot_descriptions(corrupt)
        assert str(corrupt) in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, ValueError)


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

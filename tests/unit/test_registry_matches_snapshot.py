"""Local registry models must match the platform's advertised schemas.

``contracts/platform-tools.snapshot.json`` is platform-authoritative. Output models are
what the transitions consume; input models are what ``wire.py`` default-fills every
outgoing body from — the bytes the platform hashes (ADR 0010). The output leg ignores
``description``; the input leg also ignores ``title`` (``_EmptyInput`` vs ``_EmptyIn``).
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

    Titles differ legitimately there; stripping them on the output side weakens it.
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

    ``wire_arguments`` default-fills every outgoing body from these models and the platform
    hashes those bytes, so a default drift breaks dedup.
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


class TestEverySnapshotOutputFieldLandsOnAModel:
    """No field the platform sends is dropped on the floor (LESSONS 2026-09-08).

    ``extra="ignore"`` is the right default — a field a later release adds must not
    stop a response parsing mid-run — and its cost is that an undeclared field is
    discarded with no error: a fence claim on v0.6.2 (cmd #206), all twelve of
    ``get_postgres_health``'s new readings on v0.6.11. ``TestRegistryMatchesSnapshot``
    fails on the same drift inside strict schema equality; this names the tool and the
    missing fields, and covers NESTED objects too.
    """

    @staticmethod
    def _local_objects(model: type[Any]) -> dict[str, set[str]]:
        """Property names per object title in one output model's schema tree."""
        schema = model.model_json_schema()
        objects = {schema.get("title", ""): set(schema.get("properties", {}))}
        for title, definition in (schema.get("$defs") or {}).items():
            if "properties" in definition:
                objects[title] = set(definition["properties"])
        return objects

    @pytest.mark.parametrize("tool_name", sorted(TOOL_REGISTRY.keys()))
    def test_no_snapshot_output_field_is_silently_dropped(self, tool_name: str) -> None:
        snapshot = _snapshot_output_schemas()[tool_name]
        local = self._local_objects(TOOL_REGISTRY[tool_name].output_model)
        remote = {snapshot.get("title", ""): set(snapshot.get("properties", {}))}
        for title, definition in (snapshot.get("$defs") or {}).items():
            if "properties" in definition:
                remote[title] = set(definition["properties"])
        for title, fields in remote.items():
            assert title in local, (
                f"{tool_name}: the snapshot describes an object {title!r} that "
                "the local output model has no counterpart for — every nested "
                "platform model needs a mirrored class, or its fields are "
                'dropped by `extra="ignore"`.'
            )
            missing = fields - local[title]
            assert not missing, (
                f"{tool_name}: {sorted(missing)} are in the platform's "
                f"outputSchema for {title!r} and not on the local model. Every "
                'output model is `extra="ignore"`, so these arrive and are '
                "thrown away — no error, no parse failure, and any claim or "
                "hypothesis that needed them is unsatisfiable. Mirror them "
                "(declaration order = the snapshot's `required` order)."
            )

    def test_the_configs_this_test_exists_for_are_what_they_claim(self) -> None:
        # Anti-vacuity. If a model ever became `extra="forbid"` or
        # `extra="allow"`, the paragraph above stops describing it: "forbid"
        # fails loudly instead of silently (and breaks every response the day
        # the platform adds a field), "allow" keeps the value under a name
        # nothing reads. Either is a decision to take deliberately, not to
        # discover here.
        odd = {
            name: spec.output_model.model_config.get("extra")
            for name, spec in TOOL_REGISTRY.items()
            if spec.output_model.model_config.get("extra") != "ignore"
        }
        assert not odd, f"output models not `extra=ignore`: {odd}"


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

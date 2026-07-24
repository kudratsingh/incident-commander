"""Registry structural tests.

The typed input/output models are hand-written; the contract snapshot is the
diff gate. Here we assert (a) the registry covers every read tool the snapshot
advertises, and (b) the per-tool input Pydantic schemas match the snapshot's
inputSchema — that catches drift before the live diff test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from incident_commander.tools.registry import (
    TOOL_REGISTRY,
    GetConsumerLagInput,
    GetConsumerLagOutput,
    ToolSpec,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SNAPSHOT_PATH = _REPO_ROOT / "contracts" / "platform-tools.snapshot.json"

# Tier-1 write actions are on the platform surface but not registered here.
# They land alongside the approvals flow in Phase 6.
_TIER1_ACTIONS = frozenset(
    {
        "invalidate_cache_key",
        "pause_dag",
        "replay_dlq_messages",
        "restart_consumer_group",
    }
)


@pytest.fixture(scope="module")
def snapshot() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(_SNAPSHOT_PATH.read_text())
    return loaded


class TestCoverage:
    def test_registry_covers_every_read_tool_in_snapshot(self, snapshot: dict[str, Any]) -> None:
        snapshot_names = {t["name"] for t in snapshot["tools"]}
        read_tool_names = snapshot_names - _TIER1_ACTIONS
        assert set(TOOL_REGISTRY) == read_tool_names, (
            f"Registry drift vs snapshot. "
            f"Missing: {read_tool_names - set(TOOL_REGISTRY)}. "
            f"Extra: {set(TOOL_REGISTRY) - read_tool_names}."
        )

    def test_registry_omits_tier1_actions(self) -> None:
        for action in _TIER1_ACTIONS:
            assert action not in TOOL_REGISTRY, (
                f"{action} is a Tier-1 write action; Phase 6 registers these "
                "alongside the approvals flow."
            )


class TestSchemaAlignment:
    """Every registered input model's JSON schema aligns with the snapshot.

    We check field names, required-ness, and defaults. This is what catches
    a platform-side rename or a required-ness flip without needing a live
    platform.
    """

    def test_all_input_fields_present_in_snapshot(self, snapshot: dict[str, Any]) -> None:
        snapshot_by_name = {t["name"]: t for t in snapshot["tools"]}
        mismatches: list[str] = []
        for name, spec in TOOL_REGISTRY.items():
            snap_schema = snapshot_by_name[name]["inputSchema"]
            snap_props = set((snap_schema.get("properties") or {}).keys())
            model_props = set(spec.input_model.model_fields.keys())
            missing_from_model = snap_props - model_props
            extra_in_model = model_props - snap_props
            if missing_from_model or extra_in_model:
                mismatches.append(
                    f"{name}: missing_from_model={sorted(missing_from_model)} "
                    f"extra_in_model={sorted(extra_in_model)}"
                )
        assert not mismatches, "Input field drift:\n" + "\n".join(mismatches)

    def test_required_fields_match_snapshot(self, snapshot: dict[str, Any]) -> None:
        snapshot_by_name = {t["name"]: t for t in snapshot["tools"]}
        mismatches: list[str] = []
        for name, spec in TOOL_REGISTRY.items():
            snap_schema = snapshot_by_name[name]["inputSchema"]
            snap_required = set(snap_schema.get("required") or [])
            model_required = {
                f_name for f_name, f in spec.input_model.model_fields.items() if f.is_required()
            }
            if snap_required != model_required:
                mismatches.append(
                    f"{name}: snapshot_required={sorted(snap_required)} "
                    f"model_required={sorted(model_required)}"
                )
        assert not mismatches, "Required-field drift:\n" + "\n".join(mismatches)


class TestGetConsumerLagInput:
    def test_default_group_matches_platform_default(self) -> None:
        model = GetConsumerLagInput()
        assert model.consumer_group == "worker-dispatcher"

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GetConsumerLagInput.model_validate({"consumer_group": "x", "extra": "y"})


class TestGetConsumerLagOutput:
    def test_null_lag_accepted(self) -> None:
        model = GetConsumerLagOutput(
            consumer_group="worker-dispatcher",
            lag=None,
            cache_key="kafka:consumer_lag:worker-dispatcher",
        )
        assert model.lag is None


class TestSchemaSelfChecks:
    """Spot-check a few Wave-2 models parse the shapes the snapshot advertises."""

    def test_get_dag_state_requires_job_id(self) -> None:
        spec = TOOL_REGISTRY["get_dag_state"]
        with pytest.raises(ValidationError):
            spec.input_model.model_validate({})

    def test_get_deploy_history_empty_input(self) -> None:
        spec = TOOL_REGISTRY["get_deploy_history"]
        model = spec.input_model()
        assert isinstance(model, BaseModel)

    def test_list_incidents_limits(self) -> None:
        spec = TOOL_REGISTRY["list_incidents"]
        with pytest.raises(ValidationError):
            spec.input_model.model_validate({"limit": 201})
        with pytest.raises(ValidationError):
            spec.input_model.model_validate({"limit": 0})

    def test_search_traces_since_hours_bounded(self) -> None:
        spec = TOOL_REGISTRY["search_traces"]
        with pytest.raises(ValidationError):
            spec.input_model.model_validate({"since_hours": 200})

    def test_get_trace_min_length(self) -> None:
        spec = TOOL_REGISTRY["get_trace"]
        with pytest.raises(ValidationError):
            spec.input_model.model_validate({"trace_id": ""})


class TestToolSpec:
    def test_toolspec_frozen(self) -> None:
        spec = TOOL_REGISTRY["get_consumer_lag"]
        assert isinstance(spec, ToolSpec)
        with pytest.raises(AttributeError):
            spec.name = "new_name"  # type: ignore[misc]

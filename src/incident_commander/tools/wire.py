"""Serialize tool arguments to the exact bytes sent over the wire.

The platform hashes the JSON body of a ``tools/call`` request for
idempotency — same key + same body = cached replay; same key + different
body = 409. That means our serialization is a load-bearing contract:
any tweak to ``model_dump`` options (``exclude_none``, ``by_alias``,
etc.) or a Pydantic default-fill change silently breaks legitimate
resume-retry semantics.

``wire_arguments`` is the single canonical function that produces those
bytes. Both ``transition_remediate`` (action call) and ``make_llm_verify``
(verify probe) route through it, and the wire-contract tests
(``tests/integration/test_idempotency_contract.py`` + the golden unit
tests) assert against it. A test that re-implemented the serialization
would drift from the thing it guards; production and tests must share
this code.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from incident_commander.tools.registry import ToolSpec


def wire_arguments(tool: ToolSpec, raw_args: Mapping[str, Any]) -> dict[str, Any]:
    """Validate ``raw_args`` against ``tool.input_model`` and dump to wire JSON.

    ``mode="json"`` is the wire format (UUIDs → strings, Decimals → strings,
    etc.). Pydantic-filled defaults ARE included — that's the platform's
    contract, per ADR 0010 on the platform side. Callers must include
    ``idempotency_key`` in ``raw_args`` for Tier-1 action tools; read
    tools ignore that field.
    """
    return tool.input_model.model_validate(dict(raw_args)).model_dump(mode="json")

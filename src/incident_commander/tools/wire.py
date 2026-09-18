"""Serialize tool arguments to the exact bytes sent over the wire.

The platform hashes a ``tools/call`` body for idempotency (same key + different
body = 409), so ``model_dump`` options here are a load-bearing contract, not a
refactor. ``wire_arguments`` is the one canonical producer of those bytes — every
outgoing call and the wire-contract tests share it (WO-R2-83).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from incident_commander.tools.registry import ToolSpec


def wire_arguments(tool: ToolSpec, raw_args: Mapping[str, Any]) -> dict[str, Any]:
    """Validate ``raw_args`` against ``tool.input_model`` and dump to wire JSON.

    Pydantic-filled defaults ARE included (ADR 0010); Tier-1 needs ``idempotency_key``.
    """
    return tool.input_model.model_validate(dict(raw_args)).model_dump(mode="json")


def canonical_arguments_body(arguments: Mapping[str, Any]) -> bytes:
    """The exact bytes the platform's idempotency store hashes.

    ADR 0010 §2's normalization, reimplemented not imported: sorted keys, no
    whitespace, ``default=str``, ``ensure_ascii`` on, UTF-8. A wire contract.
    """
    return json.dumps(dict(arguments), sort_keys=True, separators=(",", ":"), default=str).encode()


def arguments_hash(arguments: Mapping[str, Any]) -> str:
    """SHA-256 (lowercase hex) of :func:`canonical_arguments_body`.

    The commander's pinned half of the platform's ``IdempotencyRecord.arguments_hash``
    — off the request path, exercised by ``tests/unit/test_idempotency_hash_matrix.py``.
    """
    return hashlib.sha256(canonical_arguments_body(arguments)).hexdigest()

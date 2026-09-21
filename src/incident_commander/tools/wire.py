"""Turn tool arguments into the exact bytes that go over the wire.

The platform hashes the arguments of a ``tools/call`` to recognise a repeated call, so the dump
options in this file are part of that agreement: change one and the same call hashes differently.
``wire_arguments`` fills in defaults and rejects nothing, which is deliberate (ADR 0024).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from incident_commander.tools.registry import ToolSpec


def wire_arguments(tool: ToolSpec, raw_args: Mapping[str, Any]) -> dict[str, Any]:
    """Validate ``raw_args`` against the tool's input model and dump it as the JSON to be sent.

    Fields Pydantic filled in from defaults are included, because the platform hashes what it
    receives, not what the caller typed (ADR 0010). Every action tool needs ``idempotency_key``.
    """
    return tool.input_model.model_validate(dict(raw_args)).model_dump(mode="json")


def canonical_arguments_body(arguments: Mapping[str, Any]) -> bytes:
    """The exact bytes the platform hashes when it decides whether it has seen this call before.

    Sorted keys, no whitespace, non-JSON values stringified, ASCII-escaped, UTF-8 — the rules from
    ADR 0010 §2, written out here rather than imported, because importing platform code to build a
    request the platform must verify independently would defeat the check (invariant 1).
    """
    return json.dumps(dict(arguments), sort_keys=True, separators=(",", ":"), default=str).encode()


def arguments_hash(arguments: Mapping[str, Any]) -> str:
    """SHA-256, lower-case hex, of :func:`canonical_arguments_body`.

    This side's copy of the hash the platform stores as ``IdempotencyRecord.arguments_hash``. No
    request uses it; it exists so ``tests/unit/test_idempotency_hash_matrix.py`` can prove the two
    sides still agree on the same bytes.
    """
    return hashlib.sha256(canonical_arguments_body(arguments)).hexdigest()

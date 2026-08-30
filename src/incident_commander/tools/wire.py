"""Serialize tool arguments to the exact bytes sent over the wire.

The platform hashes the JSON body of a ``tools/call`` request for
idempotency — same key + same body = cached replay; same key + different
body = 409. That means our serialization is a load-bearing contract:
any tweak to ``model_dump`` options (``exclude_none``, ``by_alias``,
etc.) or a Pydantic default-fill change silently breaks legitimate
resume-retry semantics.

``wire_arguments`` is the single canonical function that produces those
bytes. Every outgoing tool call routes through it — ``transition_remediate``
(action call), ``make_llm_verify`` (verify probe), ``transition_investigate``
(opening probe) and ``_execute_probe`` (investigation probes) — and the
wire-contract tests (``tests/integration/test_idempotency_contract.py`` +
the golden unit tests) assert against it. A test that re-implemented the
serialization would drift from the thing it guards; production and tests
must share this code.

``arguments_hash`` below is the other half of the same contract: the
commander's own implementation of the normalization the platform keys
its idempotency store on. It lives beside the serializer for the reason
stated above — ``tests/unit/test_idempotency_hash_matrix.py`` used to
define the formula inside itself, hash dict literals with it, and so
stay green through any change to the bytes this module actually
produces (WO-R2-83).

Note what ``wire_arguments`` does NOT do: refuse an omitted argument. It
default-fills from ``tool.input_model``, because that is the platform's
contract. Whether a caller is *allowed* to omit a resource-naming
argument is a caller-layer question — the remediation planner is not
(``remediation._absent_resource_args``, ADR 0022), the read-only
investigation leg is.
"""

from __future__ import annotations

import hashlib
import json
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


def canonical_arguments_body(arguments: Mapping[str, Any]) -> bytes:
    """The exact bytes the platform's idempotency store hashes.

    The commander's own implementation of the normalization published in
    platform ADR 0010 §2 — sorted keys at every depth, no whitespace,
    ``default=str`` for non-JSON-native values, then UTF-8. Deliberately
    NOT imported from the platform: ADR 0010 rejects a provider-import
    (it inverts the ADR 0001 dependency arrow and verifies whatever
    checkout is on disk rather than the digest-pinned image live evals
    hit). Two independent implementations of one written spec is the
    point — drift between them is what the contract matrix catches.

    Note ``json.dumps`` keeps its ``ensure_ascii=True`` default, so
    non-ASCII characters are escaped to ``\\uXXXX`` *before* the UTF-8
    encode and the body is pure ASCII. The platform hashes the escaped
    form (its ``_hash_arguments`` takes the same default), so the
    commander must too — ADR 0010's "String encoding: UTF-8" row
    describes the ``.encode()`` call, not an unescaped body.

    A change here is a wire-contract change, not a refactor: it moves
    which requests the platform considers the same request. Follow
    ADR 0010's coordination rule.
    """
    return json.dumps(dict(arguments), sort_keys=True, separators=(",", ":"), default=str).encode()


def arguments_hash(arguments: Mapping[str, Any]) -> str:
    """SHA-256 (lowercase hex) of :func:`canonical_arguments_body`.

    The commander's reference for the value the platform stores as
    ``IdempotencyRecord.arguments_hash`` and compares an
    ``Idempotency-Key`` reuse against. Nothing on the request path calls
    it — the platform computes the authoritative hash from the bytes it
    receives — so its whole job is to be the commander's pinned half of
    that cross-repo contract, exercised by
    ``tests/unit/test_idempotency_hash_matrix.py`` over the output of
    ``wire_arguments`` above. It lives here rather than in that test
    because a matrix that hashes a formula defined inside itself proves
    nothing about either side.
    """
    return hashlib.sha256(canonical_arguments_body(arguments)).hexdigest()

"""ADR 0010 arguments-hash drift matrix (FIX_PLAN #26 close-out).

The platform keys idempotency on ``sha256`` of the canonicalised wire ``arguments``, and
both halves are load-bearing: the FORMULA and the BYTES WE SEND. Real ``ToolSpec``s run
through ``tools.wire.wire_arguments`` and the normalization through ``arguments_hash``.
A failure is commander drift or an ADR update — never a rebless to match the platform.
"""

from __future__ import annotations

import hashlib
import json
from typing import Final
from uuid import UUID

from incident_commander.tools.registry import TOOL_REGISTRY
from incident_commander.tools.wire import (
    arguments_hash,
    canonical_arguments_body,
    wire_arguments,
)

# Digests from the normalization ADR 0010 §2 publishes: the platform's side.
_RESTART_HASH: Final[str] = "d1a2f99086b139f34c4b38d7e6234bab4b537618cc0334ed195f4f0460c8a676"
_REPLAY_HASH: Final[str] = "bb176bf153df7258b49527a87bdcc1a9a24134bb533094be3b6e419d6046936b"

_JOB_ID: Final[UUID] = UUID("11111111-1111-1111-1111-000000000001")
_REPLAY_ARGS: Final[dict[str, object]] = {
    "job_ids": [_JOB_ID],
    "idempotency_key": "01234567890abcdef",
}


class TestTheBytesWeSend:
    """Pins the commander's serialization: production ``wire_arguments``."""

    def test_baseline_restart_consumer_group(self) -> None:
        """The canonical modal Tier-1 call, hashed as it leaves the process."""
        wire = wire_arguments(
            TOOL_REGISTRY["restart_consumer_group"],
            {"consumer_group": "worker-dispatcher", "idempotency_key": "01234567abcdef"},
        )
        assert canonical_arguments_body(wire) == (
            b'{"consumer_group":"worker-dispatcher","idempotency_key":"01234567abcdef"}'
        )
        assert arguments_hash(wire) == _RESTART_HASH

    def test_a_filled_default_and_a_uuid_are_part_of_the_hashed_bytes(self) -> None:
        """``replay_dlq_by_ids``: the two ways serialization silently drifts.

        ``delay_seconds`` must reach the wire as explicit ``null`` — ADR 0010 §2 hashes the wire
        dict WITH filled defaults — and ``job_ids`` UUIDs must render as strings (``mode="json"``).
        """
        wire = wire_arguments(TOOL_REGISTRY["replay_dlq_by_ids"], _REPLAY_ARGS)
        assert canonical_arguments_body(wire) == (
            b'{"delay_seconds":null,"idempotency_key":"01234567890abcdef",'
            b'"job_ids":["11111111-1111-1111-1111-000000000001"]}'
        )
        assert arguments_hash(wire) == _REPLAY_HASH


class TestTheFormulaWeAgreedOn:
    """Pins ADR 0010 §2's sensitivity table, row by row."""

    def test_key_reorder_yields_same_hash(self) -> None:
        # sort_keys=True — insertion order at the caller is not significant.
        a = arguments_hash({"a": 1, "b": 2, "c": 3})
        b = arguments_hash({"c": 3, "a": 1, "b": 2})
        assert a == b == "e6a3385fb77c287a712e7f406a451727f0625041823ecf23bea7ef39b2e39805"

    def test_nested_key_reorder_yields_same_hash(self) -> None:
        # The sort is recursive: nested dicts get sorted too.
        a = arguments_hash({"outer": {"a": 1, "b": 2}})
        b = arguments_hash({"outer": {"b": 2, "a": 1}})
        assert a == b == "8a14b37c210b85f40e7290a8e55658a59f90ad6fae1f315627109854d34d71e8"

    def test_list_reorder_yields_different_hash(self) -> None:
        # List order IS significant. Lists aren't sets.
        a = arguments_hash({"tags": ["a", "b"]})
        b = arguments_hash({"tags": ["b", "a"]})
        assert a != b
        assert a == "5272f2592556de40109bea7c48aacc8ea045e66e0bf88a0f42a209f49bcd7578"
        assert b == "b4b48c25efc8fb665cb25a9f8af2b56fbf0ec7dab2a54c91a91b0febf7bb6741"

    def test_null_vs_absent_yield_different_hashes(self) -> None:
        # {"x": null} and {} are different keys: this is the row that makes exclude_none=True
        # a retry-breaking change.
        with_null = arguments_hash({"consumer_group": "wd", "idempotency_key": "k", "note": None})
        absent = arguments_hash({"consumer_group": "wd", "idempotency_key": "k"})
        assert with_null != absent

    def test_int_vs_float_yield_different_hashes(self) -> None:
        # JSON emits `1` vs `1.0` — different bytes, different hashes.
        assert arguments_hash({"n": 1}) != arguments_hash({"n": 1.0})

    def test_non_ascii_is_escaped_before_the_utf8_encode(self) -> None:
        """The vector this file used to get backwards.

        ``json.dumps`` defaults to ``ensure_ascii=True``, so ``café`` is escaped before the
        encode: the digest was always the escaped one, and ``ensure_ascii=False`` read as a fix.
        """
        args = {"note": "café", "idempotency_key": "k"}

        body = canonical_arguments_body(args)
        assert body == rb'{"idempotency_key":"k","note":"caf\u00e9"}'
        assert body.isascii()
        assert (
            arguments_hash(args)
            == "5a1eb585a18ca90323ce2d08fdc56bf5b152b36690da200162455f59a399d347"
        )

        # What a UTF-8-emitting encoder would have hashed instead. Different
        # request as far as the platform's store is concerned.
        unescaped = json.dumps(
            args, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        assert unescaped == '{"idempotency_key":"k","note":"café"}'.encode()
        assert hashlib.sha256(unescaped).hexdigest() != arguments_hash(args)


class TestTheMatrixWouldSeeDrift:
    """The matrix's own alibi: perform the regressions, watch the digests move.

    A contract test is only worth its sensitivity, and this file spent its
    whole life with none — these are the exact changes it was standing
    guard against, each shown to break a pinned vector above.
    """

    def test_an_exclude_none_serializer_breaks_a_pinned_vector(self) -> None:
        # The regression ADR 0010 §2 names: exclude_none=True drops `delay_seconds: null`.
        spec = TOOL_REGISTRY["replay_dlq_by_ids"]
        drifted = spec.input_model.model_validate(dict(_REPLAY_ARGS)).model_dump(
            mode="json", exclude_none=True
        )
        assert arguments_hash(drifted) != _REPLAY_HASH

    def test_a_python_mode_dump_breaks_a_pinned_vector(self) -> None:
        # mode="python" leaves UUIDs as objects and `default=str` stringifies them, so the
        # bytes agree only by luck.
        spec = TOOL_REGISTRY["replay_dlq_by_ids"]
        drifted = spec.input_model.model_validate(dict(_REPLAY_ARGS)).model_dump(mode="python")
        drifted["delay_seconds"] = 0  # a Python-side default change, same shape
        assert arguments_hash(drifted) != _REPLAY_HASH

    def test_a_normalization_change_breaks_a_pinned_vector(self) -> None:
        # Whitespace between tokens: `separators=(", ", ": ")` is the
        # json.dumps default a refactor would land on.
        loose = json.dumps(
            wire_arguments(
                TOOL_REGISTRY["restart_consumer_group"],
                {"consumer_group": "worker-dispatcher", "idempotency_key": "01234567abcdef"},
            ),
            sort_keys=True,
        ).encode()
        assert hashlib.sha256(loose).hexdigest() != _RESTART_HASH

    def test_an_unsorted_normalization_breaks_a_pinned_vector(self) -> None:
        unsorted = json.dumps({"b": 2, "a": 1}, separators=(",", ":")).encode()
        assert hashlib.sha256(unsorted).hexdigest() != arguments_hash({"a": 1, "b": 2})

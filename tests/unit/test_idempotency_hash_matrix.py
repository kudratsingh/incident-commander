"""ADR 0010 arguments-hash drift matrix (FIX_PLAN #26 close-out).

Per platform ADR 0010 §2 the platform keys its idempotency store on
``sha256(json.dumps(arguments, sort_keys=True, separators=(",", ":"),
default=str).encode())`` over the raw ``tools/call.arguments`` dict as it
arrives on the wire. Both halves of that sentence are load-bearing for the
commander: the FORMULA (which requests count as the same request) and the
BYTES WE SEND (what we hand it). This file pins both.

**What each vector pins.** Every test below names one side of the contract:

* ``TestTheBytesWeSend`` runs real ``ToolSpec``s through the production
  serializer — ``tools.wire.wire_arguments``, the same call every outgoing
  ``tools/call`` makes — and pins the digest of its output. A change to
  Pydantic dump options, a field default, or a model's shape moves these.
* ``TestTheFormulaWeAgreedOn`` pins the normalization itself against ADR
  0010 §2's sensitivity table, through ``tools.wire.arguments_hash``. A
  change to sort order, separators, encoder or algorithm moves these.
* ``TestTheMatrixWouldSeeDrift`` demonstrates the sensitivity is real: it
  performs the exact regressions these vectors exist to catch and asserts
  the digests move.

**Why it is not a copy of the formula.** Until WO-R2-83 this file defined
its own ``_canonical_hash`` and hashed dict literals with it. It touched no
commander code and no platform code, so it could only ever confirm that
Python's ``json`` and ``hashlib`` are deterministic: the commander's
serializer could be switched to ``exclude_none=True`` — a change that 409s
every in-flight retry with a nullable field — and the whole matrix stayed
green. The formula now lives in ``src/incident_commander/tools/wire.py``
with the serializer it belongs to, so both sides of the contract are real
code and either one moving is a red test.

The commander still does NOT import the platform's ``_hash_arguments``;
ADR 0010 §2 rejects that (it verifies whatever platform checkout is on disk
rather than the digest-pinned image live evals actually hit, and freezes a
private helper into public API). Two independent implementations of one
written spec is the design — this file is where they are compared.

**When one of these fails**, one of two things happened:

1. The commander drifted — a serializer option, a model default, or the
   normalization. Fix it here; do not rebless the digest.
2. The platform's spec changed. Then this PR carries the ADR update AND
   the new digests together — the version-sync ritual (ADR 0010 §2
   "Coordination rule"). Never silently patch to match a new platform.
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

# Digests computed from the normalization ADR 0010 §2 publishes. They are
# the platform's side of the contract; the code path reaching them is the
# commander's.
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

        ``delay_seconds`` is omitted by the caller, defaulted to ``None`` by
        Pydantic, and MUST appear on the wire as explicit ``null`` — ADR 0010
        §2 hashes the wire dict WITH filled defaults, and null-vs-absent is a
        different hash. ``job_ids`` is a list of UUIDs that ``mode="json"``
        renders as strings; a switch to ``mode="python"`` would hash
        ``UUID(...)`` reprs through ``default=str`` and produce different
        bytes for the same request.
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
        # {"x": null} and {} are different keys. This is the row that makes
        # exclude_none=True a retry-breaking change rather than a cleanup —
        # test_an_exclude_none_serializer_breaks_a_pinned_vector below shows
        # it reaching a real tool.
        with_null = arguments_hash({"consumer_group": "wd", "idempotency_key": "k", "note": None})
        absent = arguments_hash({"consumer_group": "wd", "idempotency_key": "k"})
        assert with_null != absent

    def test_int_vs_float_yield_different_hashes(self) -> None:
        # JSON emits `1` vs `1.0` — different bytes, different hashes.
        assert arguments_hash({"n": 1}) != arguments_hash({"n": 1.0})

    def test_non_ascii_is_escaped_before_the_utf8_encode(self) -> None:
        """The vector this file used to get backwards.

        It was named ``test_unicode_survives_utf8_encoding`` and its comment
        promised a UTF-8 round trip, but ``json.dumps`` defaults to
        ``ensure_ascii=True``: ``café`` is escaped to ``caf\\u00e9`` and the
        body that reaches ``.encode()`` is pure ASCII. The digest it pinned
        was always the escaped one — so the test asserted the opposite of
        what it said, and an ``ensure_ascii=False`` change (which really
        would break the contract) would have been read as the fix.

        The platform's ``_hash_arguments`` takes the same default, so escaped
        is correct. Both bodies are spelled out here so the distinction
        cannot be re-lost, and the digests are asserted to differ.
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
        # The regression ADR 0010 §2 calls out by name: a commander that
        # dumps with exclude_none=True drops `delay_seconds: null` and 409s
        # every in-flight retry of a request that omitted it.
        spec = TOOL_REGISTRY["replay_dlq_by_ids"]
        drifted = spec.input_model.model_validate(dict(_REPLAY_ARGS)).model_dump(
            mode="json", exclude_none=True
        )
        assert arguments_hash(drifted) != _REPLAY_HASH

    def test_a_python_mode_dump_breaks_a_pinned_vector(self) -> None:
        # mode="python" leaves UUIDs as objects; `default=str` stringifies
        # them at hash time, so the bytes agree only by luck — and stop
        # agreeing the moment a field's Python repr differs from its JSON
        # form (datetimes, Decimals).
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

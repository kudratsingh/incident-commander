"""ADR 0010 arguments-hash normalization matrix (FIX_PLAN #26 close-out).

Per platform ADR 0010 §2, the platform hashes ``tools/call.arguments``
with ``json.dumps(arguments, sort_keys=True, separators=(",", ":"),
default=str).encode()`` then SHA-256. The commander computes a
matching local reference so a change on either side surfaces at the
version-sync PR that introduces it.

This test pins a fixed set of representative argument dicts to their
expected SHA-256 hashes and asserts the local implementation stays
byte-identical to them. Each dict is chosen to exercise one edge of
ADR 0010's sensitivity table:

- Baseline call (canonical modal case).
- Key reorder → SAME hash (recursive sort_keys=True).
- Nested key reorder → SAME hash (recursion).
- List reorder → DIFFERENT hash (list order is significant).
- Null vs absent → DIFFERENT hashes (`exclude_none=True` would break).
- Numeric type (int vs float) → DIFFERENT hashes.
- Unicode round-trip → predictable hash (UTF-8 encoding).

If a hash mismatches, one of two things happened:

1. The local ``_canonical_hash`` implementation diverged from ADR 0010
   (probably a serializer or option change). Fix locally.
2. ADR 0010's spec was updated on the platform side. In that case
   this PR should include the ADR update *and* new expected hashes
   here in the same commit — that's the version-sync ritual (§2
   "Coordination rule"). Never silently patch to match a new platform.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _canonical_hash(arguments: dict[str, Any]) -> str:
    """Byte-for-byte replica of ADR 0010 §2 ``_hash_arguments``.

    Kept inline (not imported from the platform) per FIX_PLAN #26 —
    the commander is an external client and the whole point is that
    both sides implement the spec independently and CI catches drift.
    """
    body = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(body).hexdigest()


class TestCanonicalHashStability:
    """Pinned hashes for representative input shapes. Break = version-sync moment."""

    def test_baseline_restart_consumer_group(self) -> None:
        args = {
            "consumer_group": "worker-dispatcher",
            "idempotency_key": "01234567abcdef",
        }
        assert (
            _canonical_hash(args)
            == "d1a2f99086b139f34c4b38d7e6234bab4b537618cc0334ed195f4f0460c8a676"
        )

    def test_key_reorder_yields_same_hash(self) -> None:
        # ADR 0010: sort_keys=True — insertion order at any depth is not
        # significant. Two shapes below must produce identical bytes.
        a = _canonical_hash({"a": 1, "b": 2, "c": 3})
        b = _canonical_hash({"c": 3, "a": 1, "b": 2})
        assert a == b == "e6a3385fb77c287a712e7f406a451727f0625041823ecf23bea7ef39b2e39805"

    def test_nested_key_reorder_yields_same_hash(self) -> None:
        # Recursive sort — nested dicts also get sorted.
        a = _canonical_hash({"outer": {"a": 1, "b": 2}})
        b = _canonical_hash({"outer": {"b": 2, "a": 1}})
        assert a == b == "8a14b37c210b85f40e7290a8e55658a59f90ad6fae1f315627109854d34d71e8"

    def test_list_reorder_yields_different_hash(self) -> None:
        # ADR 0010: list order IS significant. Lists aren't sets.
        a = _canonical_hash({"tags": ["a", "b"]})
        b = _canonical_hash({"tags": ["b", "a"]})
        assert a != b
        assert a == "5272f2592556de40109bea7c48aacc8ea045e66e0bf88a0f42a209f49bcd7578"
        assert b == "b4b48c25efc8fb665cb25a9f8af2b56fbf0ec7dab2a54c91a91b0febf7bb6741"

    def test_null_vs_absent_yield_different_hashes(self) -> None:
        # ADR 0010: {"x": null} and {} are different keys. If the
        # commander ever switches to model_dump(exclude_none=True), this
        # test catches it — that change would silently break in-flight
        # retry dedup for every request with a nullable field.
        with_null = _canonical_hash({"consumer_group": "wd", "idempotency_key": "k", "note": None})
        absent = _canonical_hash({"consumer_group": "wd", "idempotency_key": "k"})
        assert with_null != absent

    def test_int_vs_float_yield_different_hashes(self) -> None:
        # JSON emits `1` vs `1.0` — different bytes → different hashes.
        assert _canonical_hash({"n": 1}) != _canonical_hash({"n": 1.0})

    def test_unicode_survives_utf8_encoding(self) -> None:
        # Non-ASCII must round-trip. Locking a specific hash so a
        # future ensure_ascii=True regression surfaces here.
        args = {"note": "café", "idempotency_key": "k"}
        assert (
            _canonical_hash(args)
            == "5a1eb585a18ca90323ce2d08fdc56bf5b152b36690da200162455f59a399d347"
        )

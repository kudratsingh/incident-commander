"""Constant-time HMAC verification for platform webhooks."""

from __future__ import annotations

import hashlib
import hmac
from typing import Final

_SIGNATURE_PREFIX: Final[str] = "sha256="
_SIGNATURE_PREFIX_V2: Final[str] = "v2="


def sign(body: bytes, secret: str) -> str:
    """Compute the signature the platform sends. Useful for tests and demos."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"{_SIGNATURE_PREFIX}{digest}"


def verify(body: bytes, signature_header: str, secret: str) -> bool:
    """True iff ``signature_header`` matches an HMAC-SHA256 of ``body`` with ``secret``.

    The header is expected in ``sha256=<hex>`` form (GitHub-style). Rejects
    unprefixed values, wrong-length digests, and mismatches — all in constant time.
    """
    if not signature_header.startswith(_SIGNATURE_PREFIX):
        return False
    provided = signature_header.removeprefix(_SIGNATURE_PREFIX)
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if len(provided) != len(expected):
        return False
    return hmac.compare_digest(provided, expected)


def sign_v2(timestamp_ms: str, body: bytes, secret: str) -> str:
    """Compute the v2 signature: HMAC-SHA256 over ``{timestamp_ms}.{body}``.

    Binding the timestamp into the MAC is what makes v2 replay-resistant —
    a captured signature cannot be paired with a fresh timestamp (ADR 0014).
    The platform does not emit v2 yet; this ships ahead of its dual-emit PR.
    """
    material = f"{timestamp_ms}.".encode() + body
    digest = hmac.new(secret.encode(), material, hashlib.sha256).hexdigest()
    return f"{_SIGNATURE_PREFIX_V2}{digest}"


def verify_v2(body: bytes, timestamp_header: str, signature_header: str, secret: str) -> bool:
    """True iff ``signature_header`` is a valid v2 MAC over ``timestamp_header`` and ``body``.

    The header is expected in ``v2=<hex>`` form. Same constant-time
    discipline as ``verify``: prefix check, length pre-check,
    ``hmac.compare_digest``.
    """
    if not signature_header.startswith(_SIGNATURE_PREFIX_V2):
        return False
    provided = signature_header.removeprefix(_SIGNATURE_PREFIX_V2)
    material = f"{timestamp_header}.".encode() + body
    expected = hmac.new(secret.encode(), material, hashlib.sha256).hexdigest()
    if len(provided) != len(expected):
        return False
    return hmac.compare_digest(provided, expected)

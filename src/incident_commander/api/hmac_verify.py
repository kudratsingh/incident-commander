"""Constant-time HMAC verification for platform webhooks."""

from __future__ import annotations

import hashlib
import hmac
from typing import Final

_SIGNATURE_PREFIX: Final[str] = "sha256="


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

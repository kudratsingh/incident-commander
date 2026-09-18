"""Constant-time HMAC verification for platform webhooks.

Two schemes (ADR 0023): nonce-bound over ``{timestamp}.{nonce}.{body}``, selected
by the presence of ``X-Alert-Nonce`` and preferred; and legacy body-only. Both
carry the digest as ``sha256=<hex>``, so the prefix cannot tell them apart.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Final

_SIGNATURE_PREFIX: Final[str] = "sha256="


def sign(body: bytes, secret: str) -> str:
    """Compute the legacy body-only signature. Useful for tests and demos."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"{_SIGNATURE_PREFIX}{digest}"


def verify(body: bytes, signature_header: str, secret: str) -> bool:
    """True iff ``signature_header`` matches an HMAC-SHA256 of ``body`` with ``secret``.

    Header in ``sha256=<hex>`` form. Every rejection is in constant time.
    """
    return _matches(signature_header, body, secret)


def signed_material(timestamp: str, nonce: str, body: bytes) -> bytes:
    """The exact bytes the nonce-bound signature covers: ``{timestamp}.{nonce}.{body}``.

    Transcribed from the emitter's ``alerts.signed_material`` — two ends that
    disagree verify nothing.
    """
    return f"{timestamp}.{nonce}.".encode() + body


def sign_delivery(secret: str, timestamp: str, nonce: str, body: bytes) -> str:
    """Compute the nonce-bound signature the platform sends. For tests and demos.

    Argument order mirrors the emitter's ``alerts.sign_delivery``.
    """
    digest = hmac.new(
        secret.encode(), signed_material(timestamp, nonce, body), hashlib.sha256
    ).hexdigest()
    return f"{_SIGNATURE_PREFIX}{digest}"


def verify_delivery(
    body: bytes,
    timestamp_header: str,
    nonce_header: str,
    signature_header: str,
    secret: str,
) -> bool:
    """True iff ``signature_header`` is a valid MAC over timestamp, nonce and body.

    Constant-time, like ``verify``. The timestamp inside the MAC is what bounds
    replay.
    """
    return _matches(signature_header, signed_material(timestamp_header, nonce_header, body), secret)


def _matches(signature_header: str, material: bytes, secret: str) -> bool:
    """Shared constant-time comparison for both schemes.

    One implementation so the two acceptance paths cannot drift apart.
    """
    if not signature_header.startswith(_SIGNATURE_PREFIX):
        return False
    provided = signature_header.removeprefix(_SIGNATURE_PREFIX)
    expected = hmac.new(secret.encode(), material, hashlib.sha256).hexdigest()
    if len(provided) != len(expected):
        return False
    return hmac.compare_digest(provided, expected)

"""Checking that an inbound webhook really came from the platform, in constant time.

Two signing schemes are accepted (ADR 0023): the newer one signs ``{timestamp}.{nonce}.{body}`` and
is chosen by the presence of ``X-Alert-Nonce``; the legacy one signs the body alone. Both write
``sha256=<hex>``, so the signature itself cannot say which scheme produced it.
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
    """True when ``signature_header`` matches an HMAC-SHA256 of ``body`` alone under ``secret``.

    The header must read ``sha256=<hex>``. Every comparison takes the same time whether it matches
    or not, so nothing about the expected value can be learned by timing repeated attempts.
    """
    return _matches(signature_header, body, secret)


def signed_material(timestamp: str, nonce: str, body: bytes) -> bytes:
    """The exact bytes the newer signature covers: ``{timestamp}.{nonce}.{body}``.

    Copied from the platform emitter's own ``alerts.signed_material``. If the two ever disagree by
    one byte, every delivery fails the check, so this is a contract and not an implementation note.
    """
    return f"{timestamp}.{nonce}.".encode() + body


def sign_delivery(secret: str, timestamp: str, nonce: str, body: bytes) -> str:
    """Compute the newer signature the platform sends. For tests and demos, never for ingress.

    The argument order matches the emitter's ``alerts.sign_delivery`` so the two can be read side
    by side.
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
    """True when ``signature_header`` is a valid signature over timestamp, nonce and body together.

    Constant-time, like ``verify``. Because the timestamp is inside what was signed, nobody can
    change it, which is what lets the caller put a real age limit on a delivery.
    """
    return _matches(signature_header, signed_material(timestamp_header, nonce_header, body), secret)


def _matches(signature_header: str, material: bytes, secret: str) -> bool:
    """The constant-time comparison both schemes use.

    Written once so the two ways in cannot drift apart: a fix to one would otherwise leave the
    other accepting signatures it should not.
    """
    if not signature_header.startswith(_SIGNATURE_PREFIX):
        return False
    provided = signature_header.removeprefix(_SIGNATURE_PREFIX)
    expected = hmac.new(secret.encode(), material, hashlib.sha256).hexdigest()
    if len(provided) != len(expected):
        return False
    return hmac.compare_digest(provided, expected)

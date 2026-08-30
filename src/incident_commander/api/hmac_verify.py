"""Constant-time HMAC verification for platform webhooks.

Two schemes are accepted, because the emitter and this receiver live in
different repos and re-pin on different days (ADR 0023):

* **nonce-bound** — what the platform emits since plat #183. The MAC covers
  ``{timestamp}.{nonce}.{body}``, and the presence of ``X-Alert-Nonce`` is
  what selects it. This is the scheme to prefer.
* **legacy body-only** — what the pinned image still emits until the wave-9
  re-pin, plus pre-fix tooling. The MAC covers the body alone.

Both carry the digest as ``sha256=<hex>``: the platform kept the prefix and
added a header rather than versioning the value, so the prefix cannot be used
to tell the schemes apart.
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

    The header is expected in ``sha256=<hex>`` form (GitHub-style). Rejects
    unprefixed values, wrong-length digests, and mismatches — all in constant time.
    """
    return _matches(signature_header, body, secret)


def signed_material(timestamp: str, nonce: str, body: bytes) -> bytes:
    """The exact bytes the nonce-bound signature covers: ``{timestamp}.{nonce}.{body}``.

    Transcribed from the emitter's ``alerts.signed_material``, which is the
    canonical composition — a signature scheme whose two ends disagree about
    what is being signed verifies nothing. The separators are unambiguous
    because both prefixes are fixed-alphabet (digits, hex) and contain no
    ``.`` themselves, so no length prefix is needed to keep the parse honest.
    """
    return f"{timestamp}.{nonce}.".encode() + body


def sign_delivery(secret: str, timestamp: str, nonce: str, body: bytes) -> str:
    """Compute the nonce-bound signature the platform sends. For tests and demos.

    Argument order mirrors the emitter's ``alerts.sign_delivery`` so the two
    can be read side by side.
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

    Same constant-time discipline as ``verify``: prefix check, length
    pre-check, ``hmac.compare_digest``. Because the timestamp is inside the
    MAC, a captured signature cannot be paired with a fresh timestamp, which
    is what lets the caller's skew window genuinely bound replay; the nonce
    then makes a replay *inside* the window detectable too.
    """
    return _matches(signature_header, signed_material(timestamp_header, nonce_header, body), secret)


def _matches(signature_header: str, material: bytes, secret: str) -> bool:
    """Shared constant-time comparison for both schemes.

    One implementation so the two acceptance paths cannot drift into
    different comparison discipline — the length pre-check and
    ``compare_digest`` are the audited part, and they should be audited once.
    """
    if not signature_header.startswith(_SIGNATURE_PREFIX):
        return False
    provided = signature_header.removeprefix(_SIGNATURE_PREFIX)
    expected = hmac.new(secret.encode(), material, hashlib.sha256).hexdigest()
    if len(provided) != len(expected):
        return False
    return hmac.compare_digest(provided, expected)

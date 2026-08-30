import hashlib
import hmac

from incident_commander.api.hmac_verify import (
    sign,
    sign_delivery,
    signed_material,
    verify,
    verify_delivery,
)


class TestSign:
    def test_produces_prefixed_hex(self) -> None:
        signature = sign(b"payload", "secret")
        assert signature.startswith("sha256=")
        assert len(signature) == len("sha256=") + 64


class TestVerify:
    def test_accepts_matching_signature(self) -> None:
        body = b'{"foo":"bar"}'
        secret = "svc-secret"
        signature = sign(body, secret)
        assert verify(body, signature, secret) is True

    def test_rejects_mismatched_signature(self) -> None:
        body = b'{"foo":"bar"}'
        signature = sign(body, "secret-a")
        assert verify(body, signature, "secret-b") is False

    def test_rejects_missing_prefix(self) -> None:
        body = b'{"foo":"bar"}'
        signature = sign(body, "secret").removeprefix("sha256=")
        assert verify(body, signature, "secret") is False

    def test_rejects_empty_header(self) -> None:
        assert verify(b"body", "", "secret") is False

    def test_rejects_wrong_length_digest(self) -> None:
        assert verify(b"body", "sha256=deadbeef", "secret") is False

    def test_rejects_body_tampering(self) -> None:
        original = b'{"amount":10}'
        tampered = b'{"amount":1000}'
        signature = sign(original, "secret")
        assert verify(tampered, signature, "secret") is False


class TestSignedMaterial:
    """The bytes the platform emitter MACs, transcribed independently.

    Composed by hand rather than by calling the helper under test: if this
    repo's idea of the material drifts from the emitter's, that is exactly
    the failure the test exists to catch, and a self-referential assertion
    would drift right along with it.
    """

    def test_matches_the_emitter_composition(self) -> None:
        assert signed_material("1723180800000", "a1b2", b'{"k":1}') == b'1723180800000.a1b2.{"k":1}'

    def test_keeps_the_body_bytes_verbatim(self) -> None:
        # Dots inside the body must not confuse the composition — the two
        # prefixes are fixed-alphabet, so the parse stays unambiguous.
        body = b'{"note":"a.b.c"}'
        assert signed_material("1", "f", body).endswith(body)


class TestSignDelivery:
    def test_produces_sha256_prefixed_hex(self) -> None:
        # The platform kept the sha256= prefix and added a header rather than
        # versioning the value, so the prefix cannot select the scheme.
        signature = sign_delivery("secret", "1723180800000", "a1b2", b"payload")
        assert signature.startswith("sha256=")
        assert len(signature) == len("sha256=") + 64

    def test_matches_an_independently_computed_hmac(self) -> None:
        expected = hmac.new(b"secret", b'1723180800000.a1b2.{"k":1}', hashlib.sha256).hexdigest()
        assert sign_delivery("secret", "1723180800000", "a1b2", b'{"k":1}') == f"sha256={expected}"

    def test_timestamp_changes_the_signature(self) -> None:
        assert sign_delivery("secret", "1723180800000", "a1b2", b"payload") != sign_delivery(
            "secret", "1723184400000", "a1b2", b"payload"
        )

    def test_nonce_changes_the_signature(self) -> None:
        assert sign_delivery("secret", "1723180800000", "a1b2", b"payload") != sign_delivery(
            "secret", "1723180800000", "c3d4", b"payload"
        )


class TestVerifyDelivery:
    def test_round_trips_with_sign_delivery(self) -> None:
        body = b'{"alert_id":"abc"}'
        assert (
            verify_delivery(
                body,
                "1723180800000",
                "a1b2",
                sign_delivery("secret", "1723180800000", "a1b2", body),
                "secret",
            )
            is True
        )

    def test_rejects_a_swapped_timestamp(self) -> None:
        # The point of the scheme: a captured signature cannot be paired with
        # a fresh timestamp.
        body = b"body"
        signature = sign_delivery("secret", "1723180800000", "a1b2", body)
        assert verify_delivery(body, "1723184400000", "a1b2", signature, "secret") is False

    def test_rejects_a_swapped_nonce(self) -> None:
        body = b"body"
        signature = sign_delivery("secret", "1723180800000", "a1b2", body)
        assert verify_delivery(body, "1723180800000", "c3d4", signature, "secret") is False

    def test_rejects_a_body_only_signature(self) -> None:
        # The downgrade case: same header, same prefix, old material.
        body = b"body"
        assert (
            verify_delivery(body, "1723180800000", "a1b2", sign(body, "secret"), "secret") is False
        )

    def test_rejects_missing_prefix(self) -> None:
        body = b"body"
        bare = sign_delivery("secret", "1723180800000", "a1b2", body).removeprefix("sha256=")
        assert verify_delivery(body, "1723180800000", "a1b2", bare, "secret") is False

    def test_rejects_wrong_length_digest(self) -> None:
        assert (
            verify_delivery(b"body", "1723180800000", "a1b2", "sha256=deadbeef", "secret") is False
        )

    def test_rejects_empty_header(self) -> None:
        assert verify_delivery(b"body", "1723180800000", "a1b2", "", "secret") is False

    def test_rejects_body_tampering(self) -> None:
        signature = sign_delivery("secret", "1723180800000", "a1b2", b'{"amount":10}')
        assert (
            verify_delivery(b'{"amount":1000}', "1723180800000", "a1b2", signature, "secret")
            is False
        )

    def test_rejects_wrong_secret(self) -> None:
        body = b"body"
        signature = sign_delivery("secret-a", "1723180800000", "a1b2", body)
        assert verify_delivery(body, "1723180800000", "a1b2", signature, "secret-b") is False

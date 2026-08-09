from incident_commander.api.hmac_verify import sign, sign_v2, verify, verify_v2


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


class TestSignV2:
    def test_produces_v2_prefixed_hex(self) -> None:
        signature = sign_v2("1723180800000", b"payload", "secret")
        assert signature.startswith("v2=")
        assert len(signature) == len("v2=") + 64

    def test_timestamp_is_part_of_the_signed_material(self) -> None:
        assert sign_v2("1723180800000", b"payload", "secret") != sign_v2(
            "1723180800001", b"payload", "secret"
        )


class TestVerifyV2:
    def test_round_trips_with_sign_v2(self) -> None:
        body = b'{"foo":"bar"}'
        timestamp = "1723180800000"
        secret = "svc-secret"
        signature = sign_v2(timestamp, body, secret)
        assert verify_v2(body, timestamp, signature, secret) is True

    def test_rejects_timestamp_differing_from_signed_one(self) -> None:
        # The property v1 lacks: the timestamp is inside the MAC, so a
        # captured signature cannot be paired with a fresh timestamp.
        body = b'{"foo":"bar"}'
        signature = sign_v2("1723180800000", body, "secret")
        assert verify_v2(body, "1723184400000", signature, "secret") is False

    def test_rejects_unprefixed_header(self) -> None:
        body = b'{"foo":"bar"}'
        bare = sign_v2("1723180800000", body, "secret").removeprefix("v2=")
        assert verify_v2(body, "1723180800000", bare, "secret") is False

    def test_rejects_v1_prefixed_header(self) -> None:
        body = b'{"foo":"bar"}'
        assert verify_v2(body, "1723180800000", sign(body, "secret"), "secret") is False

    def test_rejects_wrong_length_digest(self) -> None:
        assert verify_v2(b"body", "1723180800000", "v2=deadbeef", "secret") is False

    def test_rejects_body_tampering(self) -> None:
        signature = sign_v2("1723180800000", b'{"amount":10}', "secret")
        assert verify_v2(b'{"amount":1000}', "1723180800000", signature, "secret") is False

    def test_rejects_wrong_secret(self) -> None:
        signature = sign_v2("1723180800000", b"body", "secret-a")
        assert verify_v2(b"body", "1723180800000", signature, "secret-b") is False

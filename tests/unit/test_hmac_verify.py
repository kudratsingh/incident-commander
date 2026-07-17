from incident_commander.api.hmac_verify import sign, verify


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

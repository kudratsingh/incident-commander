import pytest
from pydantic import BaseModel

from incident_commander.llm.client import LLMError
from incident_commander.llm.fakes import CannedLLMClient


class _Sample(BaseModel):
    label: str


class TestCannedLLMClient:
    def test_returns_validated_output(self) -> None:
        client = CannedLLMClient([{"label": "one"}, {"label": "two"}])
        r1 = client.call("s", "u", _Sample, model="m")
        r2 = client.call("s", "u", _Sample, model="m")
        assert r1.output.label == "one"
        assert r2.output.label == "two"

    def test_records_calls(self) -> None:
        client = CannedLLMClient([{"label": "x"}])
        client.call("system-prompt", "user-msg", _Sample, model="m")
        assert client.calls == [("system-prompt", "user-msg")]

    def test_exhausted_responses_raises(self) -> None:
        client = CannedLLMClient([{"label": "x"}])
        client.call("s", "u", _Sample, model="m")
        with pytest.raises(LLMError, match="no more canned"):
            client.call("s", "u", _Sample, model="m")

    def test_zero_token_counts(self) -> None:
        client = CannedLLMClient([{"label": "x"}])
        result = client.call("s", "u", _Sample, model="m")
        assert result.input_tokens == 0
        assert result.output_tokens == 0
        assert result.cache_creation_tokens == 0
        assert result.cache_read_tokens == 0
        assert result.stop_reason == "canned"

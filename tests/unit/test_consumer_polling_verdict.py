"""Platform v0.6.25 (plat #244, platform ADR 0041 amendment, INC-008 addendum): the lag verdict.

`get_consumer_lag` gains `polling` (did the consumer poll within 5 poll intervals when the
platform looked: true / false / null) and `poll_interval_seconds` (the cadence that verdict was
drawn against: 2.0 on the two live groups, null elsewhere). INC-008's addendum is why: on v0.6.24
the dead resolver read `last_poll_age_seconds 39` beside `age_seconds 3`, and the planner called
that "polling". These tests pin that both fields reach the typed model — `extra="ignore"` would
otherwise drop the one reading that says a consumer is dead — that a replay holds them, and that
every canned lag reading carries both, written by the platform's own rule.

RED BEFORE: on `main` at the v0.6.24 pin, `GetConsumerLagOutput` has neither field, so every
parse test below fails there (the attribute is missing), and the replay test fails on the
unlisted `poll_interval_seconds`. The fixture tests fail against the canned readings as v0.6.24
left them, none of which carries either field.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

from evals import recorded_client
from evals.fixture_drift import _VOLATILE, canned_calls
from evals.scenarios.loader import load_scenarios
from incident_commander.tools.mcp_client import ToolResult
from incident_commander.tools.registry import GetConsumerLagOutput

_SNAPSHOT: Final[Path] = (
    Path(__file__).resolve().parents[2] / "contracts" / "platform-tools.snapshot.json"
)
_SCENARIOS_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "evals" / "scenarios"

#: The platform's rule (`app/core/consumer_lag.py`): the groups it measures, the poll loop's
#: fetch bound, and how many of those may pass since the last poll before `polling` is false.
_MEASURED_GROUPS: Final[frozenset[str]] = frozenset({"worker-dispatcher", "dependency-resolver"})
_POLL_INTERVAL_SECONDS: Final[float] = 2.0
_POLLING_INTERVALS: Final[int] = 5

#: `get_consumer_lag(dependency-resolver)` as the v0.6.25 demo stack answered it on 2026-10-10,
#: under the read-only token (`recent_samples` trimmed to one entry).
_RESOLVER_LIVE: Final[dict[str, Any]] = {
    "consumer_group": "dependency-resolver",
    "lag": 0,
    "lag_known": True,
    "source": "live",
    "cache_key": "kafka:consumer_lag:dependency-resolver",
    "measured_at": "2026-10-10T17:58:56.198073Z",
    "age_seconds": 0,
    "recent_samples": [{"lag": 0, "measured_at": "2026-10-10T17:58:56.198073Z"}],
    "last_poll_at": "2026-10-10T17:58:56.194174Z",
    "last_poll_age_seconds": 0,
    "polling": True,
    "poll_interval_seconds": 2.0,
}

#: The same stack's answer for one of the seven recorded-constant groups.
_STATIC_LIVE: Final[dict[str, Any]] = {
    "consumer_group": "billing-consumer",
    "lag": 15000,
    "lag_known": True,
    "source": "static",
    "cache_key": "kafka:consumer_lag:billing-consumer",
    "measured_at": None,
    "age_seconds": None,
    "recent_samples": [],
    "last_poll_at": None,
    "last_poll_age_seconds": None,
    "polling": None,
    "poll_interval_seconds": None,
}


class TestThePollingVerdictParses:
    """The two new fields reach the typed model rather than being thrown away."""

    def test_a_running_consumer_reads_polling_true_with_its_cadence(self) -> None:
        reading = GetConsumerLagOutput.model_validate(_RESOLVER_LIVE)
        assert reading.polling is True
        assert reading.poll_interval_seconds == 2.0

    def test_a_dead_consumer_reads_polling_false_whatever_its_lag_says(self) -> None:
        # The INC-008 addendum's reading on v0.6.25: lag 0, known, fresh — and not polling.
        dead = {
            **_RESOLVER_LIVE,
            "last_poll_at": "2026-10-10T17:58:17.000000Z",
            "last_poll_age_seconds": 39,
            "age_seconds": 3,
            "polling": False,
        }
        reading = GetConsumerLagOutput.model_validate(dead)
        assert reading.lag == 0 and reading.lag_known and reading.source == "live"
        assert reading.polling is False
        assert reading.poll_interval_seconds == 2.0

    def test_a_static_group_reads_null_which_is_unknown_not_false(self) -> None:
        reading = GetConsumerLagOutput.model_validate(_STATIC_LIVE)
        assert reading.polling is None
        assert reading.poll_interval_seconds is None

    def test_a_reading_from_before_v0_6_25_still_parses(self) -> None:
        # Every recording made on v0.6.24 or earlier lacks both fields; they are optional on the
        # platform side too, so an absent field is null — unknown, never a parse failure.
        older = {
            k: v for k, v in _RESOLVER_LIVE.items() if k not in {"polling", "poll_interval_seconds"}
        }
        reading = GetConsumerLagOutput.model_validate(older)
        assert reading.polling is None
        assert reading.poll_interval_seconds is None

    def test_the_verdict_survives_a_round_trip_through_the_model(self) -> None:
        # Kept, never dropped: what the agent's evidence ledger stores is the model's dump.
        dumped = GetConsumerLagOutput.model_validate(
            {**_RESOLVER_LIVE, "polling": False}
        ).model_dump(mode="json")
        assert dumped["polling"] is False
        assert dumped["poll_interval_seconds"] == 2.0

    def test_the_model_declares_both_fields_as_the_snapshot_does(self) -> None:
        output = _snapshot_tool("get_consumer_lag")["outputSchema"]
        properties = output["properties"]
        assert properties["polling"]["anyOf"] == [{"type": "boolean"}, {"type": "null"}]
        assert properties["poll_interval_seconds"]["anyOf"] == [
            {"type": "number"},
            {"type": "null"},
        ]
        assert "polling" not in output["required"]
        assert "poll_interval_seconds" not in output["required"]
        assert list(GetConsumerLagOutput.model_fields)[-2:] == [
            "polling",
            "poll_interval_seconds",
        ]


class TestTheVerdictReplaysUnchanged:
    """A recorded world on v0.6.25 keeps the verdict and the cadence exactly as recorded."""

    def test_the_cadence_is_held_and_the_verdict_is_untouched(self) -> None:
        delta = timedelta(days=1)
        moved = _payload(
            recorded_client.rebase_result(
                "get_consumer_lag", _result({**_RESOLVER_LIVE, "polling": False}), delta
            )
        )
        assert moved["polling"] is False
        assert moved["poll_interval_seconds"] == 2.0
        assert "poll_interval_seconds" in recorded_client.HELD_DURATION_FIELDS["get_consumer_lag"]
        assert "polling" not in recorded_client.HELD_DURATION_FIELDS["get_consumer_lag"]
        assert "polling" not in recorded_client.SHIFTED_CLOCK_FIELDS["get_consumer_lag"]


class TestEveryCannedReadingCarriesTheVerdict:
    """The canned fixtures were brought up to v0.6.25 in one commit, by the platform's own rule.

    RED BEFORE: against the fixtures as v0.6.24 left them, no reading carries either field.
    """

    def test_every_reading_carries_both_fields_by_the_platforms_rule(self) -> None:
        readings = _canned_lag_readings()
        assert len(readings) >= 40, "the walk lost its subject"
        wrong = []
        for label, payload in readings:
            if "polling" not in payload or "poll_interval_seconds" not in payload:
                wrong.append(f"{label}: missing")
            elif (payload["polling"], payload["poll_interval_seconds"]) != _expected(payload):
                wrong.append(
                    f"{label}: {payload['polling']!r} {payload['poll_interval_seconds']!r}"
                )
        assert not wrong, "\n".join(wrong)

    def test_the_corpus_holds_both_verdicts(self) -> None:
        # Running and stopped consumers alike: a corpus of one verdict could not show the agent
        # reading the other.
        verdicts = {payload.get("polling") for _, payload in _canned_lag_readings()}
        assert {True, False, None} <= verdicts

    def test_the_verdict_is_compared_exactly_by_the_drift_walk(self) -> None:
        # `polling` is the evidence a stopped-consumer world rests on, so the drift walk holds it
        # to the live value (a seeded stop is ledgered post-fault, like `lag`), and the interval
        # is a code constant, the same on every stack. Neither is forgiven as volatile.
        assert "polling" not in _VOLATILE["get_consumer_lag"]
        assert "poll_interval_seconds" not in _VOLATILE["get_consumer_lag"]


def _canned_lag_readings() -> list[tuple[str, dict[str, Any]]]:
    return [
        (call.label, dict(call.payload))
        for call in canned_calls(load_scenarios(_SCENARIOS_DIR))
        if call.tool == "get_consumer_lag"
    ]


def _expected(payload: dict[str, Any]) -> tuple[bool | None, float | None]:
    """What the platform answers for this reading: by group NAME, then by poll time."""
    if payload.get("consumer_group") not in _MEASURED_GROUPS:
        return None, None
    last_poll_at, measured_at = payload.get("last_poll_at"), payload.get("measured_at")
    if last_poll_at is None or measured_at is None:
        return None, _POLL_INTERVAL_SECONDS
    gap = (_parse(measured_at) - _parse(last_poll_at)).total_seconds()
    return max(0.0, gap) <= _POLLING_INTERVALS * _POLL_INTERVAL_SECONDS, _POLL_INTERVAL_SECONDS


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _snapshot_tool(name: str) -> dict[str, Any]:
    tools = json.loads(_SNAPSHOT.read_text())["tools"]
    return next(tool for tool in tools if tool["name"] == name)


def _result(payload: dict[str, Any]) -> ToolResult:
    return ToolResult(content=[{"type": "text", "text": json.dumps(payload)}])


def _payload(result: ToolResult) -> dict[str, Any]:
    text = result.content[0]["text"]
    assert isinstance(text, str)
    parsed = json.loads(text)
    assert isinstance(parsed, dict)
    return parsed

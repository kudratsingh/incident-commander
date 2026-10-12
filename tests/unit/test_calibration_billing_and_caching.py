"""WO-R3-384 + WO-R3-385 (owner O-62): billed usage on every calibration, and its prompt caching.

Free and hermetic: a scripted fake judge, or the real ``LLMClient`` over a mocked SDK. No key, no
stack, no model.

384 — every calibration report sums the API's own usage block per call (input, output, cache-write,
cache-read), per leg and per model, prices it with the pinned map, stores ``usage`` and
``usd_used`` in the JSON and prints ``billed $X.XX (N calls, cache-read P%)`` on its summary line.
A report written before this reads ``usd_used: None``, never ``0``.

385 — the paid path's client marks the rubric (system prompt) on every call, and the question too
when it is asked more than once, so the second ask of a question carries the marker and its cache
read is counted, and charged at the cache-read rate (ADR 0084). The eval-time judge keeps the
agent's system-prompt marker, and the runner's meter prices its cache reads at the cache price.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, Final
from unittest.mock import MagicMock

import pytest

from evals.graders.llm_judge import JUDGE_PROMPT, judge_briefing
from evals.judge_calibration import __main__ as cli
from evals.judge_calibration.billing import (
    LABEL_LEG,
    TRAP_LEG,
    billed_line,
    billed_of,
    usage_block,
)
from evals.judge_calibration.fakes import FakeJudgeClient, answers_for, answers_for_labels
from evals.judge_calibration.harness import LIVE_CLIENT, calibrate, live_client
from evals.judge_calibration.labels import LABELS_FILE, load_briefing
from evals.judge_calibration.roles import (
    ACTION_VERIFIER,
    BRIEFING_JUDGE,
    CALIBRATED_ROLES,
    CANDIDATE_SELECTOR,
)
from evals.judge_calibration.traps import traps_for
from incident_commander.agent.accounting import RunAccounting
from incident_commander.llm import pricing
from incident_commander.llm.client import CACHE_CONTROL, LLMClient, LLMOutputError, LLMUsage
from incident_commander.llm.prompts.loader import load_prompt

_HAIKU: Final[str] = "claude-haiku-4-5"
_SONNET: Final[str] = "claude-sonnet-4-6"
_REPORTS: Final[Path] = Path("evals/reports/judge-calibration/briefing_judge")
#: The two newest committed reports, written before WO-R3-384: read, never written.
_OLD_REPORTS: Final[tuple[str, ...]] = (
    "briefing_judge.20261012T051538Z.1b137efa119f.json",
    "briefing_judge.20261012T074929Z.b31b8e836163.json",
)

#: One fake usage block, the shape a cached ask returns: a little fresh input, most from cache.
_CACHED_ASK: Final[LLMUsage] = LLMUsage(
    input_tokens=200, output_tokens=150, cache_creation_tokens=0, cache_read_tokens=3_400
)


def _expected_usd(model: str, usage: LLMUsage, calls: int) -> float:
    return float(pricing.cost_of(model, usage) * calls)


# ---------------------------------------------------------------------------
# 384: the billed figure


class TestEveryReportRecordsItsBilledUsage:
    @pytest.mark.parametrize("model", [_HAIKU, _SONNET])
    def test_usage_is_summed_and_priced_per_model(self, model: str) -> None:
        client = FakeJudgeClient(answers_for(ACTION_VERIFIER), usage=_CACHED_ASK)
        report = calibrate(ACTION_VERIFIER, client=client, model=model, reps=2)
        calls = len(traps_for(ACTION_VERIFIER)) * 2
        usage = report.usage
        assert usage is not None
        assert usage["calls"] == calls
        assert usage["failed_calls"] == 0
        assert usage["input_tokens"] == 200 * calls
        assert usage["output_tokens"] == 150 * calls
        assert usage["cache_read_tokens"] == 3_400 * calls
        assert usage["cache_write_tokens"] == 0
        assert usage["cache_read_share"] == round(3_400 / 3_600, 4)
        assert usage["usd_used"] == pytest.approx(_expected_usd(model, _CACHED_ASK, calls))
        assert list(usage["by_model"]) == [model]
        assert usage["by_model"][model]["usd_used"] == usage["usd_used"]
        assert (
            usage["by_model"][model]["cache_minimum_tokens"]
            == (pricing.CACHE_MINIMUM_TOKENS[model])
        )
        assert list(usage["by_leg"]) == [TRAP_LEG]

    def test_the_two_models_bill_the_same_tokens_differently(self) -> None:
        bills = {
            model: calibrate(
                ACTION_VERIFIER,
                client=FakeJudgeClient(answers_for(ACTION_VERIFIER), usage=_CACHED_ASK),
                model=model,
                reps=1,
            ).usage
            for model in (_HAIKU, _SONNET)
        }
        haiku, sonnet = (bills[m] for m in (_HAIKU, _SONNET))
        assert haiku is not None and sonnet is not None
        assert haiku["calls"] == sonnet["calls"]
        # Sonnet 4.6 is 3x Haiku 4.5 on every token class.
        assert sonnet["usd_used"] == pytest.approx(3 * haiku["usd_used"])

    def test_cache_reads_are_charged_at_the_cache_read_rate(self) -> None:
        reads_only = LLMUsage(cache_read_tokens=1_000_000)
        report = calibrate(
            ACTION_VERIFIER,
            client=FakeJudgeClient(answers_for(ACTION_VERIFIER), usage=reads_only),
            model=_HAIKU,
            reps=1,
        )
        assert report.usage is not None
        # $0.10 per million cache-read tokens on Haiku 4.5, not the $1.00 input rate.
        assert report.usage["usd_used"] == pytest.approx(0.10 * report.usage["calls"])

    def test_the_json_carries_usage_and_usd_used(self) -> None:
        report = calibrate(
            ACTION_VERIFIER,
            client=FakeJudgeClient(answers_for(ACTION_VERIFIER), usage=_CACHED_ASK),
            model=_HAIKU,
            reps=1,
        )
        payload = json.loads(report.to_json())
        assert payload["usd_used"] == payload["usage"]["usd_used"] > 0
        assert payload["usage"]["means"].startswith("the API's own usage block")
        assert billed_of(payload)["usd_used"] == payload["usd_used"]

    def test_the_label_leg_is_billed_on_its_own(self) -> None:
        script = answers_for(BRIEFING_JUDGE) | answers_for_labels(LABELS_FILE)
        report = calibrate(
            BRIEFING_JUDGE,
            client=FakeJudgeClient(script, usage=_CACHED_ASK),
            model=_HAIKU,
            reps=1,
            labels=LABELS_FILE,
            packet=2,
            dev_packet=1,
        )
        usage = report.usage
        assert usage is not None
        legs = usage["by_leg"]
        assert set(legs) == {LABEL_LEG, TRAP_LEG}
        assert legs[TRAP_LEG]["calls"] == len(traps_for(BRIEFING_JUDGE))
        assert legs[LABEL_LEG]["calls"] == 13 + 16  # held-out packet 2 + dev packet 1
        assert legs[LABEL_LEG]["calls"] + legs[TRAP_LEG]["calls"] == usage["calls"]
        assert legs[LABEL_LEG]["usd_used"] + legs[TRAP_LEG]["usd_used"] == pytest.approx(
            usage["usd_used"]
        )

    def test_a_billed_call_that_failed_is_still_charged(self) -> None:
        class Malformed:
            """Every answer comes back billed and unreadable; the repair re-ask too."""

            def call(self, *_: Any, model: str, **__: Any) -> Any:
                raise LLMOutputError("unreadable", usage=_CACHED_ASK)

        report = calibrate(ACTION_VERIFIER, client=Malformed(), model=_HAIKU, reps=1)  # type: ignore[arg-type]
        usage = report.usage
        assert usage is not None
        assert report.trap_agreement["answered"] == 0
        # One ask and one repair re-ask per case, both billed, both failed.
        assert usage["failed_calls"] == usage["calls"] == 2 * len(traps_for(ACTION_VERIFIER))
        assert usage["usd_used"] == pytest.approx(
            _expected_usd(_HAIKU, _CACHED_ASK, usage["calls"])
        )


class TestTheSummaryLine:
    def test_format(self) -> None:
        usage = {"usd_used": 1.23456, "calls": 130, "cache_read_share": 0.714}
        assert billed_line(usage) == "billed $1.23 (130 calls, cache-read 71%)"

    def test_no_prompt_tokens_reads_n_a(self) -> None:
        usage = usage_block([], question_cached=False)
        assert billed_line(usage) == "billed $0.00 (0 calls, cache-read n/a)"

    def test_the_free_path_prints_it_on_the_first_line_without_a_price_warning(
        self, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
    ) -> None:
        pricing._warned_models.clear()
        with caplog.at_level(logging.WARNING, logger=pricing.__name__):
            assert cli.main(["--judge", ACTION_VERIFIER]) == 0
        first = capsys.readouterr().out.splitlines()[0]
        calls = 5 * len(traps_for(ACTION_VERIFIER))
        assert first.endswith(f"billed $0.00 ({calls} calls, cache-read n/a)")
        # The script's made-up model id is not an unpriced model: it billed nothing.
        assert "no pinned price row" not in caplog.text

    def test_the_label_leg_prints_its_own_billed_line(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        argv = ["--judge", BRIEFING_JUDGE, "--packet", "2", "--dev-packet", "1", "--reps", "1"]
        assert cli.main(argv) == 0
        out = capsys.readouterr().out
        assert "  label leg   billed $0.00 (29 calls, cache-read n/a)" in out


class TestAReportFromBeforeReadsNull:
    @pytest.mark.parametrize("name", _OLD_REPORTS)
    def test_usd_used_is_none_not_zero(self, name: str) -> None:
        payload = json.loads((_REPORTS / name).read_text(encoding="utf-8"))
        assert "usage" not in payload
        billed = billed_of(payload)
        assert billed["usd_used"] is None
        assert billed["usage"] is None
        assert billed_line(billed["usage"]).startswith("billed n/a")


# ---------------------------------------------------------------------------
# 385: prompt caching on the judge and selector calls


def _message(payload: Mapping[str, Any], *, write: int, read: int) -> MagicMock:
    """The SDK ``Message`` shape ``LLMClient`` reads: one forced tool call and a usage block."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = "record_output"
    block.input = dict(payload)
    response = MagicMock()
    response.content = [block]
    response.stop_reason = "tool_use"
    response.usage.input_tokens = 40
    response.usage.output_tokens = 120
    response.usage.cache_creation_input_tokens = write
    response.usage.cache_read_input_tokens = read
    return response


def _question(body: Mapping[str, Any]) -> str:
    content = body["messages"][0]["content"]
    return content if isinstance(content, str) else content[0]["text"]


class _CachingSDK:
    """A mocked Anthropic SDK that answers each trap from its script and caches like the API.

    The first time it sees a prompt (rubric + question) it reports a cache WRITE of the prompt;
    every later identical prompt reports a cache READ — the behaviour the markers buy on a model
    whose minimum the prefix clears. ``bodies`` keeps every request for the assertions.
    """

    PROMPT_TOKENS: Final[int] = 3_400

    def __init__(self, judge: str) -> None:
        self._script = answers_for(judge)
        self._seen: set[tuple[str, str]] = set()
        self.bodies: list[dict[str, Any]] = []
        self.messages = MagicMock()
        self.messages.create.side_effect = self._create

    def _create(self, **body: Any) -> MagicMock:
        self.bodies.append(body)
        key = (body["system"][0]["text"], _question(body))
        cached = key in self._seen
        self._seen.add(key)
        payload = self._script[_question(body)][0]
        size = self.PROMPT_TOKENS
        return _message(payload, write=0 if cached else size, read=size if cached else 0)


class TestTheSecondAskReadsTheCache:
    @pytest.mark.parametrize("judge", CALIBRATED_ROLES)
    def test_every_call_marks_the_rubric_and_the_question(self, judge: str) -> None:
        sdk = _CachingSDK(judge)
        calibrate(
            judge,
            client=live_client("not-a-key", reps=2, sdk=sdk),
            model=_SONNET,
            reps=2,
            client_kind=LIVE_CLIENT,
        )
        assert len(sdk.bodies) == 2 * len(traps_for(judge))
        for body in sdk.bodies:
            assert body["system"][0]["cache_control"] == CACHE_CONTROL
            assert body["messages"][0]["content"][0]["cache_control"] == CACHE_CONTROL
        # Ask 2 of each question is byte-identical to ask 1, so the marked prefix matches.
        first, second = sdk.bodies[0], sdk.bodies[1]
        assert second["system"] == first["system"]
        assert second["messages"] == first["messages"]

    @pytest.mark.parametrize("judge", CALIBRATED_ROLES)
    def test_the_usage_sum_counts_the_cache_reads(self, judge: str) -> None:
        sdk = _CachingSDK(judge)
        report = calibrate(
            judge,
            client=live_client("not-a-key", reps=2, sdk=sdk),
            model=_SONNET,
            reps=2,
            client_kind=LIVE_CLIENT,
        )
        cases = len(traps_for(judge))
        usage = report.usage
        assert usage is not None
        assert usage["cache_markers"] == {"rubric": True, "question": True}
        assert usage["cache_write_tokens"] == cases * _CachingSDK.PROMPT_TOKENS
        assert usage["cache_read_tokens"] == cases * _CachingSDK.PROMPT_TOKENS
        written = LLMUsage(input_tokens=40, output_tokens=120, cache_creation_tokens=3_400)
        read = LLMUsage(input_tokens=40, output_tokens=120, cache_read_tokens=3_400)
        expected = (pricing.cost_of(_SONNET, written) + pricing.cost_of(_SONNET, read)) * cases
        assert Decimal(str(usage["usd_used"])) == expected

    def test_the_selector_asks_with_its_own_prompt_marked(self) -> None:
        sdk = _CachingSDK(CANDIDATE_SELECTOR)
        calibrate(
            CANDIDATE_SELECTOR,
            client=live_client("not-a-key", reps=2, sdk=sdk),
            model=_SONNET,
            reps=2,
            client_kind=LIVE_CLIENT,
        )
        assert {body["system"][0]["text"] for body in sdk.bodies} == {
            load_prompt("candidate_selector")
        }

    def test_one_ask_per_question_leaves_the_question_unmarked(self) -> None:
        sdk = _CachingSDK(ACTION_VERIFIER)
        report = calibrate(
            ACTION_VERIFIER,
            client=live_client("not-a-key", reps=1, sdk=sdk),
            model=_SONNET,
            reps=1,
            client_kind=LIVE_CLIENT,
        )
        for body in sdk.bodies:
            assert body["system"][0]["cache_control"] == CACHE_CONTROL
            assert isinstance(body["messages"][0]["content"], str)
        assert report.usage is not None
        assert report.usage["cache_markers"] == {"rubric": True, "question": False}


class TestTheEvalTimeJudge:
    """The runner's judge is a plain ``LLMClient`` behind ``RunAccounting.meter``."""

    def _sdk(self) -> MagicMock:
        payload = {"groundedness": 0.9, "actionability": 0.9, "reasoning": "no check fired"}
        sdk = MagicMock()
        sdk.messages.create.side_effect = [
            _message(payload, write=3_400, read=0),
            _message(payload, write=0, read=3_400),
        ]
        return sdk

    def test_its_rubric_is_marked_and_its_cache_reads_are_charged_at_the_cache_price(
        self,
    ) -> None:
        sdk = self._sdk()
        accounting = RunAccounting()
        judge = accounting.meter(
            LLMClient(api_key="not-a-key", client=sdk), "briefing_judge", charged_to_ledger=False
        )
        briefing = load_briefing(_first_labelled_briefing())
        for _ in range(2):
            judge_briefing(briefing, judge, _HAIKU)
        bodies = [call.kwargs for call in sdk.messages.create.call_args_list]
        assert [b["system"][0]["text"] for b in bodies] == [load_prompt(JUDGE_PROMPT)] * 2
        assert all(b["system"][0]["cache_control"] == CACHE_CONTROL for b in bodies)
        # The runner's judge asks each briefing once, so the question stays unmarked there.
        assert all(isinstance(b["messages"][0]["content"], str) for b in bodies)
        second = accounting.calls[1]
        assert second.cache_read_tokens == 3_400
        # Haiku 4.5: 40 x $1.00 + 120 x $5.00 + 3,400 x $0.10 (the cache-read rate), per million.
        assert second.usd_used == Decimal("0.000980")


def _first_labelled_briefing() -> str:
    from evals.judge_calibration import label_leg

    return str(label_leg.load_rows(LABELS_FILE)[0]["id"])


class TestPricingTable:
    def test_every_priced_model_states_its_cache_minimum(self) -> None:
        assert set(pricing.CACHE_MINIMUM_TOKENS) == set(pricing.MODEL_PRICING)

    def test_haiku_needs_more_than_the_rubric(self) -> None:
        # Why ADR 0084 marks the question: the briefing rubric alone is under Haiku's minimum.
        rubric_chars = len(load_prompt(JUDGE_PROMPT))
        assert rubric_chars / 4 < pricing.CACHE_MINIMUM_TOKENS[_HAIKU]
        assert rubric_chars / 4 > pricing.CACHE_MINIMUM_TOKENS[_SONNET]

    def test_a_zero_usage_call_costs_nothing_and_looks_nothing_up(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        pricing._warned_models.clear()
        with caplog.at_level(logging.WARNING, logger=pricing.__name__):
            assert pricing.cost_of("fake-judge", LLMUsage()) == Decimal("0")
        assert caplog.text == ""

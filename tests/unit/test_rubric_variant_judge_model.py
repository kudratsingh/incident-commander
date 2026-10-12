"""WO-R3-383 (owner O-61 item 3): a rubric variant and a judge-model override for one trial.

The owner ordered one trial: rubric v2 with its "derived, not copied" check removed (that pattern
is a deterministic lookup now, WO-R3-382 / ADR 0083), asked on claude-sonnet-4-6 instead of
``JUDGE_MODEL``, scored on packet 2 as held out with packet 1 as dev. Pinned here, free and
hermetic (a scripted fake, no key, no stack):

* the variant is its own prompt file, v2 minus exactly that check's two lines, and v2's served
  hash does not move;
* ``RUBRIC=`` puts every trap and every label under the variant, and the report records the file
  name, its sha256 and what it drops;
* ``JUDGE_MODEL_OVERRIDE=`` reaches the client on the judge legs only, and the report says
  ``model_setting: JUDGE_MODEL_OVERRIDE``;
* the free path prints the exact paid command;
* the leaderboard's judge gate refuses a report made on another model or on a variant rubric,
  while today's registered report (claude-haiku-4-5, the production rubric) still opens it.
"""

from __future__ import annotations

import difflib
import hashlib
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest

from evals import research_report as research
from evals.graders.llm_judge import (
    BRIEFING_JUDGE_CHECKS,
    BRIEFING_JUDGE_RUBRICS,
    COPIED_CHECK,
    JUDGE_PROMPT,
    MINUS_COPIED_RUBRIC,
    UnknownRubricError,
    checks_of,
    judge_briefing,
)
from evals.judge_calibration.fakes import FakeJudgeClient, answers_for, answers_for_labels
from evals.judge_calibration.harness import (
    FAKE_CLIENT,
    JUDGE_MODEL_OVERRIDE_SETTING,
    LIVE_CLIENT,
    calibrate,
    leg_model,
    write_report,
)
from evals.judge_calibration.labels import LABELS_FILE
from evals.judge_calibration.roles import (
    ACTION_VERIFIER,
    BRIEFING_JUDGE,
    CALIBRATED_ROLES,
    CANDIDATE_SELECTOR,
)
from evals.judge_calibration.traps import BRIEFING_CHECK_TRAPS, traps_for
from evals.research_report import LEADERBOARD_JUDGE, Row, arm_summary, judge_calibration_gate
from incident_commander.config import Settings
from incident_commander.llm.prompts.loader import load_prompt, raw_prompt
from tests.unit.test_runner import _test_settings

_HAIKU: Final[str] = "claude-haiku-4-5"
_SONNET: Final[str] = "claude-sonnet-4-6"
#: rubric v2's served-text hash (WO-R3-378); this order must not move it.
_V2_SERVED_SHA: Final[str] = "9f684d7446ce8fbc4904eeaf0366be65a9a4f61d8d314ce781d0a3dc7af4e963"
#: The calibration report's rubric sha for v2 (over the served lines), from report 1b137efa119f.
_V2_REPORT_SHA_PREFIX: Final[str] = "20230ebc148a"
#: The trial the owner pre-authorised (O-61), as the free path must print it.
_PAID: Final[str] = (
    "make judge-calibration JUDGE=briefing_judge LABELS=1 PACKET=2 DEV_PACKET=1 "
    "RUBRIC=briefing_judge.v2-minus-copied JUDGE_MODEL_OVERRIDE=claude-sonnet-4-6 "
    "LIVE=1 YES_SPEND=1 WRITE=1"
)
_TRIAL_ARGV: Final[list[str]] = [
    "--judge",
    BRIEFING_JUDGE,
    "--packet",
    "2",
    "--dev-packet",
    "1",
    "--rubric",
    MINUS_COPIED_RUBRIC,
    "--judge-model-override",
    _SONNET,
]


def _agreeing() -> FakeJudgeClient:
    return FakeJudgeClient(answers_for(BRIEFING_JUDGE))


# ---------------------------------------------------------------------------
# The variant file


class TestTheVariantIsV2MinusOneCheck:
    def test_the_variant_loads_and_is_registered(self) -> None:
        assert MINUS_COPIED_RUBRIC == "briefing_judge.v2-minus-copied"
        assert load_prompt(MINUS_COPIED_RUBRIC)
        assert set(BRIEFING_JUDGE_RUBRICS) == {JUDGE_PROMPT, MINUS_COPIED_RUBRIC}
        assert checks_of(JUDGE_PROMPT) == BRIEFING_JUDGE_CHECKS

    def test_its_checks_are_v2s_without_the_copied_one_in_order(self) -> None:
        assert COPIED_CHECK == "derived, not copied"
        assert checks_of(MINUS_COPIED_RUBRIC) == (
            "invented fact",
            "blind probe",
            "a guess is not a finding",
            "look at X, do nothing",
        )
        text = load_prompt(MINUS_COPIED_RUBRIC)
        positions = [text.find(f"**{name}**") for name in checks_of(MINUS_COPIED_RUBRIC)]
        assert all(p >= 0 for p in positions) and positions == sorted(positions)
        assert f"**{COPIED_CHECK}**" not in text
        # Its example went with it: the variant quotes no copied-row briefing.
        assert "0db6fe722f7c:deploy_correlation" not in text

    def test_it_differs_from_v2_by_exactly_that_check_and_its_example(self) -> None:
        v2 = raw_prompt(JUDGE_PROMPT).splitlines()
        variant = raw_prompt(MINUS_COPIED_RUBRIC).splitlines()
        removed = [line for line in difflib.ndiff(v2, variant) if line.startswith("- ")]
        added = [line for line in difflib.ndiff(v2, variant) if line.startswith("+ ")]
        assert added == []
        assert len(removed) == 2
        assert removed[0].startswith(f"- 2. **{COPIED_CHECK}**")
        assert removed[1].startswith("-    Example: `0db6fe722f7c:deploy_correlation`")
        # Every other byte is v2's, in v2's order.
        assert [line for line in v2 if f"- {line}" not in removed] == variant

    def test_its_hash_differs_and_v2s_does_not_move(self) -> None:
        def served(name: str) -> str:
            return hashlib.sha256(load_prompt(name).encode("utf-8")).hexdigest()

        assert served(JUDGE_PROMPT) == _V2_SERVED_SHA
        assert served(MINUS_COPIED_RUBRIC) != _V2_SERVED_SHA

    def test_an_unknown_rubric_is_refused(self) -> None:
        with pytest.raises(UnknownRubricError, match="no briefing-judge rubric"):
            checks_of("briefing_judge.v9")
        with pytest.raises(UnknownRubricError):
            judge_briefing(
                traps_for(BRIEFING_JUDGE)[0].subject.briefing,  # type: ignore[attr-defined]
                _agreeing(),
                "fake-judge",
                rubric="verification_judge",
            )

    def test_the_production_judge_still_loads_v2(self) -> None:
        """No caller but the calibration names a rubric: the judge a run calls is unchanged."""
        client = _agreeing()
        case = traps_for(BRIEFING_JUDGE)[0]
        case.subject.ask(client=client, model="fake-judge")
        assert {system for system, _ in client.calls} == {load_prompt(JUDGE_PROMPT)}


# ---------------------------------------------------------------------------
# The report


class TestTheReportRecordsTheRubric:
    def test_the_default_report_is_v2_as_before(self) -> None:
        report = calibrate(BRIEFING_JUDGE, client=_agreeing(), model="fake-judge")
        rubric = report.to_dict()["rubric"]
        assert rubric["prompt"] == JUDGE_PROMPT
        assert rubric["file"] == "briefing_judge.md"
        assert rubric["sha256"].startswith(_V2_REPORT_SHA_PREFIX)
        assert rubric["checks"] == list(BRIEFING_JUDGE_CHECKS)
        assert "variant_of" not in rubric

    def test_a_variant_report_names_the_file_its_sha_and_what_it_drops(self) -> None:
        report = calibrate(
            BRIEFING_JUDGE, client=_agreeing(), model="fake-judge", rubric=MINUS_COPIED_RUBRIC
        )
        rubric = report.to_dict()["rubric"]
        body = "\n".join(load_prompt(MINUS_COPIED_RUBRIC).splitlines())
        assert rubric["prompt"] == MINUS_COPIED_RUBRIC
        assert rubric["file"] == "briefing_judge.v2-minus-copied.md"
        assert rubric["sha256"] == hashlib.sha256(body.encode("utf-8")).hexdigest()
        assert not rubric["sha256"].startswith(_V2_REPORT_SHA_PREFIX)
        assert rubric["lines"] == 37
        assert rubric["checks"] == list(checks_of(MINUS_COPIED_RUBRIC))
        assert rubric["variant_of"] == JUDGE_PROMPT
        assert rubric["production"] is False
        assert rubric["removed_checks"] == [COPIED_CHECK]
        assert rubric["traps_for_removed_checks"] == list(BRIEFING_CHECK_TRAPS[COPIED_CHECK])
        assert "ADR 0083" in rubric["why_removed"]

    def test_every_trap_is_asked_under_the_variant(self) -> None:
        """All ten traps stay in (comparable with v2's reports); only the system prompt moves."""
        client = _agreeing()
        report = calibrate(
            BRIEFING_JUDGE, client=client, model="fake-judge", reps=1, rubric=MINUS_COPIED_RUBRIC
        )
        assert len(client.calls) == len(traps_for(BRIEFING_JUDGE)) == 10
        assert {system for system, _ in client.calls} == {load_prompt(MINUS_COPIED_RUBRIC)}
        assert report.trap_agreement["cases"] == 10

    def test_every_label_is_asked_under_the_variant(self) -> None:
        script = answers_for(BRIEFING_JUDGE) | answers_for_labels(LABELS_FILE)
        client = FakeJudgeClient(script)
        report = calibrate(
            BRIEFING_JUDGE,
            client=client,
            model="fake-judge",
            reps=1,
            labels=LABELS_FILE,
            packet=2,
            dev_packet=1,
            rubric=MINUS_COPIED_RUBRIC,
        )
        assert {system for system, _ in client.calls} == {load_prompt(MINUS_COPIED_RUBRIC)}
        leg = report.label_agreement
        assert leg is not None
        assert leg["value"]["n"] == 13  # packet 2's held-out briefings
        assert leg["dev_set"]["n"] == 16  # packet 1, NOT COUNTED
        assert len(client.calls) == 10 + 13 + 16

    @pytest.mark.parametrize("judge", [ACTION_VERIFIER, CANDIDATE_SELECTOR])
    def test_a_rubric_on_another_judge_is_refused(self, judge: str) -> None:
        with pytest.raises(ValueError, match="rubric variant exists for briefing_judge only"):
            calibrate(
                judge,
                client=FakeJudgeClient(answers_for(judge)),
                model="fake-judge",
                rubric=MINUS_COPIED_RUBRIC,
            )


# ---------------------------------------------------------------------------
# The model override


def _settings(**overrides: Any) -> Settings:
    return _test_settings(judge_model=_HAIKU, development_model=_HAIKU, **overrides)


def _run_live(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], *, labels: bool = False
) -> tuple[int, FakeJudgeClient]:
    """``main`` down its PAID path with Settings and the client swapped for one scripted fake."""
    from evals.judge_calibration.__main__ import main

    script: dict[str, Any] = {}
    for judge in CALIBRATED_ROLES:
        script |= answers_for(judge)
    if labels:
        script |= answers_for_labels(LABELS_FILE)
    client = FakeJudgeClient(script)
    settings = _settings()
    monkeypatch.setattr("incident_commander.config.get_settings", lambda: settings)
    monkeypatch.setattr("incident_commander.llm.client.LLMClient", lambda **_kwargs: client)
    return main([*argv, "--live", "--yes-spend", "--reps", "1"]), client


class TestTheJudgeModelOverride:
    def test_the_override_reaches_the_client_on_the_paid_path(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, client = _run_live(monkeypatch, _TRIAL_ARGV, labels=True)
        assert code == 0
        assert client.calls
        assert set(client.models) == {_SONNET}
        assert {system for system, _ in client.calls} == {load_prompt(MINUS_COPIED_RUBRIC)}
        out = capsys.readouterr().out
        assert f"model       {_SONNET}  from JUDGE_MODEL_OVERRIDE={_SONNET}, not JUDGE_MODEL" in out
        assert "VARIANT    of briefing_judge.md" in out
        assert "paid command" not in out  # it was the paid command

    def test_without_the_override_the_judge_model_is_asked_as_before(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        code, client = _run_live(monkeypatch, ["--judge", BRIEFING_JUDGE])
        assert code == 0
        assert set(client.models) == {_HAIKU}

    def test_the_override_reaches_the_client_through_calibrate(self) -> None:
        client = _agreeing()
        report = calibrate(
            BRIEFING_JUDGE,
            client=client,
            model=_SONNET,
            reps=1,
            client_kind=LIVE_CLIENT,
            judge_model_override=_SONNET,
        )
        assert set(client.models) == {_SONNET}
        payload = report.to_dict()
        assert payload["model"] == _SONNET
        assert payload["model_setting"] == JUDGE_MODEL_OVERRIDE_SETTING == "JUDGE_MODEL_OVERRIDE"
        assert payload["judge_model_override"]["model"] == _SONNET
        assert payload["judge_model_override"]["replaces"] == "JUDGE_MODEL"

    def test_a_report_without_the_override_says_judge_model(self) -> None:
        payload = calibrate(BRIEFING_JUDGE, client=_agreeing(), model=_HAIKU).to_dict()
        assert payload["model_setting"] == "JUDGE_MODEL"
        assert "judge_model_override" not in payload

    def test_a_live_leg_handed_another_model_is_refused(self) -> None:
        with pytest.raises(ValueError, match="a model nobody asked"):
            calibrate(
                BRIEFING_JUDGE,
                client=_agreeing(),
                model=_HAIKU,
                client_kind=LIVE_CLIENT,
                judge_model_override=_SONNET,
            )

    def test_leg_model_gives_the_judge_legs_the_override_and_refuses_the_selector(self) -> None:
        settings = _settings()
        for judge in (ACTION_VERIFIER, BRIEFING_JUDGE):
            assert leg_model(judge, settings, None, _SONNET) == _SONNET
            assert leg_model(judge, settings, None) == _HAIKU
        with pytest.raises(ValueError, match="judge legs only"):
            leg_model(CANDIDATE_SELECTOR, settings, None, _SONNET)
        with pytest.raises(ValueError, match="judge legs only"):
            calibrate(
                CANDIDATE_SELECTOR,
                client=FakeJudgeClient(answers_for(CANDIDATE_SELECTOR)),
                model="fake-judge",
                judge_model_override=_SONNET,
            )

    def test_the_free_path_records_the_override_and_asks_the_script(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from evals.judge_calibration.__main__ import main

        assert main([*_TRIAL_ARGV, "--reps", "1"]) == 0
        out = capsys.readouterr().out
        assert f"(scripted; a live run asks JUDGE_MODEL_OVERRIDE={_SONNET}, not JUDGE_MODEL)" in out
        assert "client fake" in out


# ---------------------------------------------------------------------------
# The CLI: the paid command, and the refusals


class TestTheFreePathPrintsThePaidCommand:
    def test_the_trial_prints_the_exact_pre_authorised_command(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from evals.judge_calibration.__main__ import main

        assert main(_TRIAL_ARGV) == 0
        out = capsys.readouterr().out
        lines = [line for line in out.splitlines() if line.startswith("paid command")]
        assert len(lines) == 1
        assert lines[0].endswith(f": {_PAID}")
        assert "SPENDS MONEY" in lines[0]

    def test_a_plain_free_run_prints_its_own_paid_command(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from evals.judge_calibration.__main__ import main

        assert main(["--judge", ACTION_VERIFIER]) == 0
        out = capsys.readouterr().out
        assert out.rstrip().endswith(
            "make judge-calibration JUDGE=action_verifier LIVE=1 YES_SPEND=1 WRITE=1"
        )

    def test_the_makefile_passes_both_variables(self) -> None:
        makefile = (research.REPO_ROOT / "Makefile").read_text()
        assert "$(if $(RUBRIC),--rubric $(RUBRIC),)" in makefile
        assert (
            "$(if $(JUDGE_MODEL_OVERRIDE),--judge-model-override $(JUDGE_MODEL_OVERRIDE),)"
            in makefile
        )


class TestTheCliRefuses:
    @pytest.mark.parametrize(
        ("argv", "said"),
        [
            (["--rubric", MINUS_COPIED_RUBRIC], "--rubric (RUBRIC=) names a briefing_judge"),
            (
                ["--judge", ACTION_VERIFIER, "--rubric", MINUS_COPIED_RUBRIC],
                "--rubric (RUBRIC=) names a briefing_judge",
            ),
            (["--judge-model-override", _SONNET], "judge legs only"),
            (["--judge", CANDIDATE_SELECTOR, "--judge-model-override", _SONNET], "judge legs only"),
            (
                ["--judge", BRIEFING_JUDGE, "--judge-model-override", "claude-sonnet-9"],
                "has no row in MODEL_PRICING",
            ),
        ],
    )
    def test_before_any_call(
        self, argv: list[str], said: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from evals.judge_calibration.__main__ import main

        assert main(argv) == 2
        assert said in capsys.readouterr().err

    def test_an_unknown_rubric_is_refused_by_the_parser(self) -> None:
        from evals.judge_calibration.__main__ import main

        with pytest.raises(SystemExit):
            main(["--judge", BRIEFING_JUDGE, "--rubric", "briefing_judge.v9"])


# ---------------------------------------------------------------------------
# The leaderboard's judge gate


_ARM: Final[tuple[str, str, str]] = ("baseline", "benchmark", "live")


def _rows(judge_model: str = _HAIKU) -> list[Row]:
    return [
        Row(
            archive="aaaaaaaaaaaa",
            scenario="remediate_dlq_backlog_success",
            agent_model=_SONNET,
            strategy="baseline",
            model_role="benchmark",
            execution_mode="live",
            group={"family": "dlq", "difficulty": "medium"},
            passed=True,
            dimensions=(("outcome", True, "terminal state resolved matched expectation"),),
            root_cause_graded=True,
            root_cause_correct=True,
            tool_calls=4,
            max_tool_calls=13,
            tokens=18_000,
            usd=Decimal("0.13"),
            wall_seconds=41.2,
            judge_overall=0.85,
            judge_model=judge_model,
        )
    ]


def _register(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    model: str,
    rubric: str | None = None,
    override: str | None = None,
) -> str:
    """Write one calibration report under ``root`` and register it as the leaderboard judge's."""
    report = calibrate(
        BRIEFING_JUDGE,
        client=_agreeing(),
        model=model,
        client_kind=FAKE_CLIENT,
        report_id="abcdef012345",
        rubric=rubric,
        judge_model_override=override,
    )
    write_report(report, root=root)
    monkeypatch.setattr(
        "evals.research_report.JUDGE_CALIBRATION_REPORTS", {LEADERBOARD_JUDGE: "abcdef012345"}
    )
    return "abcdef012345"


class TestTheJudgeGateRefusesAnotherModel:
    def test_a_sonnet_override_report_is_refused_for_a_haiku_judged_leaderboard(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        report_id = _register(
            monkeypatch, tmp_path, model=_SONNET, rubric=MINUS_COPIED_RUBRIC, override=_SONNET
        )
        summary = arm_summary(_ARM, _rows(_HAIKU), root=tmp_path)
        assert summary["judge_calibration_report_id"] is None
        assert summary["judge_mean_overall"].startswith("withheld: ")
        assert report_id in summary["judge_calibration_refused"]

    def test_the_model_alone_refuses_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Production rubric, Sonnet model, rows judged on Haiku: refused, both models named."""
        _register(monkeypatch, tmp_path, model=_SONNET, override=_SONNET)
        gate = judge_calibration_gate(LEADERBOARD_JUDGE, [_HAIKU], root=tmp_path)
        assert not gate["opens"]
        assert gate["model"] == _SONNET
        assert _SONNET in gate["why_not"] and _HAIKU in gate["why_not"]
        summary = arm_summary(_ARM, _rows(_HAIKU), root=tmp_path)
        assert summary["judge_mean_overall"] == f"withheld: {gate['why_not']} (plan 02:243)"

    def test_the_variant_rubric_alone_refuses_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Even on the leaderboard's own model, a variant is not the judge every run calls."""
        _register(monkeypatch, tmp_path, model=_HAIKU, rubric=MINUS_COPIED_RUBRIC)
        gate = judge_calibration_gate(LEADERBOARD_JUDGE, [_HAIKU], root=tmp_path)
        assert not gate["opens"]
        assert MINUS_COPIED_RUBRIC in gate["why_not"]

    def test_it_opens_only_when_the_leaderboard_judge_ran_on_that_model(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _register(monkeypatch, tmp_path, model=_SONNET, override=_SONNET)
        summary = arm_summary(_ARM, _rows(_SONNET), root=tmp_path)
        assert summary["judge_mean_overall"] == pytest.approx(0.85)
        assert summary["judge_calibration_report_id"] == "abcdef012345"

    def test_a_registered_id_with_no_report_behind_it_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            "evals.research_report.JUDGE_CALIBRATION_REPORTS", {LEADERBOARD_JUDGE: "000000000000"}
        )
        gate = judge_calibration_gate(LEADERBOARD_JUDGE, [_HAIKU], root=tmp_path)
        assert not gate["opens"]

    def test_todays_register_opens_for_the_committed_archives(self) -> None:
        """99181bf074ec: claude-haiku-4-5, the production rubric — what every scoped run used."""
        gate = judge_calibration_gate(LEADERBOARD_JUDGE, [_HAIKU])
        assert gate == {
            "report_id": "99181bf074ec",
            "model": _HAIKU,
            "rubric": JUDGE_PROMPT,
            "opens": True,
            "why_not": "",
        }
        assert dict(research.JUDGE_CALIBRATION_REPORTS) == {LEADERBOARD_JUDGE: "99181bf074ec"}

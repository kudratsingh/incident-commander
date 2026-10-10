"""WO-R3-364: the selector's calibration is asked on the model the selector runs on.

A run's selector is called on the run's own model (``ctx.model``; ``BENCHMARK_MODEL`` under
``--model-role benchmark``), while ``make judge-calibration`` asked every leg ``JUDGE_MODEL``,
so the 2026-10-08 selector report ``4fc2722d286e`` measured claude-haiku-4-5 for an arm whose
selector runs on claude-sonnet-4-6. Pinned here, free and hermetic (a fake client, no key):
the selector leg under a role asks that role's model while the judge legs keep ``JUDGE_MODEL``;
the report records the model and the role; and both readers of the register refuse, by name,
a calibration made on another model, through one helper.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import pytest

from evals import artifacts, oracle_gap
from evals import research_report as research
from evals.graders.llm_judge import JUDGE_PROMPT
from evals.judge_calibration.fakes import FakeJudgeClient, answers_for
from evals.judge_calibration.harness import calibrate, write_report
from evals.judge_calibration.roles import (
    ACTION_VERIFIER,
    BRIEFING_JUDGE,
    CALIBRATED_ROLES,
    CANDIDATE_SELECTOR,
)
from incident_commander.agent.remediation import VERIFICATION_JUDGE_PROMPT
from incident_commander.agent.selection import SELECTOR_PROMPT
from incident_commander.config import ModelRole, Settings
from incident_commander.llm.prompts.loader import load_prompt
from tests.unit import test_oracle_gap as og
from tests.unit import test_research_report_applies as applies
from tests.unit.test_runner import _test_settings

_JUDGE: Final[str] = "claude-haiku-4-5"
_BENCHMARK: Final[str] = "claude-sonnet-4-6"
#: The committed 2026-10-08 selector report, made on the judge model.
_HAIKU_REPORT: Final[str] = "4fc2722d286e"
#: The oracle-gap study's arm (``evals/samples/oracle_gap.json``).
_STUDY_ARM: Final[str] = "candidate_selector/best_of_n_enumerated/n=8"


def _settings(**overrides: Any) -> Settings:
    """The judge and benchmark pins on two different models, so a mix-up cannot pass."""
    values: dict[str, Any] = {
        "judge_model": _JUDGE,
        "benchmark_model": _BENCHMARK,
        "development_model": _JUDGE,
    }
    values.update(overrides)
    return _test_settings(**values)


def _run_live(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], settings: Settings
) -> tuple[int, FakeJudgeClient]:
    """``main`` down its PAID path, with the settings and the client swapped for fakes.

    The real ``LLMClient`` is never built: the factory hands back one scripted fake for every
    judge, so what is asserted is the model string each call carried, not a model's answer.
    """
    from evals.judge_calibration.__main__ import main

    script: dict[str, Any] = {}
    for judge in CALIBRATED_ROLES:
        script |= answers_for(judge)
    client = FakeJudgeClient(script)
    monkeypatch.setattr("incident_commander.config.get_settings", lambda: settings)
    monkeypatch.setattr("incident_commander.llm.client.LLMClient", lambda **_kwargs: client)
    return main([*argv, "--live", "--yes-spend", "--reps", "1"]), client


def _models_by_prompt(client: FakeJudgeClient) -> dict[str, set[str]]:
    """Which model ids each system prompt was asked on."""
    seen: dict[str, set[str]] = {}
    for (system, _user), model in zip(client.calls, client.models, strict=True):
        seen.setdefault(system, set()).add(model)
    return seen


class TestTheSelectorLegAsksTheRolesModel:
    def test_benchmark_role_pins_the_benchmark_model(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, client = _run_live(
            monkeypatch, ["--judge", CANDIDATE_SELECTOR, "--model-role", "benchmark"], _settings()
        )
        assert code == 0
        assert client.calls
        assert set(client.models) == {_BENCHMARK}
        out = capsys.readouterr().out
        assert f"model       {_BENCHMARK}" in out
        assert "BENCHMARK_MODEL" in out
        assert "role benchmark" in out

    def test_the_judge_legs_keep_the_judge_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """One invocation, all three legs: only the selector moves."""
        code, client = _run_live(monkeypatch, ["--model-role", "benchmark"], _settings())
        assert code == 0
        assert _models_by_prompt(client) == {
            load_prompt(VERIFICATION_JUDGE_PROMPT): {_JUDGE},
            load_prompt(JUDGE_PROMPT): {_JUDGE},
            load_prompt(SELECTOR_PROMPT): {_BENCHMARK},
        }

    def test_the_development_role_asks_the_development_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _settings(development_model=_BENCHMARK, benchmark_model=_JUDGE)
        code, client = _run_live(
            monkeypatch, ["--judge", CANDIDATE_SELECTOR, "--model-role", "development"], settings
        )
        assert code == 0
        assert set(client.models) == {_BENCHMARK}

    def test_without_a_role_every_leg_asks_the_judge_model_as_before(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No switch, no change: the 2026-10-08 behaviour, and the summary says what it was."""
        code, client = _run_live(monkeypatch, ["--judge", CANDIDATE_SELECTOR], _settings())
        assert code == 0
        assert set(client.models) == {_JUDGE}
        out = capsys.readouterr().out
        assert f"model       {_JUDGE}  from JUDGE_MODEL" in out
        assert "MODEL_ROLE=benchmark" in out  # the note says how to ask the arm's model

    def test_an_unknown_role_is_refused(self) -> None:
        from evals.judge_calibration.__main__ import main

        with pytest.raises(SystemExit) as refused:
            main(["--judge", CANDIDATE_SELECTOR, "--model-role", "benchmarkk"])
        assert refused.value.code == 2

    def test_the_free_path_records_the_role_and_asks_no_model(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from evals.judge_calibration.__main__ import main

        assert main(["--judge", CANDIDATE_SELECTOR, "--model-role", "benchmark"]) == 0
        out = capsys.readouterr().out
        assert "client fake" in out
        assert "model       fake-judge" in out
        assert "role benchmark" in out
        assert "SCRIPTED FAKE JUDGE" in out


class TestTheReportRecordsTheModelAndTheRole:
    def test_a_selector_report_names_its_model_and_role(self, tmp_path: Path) -> None:
        report = calibrate(
            CANDIDATE_SELECTOR,
            client=FakeJudgeClient(answers_for(CANDIDATE_SELECTOR)),
            model=_BENCHMARK,
            reps=1,
            model_role=ModelRole.BENCHMARK,
        )
        written = json.loads(write_report(report, root=tmp_path).read_text())
        assert (written["model"], written["model_role"], written["model_setting"]) == (
            _BENCHMARK,
            "benchmark",
            "BENCHMARK_MODEL",
        )

    @pytest.mark.parametrize("judge", CALIBRATED_ROLES)
    def test_without_a_role_the_report_says_judge_model(self, judge: str) -> None:
        report = calibrate(judge, client=FakeJudgeClient(answers_for(judge)), model=_JUDGE, reps=1)
        payload = report.to_dict()
        assert (payload["model"], payload["model_role"], payload["model_setting"]) == (
            _JUDGE,
            None,
            "JUDGE_MODEL",
        )

    @pytest.mark.parametrize("judge", [ACTION_VERIFIER, BRIEFING_JUDGE])
    def test_a_role_on_a_judge_leg_is_refused(self, judge: str) -> None:
        """A judge grades a run from outside it; it has no run role to borrow a model from."""
        with pytest.raises(ValueError, match="JUDGE_MODEL"):
            calibrate(
                judge,
                client=FakeJudgeClient(answers_for(judge)),
                model=_BENCHMARK,
                reps=1,
                model_role=ModelRole.BENCHMARK,
            )

    def test_the_committed_selector_report_was_made_on_the_judge_model(self) -> None:
        """The finding this order exists for, read off the committed file."""
        (path,) = [
            p
            for p in artifacts.versions("judge_calibration", CANDIDATE_SELECTOR)
            if p.name.endswith(f".{_HAIKU_REPORT}.json")
        ]
        assert json.loads(path.read_text())["model"] == _JUDGE


# --------------------------------------------------------------------------
# The register: one rule, both readers
# --------------------------------------------------------------------------


def _calibration_on(root: Path, model: str, report_id: str) -> None:
    artifacts.write_versioned(
        "judge_calibration",
        CANDIDATE_SELECTOR,
        content=json.dumps({"report_id": report_id, "model": model, "judge_client": "live"}),
        timestamp=applies._AT,
        invocation_id=report_id,
        root=root,
    )


def _register(monkeypatch: pytest.MonkeyPatch, arm: str, report_id: str) -> None:
    monkeypatch.setattr(research, "CALIBRATION_REPORTS", MappingProxyType({arm: report_id}))


class TestResearchReportRefusesAnotherModelsCalibration:
    """``research_report`` used to open on a registered id alone (#354 fixed only oracle_gap)."""

    _ID: Final[str] = "ca1ca1ca1ca1"

    def _sections(self, tmp_path: Path) -> dict[str, Any]:
        return applies._sections(applies._root(tmp_path, unseeded_applies=True))

    def test_a_haiku_calibration_keeps_the_oracle_gap_withheld_and_says_why(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _calibration_on(tmp_path, _JUDGE, self._ID)
        _register(monkeypatch, applies._ARM, self._ID)
        section = self._sections(tmp_path)["oracle_gap"]
        assert section["measurable"] is False
        for name in (self._ID, _JUDGE, _BENCHMARK, applies._ARM):
            assert name in section["why"], name

    def test_a_haiku_calibration_withholds_every_row_by_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _calibration_on(tmp_path, _JUDGE, self._ID)
        _register(monkeypatch, applies._ARM, self._ID)
        rows = self._sections(tmp_path)["pass_at_k_vs_selected_at_k"]["value"]["rows"]
        assert rows
        for row in rows:
            assert row["calibration_report_id"] is None
            assert self._ID in row["calibration_refused"]
            for field in ("selected_at_k", "oracle_gap_at_k", "selector_decision"):
                assert row[field].startswith("withheld:"), field
                assert self._ID in row[field] and _JUDGE in row[field], field

    def test_a_calibration_on_the_runs_model_opens_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _calibration_on(tmp_path, _BENCHMARK, self._ID)
        _register(monkeypatch, applies._ARM, self._ID)
        sections = self._sections(tmp_path)
        assert sections["oracle_gap"]["measurable"] is True
        assert sections["oracle_gap"]["value"]["calibration_reports"] == {applies._ARM: self._ID}
        for row in sections["pass_at_k_vs_selected_at_k"]["value"]["rows"]:
            assert row["calibration_report_id"] == self._ID
            assert row["calibration_refused"] == ""
            assert isinstance(row["selected_at_k"], list)

    def test_a_registered_id_with_no_report_behind_it_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _register(monkeypatch, applies._ARM, self._ID)
        section = self._sections(tmp_path)["oracle_gap"]
        assert section["measurable"] is False
        assert f"calibration report {self._ID} measured no model it names" in section["why"]


class TestOracleGapRefusesAnotherModelsCalibration:
    _ID: Final[str] = "abcabcabcabc"

    def test_the_refusal_names_the_report_and_both_models(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        og._archive(
            tmp_path,
            "bbbbbbbbbbbb",
            [og._outcome(*og._PAUSED)],
            {og._PAUSED[0]: [og._record(og._SET, selected="c1")]},
        )
        plan = og._plan(og._PAUSED)
        _calibration_on(tmp_path, _JUDGE, self._ID)
        _register(monkeypatch, plan.arm, self._ID)
        document = oracle_gap.assemble(["bbbbbbbbbbbb"], plan, root=tmp_path)
        assert document["calibration"]["opens"] is False
        for name in (self._ID, _JUDGE, _BENCHMARK):
            assert name in document["calibration"]["why_not"], name


class TestOneGateServesBothReaders:
    def test_the_committed_haiku_report_is_refused_for_the_study_arm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Registering 4fc2722d286e today would vouch for a model the arm never ran on."""
        _register(monkeypatch, _STUDY_ARM, _HAIKU_REPORT)
        gate = research.calibration_gate(_STUDY_ARM, _BENCHMARK)
        assert gate["opens"] is False
        assert gate["model"] == _JUDGE
        assert gate["why_not"] == (
            f"calibration report {_HAIKU_REPORT} measured {_JUDGE}, "
            f"and this arm's selector ran on {_BENCHMARK}"
        )

    def test_oracle_gap_has_no_gate_of_its_own(self) -> None:
        assert not hasattr(oracle_gap, "calibration_gate")

    def test_both_readers_ask_the_one_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked: list[tuple[str, str]] = []
        real: Callable[..., dict[str, Any]] = research.calibration_gate

        def spy(arm: str, model: str, **kwargs: Any) -> dict[str, Any]:
            asked.append((arm, model))
            return real(arm, model, **kwargs)

        monkeypatch.setattr(research, "calibration_gate", spy)
        og_root = tmp_path / "og"
        og._archive(
            og_root,
            "bbbbbbbbbbbb",
            [og._outcome(*og._PAUSED)],
            {og._PAUSED[0]: [og._record(og._SET, selected="c1")]},
        )
        plan = og._plan(og._PAUSED)
        oracle_gap.assemble(["bbbbbbbbbbbb"], plan, root=og_root)
        assert asked == [(plan.arm, _BENCHMARK)]
        asked.clear()
        applies._sections(applies._root(tmp_path / "rr", unseeded_applies=True))
        assert asked and set(asked) == {(applies._ARM, _BENCHMARK)}

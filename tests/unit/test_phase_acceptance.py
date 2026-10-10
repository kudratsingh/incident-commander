"""Phases 3–15 declare what their close reads; absent evidence is "not yet measured".

WO-R3-362. Red before: ``make phase-close-report PHASE=3`` stopped at argparse
(``--phase {1,2}``).
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from evals import artifacts, phase_acceptance
from evals import phase_close_report as close
from evals.judge_calibration.roles import CALIBRATED_ROLES
from evals.phase_acceptance import (
    BLOCKED,
    MEASURED,
    NOT_YET,
    AcceptanceScope,
    Artefact,
    Blocked,
    Calibrations,
    Context,
    DriftVerdicts,
    Recordings,
    RunRow,
    Runs,
    Subjects,
    UnitTests,
)
from evals.scenarios.loader import load_scenarios
from incident_commander.agent.strategies.names import StrategyName

ACCEPTANCE_PHASES: list[int] = sorted(close.ACCEPTANCE_SCOPES)
_CORPUS = {s.name: s for s in load_scenarios(close.REPO_ROOT / "evals/scenarios")}
_TIER_A: tuple[str, ...] = (
    "a0354a3f9cba",
    "e7fd45fb8a7a",
    "86e3b8006caf",
    "4e729b6803a2",
    "6e3abb9954d6",
    "eb25a58feb70",
    "03f50e776217",
)


def _scope(phase: int) -> AcceptanceScope:
    return close.ACCEPTANCE_SCOPES[phase]


def test_every_phase_from_1_to_15_is_declared() -> None:
    assert sorted(close.SCOPES) == list(range(1, 16))
    assert list(range(3, 16)) == ACCEPTANCE_PHASES
    assert close.LATEST_PHASE == 3


@pytest.mark.parametrize("phase", ACCEPTANCE_PHASES)
def test_make_phase_close_report_assembles_every_built_phase(
    phase: int, capsys: pytest.CaptureFixture[str]
) -> None:
    """Red before: ``--phase 3`` was an argparse error (exit 2)."""
    assert close.main(["--phase", str(phase), "--format", "json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["phase"] == phase
    assert document["closing"] is False
    assert len(document["lines"]) == len(_scope(phase).lines)


# --------------------------------------------------------------------------
# A scope names only what the repo can produce
# --------------------------------------------------------------------------


def _resolved(subjects: Subjects) -> tuple[str, ...]:
    ctx = phase_acceptance.context(close.REPO_ROOT)
    names = subjects.resolve(ctx)
    assert names, f"{subjects} resolves to no scenario"
    return names


@pytest.mark.parametrize("phase", ACCEPTANCE_PHASES)
def test_the_scope_names_only_artefacts_the_repo_can_produce(phase: int) -> None:
    families = {phase_acceptance._family(s) for s in _CORPUS.values()}
    for line in _scope(phase).lines:
        assert line.packet.startswith(f"WP-{phase}."), line.packet
        assert line.reads, line.text
        for reading in line.reads:
            if isinstance(reading, Recordings | DriftVerdicts | Runs):
                subjects = reading.subjects
                assert subjects.family is None or subjects.family in families
                assert set(subjects.scenarios) <= set(_CORPUS)
                for name in _resolved(subjects):
                    # A canned-only scenario has no live leg, so nothing can record or run it.
                    assert not _CORPUS[name].canned_only, (phase, name)
            if isinstance(reading, Runs):
                assert reading.mode in phase_acceptance.RUN_MODES
                assert set(reading.strategies) <= {s.value for s in StrategyName}
                if reading.mode == "recorded":
                    recorded = set(phase_acceptance.context(close.REPO_ROOT).recordings)
                    assert set(_resolved(reading.subjects)) <= recorded
            elif isinstance(reading, Calibrations):
                assert set(reading.judges) <= set(CALIBRATED_ROLES)
            elif isinstance(reading, Artefact):
                assert reading.kind in artifacts.KINDS
            elif isinstance(reading, UnitTests):
                assert (close.REPO_ROOT / reading.path).is_file()
            elif isinstance(reading, Blocked):
                # A blocked reading may not hide a run the repo could make.
                for name in re.findall(r"`([a-z_]+)`", reading.reason):
                    if name in _CORPUS:
                        assert _CORPUS[name].canned_only, name


def test_the_work_orders_are_the_plan_s_close_packets() -> None:
    closes = {scope.phase: scope.close_packet for scope in close.ACCEPTANCE_SCOPES.values()}
    assert all(packet.startswith(f"WP-{phase}.") for phase, packet in closes.items())
    assert len({scope.work_order for scope in close.ACCEPTANCE_SCOPES.values()}) == 13


# --------------------------------------------------------------------------
# Absent evidence is "not yet measured", never a failure
# --------------------------------------------------------------------------


@pytest.fixture
def empty_repo(tmp_path: Path) -> Path:
    shutil.copytree(close.REPO_ROOT / "evals/scenarios", tmp_path / "evals/scenarios")
    for folder in ("runs", "recorded_worlds", "reports"):
        (tmp_path / "evals" / folder).mkdir(parents=True)
    return tmp_path


@pytest.mark.parametrize("phase", ACCEPTANCE_PHASES)
def test_a_scope_with_no_evidence_renders_not_yet_measured(empty_repo: Path, phase: int) -> None:
    document = close.assemble_acceptance(empty_repo, _scope(phase))
    for line in document["lines"]:
        for result in line["readings"]:
            if result["reads"] == "alert texts (corpus)":
                continue  # read from the scenario files, which are always there
            assert result["status"] in (NOT_YET, BLOCKED), (phase, result)
    assert "not yet measured" in phase_acceptance.render_markdown(document)


def test_nothing_measured_is_refused_at_write_not_at_assembly(empty_repo: Path) -> None:
    scope = _scope(5)
    document = close.assemble_acceptance(empty_repo, scope)
    assert document["as_of"] is None
    with pytest.raises(ValueError, match="nothing is measured yet"):
        close.write_acceptance(document, scope, root=empty_repo)


def test_writing_twice_refuses_rather_than_replacing(tmp_path: Path) -> None:
    document = close.assemble_acceptance(close.REPO_ROOT, close.PHASE3)
    first = close.write_acceptance(document, close.PHASE3, root=tmp_path)
    assert all(".phase03." in path.name for path in first)
    with pytest.raises(FileExistsError):
        close.write_acceptance(document, close.PHASE3, root=tmp_path)


# --------------------------------------------------------------------------
# The committed Phase 3 status
# --------------------------------------------------------------------------


def _committed_phase3() -> tuple[dict[str, Any], Path, Path]:
    json_path, md_path = close.committed(3)
    return json.loads(json_path.read_text()), json_path, md_path


def test_the_committed_phase_3_status_regenerates_byte_for_byte() -> None:
    document, json_path, md_path = _committed_phase3()
    as_of = datetime.fromisoformat(str(document["as_of"]))
    regenerated = close.assemble_acceptance(close.REPO_ROOT, close.PHASE3, as_of=as_of)
    assert close.render_json(regenerated) == json_path.read_text()
    assert phase_acceptance.render_markdown(regenerated) == md_path.read_text()


def test_the_phase_3_status_says_drift_not_green_and_re_record_pending() -> None:
    document, _, _ = _committed_phase3()
    lines = {line["text"]: line for line in document["lines"]}
    drift = lines["drift check green"]["readings"][0]
    verdicts = {row["verdict"] for row in drift["rows"]}
    assert "CLEAN" not in verdicts and verdicts <= {"DRIFT", "REFUSED"}
    assert len(drift["rows"]) == 17
    assert "drift check NOT green" in drift["summary"]
    assert [p["work_order"] for p in document["pending"]] == ["WO-R3-294"]
    recorded, live = lines["a recorded run of each matches its live grade on ROOT_CAUSE"][
        "readings"
    ]
    assert recorded["status"] == NOT_YET
    graded = {row["archive"]: row for row in live["rows"]}
    assert set(_TIER_A) <= set(graded)
    assert all(graded[archive]["passed"] is False for archive in _TIER_A)


def test_as_of_leaves_out_evidence_that_landed_later() -> None:
    before_tier_a = datetime(2026, 10, 1, tzinfo=UTC)
    document = close.assemble_acceptance(close.REPO_ROOT, close.PHASE3, as_of=before_tier_a)
    drift = document["lines"][2]["readings"][0]
    assert drift["status"] == NOT_YET
    archives = {row["archive"] for row in document["lines"][1]["readings"][1]["rows"]}
    assert not archives & set(_TIER_A)


# --------------------------------------------------------------------------
# Readings in isolation
# --------------------------------------------------------------------------


def _run(archive: str, *, passed: bool) -> RunRow:
    return RunRow(
        archive=archive,
        generated_at="2026-09-17T00:00:00Z",
        scenario="dlq_backlog",
        mode="live",
        strategy="baseline",
        model_role="benchmark",
        passed=passed,
        final_state="escalated",
        root_cause="PASS",
        failed_dimensions=(),
    )


def test_a_withdrawn_grade_is_never_reprinted() -> None:
    ctx = Context(
        root=close.REPO_ROOT,
        as_of=None,
        corpus=_CORPUS,
        recordings={},
        runs=(_run("aaaaaaaaaaaa", passed=True), _run("bbbbbbbbbbbb", passed=True)),
        withdrawn={"aaaaaaaaaaaa": "evals/reports/regrades/x.json"},
    )
    result = Runs(Subjects(scenarios=("dlq_backlog",)), "live").read(ctx)
    withdrawn, kept = result["rows"]
    assert withdrawn["passed"] is None and "x.json" in withdrawn["note"]
    assert kept["passed"] is True
    assert "1 of 1 passed" in result["summary"]


def test_the_phase_2_read_only_pass_is_the_withdrawn_archive() -> None:
    assert close.PHASE2.read_only_pass is not None
    assert close.PHASE2.read_only_pass.archive_id in close.WITHDRAWN_GRADES


def test_a_blocked_reading_says_why_and_counts_as_blocked() -> None:
    document = close.assemble_acceptance(close.REPO_ROOT, close.PHASE11)
    assert {line["status"] for line in document["lines"]} == {BLOCKED}
    assert document["counts"][BLOCKED] == 2
    assert document["counts"][MEASURED] == 0

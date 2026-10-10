"""``make world-drift-all``: a reset before every check, one table out (WO-R3-362).

Fakes only: the live loop touches the shared world. Red before: a hand loop on 2026-10-08
refused 4 of 17 recordings for a world the previous check left dirty.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from evals import artifacts, world_drift_table
from evals.fixture_drift import Drift
from evals.recorder import EXIT_PRECONDITION
from evals.world_drift import DriftReport
from evals.world_drift_table import CLEAN, DRIFT, NOT_RUN, REFUSED, DriftRow

_REPO = world_drift_table.REPO_ROOT
_TABLE_20261008 = (
    _REPO / "evals/reports/world-drift/world_drift_table.20261008T160827Z.86b59809ce24.json"
)
_TABLE_20261010 = (
    _REPO / "evals/reports/world-drift/world_drift_table.20261010T094237Z.899a671c44d0.json"
)
_TABLE_WO_R3_366 = (
    _REPO / "evals/reports/world-drift/world_drift_table.20261010T110003Z.aa740648de40.json"
)

_CMD = 'PLATFORM_COMPOSE="demo/compose.yml" uv run python -m evals.world_drift --world '
_FILE = "dlq_backlog/dlq_backlog.20260917T182256Z"
_DRIFT_LOG = "\n".join(
    (
        _CMD + "dlq_backlog",
        "DRIFT: re-reading 8 recorded call(s) of `dlq_backlog`",
        "DRIFT: world      dlq_backlog",
        f"DRIFT: recording  /Users/someone/incident-commander/evals/recorded_worlds/{_FILE}"
        ".1800b3ddafb5.json",
        "DRIFT: 14 disagreement(s):",
        "  - dlq_backlog:get_postgres_health pool_size [live_only_field] canned=null live=5",
        "make: *** [world-drift] Error 1",
        "",
    )
)
_CLEAN_LOG = "\n".join(
    (
        _CMD + "a1b2c3d4e5f6",
        f"DRIFT: recording  evals/recorded_worlds/{_FILE}.a1b2c3d4e5f6.json",
        "DRIFT: none — every recorded call still answers the same.",
        "",
    )
)
_REFUSED_LOG = "\n".join(
    (
        _CMD + "jobs_not_progressing_outbox_stall",
        "DRIFT FAIL (precondition): 1 of 2 not met.",
        "  - get_outbox_status: unpublished_count expected at_least 10.0, observed [0]",
        "make: *** [world-drift] Error 7",
        "",
    )
)


def _report(drifts: int) -> DriftReport:
    return DriftReport(
        world="w",
        scenario="dlq_backlog",
        recording=Path("evals/recorded_worlds/dlq_backlog/x.json"),
        recorded_fingerprint="a",
        live_fingerprint="b",
        drifts=tuple(
            Drift(
                scenario="dlq_backlog",
                tool="get_postgres_health",
                path=f"p{i}",
                kind="live_only_field",
                canned=None,
                live=i,
            )
            for i in range(drifts)
        ),
    )


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


def test_every_check_is_preceded_by_a_reset() -> None:
    calls: list[str] = []

    def reset() -> tuple[int, bool]:
        calls.append("reset")
        return 0, True

    def check(world: str) -> tuple[int, DriftReport | None]:
        calls.append(f"check {world}")
        return (1, _report(3)) if world == "bbb" else (0, _report(0))

    rows = world_drift_table.run_all(
        [("s1", "aaa"), ("s2", "bbb"), ("s3", "ccc")], check=check, reset=reset
    )
    assert calls == ["reset", "check aaa", "reset", "check bbb", "reset", "check ccc"]
    assert [row.verdict for row in rows] == [CLEAN, DRIFT, CLEAN]
    assert rows[1].disagreements == 3


def test_a_failed_reset_stops_the_loop_and_marks_the_rest_not_run() -> None:
    resets = iter([(0, True), (0, False)])
    checked: list[str] = []

    def check(world: str) -> tuple[int, DriftReport | None]:
        checked.append(world)
        return 0, _report(0)

    rows = world_drift_table.run_all(
        [("s1", "aaa"), ("s2", "bbb"), ("s3", "ccc")],
        check=check,
        reset=lambda: next(resets),
    )
    assert checked == ["aaa"]
    assert [row.verdict for row in rows] == [CLEAN, NOT_RUN, NOT_RUN]
    assert "baseline re-audit FAIL" in rows[1].detail


def test_each_row_keeps_what_the_operator_saw(capsys: pytest.CaptureFixture[str]) -> None:
    def check(world: str) -> tuple[int, DriftReport | None]:
        print(f"DRIFT: recording  {_REPO}/evals/recorded_worlds/{world}.json")
        return 0, _report(0)

    (row,) = world_drift_table.run_all([("s", "aaa")], check=check, reset=lambda: (0, True))
    assert "DRIFT: recording" in capsys.readouterr().out
    assert row.output.strip() == "DRIFT: recording  evals/recorded_worlds/aaa.json"


def test_a_checkout_named_like_its_parent_is_stripped_whole() -> None:
    # GitHub Actions checks the repo out at …/work/incident-commander/incident-commander/.
    ci = "/home/runner/work/incident-commander/incident-commander"
    worktree = "/Users/someone/audit-ws/scratchpad/wp362"
    for root in (ci, worktree):
        text = f"DRIFT: recording  {root}/evals/recorded_worlds/aaa.json\n"
        assert (
            world_drift_table._strip_checkout(text)
            == "DRIFT: recording  evals/recorded_worlds/aaa.json\n"
        )


def test_a_refused_check_is_refused_with_its_reason() -> None:
    row = world_drift_table.row_of("s", "aaa", EXIT_PRECONDITION, None, "")
    assert row.verdict == REFUSED
    assert row.disagreements is None
    assert row.detail.startswith("precondition")


# --------------------------------------------------------------------------
# Reading earlier logs back
# --------------------------------------------------------------------------


def test_a_drift_log_reads_back_as_drift() -> None:
    row = world_drift_table.row_from_log(_DRIFT_LOG, checked_at="2026-10-08T15:57:00+00:00")
    assert (row.scenario, row.recording, row.verdict, row.disagreements, row.exit_code) == (
        "dlq_backlog",
        "1800b3ddafb5",
        DRIFT,
        14,
        1,
    )
    assert "/Users/" not in row.output


def test_a_clean_and_a_refused_log_read_back_as_such() -> None:
    clean = world_drift_table.row_from_log(_CLEAN_LOG, checked_at="t")
    assert (clean.verdict, clean.disagreements, clean.exit_code) == (CLEAN, 0, 0)
    refused = world_drift_table.row_from_log(_REFUSED_LOG, checked_at="t")
    assert (refused.verdict, refused.recording, refused.exit_code) == (REFUSED, None, 7)
    assert "unpublished_count" in refused.detail


def test_a_log_with_no_verdict_is_refused() -> None:
    with pytest.raises(ValueError, match="not a `make world-drift` log"):
        world_drift_table.row_from_log("hello", checked_at="t")
    with pytest.raises(ValueError, match="no verdict"):
        world_drift_table.row_from_log(_DRIFT_LOG.split("DRIFT: 14")[0], checked_at="t")


def test_an_empty_log_folder_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no \\*.log"):
        world_drift_table.rows_from_logs(tmp_path)


def test_main_from_logs_exits_1_on_drift_and_0_when_all_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "a.log").write_text(_CLEAN_LOG)
    assert world_drift_table.main(["--from-logs", str(tmp_path)]) == 0
    (tmp_path / "b.log").write_text(_DRIFT_LOG)
    assert world_drift_table.main(["--from-logs", str(tmp_path)]) == 1
    assert "| `dlq_backlog` |" in capsys.readouterr().out


_NOT_RUN_LOG = "\n".join(
    (
        "DRIFT NOT RUN (api_latency_db_query): INC-007 window — every api_latency precondition "
        "refuses until ~09:02 UTC 2026-10-11",
        "DRIFT: recording  evals/recorded_worlds/api_latency_db_query/"
        "api_latency_db_query.20261010T082148Z.8c4a32a50d4b.json",
        "",
    )
)


def test_a_check_deliberately_not_run_reads_back_as_not_run_with_its_reason() -> None:
    """WO-R3-366: a table can carry every world while saying which were not checked."""
    row = world_drift_table.row_from_log(_NOT_RUN_LOG, checked_at="t")
    assert (row.scenario, row.recording, row.verdict, row.disagreements) == (
        "api_latency_db_query",
        "8c4a32a50d4b",
        NOT_RUN,
        None,
    )
    assert row.detail.startswith("INC-007 window")


def test_a_not_run_marker_is_not_a_failed_reset(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 6 means a reset failed mid-loop; a marked world is just not all-clean (1)."""
    (tmp_path / "a.log").write_text(_CLEAN_LOG)
    (tmp_path / "b.log").write_text(_NOT_RUN_LOG)
    assert world_drift_table.main(["--from-logs", str(tmp_path)]) == 1
    assert "| `api_latency_db_query` | `8c4a32a50d4b` | NOT RUN |" in capsys.readouterr().out


def test_an_expired_breaker_record_has_its_own_reason() -> None:
    from evals.world_drift import EXIT_RECORD_EXPIRED, RECORD_EXPIRED_MESSAGE

    row = world_drift_table.row_of("s", "aaa", EXIT_RECORD_EXPIRED, None, "")
    assert row.verdict == REFUSED
    assert row.detail == f"record expired: {RECORD_EXPIRED_MESSAGE}"


# --------------------------------------------------------------------------
# The table as evidence
# --------------------------------------------------------------------------


def _rows() -> list[DriftRow]:
    return [
        world_drift_table.row_from_log(_DRIFT_LOG, checked_at="2026-10-08T15:57:00+00:00"),
        world_drift_table.row_from_log(_REFUSED_LOG, checked_at="2026-10-08T16:01:00+00:00"),
    ]


def test_the_table_is_written_once_under_world_drift(tmp_path: Path) -> None:
    doc = world_drift_table.document(_rows(), source="test", pin="v0.0.0")
    assert doc["counts"] == {CLEAN: 0, DRIFT: 1, REFUSED: 1, NOT_RUN: 0}
    assert doc["checked_at"] == "2026-10-08T16:01:00+00:00"
    json_path, md_path = world_drift_table.write(doc, root=tmp_path)
    assert json_path.parent == tmp_path / "evals/reports/world-drift"
    assert json_path.name.startswith("world_drift_table.20261008T160100Z.")
    assert artifacts.newest("world_drift_table", root=tmp_path) == json_path
    assert "| `dlq_backlog` | `1800b3ddafb5` | DRIFT | 14 |" in md_path.read_text()
    with pytest.raises(FileExistsError):
        world_drift_table.write(doc, root=tmp_path)


def test_the_committed_2026_10_08_table_has_no_clean_recording() -> None:
    table = json.loads(_TABLE_20261008.read_text())
    assert len(table["rows"]) == 17
    assert table["counts"][CLEAN] == 0
    assert table["platform_pin"] == "v0.6.23"


def test_the_committed_2026_10_10_table_is_the_re_recorded_worlds() -> None:
    """WO-R3-294: all 17 worlds re-recorded on v0.6.23, read back from their own drift logs."""
    table = json.loads(_TABLE_20261010.read_text())
    assert len(table["rows"]) == 17
    assert (table["counts"][CLEAN], table["counts"][DRIFT]) == (9, 8)
    assert table["platform_pin"] == "v0.6.23"


def test_the_wo_r3_366_table_is_clean_where_it_was_checked() -> None:
    """WO-R3-366: 10 worlds checked live with the honest-movement rules, all CLEAN; 7 not run.

    Four api_latency worlds wait for the INC-007 window; three traffic worlds were stopped after
    the dispatcher_stall check's burst left late dispatches. Every NOT RUN row says why.
    """
    table = json.loads(_TABLE_WO_R3_366.read_text())
    assert len(table["rows"]) == 17
    assert table["counts"] == {CLEAN: 10, DRIFT: 0, REFUSED: 0, NOT_RUN: 7}
    assert table["platform_pin"] == "v0.6.23"
    assert artifacts.newest("world_drift_table") == _TABLE_WO_R3_366
    not_run = [row for row in table["rows"] if row["verdict"] == NOT_RUN]
    assert {row["scenario"] for row in not_run} >= {
        "api_latency_db_query",
        "api_latency_downstream",
        "api_latency_healthy_control",
        "api_latency_redis",
    }
    assert all(row["detail"] and row["recording"] for row in not_run)
    previous = {
        row["scenario"]: row["recording"] for row in json.loads(_TABLE_20261010.read_text())["rows"]
    }
    assert {row["scenario"]: row["recording"] for row in table["rows"]} == previous


def test_the_newest_recording_of_each_scenario_is_what_the_loop_checks() -> None:
    worlds = world_drift_table.newest_recordings()
    assert len(worlds) == len({scenario for scenario, _ in worlds}) == 17
    assert all(re.fullmatch(r"[0-9a-f]{12}", recording) for _, recording in worlds)
    assert ("api_latency_db_query", "8c4a32a50d4b") in worlds


def test_the_make_target_runs_every_recording_or_reads_logs() -> None:
    def recipe(*args: str) -> str:
        return subprocess.run(
            ["make", "-n", "world-drift-all", *args],
            cwd=_REPO,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    assert "evals.world_drift_table" in recipe() and "--all" in recipe()
    logs = recipe("FROM_LOGS=/tmp/x", "WRITE=1")
    assert "--from-logs /tmp/x" in logs and "--write" in logs and "--all" not in logs

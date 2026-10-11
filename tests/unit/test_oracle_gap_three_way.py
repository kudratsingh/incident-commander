"""The oracle gap read three ways, per world and pooled (WO-R3-376, owner O-57).

Picked correct / picked wrong / declined to pick (``probe_more`` and ``escalate`` each shown, and
``none`` named). The owner's framing: the selector's caution is not a flaw in the instrument, it
IS the quantity — wrong picks are a selection problem, declines with the right answer in view are a
commitment problem, and the two have different fixes. Hermetic and free: step records are
hand-built and archives are written to a temporary directory.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest

from evals import oracle_gap, research_report
from evals.candidate_metrics import world_key
from evals.oracle_gap import RunRow
from incident_commander.agent.hypothesis import HypothesisCategory
from tests.unit.test_oracle_gap import (
    _CAL,
    _LAG,
    _PAUSED,
    _SET,
    _archive,
    _calibrate,
    _outcome,
    _plan,
    _record,
    _row,
)

#: c1 is the truth of ``_LAG`` (consumer_saturation); c2 is not.
_LAG_SET: Final[tuple[str, ...]] = ("consumer_saturation", "resolver_stall")
#: Three batch archives, each one run of each world.
_ARCHIVES: Final[tuple[str, ...]] = ("a1a1a1a1a1a1", "b2b2b2b2b2b2", "c3c3c3c3c3c3")
#: The labels the shared renderer gives the three numbers.
_PICK_LABELS: Final[tuple[str, ...]] = ("picked correct@", "picked wrong@", "declined to pick@")


def _four_decisions() -> list[RunRow]:
    """One world: each decision the selector can make, a wrong pick, and a record naming none."""
    return [
        _row("w1", _SET, selected="c1"),  # select, the right one
        _row("w1", _SET, selected="c2"),  # select, the wrong one
        _row("w1", _SET, selected=None, decision="probe_more"),
        _row("w1", _SET, selected=None, decision="escalate"),
        _row("w1", _SET, selected=None, decision=""),  # a selector record with no decision
    ]


def _no_selector(world: str) -> RunRow:
    """A run whose deciding step had no selector at all: not scored, at any k."""
    record = _record(_SET, selected=None)
    record["selector"] = None
    return oracle_gap.run_row(
        archive="aaaaaaaaaaaa",
        scenario=world,
        recording="0123456789ab",
        world=world_key(
            scenario=world, execution_mode="recorded", archive="a", world_fingerprint=world
        ),
        agent_model="claude-sonnet-4-6",
        records=[record],
        expected=[HypothesisCategory.DAG_PAUSED],
    )


def _sums_to_n(split: dict[str, Any]) -> bool:
    declined = split["declined"]
    return bool(
        sum(declined.values()) == split["declined_total"]
        and split["picked_correct"] + split["picked_wrong"] + split["declined_total"] == split["n"]
    )


class TestThePickSplit:
    def test_every_decision_lands_in_exactly_one_bucket(self) -> None:
        assert oracle_gap.pick_split(_four_decisions(), 8) == {
            "k": 8,
            "n": 5,
            "picked_correct": 1,
            "picked_wrong": 1,
            "declined": {"probe_more": 1, "escalate": 1, "none": 1},
            "declined_total": 3,
        }

    def test_the_three_numbers_sum_to_the_same_n_as_the_gap(self) -> None:
        rows = [*_four_decisions(), _no_selector("w1")]
        split = oracle_gap.pick_split(rows, 8)
        assert _sums_to_n(split)
        # The run with no selector is scored nowhere, here as in pass@k / selected@k / gap.
        assert split["n"] == oracle_gap.at_k(rows, 8)["n"] == 5

    def test_a_wrong_pick_with_no_correct_candidate_is_wrong_but_not_gap(self) -> None:
        """The split covers every scored run; the gap only those with the answer in view."""
        (row,) = rows = [_row("w1", ("resolver_stall", "consumer_saturation"), selected="c1")]
        assert (row.pass_hit[8], row.selected_hit[8], row.gap[8]) == (False, False, 0)
        assert oracle_gap.pick_split(rows, 8)["picked_wrong"] == 1
        assert oracle_gap.gap_by_decision(rows, 8) == {}

    def test_a_select_that_names_no_candidate_is_a_wrong_pick(self) -> None:
        split = oracle_gap.pick_split([_row("w1", _SET, selected=None, decision="select")], 8)
        assert (split["picked_correct"], split["picked_wrong"], split["declined_total"]) == (
            0,
            1,
            0,
        )

    def test_the_gap_by_decision_is_kept_beside_it(self) -> None:
        """Kept for compatibility: the gap's own split, over gap runs only."""
        assert oracle_gap.gap_by_decision(_four_decisions(), 8) == {
            "escalate": 1,
            "none": 1,
            "probe_more": 1,
            "select": 1,
        }

    def test_an_unscored_run_has_no_place_in_the_split(self) -> None:
        with pytest.raises(ValueError, match="not scored"):
            research_report.pick_split([("select", None)])


class TestPerWorldAndPooled:
    def test_each_world_and_the_pool_carry_it_and_each_sums_to_its_n(self) -> None:
        rows = [
            _row("w1", _SET, selected="c1"),
            _row("w1", _SET, selected=None, decision="probe_more"),
            _row("w2", _SET, selected="c2"),
            _row("w2", _SET, selected=None, decision="escalate"),
            _row("w2", _SET, selected="c1"),
            _no_selector("w2"),
        ]
        summary = oracle_gap.summarise(rows, headline_k=8)
        worlds = {world["scenario"]: world for world in summary["per_world"]}
        assert worlds["w1"]["pick_split"] == {
            "k": 8,
            "n": 2,
            "picked_correct": 1,
            "picked_wrong": 0,
            "declined": {"probe_more": 1, "escalate": 0, "none": 0},
            "declined_total": 1,
        }
        assert worlds["w2"]["pick_split"]["n"] == 3
        pooled = summary["pooled"]["pick_split"]
        for world in summary["per_world"]:
            headline = next(entry for entry in world["at_k"] if entry["k"] == 8)
            assert _sums_to_n(world["pick_split"])
            assert world["pick_split"]["n"] == headline["n"]
        assert _sums_to_n(pooled)
        assert pooled["n"] == sum(world["pick_split"]["n"] for world in summary["per_world"]) == 5
        for key in ("picked_correct", "picked_wrong", "declined_total"):
            assert pooled[key] == sum(world["pick_split"][key] for world in summary["per_world"])


# --------------------------------------------------------------------------
# The report on disk


@pytest.fixture
def two_worlds(tmp_path: Path) -> Path:
    """Three batch archives over two worlds: every decision, each world three runs."""
    # Per archive, (candidate taken, decision) in each world; c1 is right in both, c2 is not.
    picks: tuple[tuple[tuple[str | None, str], tuple[str | None, str]], ...] = (
        (("c1", "select"), (None, "escalate")),
        (("c2", "select"), ("c1", "select")),
        ((None, "probe_more"), (None, "probe_more")),
    )
    for archive, ((paused, paused_did), (lag, lag_did)) in zip(_ARCHIVES, picks, strict=True):
        _archive(
            tmp_path,
            archive,
            [_outcome(*_PAUSED), _outcome(*_LAG)],
            {
                _PAUSED[0]: [_record(_SET, selected=paused, decision=paused_did)],
                _LAG[0]: [_record(_LAG_SET, selected=lag, decision=lag_did)],
            },
        )
    return tmp_path


def _pick_cells(markdown: str) -> list[str]:
    """Every Markdown cell that states one of the three numbers; a column header states none."""
    cells = [cell.strip() for line in markdown.splitlines() for cell in line.split("|")]
    return [cell for cell in cells if cell.startswith(_PICK_LABELS)]


def _world_line(markdown: str, world: tuple[str, str]) -> str:
    """The per-world table's row for one (scenario, recording); per-run rows name an archive."""
    scenario, recording = world
    prefix = f"| {scenario} | {recording} | "
    (line,) = [line for line in markdown.splitlines() if line.startswith(prefix)]
    return line


class TestTheReport:
    def test_the_json_carries_three_numbers_per_world_and_pooled(
        self, two_worlds: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _calibrate(two_worlds, monkeypatch)
        document = oracle_gap.assemble(list(_ARCHIVES), _plan(_PAUSED, _LAG), root=two_worlds)
        summary = json.loads(oracle_gap.render_json(document))["summary"]
        worlds = {world["scenario"]: world["pick_split"] for world in summary["per_world"]}
        assert worlds[_PAUSED[0]] == {
            "k": 8,
            "n": 3,
            "picked_correct": 1,
            "picked_wrong": 1,
            "declined": {"probe_more": 1, "escalate": 0, "none": 0},
            "declined_total": 1,
            "calibration_report_id": _CAL,
        }
        assert worlds[_LAG[0]]["declined"] == {"probe_more": 1, "escalate": 1, "none": 0}
        assert summary["pooled"]["pick_split"] == {
            "k": 8,
            "n": 6,
            "picked_correct": 2,
            "picked_wrong": 1,
            "declined": {"probe_more": 2, "escalate": 1, "none": 0},
            "declined_total": 3,
            "calibration_report_id": _CAL,
        }
        headline = next(entry for entry in summary["pooled"]["at_k"] if entry["k"] == 8)
        assert summary["pooled"]["pick_split"]["n"] == headline["n"]

    def test_the_markdown_puts_each_number_in_its_own_cell_with_the_calibration_id(
        self, two_worlds: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _calibrate(two_worlds, monkeypatch)
        document = oracle_gap.assemble(list(_ARCHIVES), _plan(_PAUSED, _LAG), root=two_worlds)
        markdown = oracle_gap.render_markdown(document)
        cells = _pick_cells(markdown)
        # Three per world, three pooled; each through the shared renderer, its id in the cell.
        assert len(cells) == 9
        assert all(cell.endswith(f"[cal {_CAL}]") for cell in cells), cells
        paused = _world_line(markdown, _PAUSED)
        assert f"| picked correct@8 = 1/3 [cal {_CAL}] |" in paused
        assert f"| picked wrong@8 = 1/3 [cal {_CAL}] |" in paused
        assert (
            f"| declined to pick@8 = 1/3 (probe_more 1, escalate 0, none 0) [cal {_CAL}] |"
            in paused
        )
        assert f"| 6 | picked correct@8 = 2/6 [cal {_CAL}] | picked wrong@8 = 1/6 [cal {_CAL}]" in (
            markdown
        )
        assert f"declined to pick@8 = 3/6 (probe_more 2, escalate 1, none 0) [cal {_CAL}]" in cells
        # ``none`` is named where it is shown.
        assert "`none`" in markdown

    def test_withheld_the_counts_are_replaced_and_the_reason_is_in_each_cell(
        self, two_worlds: Path
    ) -> None:
        """The register's real id has no report under this root, so the gate stays shut."""
        document = oracle_gap.assemble(list(_ARCHIVES), _plan(_PAUSED, _LAG), root=two_worlds)
        why = document["calibration"]["why_not"]
        assert why
        summary = document["summary"]
        splits = [summary["pooled"]["pick_split"], *(w["pick_split"] for w in summary["per_world"])]
        for split in splits:
            assert split["calibration_report_id"] is None
            assert split["withheld_because"] == why
            for key in ("picked_correct", "picked_wrong", "declined", "declined_total"):
                assert split[key] == research_report.WITHHELD
            assert isinstance(split["n"], int)  # how many runs were scored is not a selector number
        cells = _pick_cells(oracle_gap.render_markdown(document))
        assert len(cells) == 9
        assert all(cell.endswith(f"withheld [{why}]") for cell in cells), cells
        assert not any(" = " in cell for cell in cells)


# --------------------------------------------------------------------------
# The research report's oracle-gap section: the same three numbers, the same renderer


def _research_rows(calibration: str | None) -> list[dict[str, Any]]:
    """One world, four runs: one of each decision, as ``_candidate_rows`` writes them."""
    runs = (
        ("select", True, 0),
        ("select", False, 1),
        ("probe_more", False, 1),
        ("escalate", False, 1),
    )
    return [
        {
            "world": "w1",
            "group": {"family": "workflow_stuck", "difficulty": "single"},
            "calibration_report_id": calibration,
            "selector_decision": decision,
            "oracle_gap_at_k": [
                {"k": 8, "pass_hit": True, "selected_hit": hit, "gap": gap},
            ],
        }
        for decision, hit, gap in runs
    ]


def _research_value(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "calibration_reports": {"arm": _CAL},
        "by_world": research_report._gap_by(rows, lambda row: row["world"]),
        "by_family": research_report._gap_by(rows, lambda row: row["group"]["family"]),
        "by_difficulty": research_report._gap_by(rows, lambda row: row["group"]["difficulty"]),
    }


class TestTheResearchReportShowsTheSame:
    def test_each_group_carries_the_split_at_each_k(self) -> None:
        value = _research_value(_research_rows(_CAL))
        (world,) = value["by_world"]
        at_eight = next(entry for entry in world["at_k"] if entry["k"] == 8)
        assert at_eight["pick_split"] == {
            "n": 4,
            "picked_correct": 1,
            "picked_wrong": 1,
            "declined": {"probe_more": 1, "escalate": 1, "none": 0},
            "declined_total": 2,
        }
        assert at_eight["pick_split"]["n"] == at_eight["scored_runs"]
        assert _sums_to_n(at_eight["pick_split"])

    def test_it_renders_through_the_shared_renderer_with_the_id(self) -> None:
        cells = _pick_cells(
            "\n".join(research_report._render_oracle_gap(_research_value(_research_rows(_CAL))))
        )
        # Three groupings (world, family, difficulty) x four k rows x three numbers.
        assert len(cells) == 36
        assert all(cell.endswith(f"[cal {_CAL}]") for cell in cells), cells
        assert f"picked correct@8 = 1/4 [cal {_CAL}]" in cells
        assert f"picked wrong@8 = 1/4 [cal {_CAL}]" in cells
        # A k with no scored run says so rather than printing 0/0.
        assert f"picked correct@1 = — [cal {_CAL}]" in cells
        assert f"declined to pick@8 = 2/4 (probe_more 1, escalate 1, none 0) [cal {_CAL}]" in cells

    def test_with_no_id_each_cell_says_why(self) -> None:
        cells = _pick_cells(
            "\n".join(research_report._render_oracle_gap(_research_value(_research_rows(None))))
        )
        assert len(cells) == 36
        assert all(
            cell.endswith(f"withheld [{research_report.SELECTOR_GATE_RULE}]") for cell in cells
        ), cells

"""WO-R3-278: the owner-label packet, the append-only label file, and the label leg.

Pinned: the packet's selection (same archives give the same 16), the append-only rule,
the refusal with zero labels, and the agreement arithmetic, all against the scripted fake.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from evals.graders.llm_judge import format_briefing_context
from evals.judge_calibration import __main__ as cli
from evals.judge_calibration import labels
from evals.judge_calibration.fakes import FakeJudgeClient, answers_for, answers_for_labels
from evals.judge_calibration.harness import calibrate
from evals.judge_calibration.label_leg import NoLabelsError, label_agreement
from evals.judge_calibration.roles import ACTION_VERIFIER, BRIEFING_JUDGE

_MODEL: Final[str] = "fake-judge"
_AT: Final[datetime] = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
_TODAY: Final[date] = date(2026, 10, 8)

#: The committed archives on 2026-10-08 (main at f485dbe). Frozen so a later archive cannot
#: move the pin: the selection is a function of the archives it is given.
_ARCHIVES: Final[tuple[str, ...]] = (
    "06e14be3e7b1", "0aced73979ec", "0db6fe722f7c", "16ae3c7a4c9d", "2408b07ef532",
    "28e30884e079", "2988f414afb4", "31267faa2f5c", "31a1335a807e", "32ae38f6b38b",
    "3c65c04326d4", "42000dfda188", "42c675d9c145", "4753c12f8132", "4779f94faa3c",
    "47abb70a2b9e", "4974811d236f", "54ab08425f82", "5c8895771fbd", "5f92388a1f2b",
    "648a32f2339d", "6888d24966fa", "6e2def805db4", "6ef0ff958958", "759e198cdd27",
    "779b19a287a7", "7a592aa6ed35", "7acd2b441961", "82d114a34002", "845bdae22195",
    "9319fe9c748a", "9949c45145d4", "9a7b56063450", "a0aa257bf865", "a87a9f4e17d8",
    "adcdcadd94a3", "aeadd5ef3edd", "b75527784077", "bb1fa70abb4c", "cde5a14485c3",
    "d16aa18dce08", "d567c8f90f9b", "e72b5ffb9df0", "e8404306138c", "ee183c85429c",
    "efdc3b2a9864", "f32f023eaf33", "fc896b25a09c",
)  # fmt: skip

_PINNED: Final[tuple[str, ...]] = (
    "54ab08425f82:remediate_dlq_backlog_success",
    "648a32f2339d:remediate_stale_cache_success",
    "0db6fe722f7c:noise_missing_severity",
    "759e198cdd27:remediate_consumer_lag_success",
    "0db6fe722f7c:trace_investigation",
    "0db6fe722f7c:deploy_correlation",
    "0db6fe722f7c:postgres_slow",
    "aeadd5ef3edd:remediate_runaway_saga_success",
    "cde5a14485c3:incidents_overview",
    "0db6fe722f7c:redis_saturation",
    "0db6fe722f7c:noise_low_severity",
    "9949c45145d4:dlq_wait_and_replay_success",
    "0db6fe722f7c:multi_probe_hypothesis_evolution",
    "0db6fe722f7c:failed_traces_scan",
    "3c65c04326d4:dlq_replay_safe_success",
    "6e2def805db4:saga_stuck",
)

_USEFUL_ID: Final[str] = labels.INC_002_ID
_OTHER_ID: Final[str] = "0db6fe722f7c:postgres_slow"
_THIRD_ID: Final[str] = "759e198cdd27:remediate_consumer_lag_success"


@pytest.fixture(scope="module")
def picks() -> list[labels.Pick]:
    return labels.select(labels.candidates(_ARCHIVES))


def _row(label_id: str, label: str, reason: str = "because", **extra: Any) -> str:
    row = {
        "id": label_id,
        "label": label,
        "reason": reason,
        "labelled_by": "owner",
        "labelled_at": "2026-10-08",
        **extra,
    }
    return json.dumps(row)


def _labels_file(tmp_path: Path, *rows: str) -> Path:
    path = tmp_path / "briefings.jsonl"
    path.write_text("".join(f"{row}\n" for row in rows))
    return path


class TestTheSelectionIsPinned:
    def test_the_same_archives_give_the_same_sixteen(self, picks: list[labels.Pick]) -> None:
        assert tuple(p.candidate.id for p in picks) == _PINNED

    def test_the_order_archives_are_given_in_does_not_matter(self) -> None:
        shuffled = list(_ARCHIVES)
        random.Random(278).shuffle(shuffled)
        again = labels.select(labels.candidates(shuffled))
        assert tuple(p.candidate.id for p in again) == _PINNED

    def test_inc_002_is_always_first(self, picks: list[labels.Pick]) -> None:
        assert picks[0].candidate.id == labels.INC_002_ID
        assert "bj-05" in picks[0].why

    def test_live_runs_only_and_at_most_two_per_scenario(self, picks: list[labels.Pick]) -> None:
        assert all(p.candidate.live for p in picks)
        scenarios = [p.candidate.scenario for p in picks]
        assert max(scenarios.count(s) for s in scenarios) <= labels.PER_SCENARIO_CAP

    def test_it_spreads_across_outcomes_and_families(self, picks: list[labels.Pick]) -> None:
        outcomes = {p.candidate.outcome for p in picks}
        assert outcomes == {labels.RESOLVED, labels.ESCALATED, labels.FAILED}
        assert len({p.candidate.family for p in picks}) >= 8

    def test_without_inc_002_the_selection_refuses(self) -> None:
        pool = [c for c in labels.candidates(_ARCHIVES) if c.id != labels.INC_002_ID]
        with pytest.raises(labels.LabelError, match="INC-002"):
            labels.select(pool)

    def test_only_tracked_archives_count(self) -> None:
        assert set(labels.committed_archives()) >= set(_ARCHIVES)


class TestThePacket:
    def test_it_says_what_useful_means_at_the_top(self, picks: list[labels.Pick]) -> None:
        text = labels.render_packet(picks, on=_TODAY)
        head = text.split("---", 1)[0]
        assert labels.USEFUL_MEANS in head
        assert "act correctly" in labels.USEFUL_MEANS

    def test_each_briefing_is_shown_as_the_judge_sees_it(self, picks: list[labels.Pick]) -> None:
        text = labels.render_packet(picks, on=_TODAY)
        for pick in picks:
            assert f"`{pick.candidate.id}`" in text
            context = format_briefing_context(labels.load_briefing(pick.candidate.id))
            assert context in text
        assert text.count("\nLABEL: \n") == len(picks)
        assert text.count("\nREASON: \n") == len(picks)

    def test_an_unfilled_packet_parses_to_nothing(self, picks: list[labels.Pick]) -> None:
        assert labels.parse_packet(labels.render_packet(picks, on=_TODAY)) == []

    def test_the_packet_is_never_overwritten(self, tmp_path: Path) -> None:
        first = labels.write_packet("one", on=_TODAY, directory=tmp_path)
        assert first.name == "packet.20261008.md"
        with pytest.raises(FileExistsError):
            labels.write_packet("two", on=_TODAY, directory=tmp_path)
        assert first.read_text() == "one"

    def test_the_committed_packet_names_committed_briefings(self) -> None:
        packets = sorted(labels.LABELS_DIR.glob("packet.*.md"))
        assert packets, "the WO-R3-278 packet is committed for the owner to fill"
        text = packets[0].read_text()
        for label_id in _PINNED:
            assert f"`{label_id}`" in text
            assert labels.briefing_path(label_id).is_file()


def _fill(text: str, answers: dict[str, tuple[str, str]]) -> str:
    """Write LABEL/REASON into the blocks named in ``answers``."""
    out: list[str] = []
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("## ") and "`" in line:
            current = line.split("`")[1]
        if current in answers and line == "LABEL: ":
            line = f"LABEL: {answers[current][0]}"
        if current in answers and line == "REASON: ":
            line = f"REASON: {answers[current][1]}"
        out.append(line)
    return "\n".join(out)


class TestTheParser:
    def test_filled_blocks_are_read_and_blank_ones_skipped(self, picks: list[labels.Pick]) -> None:
        text = _fill(
            labels.render_packet(picks[:4], on=_TODAY),
            {_USEFUL_ID: ("useful", "says what is left"), _THIRD_ID: ("Not Useful", "vague")},
        )
        filled = labels.parse_packet(text)
        assert [(f.id, f.label, f.reason) for f in filled] == [
            (_USEFUL_ID, "useful", "says what is left"),
            (_THIRD_ID, "not_useful", "vague"),
        ]

    def test_a_label_line_inside_the_briefing_box_is_ignored(self) -> None:
        text = (
            "## 1. `a:b`\n\n~~~~text\nLABEL: useful\nREASON: planted\n~~~~\n\nLABEL: \nREASON: \n"
        )
        assert labels.parse_packet(text) == []

    @pytest.mark.parametrize(
        ("label", "reason", "match"),
        [
            ("useful | not_useful", "x", "LABEL must be"),
            ("maybe", "x", "LABEL must be"),
            ("useful", "", "REASON"),
        ],
    )
    def test_a_malformed_block_is_refused(self, label: str, reason: str, match: str) -> None:
        text = f"## 1. `a:b`\n\nLABEL: {label}\nREASON: {reason}\n"
        with pytest.raises(labels.LabelError, match=match):
            labels.parse_packet(text)

    def test_one_id_twice_is_refused(self) -> None:
        block = "## {n}. `a:b`\n\nLABEL: useful\nREASON: x\n"
        with pytest.raises(labels.LabelError, match="twice"):
            labels.parse_packet(block.format(n=1) + block.format(n=2))

    def test_supersedes_must_name_its_own_block(self) -> None:
        text = "## 1. `a:b`\n\nLABEL: useful\nREASON: x\nSUPERSEDES: c:d\n"
        with pytest.raises(labels.LabelError, match="SUPERSEDES"):
            labels.parse_packet(text)

    def test_an_id_without_a_scenario_is_refused(self) -> None:
        with pytest.raises(labels.LabelError, match="invocation_id"):
            labels.parse_packet("## 1. `nocolon`\n\nLABEL: useful\nREASON: x\n")


class TestTheLabelFileIsAppendOnly:
    def _item(self, label_id: str, label: str, reason: str, **kw: Any) -> labels.FilledLabel:
        return labels.FilledLabel(id=label_id, label=label, reason=reason, **kw)

    def test_an_import_writes_the_documented_line(self, tmp_path: Path) -> None:
        path = tmp_path / "briefings.jsonl"
        result = labels.import_labels(
            [self._item(_USEFUL_ID, "useful", "honest")], path=path, today=_TODAY
        )
        assert result.appended == (_USEFUL_ID,)
        assert json.loads(path.read_text()) == {
            "id": _USEFUL_ID,
            "label": "useful",
            "reason": "honest",
            "labelled_by": "owner",
            "labelled_at": "2026-10-08",
        }

    def test_an_existing_id_is_refused_never_replaced(self, tmp_path: Path) -> None:
        path = tmp_path / "briefings.jsonl"
        labels.import_labels([self._item(_USEFUL_ID, "useful", "honest")], path=path, today=_TODAY)
        before = path.read_bytes()
        result = labels.import_labels(
            [self._item(_USEFUL_ID, "not_useful", "changed my mind")], path=path, today=_TODAY
        )
        assert result.refused == (_USEFUL_ID,)
        assert result.appended == ()
        assert path.read_bytes() == before

    def test_the_same_label_again_is_recorded_once(self, tmp_path: Path) -> None:
        path = tmp_path / "briefings.jsonl"
        labels.import_labels([self._item(_USEFUL_ID, "useful", "honest")], path=path, today=_TODAY)
        before = path.read_bytes()
        result = labels.import_labels(
            [self._item(_USEFUL_ID, "useful", "honest")], path=path, today=_TODAY
        )
        assert result.already_recorded == (_USEFUL_ID,)
        assert path.read_bytes() == before

    def test_a_correction_is_a_new_line_and_the_original_stays(self, tmp_path: Path) -> None:
        path = tmp_path / "briefings.jsonl"
        labels.import_labels([self._item(_USEFUL_ID, "useful", "honest")], path=path, today=_TODAY)
        before = path.read_bytes()
        result = labels.import_labels(
            [self._item(_USEFUL_ID, "not_useful", "misread", supersedes=_USEFUL_ID)],
            path=path,
            today=_TODAY,
        )
        assert result.appended == (_USEFUL_ID,)
        after = path.read_bytes()
        assert after.startswith(before)
        assert len(after.splitlines()) == 2
        assert json.loads(after.splitlines()[1])["supersedes"] == _USEFUL_ID
        assert labels.current_labels(path)[_USEFUL_ID]["label"] == "not_useful"

    def test_superseding_an_id_never_labelled_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(labels.LabelError, match="never labelled"):
            labels.import_labels(
                [self._item(_USEFUL_ID, "useful", "x", supersedes=_USEFUL_ID)],
                path=tmp_path / "briefings.jsonl",
                today=_TODAY,
            )

    def test_an_id_naming_no_committed_briefing_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(labels.LabelError, match="no committed briefing"):
            labels.import_labels(
                [self._item("000000000000:nothing", "useful", "x")],
                path=tmp_path / "briefings.jsonl",
                today=_TODAY,
            )

    def test_a_file_repeating_an_id_without_supersedes_is_refused(self, tmp_path: Path) -> None:
        path = _labels_file(tmp_path, _row(_USEFUL_ID, "useful"), _row(_USEFUL_ID, "not_useful"))
        with pytest.raises(labels.LabelError, match="without supersedes"):
            labels.read_lines(path)

    def test_the_committed_label_file_is_valid(self) -> None:
        assert labels.LABELS_FILE.is_file()
        labels.read_lines(labels.LABELS_FILE)


class TestTheLegRefusesWithZeroLabels:
    def test_an_empty_file_is_refused(self, tmp_path: Path) -> None:
        empty = _labels_file(tmp_path)
        with pytest.raises(NoLabelsError, match="zero labels"):
            label_agreement(client=FakeJudgeClient({}), model=_MODEL, reps=1, labels_path=empty)

    def test_a_missing_file_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(NoLabelsError, match="zero labels"):
            label_agreement(
                client=FakeJudgeClient({}),
                model=_MODEL,
                reps=1,
                labels_path=tmp_path / "absent.jsonl",
            )

    def test_calibrate_refuses_before_asking_anything(self, tmp_path: Path) -> None:
        client = FakeJudgeClient(answers_for(BRIEFING_JUDGE))
        with pytest.raises(NoLabelsError):
            calibrate(BRIEFING_JUDGE, client=client, model=_MODEL, labels=_labels_file(tmp_path))
        assert client.calls == []

    def test_labels_for_another_judge_are_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="briefing_judge only"):
            calibrate(
                ACTION_VERIFIER,
                client=FakeJudgeClient({}),
                model=_MODEL,
                labels=_labels_file(tmp_path, _row(_USEFUL_ID, "useful")),
            )

    def test_labels_whose_briefings_are_all_absent_are_refused(self, tmp_path: Path) -> None:
        path = _labels_file(tmp_path, _row("000000000000:gone", "useful"))
        with pytest.raises(NoLabelsError, match="none of the 1"):
            label_agreement(client=FakeJudgeClient({}), model=_MODEL, reps=1, labels_path=path)

    def test_the_cli_exits_two(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        empty = _labels_file(tmp_path)
        assert cli.main(["--judge", BRIEFING_JUDGE, "--labels", str(empty)]) == 2
        assert "zero labels" in capsys.readouterr().err

    def test_the_cli_refuses_labels_without_the_briefing_judge(self) -> None:
        assert cli.main(["--labels"]) == 2


def _three_labels(tmp_path: Path) -> Path:
    return _labels_file(
        tmp_path,
        _row(_USEFUL_ID, "useful", "honest about the slice"),
        _row(_OTHER_ID, "not_useful", "no next step"),
        _row(_THIRD_ID, "useful", "names the read"),
    )


def _payload(groundedness: float, actionability: float) -> dict[str, Any]:
    return {"groundedness": groundedness, "actionability": actionability, "reasoning": "r"}


def _context(label_id: str) -> str:
    return format_briefing_context(labels.load_briefing(label_id))


class TestTheAgreementArithmetic:
    def test_n_agree_disagree_and_the_two_directions(self, tmp_path: Path) -> None:
        path = _three_labels(tmp_path)
        script = answers_for_labels(path, wrong=(_OTHER_ID, _THIRD_ID))
        leg = label_agreement(
            client=FakeJudgeClient(script), model=_MODEL, reps=3, labels_path=path
        )
        value = leg["value"]
        assert leg["measured"] is True
        assert (value["n"], value["agree"], value["disagree"]) == (3, 1, 2)
        assert value["agreement"] == round(1 / 3, 4)
        assert value["judge_useful_owner_not"] == 1
        assert value["judge_not_useful_owner_useful"] == 1
        assert value["stability"] == {"reps": 3, "identical": 3, "fraction_identical": 1.0}
        assert [r["id"] for r in value["rows"]] == [_USEFUL_ID, _OTHER_ID, _THIRD_ID]

    def test_the_disagreements_carry_the_judges_confidence(self, tmp_path: Path) -> None:
        path = _three_labels(tmp_path)
        script = answers_for_labels(path)
        script[_context(_OTHER_ID)] = [_payload(0.95, 0.72)]
        leg = label_agreement(
            client=FakeJudgeClient(script), model=_MODEL, reps=1, labels_path=path
        )
        (row,) = leg["value"]["disagreements"]
        assert row["id"] == _OTHER_ID
        assert (row["owner_label"], row["judge_label"]) == ("not_useful", "useful")
        assert (row["groundedness"], row["actionability"]) == (0.95, 0.72)
        assert row["owner_reason"] == "no next step"
        assert row["judge_reasoning"] == "r"

    def test_useful_needs_both_dimensions_over_the_bar(self, tmp_path: Path) -> None:
        path = _labels_file(tmp_path, _row(_USEFUL_ID, "useful"))
        script = {_context(_USEFUL_ID): [_payload(0.9, 0.69)]}
        leg = label_agreement(
            client=FakeJudgeClient(script), model=_MODEL, reps=1, labels_path=path
        )
        assert leg["value"]["rows"][0]["judge_label"] == "not_useful"
        assert leg["value"]["agree"] == 0

    def test_agreement_is_the_first_ask_and_instability_is_counted(self, tmp_path: Path) -> None:
        path = _labels_file(tmp_path, _row(_USEFUL_ID, "useful"))
        script = {_context(_USEFUL_ID): [_payload(0.9, 0.9), _payload(0.2, 0.2)]}
        leg = label_agreement(
            client=FakeJudgeClient(script), model=_MODEL, reps=5, labels_path=path
        )
        value = leg["value"]
        assert value["agree"] == 1
        assert value["stability"]["identical"] == 0
        assert value["rows"][0]["observed"][0] == "grounded=yes actionable=yes"

    def test_a_judge_that_cannot_answer_is_an_error_not_a_disagreement(
        self, tmp_path: Path
    ) -> None:
        path = _three_labels(tmp_path)
        script = answers_for_labels(path)
        del script[_context(_THIRD_ID)]
        leg = label_agreement(
            client=FakeJudgeClient(script), model=_MODEL, reps=1, labels_path=path
        )
        value = leg["value"]
        assert (value["n"], value["agree"], value["disagree"]) == (2, 2, 0)
        assert [e["id"] for e in value["errors"]] == [_THIRD_ID]

    def test_a_superseded_label_is_scored_by_its_correction(self, tmp_path: Path) -> None:
        path = _labels_file(
            tmp_path,
            _row(_USEFUL_ID, "not_useful", "first read"),
            _row(_USEFUL_ID, "useful", "corrected", supersedes=_USEFUL_ID),
        )
        script = {_context(_USEFUL_ID): [_payload(0.9, 0.9)]}
        leg = label_agreement(
            client=FakeJudgeClient(script), model=_MODEL, reps=1, labels_path=path
        )
        assert leg["value"]["labels_in_force"] == 1
        assert leg["value"]["agree"] == 1

    def test_the_leg_is_in_the_report_only_when_asked(self, tmp_path: Path) -> None:
        path = _three_labels(tmp_path)
        script = answers_for(BRIEFING_JUDGE) | answers_for_labels(path)
        with_labels = calibrate(
            BRIEFING_JUDGE,
            client=FakeJudgeClient(script),
            model=_MODEL,
            reps=1,
            now=_AT,
            labels=path,
        )
        without = calibrate(
            BRIEFING_JUDGE, client=FakeJudgeClient(script), model=_MODEL, reps=1, now=_AT
        )
        assert json.loads(with_labels.to_json())["label_agreement"]["value"]["n"] == 3
        assert "label_agreement" not in without.to_dict()

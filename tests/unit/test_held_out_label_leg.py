"""WO-R3-378 (owner O-58 items 2-5, ADR 0082): the label leg scores a NAMED packet, held out.

O-58: the new rubric is scored on packet 2 only, with self-agreement at N=5; the first 16 are a
dev set whose agreement is shown but does not count; three of the first 16 come back unlabelled
inside packet 2 so the owner's own consistency can be read; and the harness reports a threshold
it does not act on. Pinned here, red before green, against the scripted fake:

* ``--packet P`` counts packet P's labels only, and only the briefings no other packet labelled
  (a repeat was in the rubric writer's view, so it is test-retest evidence, not held-out evidence);
* ``--dev-packet D`` is a separate block marked ``counted: false``;
* a label file holding more than one packet is refused without ``--packet``, before any call;
* test-retest: the ids labelled in P and in another packet, and whether the owner agreed;
* ``HELD_OUT_AGREEMENT_THRESHOLD`` and the rule for "clears", reported and never acted on.

The label rows' ``packet`` field is WO-R3-379's (rows without it are packet 1). Until that lands
``labels.read_lines`` refuses the field, so these tests read the file through the leg's own
``load_rows`` seam with a plain JSON reader; nothing here depends on which of the two lands first.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from evals.artifacts import REPO_ROOT
from evals.graders.llm_judge import JUDGE_PROMPT, format_briefing_context
from evals.judge_calibration import __main__ as cli
from evals.judge_calibration import label_leg, labels
from evals.judge_calibration.fakes import FakeJudgeClient, answers_for, answers_for_labels
from evals.judge_calibration.harness import calibrate
from evals.judge_calibration.label_leg import (
    HELD_OUT_AGREEMENT_THRESHOLD,
    NoLabelsError,
    PacketNotNamedError,
    held_out_verdict,
    label_agreement,
    labels_by_packet,
    owner_test_retest,
    packet_of,
)
from evals.judge_calibration.roles import BRIEFING_JUDGE
from incident_commander.llm.prompts.loader import load_prompt

_MODEL: Final[str] = "fake-judge"
_AT: Final[datetime] = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)

# Committed briefings (packet 1's own ids), so the leg can render each one.
_A: Final[str] = labels.INC_002_ID
_B: Final[str] = "0db6fe722f7c:postgres_slow"
_C: Final[str] = "759e198cdd27:remediate_consumer_lag_success"
_D: Final[str] = "0db6fe722f7c:noise_missing_severity"
_E: Final[str] = "0db6fe722f7c:failed_traces_scan"
_F: Final[str] = "3c65c04326d4:dlq_replay_safe_success"


def _row(
    label_id: str, label: str, packet: int | None, *, at: str = "2026-10-11", **extra: Any
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": label_id,
        "label": label,
        "reason": f"because ({label_id})",
        "labelled_by": "owner",
        "labelled_at": at,
        **extra,
    }
    if packet is not None:
        row["packet"] = packet
    return row


def _two_packets() -> list[dict[str, Any]]:
    """Packet 1 = A, B, C (no field: the rows that exist today); packet 2 = D, E, F + A, B again."""
    return [
        _row(_A, "useful", None),
        _row(_B, "not_useful", None),
        _row(_C, "useful", None),
        _row(_D, "useful", 2, at="2026-10-13"),
        _row(_A, "useful", 2, at="2026-10-13"),  # repeat, same label
        _row(_E, "not_useful", 2, at="2026-10-13"),
        _row(_B, "useful", 2, at="2026-10-13"),  # repeat, the owner changed their mind
        _row(_F, "useful", 2, at="2026-10-13"),
    ]


def _write(tmp_path: Path, rows: list[dict[str, Any]]) -> Path:
    path = tmp_path / "briefings.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


@pytest.fixture(autouse=True)
def _plain_rows(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Read label files as plain JSON lines (see the module docstring)."""

    def load(path: Path) -> list[dict[str, Any]]:
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    monkeypatch.setattr(label_leg, "load_rows", load)
    yield


def _context(label_id: str) -> str:
    return format_briefing_context(labels.load_briefing(label_id))


def _agreeing_with(path: Path, *, wrong: tuple[str, ...] = ()) -> FakeJudgeClient:
    return FakeJudgeClient(answers_for_labels(path, wrong=wrong))


class TestPackets:
    def test_a_row_without_a_packet_is_packet_one(self) -> None:
        assert packet_of(_row(_A, "useful", None)) == 1
        assert packet_of(_row(_A, "useful", 2)) == 2

    def test_each_packet_keeps_its_own_label_for_a_repeated_id(self) -> None:
        by_packet = labels_by_packet(_two_packets())
        assert sorted(by_packet) == [1, 2]
        assert by_packet[1][_B]["label"] == "not_useful"
        assert by_packet[2][_B]["label"] == "useful"
        assert list(by_packet[1]) == [_A, _B, _C]
        assert list(by_packet[2]) == [_D, _A, _E, _B, _F]

    def test_a_correction_replaces_its_original_inside_its_packet_only(self) -> None:
        rows = [
            *_two_packets(),
            _row(_E, "useful", 2, at="2026-10-14", supersedes=_E),
        ]
        by_packet = labels_by_packet(rows)
        assert by_packet[2][_E]["label"] == "useful"
        assert _E not in by_packet[1]


class TestTheHeldOutPacket:
    def test_only_the_named_packet_is_counted_and_its_repeats_are_not(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _two_packets())
        client = _agreeing_with(path)
        leg = label_agreement(client=client, model=_MODEL, reps=1, labels_path=path, packet=2)
        value = leg["value"]
        assert value["packet"] == 2
        assert value["counted"] is True
        assert [row["id"] for row in value["rows"]] == [_D, _E, _F]
        assert (value["n"], value["labels_in_force"]) == (3, 3)
        assert value["not_counted"] == [
            {"id": _A, "also_labelled_in": [1], "quoted_in_rubric": False},
            # postgres_slow is also rubric v2's example for 'blind probe'.
            {"id": _B, "also_labelled_in": [1], "quoted_in_rubric": True},
        ]

    def test_the_packet_the_rubric_was_written_from_is_refused_as_held_out(
        self, tmp_path: Path
    ) -> None:
        """Rubric v2 quotes five of packet 1's briefings: packet 1 is the dev set, by its bytes."""
        quoted = "0db6fe722f7c:deploy_correlation"
        assert quoted in load_prompt(JUDGE_PROMPT)
        path = _write(tmp_path, [_row(quoted, "not_useful", None), *_two_packets()])
        client = _agreeing_with(path)
        with pytest.raises(NoLabelsError, match="written from this packet"):
            label_agreement(client=client, model=_MODEL, reps=1, labels_path=path, packet=1)
        assert client.calls == []

    def test_a_quoted_briefing_repeated_in_a_later_packet_is_only_a_repeat(
        self, tmp_path: Path
    ) -> None:
        """postgres_slow (_B) is quoted AND repeated: packet 2 is still held out, minus it."""
        path = _write(tmp_path, _two_packets())
        leg = label_agreement(
            client=_agreeing_with(path), model=_MODEL, reps=1, labels_path=path, packet=2
        )
        assert [row["id"] for row in leg["value"]["rows"]] == [_D, _E, _F]

    def test_the_repeats_and_the_other_packet_are_never_asked(self, tmp_path: Path) -> None:
        """The dev set costs nothing unless it is asked for."""
        path = _write(tmp_path, _two_packets())
        client = _agreeing_with(path)
        label_agreement(client=client, model=_MODEL, reps=1, labels_path=path, packet=2)
        assert client.asked(_context(_A)) == client.asked(_context(_B)) == 0
        assert client.asked(_context(_C)) == 0
        assert client.asked(_context(_D)) == 1

    def test_the_held_out_number_carries_its_stability(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _two_packets())
        leg = label_agreement(
            client=_agreeing_with(path), model=_MODEL, reps=5, labels_path=path, packet=2
        )
        assert leg["value"]["stability"] == {"reps": 5, "identical": 3, "fraction_identical": 1.0}

    def test_a_packet_with_no_labels_is_refused(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _two_packets()[:3])
        with pytest.raises(NoLabelsError, match="packet 2 holds zero labels"):
            label_agreement(
                client=FakeJudgeClient({}), model=_MODEL, reps=1, labels_path=path, packet=2
            )

    def test_a_packet_of_repeats_only_holds_nothing_out(self, tmp_path: Path) -> None:
        rows = [*_two_packets()[:3], _row(_A, "useful", 2), _row(_B, "useful", 2)]
        path = _write(tmp_path, rows)
        with pytest.raises(NoLabelsError, match="nothing is held out"):
            label_agreement(
                client=FakeJudgeClient({}), model=_MODEL, reps=1, labels_path=path, packet=2
            )

    def test_a_file_of_two_packets_is_refused_without_a_named_one(self, tmp_path: Path) -> None:
        """Scoring everything would put the dev set into the number O-58 says it must not enter."""
        path = _write(tmp_path, _two_packets())
        client = _agreeing_with(path)
        with pytest.raises(PacketNotNamedError, match=r"packets \[1, 2\]"):
            label_agreement(client=client, model=_MODEL, reps=1, labels_path=path)
        assert client.calls == []

    def test_one_packet_and_no_name_scores_it_as_before(self, tmp_path: Path) -> None:
        """Today's file: 16 rows, no packet field. Nothing changes for it."""
        path = _write(tmp_path, _two_packets()[:3])
        leg = label_agreement(client=_agreeing_with(path), model=_MODEL, reps=1, labels_path=path)
        value = leg["value"]
        assert (value["packet"], value["n"], value["agree"]) == (1, 3, 3)
        assert "threshold" not in value
        assert leg["dev_set"] is None
        assert leg["test_retest"] is None


class TestTheDevSet:
    def test_the_dev_set_is_scored_beside_and_marked_not_counted(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _two_packets())
        leg = label_agreement(
            client=_agreeing_with(path, wrong=(_C,)),
            model=_MODEL,
            reps=1,
            labels_path=path,
            packet=2,
            dev_packet=1,
        )
        dev = leg["dev_set"]
        assert dev["packet"] == 1
        assert dev["counted"] is False
        assert "not counted" in dev["why_not_counted"]
        assert [row["id"] for row in dev["rows"]] == [_A, _B, _C]
        # C is scripted wrong, and B is answered as packet 2 labelled it (the fake keys on the
        # briefing, so a repeat is one question): only A agrees.
        assert (dev["n"], dev["agree"]) == (3, 1)
        # The held-out number is untouched by the dev set's disagreement.
        assert (leg["value"]["n"], leg["value"]["agree"]) == (3, 3)

    def test_the_dev_packet_must_differ_from_the_held_out_one(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _two_packets())
        with pytest.raises(ValueError, match="must differ"):
            label_agreement(
                client=FakeJudgeClient({}),
                model=_MODEL,
                reps=1,
                labels_path=path,
                packet=2,
                dev_packet=2,
            )

    def test_a_dev_packet_needs_a_held_out_one(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _two_packets())
        with pytest.raises(ValueError, match="needs a held-out packet"):
            label_agreement(
                client=FakeJudgeClient({}), model=_MODEL, reps=1, labels_path=path, dev_packet=1
            )


class TestTheOwnersTestRetest:
    def test_the_repeats_are_paired_and_the_changed_one_named(self) -> None:
        retest = owner_test_retest(labels_by_packet(_two_packets()), packet=2)
        assert (retest["pairs"], retest["same"]) == (2, 1)
        assert retest["agreement"] == 0.5
        changed = [row for row in retest["rows"] if not row["same"]]
        assert [row["id"] for row in changed] == [_B]
        assert changed[0]["labels"] == [
            {"packet": 1, "label": "not_useful", "labelled_at": "2026-10-11"},
            {"packet": 2, "label": "useful", "labelled_at": "2026-10-13"},
        ]

    def test_no_repeat_is_zero_pairs_not_an_error(self) -> None:
        retest = owner_test_retest(labels_by_packet(_two_packets()[:3]), packet=1)
        assert (retest["pairs"], retest["same"], retest["agreement"]) == (0, 0, None)

    def test_the_leg_carries_it(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _two_packets())
        leg = label_agreement(
            client=_agreeing_with(path), model=_MODEL, reps=1, labels_path=path, packet=2
        )
        assert (leg["test_retest"]["pairs"], leg["test_retest"]["same"]) == (2, 1)


def _value(
    owner: list[str], judge: list[str], *, errors: int = 0, missing: int = 0
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = [
        {"owner_label": o, "judge_label": j, "agrees": o == j}
        for o, j in zip(owner, judge, strict=True)
    ]
    agree = sum(o == j for o, j in zip(owner, judge, strict=True))
    return {
        "n": len(rows),
        "agree": agree,
        "agreement": round(agree / len(rows), 4),
        "rows": rows,
        "errors": [{"id": f"x{i}", "error": "boom"} for i in range(errors)],
        "missing_briefings": [f"m{i}" for i in range(missing)],
    }


class TestTheThreshold:
    def test_the_number_is_the_one_the_adr_argues(self) -> None:
        assert HELD_OUT_AGREEMENT_THRESHOLD == 0.8

    def test_eleven_of_thirteen_clears(self) -> None:
        owner = ["useful"] * 8 + ["not_useful"] * 5
        judge = owner[:11] + ["not_useful", "useful"]
        verdict = held_out_verdict(_value(owner, judge))
        assert verdict["clears"] is True
        assert verdict["majority_label_baseline"] == {"label": "useful", "agree": 8, "n": 13}
        assert "owner decides" in verdict["decides"]

    def test_ten_of_thirteen_does_not(self) -> None:
        owner = ["useful"] * 8 + ["not_useful"] * 5
        judge = owner[:10] + ["useful", "useful", "useful"]
        assert held_out_verdict(_value(owner, judge))["clears"] is False

    def test_a_judge_no_better_than_the_majority_label_does_not_clear(self) -> None:
        """Twelve useful of thirteen: saying 'useful' every time scores 0.92 and proves nothing."""
        owner = ["useful"] * 12 + ["not_useful"]
        assert held_out_verdict(_value(owner, ["useful"] * 13))["clears"] is False

    def test_an_incomplete_score_does_not_clear(self) -> None:
        owner = ["useful"] * 8 + ["not_useful"] * 5
        assert held_out_verdict(_value(owner, owner, errors=1))["clears"] is False
        assert held_out_verdict(_value(owner, owner, missing=1))["clears"] is False
        assert held_out_verdict(_value(owner, owner))["clears"] is True

    def test_the_leg_reports_it_only_for_a_named_packet(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _two_packets())
        leg = label_agreement(
            client=_agreeing_with(path), model=_MODEL, reps=1, labels_path=path, packet=2
        )
        threshold = leg["value"]["threshold"]
        assert threshold["value"] == HELD_OUT_AGREEMENT_THRESHOLD
        assert threshold["constant"] == "HELD_OUT_AGREEMENT_THRESHOLD"
        assert "0082" in threshold["adr"]


class TestTheReportAndTheCli:
    def test_the_report_carries_held_out_dev_and_retest(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _two_packets())
        script = answers_for(BRIEFING_JUDGE) | answers_for_labels(path)
        report = calibrate(
            BRIEFING_JUDGE,
            client=FakeJudgeClient(script),
            model=_MODEL,
            reps=1,
            now=_AT,
            labels=path,
            packet=2,
            dev_packet=1,
        )
        leg = json.loads(report.to_json())["label_agreement"]
        assert (leg["value"]["packet"], leg["value"]["counted"]) == (2, True)
        assert (leg["dev_set"]["packet"], leg["dev_set"]["counted"]) == (1, False)
        assert leg["test_retest"]["pairs"] == 2

    def test_a_packet_without_labels_is_refused_by_calibrate(self) -> None:
        with pytest.raises(ValueError, match="needs the owner labels"):
            calibrate(BRIEFING_JUDGE, client=FakeJudgeClient({}), model=_MODEL, packet=2)

    def test_the_cli_prints_the_three_blocks(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = _write(tmp_path, _two_packets())
        code = cli.main(
            [
                "--judge", BRIEFING_JUDGE, "--labels", str(path),
                "--packet", "2", "--dev-packet", "1", "--reps", "1",
            ]
        )  # fmt: skip
        out = capsys.readouterr().out
        assert code == 0
        assert "held-out    packet 2" in out
        assert "threshold" in out and "HELD_OUT_AGREEMENT_THRESHOLD" in out
        assert "NOT COUNTED" in out
        assert "dev set     packet 1" in out
        assert "test-retest" in out and "1/2 the same" in out
        assert "SCRIPTED FAKE JUDGE" in out

    def test_the_cli_refuses_two_packets_without_a_name(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = _write(tmp_path, _two_packets())
        assert cli.main(["--judge", BRIEFING_JUDGE, "--labels", str(path)]) == 2
        assert "--packet" in capsys.readouterr().err

    def test_the_cli_refuses_a_dev_packet_alone(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = _write(tmp_path, _two_packets())
        code = cli.main(["--judge", BRIEFING_JUDGE, "--labels", str(path), "--dev-packet", "1"])
        assert code == 2
        assert "--dev-packet needs --packet" in capsys.readouterr().err

    def test_the_cli_refuses_a_packet_for_another_judge(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main(["--packet", "2"]) == 2
        assert BRIEFING_JUDGE in capsys.readouterr().err

    def test_the_makefile_passes_both_packets(self) -> None:
        text = (REPO_ROOT / "Makefile").read_text()
        assert "$(if $(PACKET),--packet $(PACKET),)" in text
        assert "$(if $(DEV_PACKET),--dev-packet $(DEV_PACKET),)" in text

"""WO-R3-379: a second label packet, drawn blind, with repeats from the first.

Pinned: the draw skips every briefing a packet already showed or the owner already labelled;
exactly the requested repeats come from an earlier packet, chosen and shuffled by the seed;
the packet carries no marker on them and the key never enters it; a label row names its
packet, so one briefing can hold one label per packet; packet 1's rows read unchanged.

No test here uses the seed packet 2 was drawn with, and none names its picks: the committed
packet and the seed would then say which briefings repeat.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Final

import pytest

from evals.artifacts import REPO_ROOT
from evals.judge_calibration import label_packet, labels
from tests.unit.test_briefing_labels import _ARCHIVES, _PINNED

_TODAY: Final[date] = date(2026, 10, 11)
_SEED: Final[int] = 5
_NEW: Final[int] = labels.PACKET_SIZE - 3

#: What the committed packets and labels look like when the tests read them.
_PACKET_1_FILE: Final[str] = "packet.20261008.md"

#: sha256 over the 16 lines the owner's first import wrote (cmd #369). The file is append-only,
#: so these bytes never change, whatever later packets add below them.
_PACKET_1_ROWS_SHA: Final[str] = "6537aceda8c166653a559bb5d63b9a7e341924ad12d5183bf5687ff12193c3c9"


def _row(
    label_id: str, label: str = "useful", reason: str = "because", **extra: Any
) -> dict[str, Any]:
    return {
        "id": label_id,
        "label": label,
        "reason": reason,
        "labelled_by": "owner",
        "labelled_at": "2026-10-10",
        **extra,
    }


#: Packet 1 as the label file holds it: no ``packet`` field, which reads as packet 1.
_PACKET_1_ROWS: Final[tuple[dict[str, Any], ...]] = tuple(
    _row(label_id, "useful" if n % 3 else "not_useful") for n, label_id in enumerate(_PINNED)
)


def _write(path: Path, *rows: dict[str, Any]) -> Path:
    path.write_text("".join(f"{json.dumps(row)}\n" for row in rows))
    return path


@pytest.fixture(scope="module")
def pool() -> list[labels.Candidate]:
    return labels.candidates(_ARCHIVES)


def _draw(pool: list[labels.Candidate], *, seed: int = _SEED, **kw: Any) -> labels.Draw:
    args: dict[str, Any] = {
        "packet": 2,
        "repeats": 3,
        "seed": seed,
        "rows": _PACKET_1_ROWS,
        "packets": {1: _PINNED},
    } | kw
    return labels.draw_packet(pool, **args)


def _blocks(text: str) -> list[str]:
    """Each briefing's block, without its briefing box."""
    out: list[str] = []
    for chunk in text.split("\n---\n")[1:]:
        head, _, rest = chunk.partition("~~~~text")
        _, _, tail = rest.partition("\n~~~~\n")
        out.append(head + tail)
    return out


class TestTheDraw:
    def test_it_shows_sixteen_thirteen_new_and_three_repeats(
        self, pool: list[labels.Candidate]
    ) -> None:
        drawn = _draw(pool)
        assert len(drawn.picks) == labels.PACKET_SIZE
        assert len(drawn.new) == _NEW
        assert len(drawn.repeats) == 3
        ids = [p.candidate.id for p in drawn.picks]
        assert len(set(ids)) == labels.PACKET_SIZE
        assert set(ids) == {p.candidate.id for p in drawn.new} | set(drawn.repeats)

    def test_no_new_pick_was_labelled_or_shown_before(self, pool: list[labels.Candidate]) -> None:
        drawn = _draw(pool)
        assert not {p.candidate.id for p in drawn.new} & set(_PINNED)

    def test_the_repeats_are_labelled_briefings_of_the_earlier_packet(
        self, pool: list[labels.Candidate]
    ) -> None:
        drawn = _draw(pool)
        assert set(drawn.repeats) <= set(_PINNED)

    def test_a_briefing_shown_but_never_labelled_is_neither_new_nor_a_repeat(
        self, pool: list[labels.Candidate]
    ) -> None:
        skipped = _PINNED[4]
        rows = tuple(r for r in _PACKET_1_ROWS if r["id"] != skipped)
        for seed in range(12):
            drawn = _draw(pool, seed=seed, rows=rows)
            assert skipped not in {p.candidate.id for p in drawn.picks}

    def test_a_labelled_briefing_no_packet_file_shows_is_still_excluded(
        self, pool: list[labels.Candidate]
    ) -> None:
        drawn = _draw(pool, packets={})
        assert not {p.candidate.id for p in drawn.new} & set(_PINNED)

    def test_the_new_picks_follow_the_selection_rule(self, pool: list[labels.Candidate]) -> None:
        fresh = [c for c in pool if c.id not in set(_PINNED)]
        rule = labels.select(fresh, size=_NEW, with_inc_002=False)
        assert [p.candidate.id for p in _draw(pool).new] == [p.candidate.id for p in rule]

    def test_the_new_picks_do_not_depend_on_the_seed(self, pool: list[labels.Candidate]) -> None:
        assert [p.candidate.id for p in _draw(pool, seed=1).new] == [
            p.candidate.id for p in _draw(pool, seed=2).new
        ]

    def test_the_same_seed_gives_the_same_packet(self, pool: list[labels.Candidate]) -> None:
        one, two = _draw(pool), _draw(pool)
        assert one.repeats == two.repeats
        assert [p.candidate.id for p in one.picks] == [p.candidate.id for p in two.picks]

    def test_the_seed_moves_the_repeats_and_the_order(self, pool: list[labels.Candidate]) -> None:
        draws = [_draw(pool, seed=seed) for seed in range(8)]
        assert len({d.repeats for d in draws}) > 1
        assert len({tuple(p.candidate.id for p in d.picks) for d in draws}) == len(draws)

    def test_the_repeats_are_shuffled_in_not_appended(self, pool: list[labels.Candidate]) -> None:
        positions = set()
        for seed in range(8):
            drawn = _draw(pool, seed=seed)
            ids = [p.candidate.id for p in drawn.picks]
            positions |= {ids.index(r) for r in drawn.repeats}
        assert positions - set(range(_NEW, labels.PACKET_SIZE))

    def test_the_key_is_packet_seed_and_repeats(self, pool: list[labels.Candidate]) -> None:
        drawn = _draw(pool)
        assert drawn.key() == {"packet": 2, "seed": _SEED, "repeats": list(drawn.repeats)}

    def test_no_repeats_draws_sixteen_new(self, pool: list[labels.Candidate]) -> None:
        drawn = _draw(pool, repeats=0)
        assert drawn.repeats == ()
        assert len(drawn.new) == labels.PACKET_SIZE

    @pytest.mark.parametrize(
        ("kw", "match"),
        [
            ({"packet": 1}, "2 or more"),
            ({"packets": {1: _PINNED, 2: ("a:b",)}}, "packet 2 already exists"),
            ({"rows": (*_PACKET_1_ROWS, _row(_PINNED[0], packet=2))}, "packet 2 already exists"),
            ({"repeats": 17}, "repeats"),
            ({"repeats": -1}, "repeats"),
            ({"rows": _PACKET_1_ROWS[:2]}, "only 2"),
        ],
    )
    def test_a_draw_that_cannot_be_honest_is_refused(
        self, pool: list[labels.Candidate], kw: dict[str, Any], match: str
    ) -> None:
        with pytest.raises(labels.LabelError, match=match):
            _draw(pool, **kw)


@pytest.fixture(scope="module")
def drawn(pool: list[labels.Candidate]) -> labels.Draw:
    return _draw(pool)


@pytest.fixture(scope="module")
def text(drawn: labels.Draw) -> str:
    return labels.render_packet(drawn.picks, on=_TODAY, packet=2, seed=_SEED)


class TestThePacketIsBlind:
    def test_the_header_names_the_packet_and_the_seed_and_says_no_more(self, text: str) -> None:
        head = text.split("\n---\n", 1)[0].splitlines()
        assert head[:7] == [
            "# Briefing usefulness labels — packet 20261011",
            "",
            "packet: 2",
            "seed: 5",
            "",
            "16 briefings; some may repeat an earlier packet.",
            "",
        ]
        assert head[7] == labels.USEFUL_MEANS

    def test_after_the_header_it_is_packet_one_word_for_word(self, text: str) -> None:
        first = (labels.LABELS_DIR / _PACKET_1_FILE).read_text()
        first_head = first.split("\n---\n", 1)[0].splitlines()
        assert text.split("\n---\n", 1)[0].splitlines()[7:] == first_head[2:]

    def test_no_block_says_why_it_is_there(self, text: str) -> None:
        assert "Why it is in the packet" not in text

    def test_nothing_after_the_header_marks_a_repeat(self, drawn: labels.Draw, text: str) -> None:
        for block in _blocks(text):
            lowered = block.lower()
            for word in ("repeat", "re-include", "retest", "earlier packet", "packet 1", "sealed"):
                assert word not in lowered, word
        assert "repeats" not in text

    def test_a_repeat_block_has_the_shape_of_a_new_one(self, drawn: labels.Draw, text: str) -> None:
        def shape(block: str) -> list[str]:
            return [line.split(":", 1)[0] for line in block.splitlines() if line.startswith("- ")]

        by_id = {b.split("`")[1]: b for b in _blocks(text)}
        new_shapes = {tuple(shape(by_id[p.candidate.id])) for p in drawn.new}
        for label_id in drawn.repeats:
            assert tuple(shape(by_id[label_id])) in new_shapes

    def test_the_packet_reads_back_its_number_and_its_order(
        self, drawn: labels.Draw, text: str
    ) -> None:
        assert labels.packet_number(text) == 2
        assert labels.packet_ids(text) == [p.candidate.id for p in drawn.picks]
        assert labels.parse_packet(text) == []

    def test_packet_one_reads_as_packet_one(self) -> None:
        text = (labels.LABELS_DIR / _PACKET_1_FILE).read_text()
        assert labels.packet_number(text) == 1
        assert labels.packet_ids(text) == list(_PINNED)

    def test_packet_one_still_renders_with_its_reasons(self) -> None:
        picks = labels.select(labels.candidates(_ARCHIVES))
        text = labels.render_packet(picks, on=date(2026, 10, 8))
        first = (labels.LABELS_DIR / _PACKET_1_FILE).read_text()
        assert text.split("\n---\n", 1)[0] == first.split("\n---\n", 1)[0]
        assert text.count("- Why it is in the packet: ") == labels.PACKET_SIZE


class TestTheKeyStaysOutOfTheRepo:
    def test_it_writes_the_documented_json(
        self, pool: list[labels.Candidate], tmp_path: Path
    ) -> None:
        drawn = _draw(pool)
        path = labels.write_key(
            drawn.key(), tmp_path / "sealed" / "key.json", repo=tmp_path / "repo"
        )
        assert json.loads(path.read_text()) == {
            "packet": 2,
            "seed": _SEED,
            "repeats": list(drawn.repeats),
        }

    def test_it_is_never_overwritten(self, tmp_path: Path) -> None:
        key = {"packet": 2, "seed": 1, "repeats": []}
        path = labels.write_key(key, tmp_path / "key.json", repo=tmp_path / "repo")
        with pytest.raises(FileExistsError):
            labels.write_key(key | {"seed": 2}, path, repo=tmp_path / "repo")
        assert json.loads(path.read_text())["seed"] == 1

    def test_a_path_inside_the_repo_is_refused(self, tmp_path: Path) -> None:
        inside = tmp_path / "repo" / "evals" / "key.json"
        with pytest.raises(labels.LabelError, match="outside"):
            labels.write_key({"packet": 2}, inside, repo=tmp_path / "repo")
        assert not inside.exists()

    def test_this_checkout_is_the_default_repo(self) -> None:
        inside = labels.LABELS_DIR / "never-written-key.json"
        with pytest.raises(labels.LabelError, match="outside"):
            labels.write_key({"packet": 2}, inside)
        assert not inside.exists()


class TestALabelNamesItsPacket:
    def _item(self, label_id: str, label: str, reason: str, **kw: Any) -> labels.FilledLabel:
        return labels.FilledLabel(id=label_id, label=label, reason=reason, **kw)

    def test_a_row_without_a_packet_reads_as_packet_one(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "b.jsonl", *_PACKET_1_ROWS)
        assert all(labels.row_packet(r) == 1 for r in labels.read_lines(path))
        assert set(labels.current_labels(path, packet=1)) == set(_PINNED)
        assert labels.current_labels(path, packet=2) == {}

    def test_an_import_under_packet_two_writes_the_packet(self, tmp_path: Path) -> None:
        path = tmp_path / "b.jsonl"
        labels.import_labels(
            [self._item(_PINNED[1], "useful", "fine")], path=path, today=_TODAY, packet=2
        )
        assert json.loads(path.read_text()) == {
            "id": _PINNED[1],
            "label": "useful",
            "reason": "fine",
            "labelled_by": "owner",
            "labelled_at": "2026-10-11",
            "packet": 2,
        }

    def test_a_second_label_for_a_repeated_id_is_appended_not_refused(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "b.jsonl", *_PACKET_1_ROWS)
        before = path.read_bytes()
        first = _PACKET_1_ROWS[0]
        flipped = "useful" if first["label"] == "not_useful" else "not_useful"
        result = labels.import_labels(
            [self._item(first["id"], flipped, "read it again")], path=path, today=_TODAY, packet=2
        )
        assert result.appended == (first["id"],)
        assert result.refused == ()
        after = path.read_bytes()
        assert after.startswith(before)
        assert labels.current_labels(path, packet=1)[first["id"]]["label"] == first["label"]
        assert labels.current_labels(path, packet=2)[first["id"]]["label"] == flipped

    def test_inside_one_packet_a_changed_label_is_still_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "b.jsonl"
        labels.import_labels([self._item(_PINNED[2], "useful", "a")], path=path, packet=2)
        before = path.read_bytes()
        result = labels.import_labels(
            [self._item(_PINNED[2], "not_useful", "b")], path=path, packet=2
        )
        assert result.refused == (_PINNED[2],)
        assert path.read_bytes() == before

    def test_a_correction_names_its_own_packet(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "b.jsonl", *_PACKET_1_ROWS)
        with pytest.raises(labels.LabelError, match="never labelled"):
            labels.import_labels(
                [self._item(_PINNED[0], "useful", "x", supersedes=_PINNED[0])],
                path=path,
                packet=2,
            )

    def test_the_file_may_hold_one_id_once_per_packet(self, tmp_path: Path) -> None:
        ok = _write(tmp_path / "ok.jsonl", _row(_PINNED[0]), _row(_PINNED[0], packet=2))
        assert len(labels.read_lines(ok)) == 2
        bad = _write(tmp_path / "bad.jsonl", _row(_PINNED[0]), _row(_PINNED[0], packet=1))
        with pytest.raises(labels.LabelError, match="without supersedes"):
            labels.read_lines(bad)

    def test_a_correction_must_follow_a_label_in_its_own_packet(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "b.jsonl",
            _row(_PINNED[0]),
            _row(_PINNED[0], "not_useful", packet=2, supersedes=_PINNED[0]),
        )
        with pytest.raises(labels.LabelError, match="never labelled"):
            labels.read_lines(path)

    @pytest.mark.parametrize("packet", [0, -1, "2", True, 1.0])
    def test_a_packet_that_is_not_a_positive_whole_number_is_refused(
        self, tmp_path: Path, packet: object
    ) -> None:
        path = _write(tmp_path / "b.jsonl", _row(_PINNED[0], packet=packet))
        with pytest.raises(labels.LabelError, match="packet"):
            labels.read_lines(path)

    def test_without_a_packet_an_id_keeps_its_first_packets_label(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "b.jsonl",
            _row(_PINNED[0], "useful"),
            _row(_PINNED[0], "not_useful", packet=2),
            _row(_PINNED[1], "useful", packet=2),
        )
        in_force = labels.current_labels(path)
        assert in_force[_PINNED[0]]["label"] == "useful"
        assert labels.row_packet(in_force[_PINNED[0]]) == 1
        assert labels.row_packet(in_force[_PINNED[1]]) == 2
        assert labels.packets(path) == (1, 2)


class TestTestRetest:
    def test_it_pairs_the_ids_labelled_in_both_packets(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "b.jsonl",
            _row(_PINNED[0], "useful"),
            _row(_PINNED[1], "not_useful"),
            _row(_PINNED[2], "useful"),
            _row(_PINNED[1], "useful", packet=2),
            _row(_PINNED[0], "useful", packet=2),
            _row("759e198cdd27:remediate_consumer_lag_success", "useful", packet=2),
        )
        pairs = labels.test_retest(1, 2, path=path)
        assert [(p.id, p.first, p.second, p.agrees) for p in pairs] == sorted(
            [
                (_PINNED[0], "useful", "useful", True),
                (_PINNED[1], "not_useful", "useful", False),
            ]
        )

    def test_a_correction_inside_a_packet_is_the_label_compared(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "b.jsonl",
            _row(_PINNED[0], "not_useful"),
            _row(_PINNED[0], "useful", supersedes=_PINNED[0]),
            _row(_PINNED[0], "useful", packet=2),
        )
        (pair,) = labels.test_retest(1, 2, path=path)
        assert pair.agrees

    def test_no_overlap_is_an_empty_answer(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "b.jsonl", *_PACKET_1_ROWS)
        assert labels.test_retest(1, 2, path=path) == ()

    def test_one_packet_against_itself_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(labels.LabelError, match="two different packets"):
            labels.test_retest(1, 1, path=tmp_path / "b.jsonl")

    def test_pytest_does_not_collect_it(self) -> None:
        assert getattr(labels.test_retest, "__test__", True) is False


class TestTheCommittedLabels:
    def test_packet_one_rows_are_unchanged(self) -> None:
        lines = labels.LABELS_FILE.read_bytes().splitlines(keepends=True)
        assert hashlib.sha256(b"".join(lines[: labels.PACKET_SIZE])).hexdigest() == (
            _PACKET_1_ROWS_SHA
        )
        first = labels.read_lines()[: labels.PACKET_SIZE]
        assert [r["id"] for r in first] == list(_PINNED)
        assert all("packet" not in r and labels.row_packet(r) == 1 for r in first)
        assert set(labels.current_labels(packet=1)) == set(_PINNED)

    def test_each_committed_packet_has_its_own_number_and_names_committed_briefings(
        self,
    ) -> None:
        on_disk = labels.packets_on_disk()
        assert on_disk[1] == _PINNED
        for ids in on_disk.values():
            assert len(ids) == labels.PACKET_SIZE
            assert all(labels.briefing_path(i).is_file() for i in ids)

    def test_a_later_committed_packet_is_blind(self) -> None:
        for path in sorted(labels.LABELS_DIR.glob("packet.*.md")):
            text = path.read_text()
            if labels.packet_number(text) == 1:
                continue
            assert "seed: " in text.split("\n---\n", 1)[0]
            assert "Why it is in the packet" not in text
            assert "repeats" not in text


class TestTheCli:
    @pytest.fixture
    def folder(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        folder = tmp_path / "labels"
        folder.mkdir()
        (folder / _PACKET_1_FILE).write_text((labels.LABELS_DIR / _PACKET_1_FILE).read_text())
        _write(folder / "briefings.jsonl", *_PACKET_1_ROWS)
        monkeypatch.setattr(labels, "LABELS_DIR", folder)
        monkeypatch.setattr(labels, "LABELS_FILE", folder / "briefings.jsonl")
        monkeypatch.setattr(labels, "committed_archives", lambda **_: list(_ARCHIVES))
        return folder

    def _draw_args(self, key: Path, *extra: str) -> list[str]:
        return [
            "--packet",
            "2",
            "--repeats",
            "3",
            "--seed",
            str(_SEED),
            "--key-out",
            str(key),
            *extra,
        ]

    def test_it_writes_the_packet_and_the_key_apart(
        self, folder: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        key = tmp_path / "sealed" / "packet2-key.json"
        assert label_packet.main(self._draw_args(key)) == 0
        out = capsys.readouterr().out
        (packet,) = [p for p in folder.glob("packet.*.md") if p.name != _PACKET_1_FILE]
        sealed = json.loads(key.read_text())
        assert sealed["packet"] == 2 and sealed["seed"] == _SEED and len(sealed["repeats"]) == 3
        text = packet.read_text()
        assert labels.packet_number(text) == 2
        assert set(sealed["repeats"]) <= set(labels.packet_ids(text))
        assert f"seed: {_SEED}" in out
        assert str(key) in out
        assert not any(r in out for r in sealed["repeats"])
        assert "repeats" not in text

    def test_a_key_inside_the_repo_is_refused_before_anything_is_written(
        self, folder: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        inside = REPO_ROOT / "evals" / "never-written-key.json"
        assert label_packet.main(self._draw_args(inside)) == 2
        assert "outside" in capsys.readouterr().err
        assert not inside.exists()
        assert sorted(p.name for p in folder.glob("packet.*.md")) == [_PACKET_1_FILE]

    @pytest.mark.parametrize(
        ("args", "match"),
        [
            (["--packet", "2", "--repeats", "3", "--key-out", "KEY"], "--seed"),
            (["--packet", "2", "--repeats", "3", "--seed", "5"], "--key-out"),
            (["--repeats", "3", "--seed", "5", "--key-out", "KEY"], "packet 1"),
            ([], "packet 1 already exists"),
        ],
    )
    def test_an_incomplete_draw_is_refused(
        self,
        folder: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        args: list[str],
        match: str,
    ) -> None:
        key = tmp_path / "never.json"
        assert label_packet.main([str(key) if a == "KEY" else a for a in args]) == 2
        assert match in capsys.readouterr().err
        assert sorted(p.name for p in folder.glob("packet.*.md")) == [_PACKET_1_FILE]
        assert not key.exists()

    def test_packet_two_is_drawn_once(
        self, folder: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert label_packet.main(self._draw_args(tmp_path / "k1.json")) == 0
        assert label_packet.main(self._draw_args(tmp_path / "k2.json")) == 2
        assert "packet 2 already exists" in capsys.readouterr().err
        assert not (tmp_path / "k2.json").exists()

    def test_the_import_reads_the_packet_from_the_header(
        self, folder: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        key = tmp_path / "key.json"
        assert label_packet.main(self._draw_args(key)) == 0
        (packet,) = [p for p in folder.glob("packet.*.md") if p.name != _PACKET_1_FILE]
        repeat = json.loads(key.read_text())["repeats"][0]
        fresh = next(i for i in labels.packet_ids(packet.read_text()) if i not in _PINNED)
        filled = _fill(packet.read_text(), {repeat: "not_useful", fresh: "useful"})
        filled_path = tmp_path / "filled.md"
        filled_path.write_text(filled)
        before = labels.LABELS_FILE.read_bytes()

        assert label_packet.main(["--import", str(filled_path)]) == 0
        assert "packet 2" in capsys.readouterr().out
        after = labels.LABELS_FILE.read_bytes()
        assert after.startswith(before)
        added = [json.loads(line) for line in after[len(before) :].splitlines()]
        assert {(r["id"], r["packet"]) for r in added} == {(repeat, 2), (fresh, 2)}
        assert [p.id for p in labels.test_retest(1, 2)] == [repeat]

    def test_an_import_naming_another_packet_is_refused(
        self, folder: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        first = folder / _PACKET_1_FILE
        assert label_packet.main(["--import", str(first), "--packet", "2"]) == 2
        assert "packet 1" in capsys.readouterr().err


def _fill(text: str, answers: dict[str, str]) -> str:
    out: list[str] = []
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("## ") and "`" in line:
            current = line.split("`")[1]
        if current in answers and line == "LABEL: ":
            line = f"LABEL: {answers[current]}"
        if current in answers and line == "REASON: ":
            line = "REASON: read again"
        out.append(line)
    return "\n".join(out) + "\n"

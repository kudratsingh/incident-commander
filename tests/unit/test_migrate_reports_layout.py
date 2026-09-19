"""The reports-layout migration moves evidence and never loses any (WO-R3-257).

The only thing here that moves an eval artifact, so invariant 9 depends on a script
rather than on a write refusing to overwrite. A manifest first, sha256 either side, an
idempotent second run, a refusal rather than an overwrite, locks restored.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Final

import pytest

from evals import artifacts
from scripts.migrate_reports_layout import (
    EXIT_OK,
    EXIT_REFUSED,
    fingerprint,
    main,
    plan,
)

_RENDER = (
    "##########\nINCIDENT TRAJECTORY: {scenario}\n##########\n\nInvocation:    {inv} (1 of 1)\n"
)

# File flags are macOS/BSD only, so the uchg assertions reach them through one guarded
# name. CI is Linux: this is None there and the uchg test skips.
_CHFLAGS: Final[Callable[[Path, int], None] | None] = getattr(os, "chflags", None)


def _flags(path: Path) -> int:
    """``st_flags`` where the platform has it, else 0."""
    return int(getattr(path.stat(), "st_flags", 0))


def _report(root: Path, stamp: str, invocation: str) -> Path:
    path = root / f"report.{stamp}.{invocation}.json"
    path.write_text(json.dumps({"invocation_id": invocation}))
    return path


def _human(root: Path, scenario: str, stamp: str, render_id: str, invocation: str) -> Path:
    path = root / "human" / f"{scenario}.{stamp}.{render_id}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_RENDER.format(scenario=scenario, inv=invocation))
    return path


def _tree(tmp_path: Path) -> Path:
    """A reports folder shaped like the real one, in miniature."""
    root = tmp_path / "reports"
    (root / "human").mkdir(parents=True)
    (root / "dossiers").mkdir()
    (root / "baseline.json").write_text('{"blessed": true}')
    (root / "latest.json").write_text('{"legacy": true}')
    _report(root, "20260907T062014Z", "aaaaaaaa0001")
    _report(root, "20261001T101010Z", "bbbbbbbb0002")
    (root / "baseline_report.20260915T132550Z.cccccccc0003.json").write_text("{}")
    (root / "baseline_report.20260915T132550Z.cccccccc0003.md").write_text("# baseline")
    (root / "phase_close_report.20260917T103549Z.dddddddd0004.json").write_text("{}")
    (root / "phase_close_report.20260917T103549Z.dddddddd0004.md").write_text("# close")
    # alpha: three renders of two runs. Two of them render the SAME run.
    _human(root, "alpha", "20260901T000000Z", "r1", "invA")
    _human(root, "alpha", "20260902T000000Z", "r2", "invA")
    _human(root, "alpha", "20260903T000000Z", "r3", "invB")
    _human(root, "beta", "20260901T000000Z", "r1", "invC")
    (root / "dossiers" / "alpha.20260908T012611Z.eeeeeeee0005.md").write_text("# dossier")
    return root


def _paths(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestThePlan:
    """Where every file is headed, decided before anything is touched."""

    def test_each_family_lands_in_its_own_folder(self, tmp_path: Path) -> None:
        root = _tree(tmp_path)
        destinations = {
            move.source.relative_to(root).as_posix(): move.destination.relative_to(root).as_posix()
            for move in plan(root).moves
        }
        assert destinations == {
            "report.20260907T062014Z.aaaaaaaa0001.json": (
                "runs/2026-09/report.20260907T062014Z.aaaaaaaa0001.json"
            ),
            "report.20261001T101010Z.bbbbbbbb0002.json": (
                "runs/2026-10/report.20261001T101010Z.bbbbbbbb0002.json"
            ),
            "baseline_report.20260915T132550Z.cccccccc0003.json": (
                "baseline/baseline_report.20260915T132550Z.cccccccc0003.json"
            ),
            "baseline_report.20260915T132550Z.cccccccc0003.md": (
                "baseline/baseline_report.20260915T132550Z.cccccccc0003.md"
            ),
            "phase_close_report.20260917T103549Z.dddddddd0004.json": (
                "phase-close/phase_close_report.20260917T103549Z.dddddddd0004.json"
            ),
            "phase_close_report.20260917T103549Z.dddddddd0004.md": (
                "phase-close/phase_close_report.20260917T103549Z.dddddddd0004.md"
            ),
            "human/alpha.20260901T000000Z.r1.txt": (
                "human/_superseded/alpha/alpha.20260901T000000Z.r1.txt"
            ),
            "human/alpha.20260902T000000Z.r2.txt": "human/alpha/alpha.20260902T000000Z.r2.txt",
            "human/alpha.20260903T000000Z.r3.txt": "human/alpha/alpha.20260903T000000Z.r3.txt",
            "human/beta.20260901T000000Z.r1.txt": "human/beta/beta.20260901T000000Z.r1.txt",
            "dossiers/alpha.20260908T012611Z.eeeeeeee0005.md": (
                "dossiers/alpha/alpha.20260908T012611Z.eeeeeeee0005.md"
            ),
        }

    def test_the_two_pinned_files_are_left_alone(self, tmp_path: Path) -> None:
        """``baseline.json`` feeds the gate by literal path; ``latest.json`` is evidence."""
        root = _tree(tmp_path)
        left = {path.name for path, _ in plan(root).left_in_place}
        assert left == {"baseline.json", "latest.json"}

    def test_an_unrecognised_file_is_left_in_place_and_reported(self, tmp_path: Path) -> None:
        """Never guess. A file the layout has no rule for stays exactly where it is."""
        root = _tree(tmp_path)
        (root / "notes-from-a-human.md").write_text("read me")
        migration = plan(root)
        left = {path.name: reason for path, reason in migration.left_in_place}
        assert "notes-from-a-human.md" in left
        assert "unrecognised" in left["notes-from-a-human.md"]

    def test_the_newest_render_of_each_run_is_the_one_that_stays(self, tmp_path: Path) -> None:
        """ "The same run" is read from the report's headers, not from its name.

        Two renders carry two ids and one ``Invocation:`` header.
        """
        root = _tree(tmp_path)
        by_reason = {
            move.source.name: move.reason
            for move in plan(root).moves
            if move.source.parent.name == "human"
        }
        assert by_reason["alpha.20260902T000000Z.r2.txt"] == "newest render of this run"
        assert by_reason["alpha.20260901T000000Z.r1.txt"].startswith("earlier render")
        assert by_reason["alpha.20260903T000000Z.r3.txt"] == "newest render of this run"

    def test_a_render_with_no_readable_header_is_never_moved_aside(self, tmp_path: Path) -> None:
        """The rule may only supersede a file it can prove is a repeat."""
        root = _tree(tmp_path)
        orphan = root / "human" / "gamma.20260901T000000Z.r1.txt"
        orphan.write_text("no header at all")
        move = next(m for m in plan(root).moves if m.source == orphan)
        assert move.destination == root / "human" / "gamma" / orphan.name


class TestTheMigration:
    """What actually happens on disk."""

    def test_nothing_is_lost_and_nothing_is_altered(self, tmp_path: Path) -> None:
        root = _tree(tmp_path)
        before = fingerprint(root)

        assert main(["--root", str(root), "--manifest", str(tmp_path / "m.json")]) == EXIT_OK

        after = fingerprint(root)
        assert len(before) == len(after), "the migration changed the file count"
        assert sorted(before.values()) == sorted(after.values()), (
            "a moved file's sha256 changed — that is not a move"
        )

    def test_every_moved_file_keeps_its_bytes(self, tmp_path: Path) -> None:
        """Per file, not just in aggregate: old digest, new path, same digest."""
        root = _tree(tmp_path)
        expected = {move.destination: _sha(move.source) for move in plan(root).moves}

        assert main(["--root", str(root), "--manifest", str(tmp_path / "m.json")]) == EXIT_OK

        for destination, digest in expected.items():
            assert destination.is_file(), f"{destination} was not created"
            assert _sha(destination) == digest

    def test_a_second_run_is_a_no_op(self, tmp_path: Path) -> None:
        root = _tree(tmp_path)
        assert main(["--root", str(root), "--manifest", str(tmp_path / "m1.json")]) == EXIT_OK
        after_first = _paths(root)

        assert main(["--root", str(root), "--manifest", str(tmp_path / "m2.json")]) == EXIT_OK

        assert _paths(root) == after_first
        document = json.loads((tmp_path / "m2.json").read_text())
        assert document["moves"] == []

    def test_it_refuses_rather_than_overwriting(self, tmp_path: Path) -> None:
        """A destination that already holds something ends the run before any move."""
        root = _tree(tmp_path)
        squatter = root / "runs" / "2026-09" / "report.20260907T062014Z.aaaaaaaa0001.json"
        squatter.parent.mkdir(parents=True)
        squatter.write_text("someone else's file")
        before = fingerprint(root)

        assert main(["--root", str(root), "--manifest", str(tmp_path / "m.json")]) == EXIT_REFUSED

        assert fingerprint(root) == before, "a refused run still moved something"
        assert squatter.read_text() == "someone else's file"

    def test_the_resolver_finds_everything_afterwards(self, tmp_path: Path) -> None:
        """The point of the exercise: the tree is tidier and still resolves."""
        root = _tree(tmp_path)
        assert main(["--root", str(root), "--manifest", str(tmp_path / "m.json")]) == EXIT_OK

        assert artifacts.newest("report", directory=root).name.startswith("report.20261001")
        assert artifacts.newest("baseline_report", directory=root).parent == root / "baseline"
        assert (
            artifacts.newest("phase_close_report_md", directory=root).parent == root / "phase-close"
        )
        human = root / "human"
        assert artifacts.newest("human", "alpha", directory=human).name.endswith("r3.txt")
        # The superseded render is still a version, just not the newest.
        assert len(artifacts.versions("human", "alpha", directory=human)) == 3
        assert artifacts.newest("dossier", "alpha", directory=root / "dossiers").parent == (
            root / "dossiers" / "alpha"
        )


class TestTheManifest:
    """The record the coordinator commits to the hub."""

    def test_dry_run_writes_the_manifest_and_moves_nothing(self, tmp_path: Path) -> None:
        root = _tree(tmp_path)
        before = fingerprint(root)
        manifest = tmp_path / "dry.json"

        assert main(["--root", str(root), "--dry-run", "--manifest", str(manifest)]) == EXIT_OK

        assert fingerprint(root) == before, "a dry run touched the tree"
        document = json.loads(manifest.read_text())
        assert document["dry_run"] is True
        assert document["work_order"] == "WO-R3-257"
        assert len(document["moves"]) == 11
        assert document["destinations"]["runs/2026-09"] == 1
        assert document["destinations"]["human/_superseded/alpha"] == 1
        assert {entry["path"] for entry in document["left_in_place"]} == {
            "baseline.json",
            "latest.json",
        }

    def test_the_real_run_records_the_verification(self, tmp_path: Path) -> None:
        root = _tree(tmp_path)
        manifest = tmp_path / "m.json"

        assert main(["--root", str(root), "--manifest", str(manifest)]) == EXIT_OK

        document = json.loads(manifest.read_text())
        assert document["dry_run"] is False
        assert document["files_before"] == document["files_after"]
        assert document["count_unchanged"] is True
        assert document["digests_unchanged"] is True

    def test_the_dry_run_prints_every_move(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = _tree(tmp_path)
        main(["--root", str(root), "--dry-run", "--manifest", str(tmp_path / "dry.json")])
        printed = capsys.readouterr().out
        assert (
            "report.20260907T062014Z.aaaaaaaa0001.json  ->  "
            "runs/2026-09/report.20260907T062014Z.aaaaaaaa0001.json"
        ) in printed
        assert "dry run — nothing was moved" in printed


class TestTheRails:
    """The refusals that keep this pointed at one folder."""

    def test_it_refuses_a_folder_that_is_not_named_reports(self, tmp_path: Path) -> None:
        """Pointed at ``evals/runs/`` — or a home directory — it does nothing.

        Archives are locked by ADR 0021.
        """
        runs = tmp_path / "runs"
        (runs / "deadbeefcafe").mkdir(parents=True)
        (runs / "deadbeefcafe" / "report.json").write_text("{}")
        before = fingerprint(runs)

        assert main(["--root", str(runs)]) == EXIT_REFUSED
        assert fingerprint(runs) == before

    def test_a_missing_folder_is_refused_not_created(self, tmp_path: Path) -> None:
        missing = tmp_path / "reports"
        assert main(["--root", str(missing)]) == EXIT_REFUSED
        assert not missing.exists()


class TestLocks:
    """Evidence is write-locked on disk (ADR 0021). It must be locked again after."""

    @staticmethod
    def _lock(path: Path) -> None:
        os.chmod(path, 0o444 if path.is_file() else 0o555)

    def test_a_locked_tree_is_migrated_and_relocked(self, tmp_path: Path) -> None:
        root = _tree(tmp_path)
        for path in sorted(root.rglob("*"), reverse=True):
            self._lock(path)
        self._lock(root)
        before = fingerprint(root)

        assert main(["--root", str(root), "--manifest", str(tmp_path / "m.json")]) == EXIT_OK

        after = fingerprint(root)
        assert sorted(before.values()) == sorted(after.values())
        for path in root.rglob("*"):
            mode = stat.S_IMODE(path.stat().st_mode)
            assert not mode & stat.S_IWUSR, f"{path} came back writable"

    @pytest.mark.skipif(_CHFLAGS is None, reason="no file flags on this platform")
    def test_a_uchg_file_is_unlocked_moved_and_re_flagged(self, tmp_path: Path) -> None:
        """macOS ``uchg`` refuses a rename outright, so it must come off and go back.

        Skipped without file flags; the mode half runs everywhere.
        """
        assert _CHFLAGS is not None  # guarded by the skipif above
        root = _tree(tmp_path)
        target = root / "report.20260907T062014Z.aaaaaaaa0001.json"
        os.chmod(target, 0o444)
        _CHFLAGS(target, stat.UF_IMMUTABLE)
        try:
            assert main(["--root", str(root), "--manifest", str(tmp_path / "m.json")]) == EXIT_OK
            moved = root / "runs" / "2026-09" / target.name
            assert moved.is_file()
            assert _flags(moved) & stat.UF_IMMUTABLE, "the lock was not put back"
        finally:
            for path in root.rglob("*"):
                _CHFLAGS(path, 0)
                os.chmod(path, 0o644 if path.is_file() else 0o755)


class TestGitTrackedFilesMoveWithGitMv:
    """History follows a tracked file, instead of reading as a delete plus an add."""

    def test_a_tracked_file_moves_through_git(self, tmp_path: Path) -> None:
        # ``.git`` must not be under the folder being migrated, or the index counts as evidence.
        root = tmp_path / "reports"
        root.mkdir()
        _report(root, "20260907T062014Z", "aaaaaaaa0001")
        for command in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "t@example.com"],
            ["git", "config", "user.name", "t"],
            ["git", "add", "-A"],
            ["git", "commit", "-qm", "seed"],
        ):
            subprocess.run(command, cwd=tmp_path, check=True, capture_output=True)

        migration = plan(root)
        assert [move.tracked for move in migration.moves] == [True]

        assert main(["--root", str(root), "--manifest", str(tmp_path / "m.json")]) == EXIT_OK

        staged = subprocess.run(
            ["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, check=True
        ).stdout.decode()
        assert staged.startswith("R "), f"not recorded as a rename: {staged!r}"

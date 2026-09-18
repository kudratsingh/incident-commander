#!/usr/bin/env python3
"""Move an existing ``evals/reports/`` tree into the per-folder layout. Never deletes.

The one-time move into the layout ``evals/artifacts.py`` defines and
``evals/reports/README.md`` describes, restating none of it:

    report → runs/<YYYY-MM>/; baseline_report → baseline/; phase_close_report →
        phase-close/; dossiers/<scenario>.… → dossiers/<scenario>/
    human/<scenario>.… → human/<scenario>/ for a run's newest render, else _superseded/
    baseline.json, latest.json, README.md → left exactly where they are

Everything is a move (invariant 9): the repeat renders that are 93% of ``human/`` stay
resolvable under ``_superseded/``. Every file is sha256'd before and after, an existing
destination ends the run before anything moves, and locks (ADR 0021) are restored as found.
Migrate the ``--root`` hub mirror FIRST, or the next ``rsync`` copies every file again.

Usage:
    uv run python scripts/migrate_reports_layout.py --dry-run
    uv run python scripts/migrate_reports_layout.py --root ../evidence/reports
    uv run python scripts/migrate_reports_layout.py
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import stat
import subprocess
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

_REPO_ROOT = Path(__file__).resolve().parents[1]
# Same bootstrap, same reason, as scripts/format_traces.py: this file is documented as
# runnable directly, and `evals` sits outside the installed package.
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from evals import artifacts  # noqa: E402
from scripts.format_traces import rendered_invocations  # noqa: E402

EXIT_OK: Final[int] = 0
EXIT_REFUSED: Final[int] = 2

#: Top-of-folder files that never move: ``baseline.json`` (the regression gate's input)
#: and ``latest.json`` are named in Makefile recipes and in `evals.yml`'s path filter.
PINNED_AT_ROOT: Final[frozenset[str]] = frozenset(
    {"baseline.json", "latest.json", "README.md", ".gitkeep", ".DS_Store"}
)

#: Top-level report families: filename stem → (artifact kind, suffix).
TOP_LEVEL_KINDS: Final[tuple[tuple[str, str, str], ...]] = (
    ("report", "report", ".json"),
    ("baseline_report", "baseline_report", ".json"),
    ("baseline_report", "baseline_report_md", ".md"),
    ("phase_close_report", "phase_close_report", ".json"),
    ("phase_close_report", "phase_close_report_md", ".md"),
)


@dataclass(frozen=True)
class Move:
    """One file's old home and its new one."""

    source: Path
    destination: Path
    reason: str
    tracked: bool = False

    def as_record(self, root: Path) -> dict[str, object]:
        return {
            "from": self.source.relative_to(root).as_posix(),
            "to": self.destination.relative_to(root).as_posix(),
            "reason": self.reason,
            "git_tracked": self.tracked,
        }


@dataclass
class Plan:
    """What the migration would do, before it does anything."""

    root: Path
    moves: list[Move] = field(default_factory=list)
    left_in_place: list[tuple[Path, str]] = field(default_factory=list)

    def destinations_by_folder(self) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for move in self.moves:
            counts[move.destination.parent.relative_to(self.root).as_posix()] += 1
        return dict(sorted(counts.items()))

    def conflicts(self) -> list[Move]:
        """Destinations that already hold something. Any one of these stops the run."""
        seen: set[Path] = set()
        clashes: list[Move] = []
        for move in self.moves:
            if move.destination.exists() or move.destination in seen:
                clashes.append(move)
            seen.add(move.destination)
        return clashes


# --- reading the tree -----------------------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(root: Path) -> dict[str, str]:
    """``{path relative to root: sha256}``: the before/after check on every move."""
    return {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _tracked_files(root: Path) -> set[str]:
    """Paths under ``root`` that git tracks, relative to ``root``.

    These move with ``git mv`` so history follows the file; a root outside a work tree
    answers the empty set.
    """
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "ls-files", "-z"],  # noqa: S607
            cwd=root,
            capture_output=True,
            check=False,
        )
    except OSError:
        return set()
    if result.returncode != 0:
        return set()
    return {name for name in result.stdout.decode().split("\0") if name}


# --- building the plan ----------------------------------------------------


def _human_moves(root: Path, tracked: set[str]) -> tuple[list[Move], list[tuple[Path, str]]]:
    """Split the flat ``human/`` folder into per-scenario folders.

    "The same run" is the invocation ids the report's headers name, not its FILENAME id.
    """
    container = root / "human"
    if not container.is_dir():
        return [], []
    moves: list[Move] = []
    left: list[tuple[Path, str]] = []
    inside = _rel(tracked, "human")
    by_scenario: dict[str, list[tuple[tuple[str, str], Path]]] = defaultdict(list)
    for path in sorted(container.iterdir()):
        if not path.is_file() or path.name in PINNED_AT_ROOT:
            continue
        parsed = artifacts.parse_version_name(path.name, suffix=".txt")
        if parsed is None:
            if path.suffix == ".txt":
                # `<scenario>.txt` — a pre-versioning render, the oldest version of its
                # family. It keeps its name and moves in with the rest.
                by_scenario[path.stem].append((("", ""), path))
            else:
                left.append((path, "not a human report"))
            continue
        by_scenario[parsed.stem].append(((parsed.stamp, parsed.invocation_id), path))

    for scenario, entries in sorted(by_scenario.items()):
        groups: dict[object, list[tuple[tuple[str, str], Path]]] = defaultdict(list)
        for key, path in entries:
            covered = frozenset(rendered_invocations(path))
            # No readable header → key on the file itself, so it is a group of one and is
            # never moved aside as somebody else's repeat.
            groups[covered or path.name].append((key, path))
        keep_dir = artifacts.write_directory("human", scenario, directory=container)
        aside_dir = container / artifacts.SUPERSEDED_DIR / scenario
        for _, members in sorted(groups.items(), key=lambda item: str(item[0])):
            members.sort(key=lambda pair: pair[0])
            newest = members[-1][1]
            for _, path in members:
                target = keep_dir if path == newest else aside_dir
                reason = (
                    "newest render of this run"
                    if path == newest
                    else "earlier render of a run that has a newer one"
                )
                moves.append(Move(path, target / path.name, reason, path.name in inside))
    return moves, left


def _rel(tracked: set[str], prefix: str) -> set[str]:
    """The tracked names inside one sub-folder, without the prefix."""
    head = f"{prefix}/"
    return {name[len(head) :] for name in tracked if name.startswith(head)}


def _scenario_moves(
    root: Path, folder: str, kind: str, suffix: str, tracked: set[str]
) -> list[Move]:
    """Flat ``<folder>/<scenario>.<stamp>.<id><suffix>`` → ``<folder>/<scenario>/``."""
    container = root / folder
    if not container.is_dir():
        return []
    moves: list[Move] = []
    inside = _rel(tracked, folder)
    for path in sorted(container.iterdir()):
        if not path.is_file() or path.name in PINNED_AT_ROOT:
            continue
        parsed = artifacts.parse_version_name(path.name, suffix=suffix)
        if parsed is None:
            continue
        target = artifacts.write_directory(kind, parsed.stem, directory=container)
        moves.append(
            Move(path, target / path.name, f"{folder} for {parsed.stem}", path.name in inside)
        )
    return moves


def plan(root: Path) -> Plan:
    """Everything the migration would move, and everything it would leave alone."""
    tracked = _tracked_files(root)
    result = Plan(root=root)

    for path in sorted(root.iterdir()):
        if path.is_dir():
            continue
        if path.name in PINNED_AT_ROOT:
            result.left_in_place.append((path, "pinned at the top of the folder"))
            continue
        for stem, kind, suffix in TOP_LEVEL_KINDS:
            parsed = artifacts.parse_version_name(path.name, suffix=suffix)
            if parsed is None or parsed.stem != stem:
                continue
            target = artifacts.write_directory(kind, timestamp=parsed.timestamp(), directory=root)
            result.moves.append(Move(path, target / path.name, f"{stem}", path.name in tracked))
            break
        else:
            result.left_in_place.append((path, "unrecognised — left untouched"))

    human_moves, human_left = _human_moves(root, tracked)
    result.moves.extend(human_moves)
    result.left_in_place.extend(human_left)
    result.moves.extend(_scenario_moves(root, "dossiers", "dossier", ".md", tracked))
    return result


# --- unlocking, moving, relocking ----------------------------------------

_IMMUTABLE: Final[int] = stat.UF_IMMUTABLE

#: ``os.chflags`` where the platform has file flags, ``None`` elsewhere (Linux, so CI,
#: has none: flags read as 0, nothing is re-flagged, modes still restore — ADR 0021).
_CHFLAGS: Final[Callable[[Path, int], None] | None] = getattr(os, "chflags", None)


def _file_flags(info: os.stat_result) -> int:
    """``st_flags`` where the platform has it, else 0 — see ``_CHFLAGS``."""
    return int(getattr(info, "st_flags", 0))


@dataclass(frozen=True)
class LockState:
    """A path's mode and flags exactly as they were found.

    ``flags`` is 0 where the platform has none, so every immutability test below is False.
    """

    mode: int
    flags: int


def _state(path: Path) -> LockState:
    info = path.stat()
    return LockState(mode=stat.S_IMODE(info.st_mode), flags=_file_flags(info))


def _unlock(path: Path) -> LockState:
    """Clear ``uchg``, add the owner write bit, report what was there.

    Best-effort, as ADR 0021's ``_lock_path`` is: let the move fail on its own terms.
    """
    original = _state(path)
    if _CHFLAGS is not None and original.flags & _IMMUTABLE:
        with contextlib.suppress(OSError):
            _CHFLAGS(path, original.flags & ~_IMMUTABLE)
    if not original.mode & stat.S_IWUSR:
        with contextlib.suppress(OSError):
            os.chmod(path, original.mode | stat.S_IWUSR)
    return original


def _relock(path: Path, original: LockState) -> None:
    """Put the mode and the flags back, in that order (flags last, or chmod fails)."""
    with contextlib.suppress(OSError):
        os.chmod(path, original.mode)
    if _CHFLAGS is not None and original.flags & _IMMUTABLE:
        with contextlib.suppress(OSError):
            _CHFLAGS(path, original.flags)


def _git_mv(root: Path, move: Move) -> None:
    source = move.source.relative_to(root).as_posix()
    destination = move.destination.relative_to(root).as_posix()
    result = subprocess.run(  # noqa: S603
        ["git", "mv", "--", source, destination],  # noqa: S607
        cwd=root,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise OSError(f"git mv {source} -> {destination} failed: {result.stderr.decode().strip()}")


def _nearest_state(path: Path, states: dict[Path, LockState]) -> LockState | None:
    """The recorded lock state of the closest ancestor of ``path``.

    Every directory in the hub mirror is ``a-w``; one created at 755 is a writable hole.
    """
    for parent in path.parents:
        if parent in states:
            return states[parent]
    return None


def apply(migration: Plan) -> list[Path]:
    """Perform the moves; returns the directories created, for the caller's report.

    Unlock, move, relock — new directories included, so a locked tree stays locked.
    """
    root = migration.root
    touched = {root}
    touched |= {move.source.parent for move in migration.moves}
    touched |= {move.destination.parent for move in migration.moves}
    existing = sorted({d for d in touched if d.is_dir()}, key=lambda p: len(p.parts))
    dir_states = {directory: _unlock(directory) for directory in existing}

    created: list[Path] = []
    try:
        for move in migration.moves:
            parent = move.destination.parent
            if not parent.is_dir():
                # Record every level that did not exist, not just the leaf: a level
                # nobody recorded is a level nobody relocks.
                missing = [p for p in (parent, *parent.parents) if not p.exists()]
                parent.mkdir(parents=True, exist_ok=True)
                created.extend(missing)
            file_state = _unlock(move.source)
            if move.tracked:
                _git_mv(root, move)
            else:
                # Checked by `conflicts()` before anything moved, and again here:
                # os.replace is silent about an existing destination (invariant 9).
                if move.destination.exists():
                    raise FileExistsError(f"destination already exists: {move.destination}")
                os.replace(move.source, move.destination)
            _relock(move.destination, file_state)
    finally:
        # Deepest first: a created directory's state comes from an ancestor, so read the
        # ancestors before they are locked back down.
        for directory in sorted(set(created), key=lambda p: len(p.parts), reverse=True):
            inherited = _nearest_state(directory, dir_states)
            if inherited is not None:
                _relock(directory, inherited)
        for directory, original in dir_states.items():
            _relock(directory, original)
    return sorted(set(created))


# --- the manifest ---------------------------------------------------------


def manifest_document(
    migration: Plan,
    *,
    dry_run: bool,
    before: dict[str, str],
    after: dict[str, str] | None,
) -> dict[str, object]:
    root = migration.root
    document: dict[str, object] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "root": str(root),
        "dry_run": dry_run,
        "work_order": "WO-R3-257",
        "files_before": len(before),
        "moves": [move.as_record(root) for move in migration.moves],
        "left_in_place": [
            {"path": path.relative_to(root).as_posix(), "reason": reason}
            for path, reason in migration.left_in_place
        ],
        "destinations": migration.destinations_by_folder(),
    }
    if after is not None:
        document["files_after"] = len(after)
        document["count_unchanged"] = len(before) == len(after)
        document["digests_unchanged"] = sorted(before.values()) == sorted(after.values())
    return document


def _print_summary(migration: Plan, document: dict[str, object], *, dry_run: bool) -> None:
    root = migration.root
    label = "WOULD MOVE" if dry_run else "MOVED"
    print(f"reports layout migration — root {root}")
    print(f"  files under the root: {document['files_before']}")
    print(f"  {label}: {len(migration.moves)} file(s)")
    for folder, count in migration.destinations_by_folder().items():
        print(f"    {folder}/  {count}")
    print(f"  left in place: {len(migration.left_in_place)} file(s)")
    for path, reason in migration.left_in_place:
        print(f"    {path.relative_to(root).as_posix()}  — {reason}")


def _print_moves(migration: Plan) -> None:
    root = migration.root
    for move in migration.moves:
        mark = "git" if move.tracked else "   "
        print(
            f"  {mark}  {move.source.relative_to(root).as_posix()}"
            f"  ->  {move.destination.relative_to(root).as_posix()}"
        )


# --- entry point ----------------------------------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, add_help=True)
    parser.add_argument(
        "--root",
        type=Path,
        default=_REPO_ROOT / "evals" / "reports",
        help="the reports folder to migrate (the hub mirror is evidence/reports)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the manifest old -> new and change nothing",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="where to write the manifest (default: ./reports-layout-migration.<stamp>.json)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    root: Path = args.root.resolve()

    if not root.is_dir():
        print(f"no such reports folder: {root}", file=sys.stderr)
        return EXIT_REFUSED
    if root.name != "reports":
        # A deliberate rail: this script unlocks and moves evidence, and pointed at
        # `evals/runs/` or a home directory it would do so there.
        print(
            f"refusing: {root} is not named 'reports'. This migrates a reports "
            "folder and nothing else; evals/runs/ is never touched.",
            file=sys.stderr,
        )
        return EXIT_REFUSED

    migration = plan(root)
    before = fingerprint(root)

    clashes = migration.conflicts()
    if clashes:
        print("refusing: these destinations already exist", file=sys.stderr)
        for move in clashes:
            print(f"  {move.destination}", file=sys.stderr)
        return EXIT_REFUSED

    stamp = artifacts.stamp(datetime.now(UTC))
    manifest_path: Path = args.manifest or Path.cwd() / f"reports-layout-migration.{stamp}.json"

    if args.dry_run:
        _print_moves(migration)
        document = manifest_document(migration, dry_run=True, before=before, after=None)
        manifest_path.write_text(json.dumps(document, indent=2) + "\n")
        _print_summary(migration, document, dry_run=True)
        print(f"  manifest: {manifest_path}")
        print("  dry run — nothing was moved")
        return EXIT_OK

    if not migration.moves:
        document = manifest_document(migration, dry_run=False, before=before, after=before)
        manifest_path.write_text(json.dumps(document, indent=2) + "\n")
        _print_summary(migration, document, dry_run=False)
        print("  already migrated — nothing to do")
        return EXIT_OK

    apply(migration)
    after = fingerprint(root)
    document = manifest_document(migration, dry_run=False, before=before, after=after)
    manifest_path.write_text(json.dumps(document, indent=2) + "\n")
    _print_summary(migration, document, dry_run=False)
    print(f"  manifest: {manifest_path}")

    if not document["count_unchanged"] or not document["digests_unchanged"]:
        print(
            f"VERIFICATION FAILED: {document['files_before']} files before, "
            f"{document['files_after']} after; digests unchanged: "
            f"{document['digests_unchanged']}. See {manifest_path}.",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    print(f"  verified: {document['files_after']} files, every sha256 unchanged, nothing deleted")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

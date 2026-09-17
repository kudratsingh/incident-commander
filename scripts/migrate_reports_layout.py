#!/usr/bin/env python3
"""Move an existing ``evals/reports/`` tree into the per-folder layout. Never deletes.

The owner's words, 2026-09-17: "the human folder under reports was not
organized, that whole reports folder should be better organized". The new
layout is defined in ``evals/artifacts.py`` and described for a human in
``evals/reports/README.md``; this script is the one-time move that brings an
existing folder to it, and the ONLY thing in the repo that moves an eval
artifact.

    report.<stamp>.<id>.json            → runs/<YYYY-MM>/
    baseline_report.<stamp>.<id>.{json,md} → baseline/
    phase_close_report.<stamp>.<id>.{json,md} → phase-close/
    dossiers/<scenario>.<stamp>.<id>.md → dossiers/<scenario>/
    human/<scenario>.<stamp>.<id>.txt   → human/<scenario>/       (newest render of a run)
                                        → human/_superseded/…     (an older render of that run)
    baseline.json, latest.json, README.md → left exactly where they are

**Everything is a move.** CLAUDE.md invariant 9 and the workspace rule above
it make eval evidence append-only: never truncated, never overwritten, never
deleted. A repeat render is not a duplicate to clean up, it is a file that
was written once and must exist forever — so the 93% of ``human/`` that is
repeat renders moves to ``_superseded/`` and stays readable, resolvable
(``artifacts.versions``) and indexed. Nothing here calls ``unlink``.

**Verified, not asserted.** Every file under the root is sha256'd before and
after. The run refuses to report success unless the file count is unchanged
and the multiset of digests is unchanged — a move that lost or altered a byte
is a failure, loudly, with the manifest already on disk.

**Refuses rather than overwrites.** A destination that already exists ends
the run before anything moves. That is what makes a second run safe: the
first run left nothing at the top level to move, so the second finds nothing
to do and says so.

**Locks are restored exactly as found.** Run archives and the hub mirror are
write-locked by the filesystem (ADR 0021, ``evidence/sync.sh``). This unlocks
only the files and directories it must touch — ``chflags nouchg`` then
``chmod u+w`` — and puts every original mode and flag back afterwards,
including on the directories it creates, which inherit the mode their parent
had before the unlock.

**It takes a root, so the mirror uses the same code.** The hub keeps a copy
at ``audit-ws/evidence/reports`` and syncs it with ``rsync
--ignore-existing`` — so the mirror must be migrated BEFORE the next sync, or
every file is copied a second time under its new name. Same script, two
roots, mirror first.

Usage:
    uv run python scripts/migrate_reports_layout.py --dry-run
    uv run python scripts/migrate_reports_layout.py --root ../evidence/reports --dry-run
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
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

_REPO_ROOT = Path(__file__).resolve().parents[1]
# Same bootstrap, and the same reason, as scripts/format_traces.py: this file
# is documented as runnable directly, and `evals` sits outside the installed
# package. The layout itself is NOT duplicated here — every destination is
# computed by evals/artifacts.py, so the migration and the resolver cannot
# disagree about where a file belongs.
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from evals import artifacts  # noqa: E402
from scripts.format_traces import rendered_invocations  # noqa: E402

EXIT_OK: Final[int] = 0
EXIT_REFUSED: Final[int] = 2

#: Files that belong at the top of the reports folder and never move.
#: ``baseline.json`` is the regression gate's input and ``latest.json`` is
#: pre-versioning evidence; both are named in Makefile recipes, in
#: `.github/workflows/evals.yml`'s path filter and in three documents. Moving
#: either would be a behaviour change wearing a tidy-up's clothes.
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
    """``{path relative to root: sha256}`` for every file under ``root``.

    The before/after comparison that turns "it moved the files" from a claim
    into a check. Symlinks are followed only if they point at a regular file;
    nothing in this tree is a symlink by design (evals/artifacts.py rejected
    the idea explicitly), so one appearing is worth the crash.
    """
    return {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _tracked_files(root: Path) -> set[str]:
    """Paths under ``root`` that git tracks, relative to ``root``.

    ``git mv`` is used for these so history follows the file instead of
    reading as a delete plus an add. A root outside a work tree (the hub
    mirror is gitignored) answers the empty set and everything moves with
    ``os.replace``.
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

    One file per distinct RUN stays in ``human/<scenario>/``: the newest
    render of it. Every other render of that same run moves to
    ``human/_superseded/<scenario>/``. "The same run" is the set of
    invocation ids the report renders, read from its own headers — the id in
    a report's FILENAME names the render session, not the traced run, so the
    filename cannot answer this and the file has to.

    A render whose headers cannot be read is treated as its own run and kept:
    the rule may only move a file it can prove is a repeat.
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
                # `<scenario>.txt` — a pre-versioning render, the oldest
                # version of its family (evals/artifacts.py). It keeps its
                # name and moves into its scenario's folder with the rest.
                by_scenario[path.stem].append((("", ""), path))
            else:
                left.append((path, "not a human report"))
            continue
        by_scenario[parsed.stem].append(((parsed.stamp, parsed.invocation_id), path))

    for scenario, entries in sorted(by_scenario.items()):
        groups: dict[object, list[tuple[tuple[str, str], Path]]] = defaultdict(list)
        for key, path in entries:
            covered = frozenset(rendered_invocations(path))
            # No readable header → key on the file itself, so it is a group
            # of one and is never moved aside as somebody else's repeat.
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

_IMMUTABLE: Final[int] = getattr(stat, "UF_IMMUTABLE", 0)


@dataclass(frozen=True)
class LockState:
    """A path's mode and flags exactly as they were found."""

    mode: int
    flags: int


def _state(path: Path) -> LockState:
    info = path.stat()
    return LockState(mode=stat.S_IMODE(info.st_mode), flags=getattr(info, "st_flags", 0))


def _unlock(path: Path) -> LockState:
    """Clear ``uchg`` then add the owner write bit, and report what was there.

    Best-effort in the same spirit as ADR 0021's ``_lock_path``: a filesystem
    that refuses ``chflags`` or ``chmod`` should not stop the migration, it
    should let the move fail on its own terms with a real error.
    """
    original = _state(path)
    if _IMMUTABLE and original.flags & _IMMUTABLE:
        with contextlib.suppress(OSError):
            os.chflags(path, original.flags & ~_IMMUTABLE)
    if not original.mode & stat.S_IWUSR:
        with contextlib.suppress(OSError):
            os.chmod(path, original.mode | stat.S_IWUSR)
    return original


def _relock(path: Path, original: LockState) -> None:
    """Put the mode and the flags back, in that order (flags last, or chmod fails)."""
    with contextlib.suppress(OSError):
        os.chmod(path, original.mode)
    if _IMMUTABLE and original.flags & _IMMUTABLE:
        with contextlib.suppress(OSError):
            os.chflags(path, original.flags)


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

    A directory this migration creates should look like the one it was
    created inside: in the hub mirror every directory is ``a-w``, and a new
    scenario folder left at 755 would be the one writable hole in a locked
    tree.
    """
    for parent in path.parents:
        if parent in states:
            return states[parent]
    return None


def apply(migration: Plan) -> list[Path]:
    """Perform the moves. Returns the directories created, for the caller's report.

    Ordering matters: unlock every directory this will write in or remove
    from, move, then put every lock back — including on the new directories,
    which take the mode their parent had before the unlock, so a locked tree
    stays a locked tree.
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
                # Record every level that did not exist, not just the leaf:
                # `human/_superseded/<scenario>` creates two, and a level
                # nobody recorded is a level nobody relocks.
                missing = [p for p in (parent, *parent.parents) if not p.exists()]
                parent.mkdir(parents=True, exist_ok=True)
                created.extend(missing)
            file_state = _unlock(move.source)
            if move.tracked:
                _git_mv(root, move)
            else:
                # Checked once by `conflicts()` before anything moved, and
                # again here: os.replace is silent about an existing
                # destination, which is exactly the overwrite invariant 9
                # forbids.
                if move.destination.exists():
                    raise FileExistsError(f"destination already exists: {move.destination}")
                os.replace(move.source, move.destination)
            _relock(move.destination, file_state)
    finally:
        # Deepest first: a created directory's own state comes from an
        # ancestor, so read the ancestors before they are locked back down.
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
        # A deliberate rail rather than a nicety. This script unlocks and
        # moves evidence; pointed at the wrong tree — `evals/runs/`, a repo
        # root, a home directory — it would do so there. The one thing it
        # migrates is a folder called `reports`.
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

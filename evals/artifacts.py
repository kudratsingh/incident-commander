"""Versioned eval artifacts — naming, exclusive-create writes, newest-wins reads.

CLAUDE.md invariant 9 says eval artifacts are append-only: a re-run adds a
record, it never replaces one. Until this module existed the invariant had a
documented exception — four "refreshable pointers" that every run overwrote
in place:

    evals/briefings/<scenario>.json
    evals/trajectories/<scenario>.json
    evals/reports/latest.json
    evals/reports/human/<scenario>.txt

The exception is withdrawn. Nothing in the eval output tree overwrites a
prior file. Each of those paths is now a *family* of versioned files:

    <stem>.<YYYYMMDDTHHMMSSZ>.<invocation_id>.<ext>

    evals/briefings/dlq_growth.20260906T101112Z.a1b2c3d4e5f6.json
    evals/reports/report.20260906T101112Z.a1b2c3d4e5f6.json

Every write is exclusive-create (``open("x")``), the same convention the run
archive uses (``evals/runner.py::_archive_trajectory``): a path that already
holds evidence raises ``FileExistsError`` instead of deleting it. The
timestamp is UTC at second resolution and the ``invocation_id`` is the run's
12-hex identity, so two runs cannot collide unless they are genuinely the
same run writing the same artifact twice — which is a bug, and now says so.

**Reading: newest wins, resolved here and nowhere else.**
``newest(kind, scenario)`` is the single resolver. Every reader in the repo
goes through it, so "which file is current" has exactly one definition and
cannot drift between the runner, the trace formatter, the tests, and
whatever reads these next. Ordering is by the *filename's* timestamp, then
by ``invocation_id`` — never by mtime. Mtime is a property of the
filesystem, not of the run: a copy, a restore, a ``touch``, or a rsync
re-orders it silently, and a resolver that can be re-ordered by a backup
tool is exactly the "artifact that can erase its own history" invariant 9
exists to forbid.

**Legacy files are the oldest version.** A pre-versioning flat file
(``latest.json``, ``<scenario>.json``) that already sits on disk is left
exactly where it is — never deleted, never renamed, it is evidence — and
sorts *below* every versioned file. So the first versioned write lands
beside it and immediately wins the resolution, and the old file stays
readable forever.

**No convenience pointer.** A symlink named like the old flat file was
considered and rejected on two counts. First, that exact path is *occupied
by legacy evidence* in every existing checkout, so installing a pointer
there means deleting a real prior artifact — the one thing this module
exists to prevent. Second, ``evals/briefings/`` and ``evals/trajectories/``
are gitignored but ``evals/reports/`` is not, and a tracked symlink is a
mode change (100644 → 120000) that churns ``git status`` and does not
survive a Windows checkout intact. Readers use ``newest()``; that is the
whole interface.

**Trade-off, stated honestly.** Disk use now grows with every run instead of
staying flat: roughly a few KB per scenario per run (a trajectory is the
large one). A 38-scenario suite run daily adds on the order of a few MB a
month across all four families. That is the price of the artifacts being
evidence, and it is the same price ``evals/runs/`` already pays. Pruning, if
it is ever wanted, is a deliberate announced operation like the archive
unlock in docs/runbook.md — never something a run does to itself.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]

#: ``strftime`` form of the version stamp. Basic ISO-8601, UTC, second
#: resolution, no separators — fixed width, so lexicographic order over the
#: string IS chronological order and the sort needs no date parsing.
TIMESTAMP_FORMAT: Final[str] = "%Y%m%dT%H%M%SZ"

_TIMESTAMP_RE: Final[re.Pattern[str]] = re.compile(r"\A\d{8}T\d{6}Z\Z")
# ``uuid.uuid4().hex[:12]`` today; kept permissive so a longer or differently
# shaped id does not silently stop parsing and demote every file to legacy.
_INVOCATION_RE: Final[re.Pattern[str]] = re.compile(r"\A[0-9A-Za-z_-]+\Z")


@dataclass(frozen=True)
class ArtifactKind:
    """One family of versioned outputs.

    ``fixed_stem`` is set for artifacts that are per-run rather than
    per-scenario (the aggregate report); those take ``scenario=None``.
    ``legacy_name`` is the pre-versioning filename for the fixed-stem kinds;
    per-scenario kinds derive theirs as ``<scenario><suffix>``.
    """

    parts: tuple[str, ...]
    suffix: str
    fixed_stem: str | None = None
    legacy_name: str | None = None

    @property
    def per_scenario(self) -> bool:
        return self.fixed_stem is None


KINDS: Final[dict[str, ArtifactKind]] = {
    "trajectory": ArtifactKind(("evals", "trajectories"), ".json"),
    "briefing": ArtifactKind(("evals", "briefings"), ".json"),
    "human": ArtifactKind(("evals", "reports", "human"), ".txt"),
    "report": ArtifactKind(
        ("evals", "reports"), ".json", fixed_stem="report", legacy_name="latest.json"
    ),
}


def _kind(kind: str) -> ArtifactKind:
    try:
        return KINDS[kind]
    except KeyError:
        known = ", ".join(sorted(KINDS))
        raise KeyError(f"unknown artifact kind {kind!r} (known: {known})") from None


def directory_for(kind: str, *, root: Path | None = None) -> Path:
    """The directory holding one artifact family."""
    return (root or REPO_ROOT).joinpath(*_kind(kind).parts)


def stamp(when: datetime) -> str:
    """Render ``when`` as a version stamp, in UTC.

    A naive datetime is rejected rather than assumed local or assumed UTC:
    the stamp is the sort key for evidence, and a silently mis-zoned stamp
    re-orders runs.
    """
    if when.tzinfo is None:
        raise ValueError("version stamps require an aware datetime; got a naive one")
    return when.astimezone(UTC).strftime(TIMESTAMP_FORMAT)


def _stem_for(kind_obj: ArtifactKind, scenario: str | None) -> str:
    if kind_obj.per_scenario:
        if not scenario:
            raise ValueError("this artifact kind is per-scenario; a scenario name is required")
        return scenario
    if scenario is not None:
        raise ValueError("this artifact kind is per-run; it takes no scenario name")
    assert kind_obj.fixed_stem is not None
    return kind_obj.fixed_stem


def legacy_name(kind: str, scenario: str | None = None) -> str:
    """The pre-versioning flat filename this family replaced.

    Still meaningful: files by this name are real prior runs, and
    ``versions()`` returns them as the oldest version of the family.
    """
    kind_obj = _kind(kind)
    if kind_obj.legacy_name is not None:
        _stem_for(kind_obj, scenario)
        return kind_obj.legacy_name
    return f"{_stem_for(kind_obj, scenario)}{kind_obj.suffix}"


def version_name(
    kind: str,
    scenario: str | None = None,
    *,
    timestamp: datetime,
    invocation_id: str,
) -> str:
    """``<stem>.<stamp>.<invocation_id>.<ext>``."""
    kind_obj = _kind(kind)
    if not invocation_id:
        raise ValueError("a versioned artifact needs an invocation_id; got an empty string")
    if not _INVOCATION_RE.match(invocation_id):
        raise ValueError(
            f"invocation_id {invocation_id!r} is not filename-safe "
            "(letters, digits, '-' and '_' only)"
        )
    return f"{_stem_for(kind_obj, scenario)}.{stamp(timestamp)}.{invocation_id}{kind_obj.suffix}"


def version_path(
    kind: str,
    scenario: str | None = None,
    *,
    timestamp: datetime,
    invocation_id: str,
    directory: Path | None = None,
    root: Path | None = None,
) -> Path:
    """Full path a versioned write would land on. Does not touch the disk."""
    target = directory if directory is not None else directory_for(kind, root=root)
    return target / version_name(kind, scenario, timestamp=timestamp, invocation_id=invocation_id)


def _order_key(kind_obj: ArtifactKind, stem: str, name: str) -> tuple[int, str, str] | None:
    """Sort key for ``name`` within its family, or ``None`` if it is not a member.

    ``(0, "", "")`` for the legacy un-versioned file — below every versioned
    file, so the first versioned write immediately becomes newest.
    ``(1, stamp, invocation_id)`` otherwise. Both components come from the
    NAME; nothing here reads the filesystem.
    """
    if not name.endswith(kind_obj.suffix):
        return None
    legacy = kind_obj.legacy_name or f"{stem}{kind_obj.suffix}"
    if name == legacy:
        return (0, "", "")
    base = name[: -len(kind_obj.suffix)]
    head, _, invocation_id = base.rpartition(".")
    file_stem, _, timestamp = head.rpartition(".")
    if not file_stem or file_stem != stem:
        return None
    if not _TIMESTAMP_RE.match(timestamp) or not _INVOCATION_RE.match(invocation_id):
        return None
    return (1, timestamp, invocation_id)


def _members(
    kind_obj: ArtifactKind, stem: str, directory: Path
) -> Iterator[tuple[tuple[int, str, str], Path]]:
    if not directory.is_dir():
        return
    for entry in directory.iterdir():
        if not entry.is_file():
            continue
        key = _order_key(kind_obj, stem, entry.name)
        if key is not None:
            yield key, entry


def versions(
    kind: str,
    scenario: str | None = None,
    *,
    directory: Path | None = None,
    root: Path | None = None,
) -> list[Path]:
    """Every version of one artifact, oldest first.

    Ordered by the stamp encoded in the filename, then by ``invocation_id``
    — never by mtime (see the module docstring). A legacy un-versioned file
    sorts first.
    """
    kind_obj = _kind(kind)
    stem = _stem_for(kind_obj, scenario)
    target = directory if directory is not None else directory_for(kind, root=root)
    return [path for _, path in sorted(_members(kind_obj, stem, target), key=lambda pair: pair[0])]


def newest_or_none(
    kind: str,
    scenario: str | None = None,
    *,
    directory: Path | None = None,
    root: Path | None = None,
) -> Path | None:
    """Newest version, or ``None`` when this artifact has never been written."""
    found = versions(kind, scenario, directory=directory, root=root)
    return found[-1] if found else None


def newest(
    kind: str,
    scenario: str | None = None,
    *,
    directory: Path | None = None,
    root: Path | None = None,
) -> Path:
    """Newest version of one artifact. The single resolver every reader uses.

    Raises ``FileNotFoundError`` when nothing has been written — a reader
    that silently substituted a default would be reporting on a run that
    does not exist.
    """
    found = newest_or_none(kind, scenario, directory=directory, root=root)
    if found is None:
        target = directory if directory is not None else directory_for(kind, root=root)
        label = f"{kind}" if scenario is None else f"{kind} for {scenario!r}"
        raise FileNotFoundError(f"no {label} artifact under {target}")
    return found


def write_versioned(
    kind: str,
    scenario: str | None = None,
    *,
    content: str,
    timestamp: datetime,
    invocation_id: str,
    directory: Path | None = None,
    root: Path | None = None,
) -> Path:
    """Exclusive-create one versioned artifact and return its path.

    ``open("x")`` by design, exactly as the run archive writes: if the path
    already exists this raises ``FileExistsError`` rather than replacing a
    prior run's evidence.
    """
    path = version_path(
        kind,
        scenario,
        timestamp=timestamp,
        invocation_id=invocation_id,
        directory=directory,
        root=root,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as handle:
        handle.write(content)
    return path


def _main(argv: list[str] | None = None) -> int:
    """``python -m evals.artifacts newest report`` — the resolver for shell callers.

    Exists so the Makefile and an operator at a prompt use the SAME
    resolution the Python readers do, instead of a shell glob or an
    ``ls -t`` that would silently order by mtime.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="python -m evals.artifacts", description=__doc__)
    parser.add_argument("action", choices=("newest", "versions"))
    parser.add_argument("kind", choices=sorted(KINDS))
    parser.add_argument("scenario", nargs="?", default=None)
    args = parser.parse_args(argv)

    try:
        if args.action == "versions":
            for path in versions(args.kind, args.scenario):
                print(path)
            return 0
        print(newest(args.kind, args.scenario))
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    raise SystemExit(_main())

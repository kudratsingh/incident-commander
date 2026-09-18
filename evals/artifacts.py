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

**The layout is here too, and nowhere else (WO-R3-257).** Versioning made
the tree honest and made it unreadable: one flat ``evals/reports/`` holding
every aggregate report, every baseline half, every phase-close half, a flat
``human/`` of hundreds of renders and a flat ``dossiers/``. The owner's
words, 2026-09-17: "that whole reports folder should be better organized."
So each family now names a *sub-folder* as well as a container, and both
halves live in ``KINDS``:

    evals/reports/baseline.json                      ← unmoved (the gate reads it)
    evals/reports/latest.json                        ← unmoved (legacy evidence)
    evals/reports/runs/<YYYY-MM>/report.<stamp>.<id>.json
    evals/reports/baseline/baseline_report.<stamp>.<id>.{json,md}
    evals/reports/phase-close/phase_close_report.<stamp>.<id>.{json,md}
    evals/reports/research/research_report.<stamp>.<id>.{json,md}
    evals/reports/dossiers/<scenario>/<scenario>.<stamp>.<id>.md
    evals/reports/human/<scenario>/<scenario>.<stamp>.<id>.txt
    evals/reports/human/_superseded/<scenario>/…     ← repeat renders of one run

``evals/reports/README.md`` is the same map for a human. Two properties are
load-bearing:

*Reads cover the old place as well as the new one.* ``versions()`` searches
the family's new sub-folder AND the flat container it used to live in, so a
checkout that has merged this code but not yet run the migration resolves
exactly as before — nothing breaks in the window between the merge and the
move, and a file some future hand leaves in the flat spot is still found
rather than silently ignored. The flat container is also where the legacy
un-versioned names (``latest.json``, ``<scenario>.txt``) sit, so that search
is permanent, not merely transitional.

*Nothing moved is hidden.* ``human/_superseded/<scenario>/`` is searched
like any other member directory. A superseded render is an older render of
a run that has a newer one, so it can never be ``newest()``; it is moved for
readability, and it stays in ``versions()`` because it is still evidence.

The move itself is ``scripts/migrate_reports_layout.py`` — idempotent,
move-only, verified by sha256, and never run by a run.
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


#: Sub-folder of a per-scenario family holding repeat renders of a run that
#: already has a newer one. Named with a leading underscore so it sorts away
#: from the scenario folders and never collides with a scenario name.
SUPERSEDED_DIR: Final[str] = "_superseded"

#: ``strftime`` form of the month folder the aggregate reports group into.
MONTH_FORMAT: Final[str] = "%Y-%m"


@dataclass(frozen=True)
class ArtifactKind:
    """One family of versioned outputs, and where in the tree it lives.

    ``fixed_stem`` is set for artifacts that are per-run rather than
    per-scenario (the aggregate report); those take ``scenario=None``.
    ``legacy_name`` is the pre-versioning filename for the fixed-stem kinds;
    per-scenario kinds derive theirs as ``<scenario><suffix>``.

    ``parts`` is the family's CONTAINER — the directory it occupied when the
    tree was flat, and the directory a ``directory=`` override names. Below
    it, ``folder`` and ``grouping`` say where a *write* actually lands:

    ``folder``    a fixed sub-folder (``runs``, ``baseline``, ``phase-close``).
    ``grouping``  ``"none"`` writes straight into the folder; ``"scenario"``
                  adds a folder per scenario; ``"month"`` adds ``YYYY-MM``
                  taken from the artifact's own timestamp.

    A read searches the container as well, so the old flat location keeps
    resolving (see the module docstring).
    """

    parts: tuple[str, ...]
    suffix: str
    fixed_stem: str | None = None
    legacy_name: str | None = None
    folder: str | None = None
    grouping: str = "none"

    @property
    def per_scenario(self) -> bool:
        return self.fixed_stem is None


KINDS: Final[dict[str, ArtifactKind]] = {
    "trajectory": ArtifactKind(("evals", "trajectories"), ".json"),
    "briefing": ArtifactKind(("evals", "briefings"), ".json"),
    # One folder per scenario. `human/` held 765 files for 56 distinct runs
    # when this landed — the folder an operator opens first, sorted by
    # scenario only by accident of the filename, and 93% repeat renders. The
    # repeats move to `human/_superseded/<scenario>/`; they are not deleted.
    "human": ArtifactKind(("evals", "reports", "human"), ".txt", grouping="scenario"),
    # One folder per calendar month, under `runs/`. A hundred aggregate
    # reports sat directly beside `baseline.json` and the two report families
    # below, so the one file the gate reads was hard to see. The month comes
    # from the report's own `generated_at`, so a file's folder is derived from
    # its contents exactly as its name is.
    "report": ArtifactKind(
        ("evals", "reports"),
        ".json",
        fixed_stem="report",
        legacy_name="latest.json",
        folder="runs",
        grouping="month",
    ),
    # `make world-dossier ONLY=<scenario>` (evals/dossier.py) — the free
    # pre-run reading of the fault world. Versioned like everything else here
    # and for the same reason: a dossier is the evidence that somebody looked
    # at the world before the money was released, so a second reading of the
    # same scenario must not overwrite the first. There is no legacy flat
    # name; this family was versioned from its first write.
    "dossier": ArtifactKind(("evals", "reports", "dossiers"), ".md", grouping="scenario"),
    # `make baseline-report --write` (evals/baseline_report.py) — the Phase 0
    # baseline assembled from the committed archives. Two kinds, one stem: the
    # JSON is the artifact of record and the Markdown is the same document for
    # a human. Versioned like everything else, and deliberately NOT named
    # `baseline` — `evals/reports/baseline.json` is the machine REGRESSION
    # baseline that `evals/regression.py` gates against and `make baseline`
    # blesses. They answer different questions, and a shared stem would make
    # this family adopt that file as its own oldest version.
    "baseline_report": ArtifactKind(
        ("evals", "reports"), ".json", fixed_stem="baseline_report", folder="baseline"
    ),
    "baseline_report_md": ArtifactKind(
        ("evals", "reports"), ".md", fixed_stem="baseline_report", folder="baseline"
    ),
    # `make phase-close-report --write` (evals/phase_close_report.py) — the
    # report plan 03 § 14 requires before a phase may be called closed. Two
    # kinds, one stem, exactly as the baseline pair above: the JSON is the
    # artifact of record and the Markdown is the same document for a human.
    #
    # It needs its own entry rather than riding on `report`: that family's
    # stem is `report`, so a phase-close report filed under it would sort
    # into the same list as every per-run aggregate and `newest("report")`
    # — which the runner, the trace formatter and `make baseline-report` all
    # call — would start resolving to a phase-close document instead of a
    # run. One phase produces one of these; one run produces one of those;
    # they are different questions and they get different stems.
    "phase_close_report": ArtifactKind(
        ("evals", "reports"), ".json", fixed_stem="phase_close_report", folder="phase-close"
    ),
    "phase_close_report_md": ArtifactKind(
        ("evals", "reports"), ".md", fixed_stem="phase_close_report", folder="phase-close"
    ),
    # `make research-report --write` (evals/research_report.py) — the aggregate
    # research report plan 03 § 15 defines: one leaderboard per model, grouped
    # by the seven keys of WP-2.5, every difference beside its paired-trial
    # count. Two kinds, one stem, one sub-folder, exactly as the two pairs
    # above, and for the same reason they each got their own stem rather than
    # riding on `report`: `newest("report")` is what the runner, the trace
    # formatter and the regression gate resolve, and an aggregate filed under
    # that stem would start answering "which run was last?" with a document
    # that is not a run. This one summarises MANY runs; that is a different
    # question, so it gets a different stem and its own `research/` folder.
    "research_report": ArtifactKind(
        ("evals", "reports"), ".json", fixed_stem="research_report", folder="research"
    ),
    "research_report_md": ArtifactKind(
        ("evals", "reports"), ".md", fixed_stem="research_report", folder="research"
    ),
    # `make regrade-archive ARCHIVE=<id>` (scripts/regrade_archive.py) — one
    # locked archive re-graded from its own trajectories under today's rules
    # (WO-R3-265, INC-003). Its own stem and folder for the same reason as
    # every pair above, and one more that is specific to it: this document is
    # ABOUT an archive, and the archive it is about is never rewritten. The
    # re-grade therefore has to land somewhere else, be versioned, and never
    # replace a previous re-grade of the same run — two re-grades under two
    # rule sets are two facts, and the older one is the record of what the
    # numbers were when somebody quoted them.
    "regrade_report": ArtifactKind(
        ("evals", "reports"), ".json", fixed_stem="regrade_report", folder="regrades"
    ),
    "regrade_report_md": ArtifactKind(
        ("evals", "reports"), ".md", fixed_stem="regrade_report", folder="regrades"
    ),
    # `make judge-calibration` (evals/judge_calibration/, WP-6.3) — one judge's
    # calibration: what its trap-set agreement, its stability over N identical
    # asks and its track record against an independent label actually are.
    #
    # Grouped per JUDGE, using the per-scenario mechanism with the judge's name
    # as the stem. Plan 03 § 112 spells the path
    # `evals/reports/judge_calibration.<timestamp>.<id>.json` — flat, with one
    # fixed stem — and that cannot carry "one per judge", which the same sentence
    # also asks for: a fixed stem has no room for the judge's name, so three
    # judges would share one family and `newest()` would resolve whichever was
    # written last regardless of which judge it was about. The register in
    # `evals/research_report.py` is keyed by judge, so "is briefing_judge
    # calibrated?" has to be answerable about briefing_judge alone. Hence
    # `judge-calibration/<judge>/<judge>.<stamp>.<id>.json`, the same shape
    # `dossier` and `human` use, and the same shape the WO-R3-257 layout gives
    # every other family. Reported as a divergence from 03:112 rather than
    # silently reshaped.
    #
    # It needs an entry here at all for the reason divergence D2 gives: `KINDS`
    # is a closed registry, so an unregistered family cannot be resolved by
    # `newest()` and its writes would not be exclusive-create. Both matter — a
    # second calibration of one judge must not replace the first (invariant 9:
    # two calibrations under two rubrics are two facts, and the older one is the
    # record of what the number was when somebody quoted it).
    #
    # No `_md` sibling, unlike the four report pairs above: a calibration is read
    # by the register and by `make judge-calibration`'s own summary, which prints
    # to the terminal. A Markdown half nobody opens is a second artifact to keep
    # byte-identical for no reader.
    "judge_calibration": ArtifactKind(
        ("evals", "reports", "judge-calibration"), ".json", grouping="scenario"
    ),
    # `make world-record ONLY=<scenario>` (evals/recorder.py, WP-3.1) — one
    # zero-LLM reading of one seeded fault world, keyed by the WIRED arguments
    # the agent's own client sends, for a replay platform to answer from.
    #
    # NOT under `evals/reports/`: a recording is not a document somebody reads,
    # it is an INPUT a later run is executed against, and the reports tree is
    # for the other thing. Divergence D2 is why it is here at all rather than
    # at the hand-built path plan 04:110 names — `KINDS` is a closed registry,
    # so an unregistered family cannot be resolved by `newest()` and its writes
    # would not be exclusive-create. Both matter: a second recording of one
    # scenario must not replace the first (invariant 9), and every reader must
    # take the newest through the one resolver.
    #
    # One folder per scenario, like `dossier` and `human`: a corpus that gets
    # re-recorded on every platform release accumulates versions per scenario
    # much faster than a report family does, and the per-scenario folder is
    # what keeps `recorded_worlds/` navigable when it holds hundreds.
    "recorded_world": ArtifactKind(("evals", "recorded_worlds"), ".json", grouping="scenario"),
    # The evaluator's answer key for the world beside it (ADR 0040 / ADR 0038):
    # a SIBLING file, so the replay path can load a recording without being
    # able to reach the ground truth through it. The suffix carries `.truth`
    # ahead of the extension, which keeps the two families disjoint by name —
    # `<scenario>.<stamp>.<id>.truth.json` does not parse as a version of the
    # kind above (its "stamp" segment would be the invocation id), and
    # `<scenario>.<stamp>.<id>.json` does not end in `.truth.json`. Pinned by
    # `tests/unit/test_recorder.py`, because "the replay never loads it" is the
    # property, not the intention.
    "recorded_world_truth": ArtifactKind(
        ("evals", "recorded_worlds"), ".truth.json", grouping="scenario"
    ),
}


def _kind(kind: str) -> ArtifactKind:
    try:
        return KINDS[kind]
    except KeyError:
        known = ", ".join(sorted(KINDS))
        raise KeyError(f"unknown artifact kind {kind!r} (known: {known})") from None


def directory_for(kind: str, *, root: Path | None = None) -> Path:
    """The CONTAINER of one artifact family.

    The directory the family occupied when the tree was flat — still the
    root of everything it owns, still the thing a ``directory=`` override
    names, and still a place reads cover. The sub-folder a write lands in is
    ``write_directory``.
    """
    return (root or REPO_ROOT).joinpath(*_kind(kind).parts)


def month(when: datetime) -> str:
    """The ``YYYY-MM`` folder an artifact stamped ``when`` groups into."""
    if when.tzinfo is None:
        raise ValueError("month folders require an aware datetime; got a naive one")
    return when.astimezone(UTC).strftime(MONTH_FORMAT)


def _check_path_component(value: str, *, what: str) -> str:
    """Refuse a stem that would steer a write out of its own family.

    Stems are scenario names, and a scenario name is now a DIRECTORY name.
    ``version_name`` has always validated the invocation id for exactly this
    reason; the stem gained the same stakes the day it started naming a
    folder.
    """
    if not value or value in {".", ".."} or "/" in value or "\\" in value or value.startswith("."):
        raise ValueError(f"{what} {value!r} is not a usable directory name")
    return value


def _subdir(kind_obj: ArtifactKind, stem: str, timestamp: datetime | None) -> tuple[str, ...]:
    """The path below the container that a write for this artifact lands in."""
    below: tuple[str, ...] = (kind_obj.folder,) if kind_obj.folder else ()
    if kind_obj.grouping == "scenario":
        return below + (_check_path_component(stem, what="scenario"),)
    if kind_obj.grouping == "month":
        if timestamp is None:
            raise ValueError("this artifact kind groups by month; a timestamp is required")
        return below + (month(timestamp),)
    return below


def write_directory(
    kind: str,
    scenario: str | None = None,
    *,
    timestamp: datetime | None = None,
    directory: Path | None = None,
    root: Path | None = None,
) -> Path:
    """The exact directory a versioned write for this artifact lands in.

    ``directory`` overrides the CONTAINER, not this — so a caller handing the
    writer a temporary directory gets the same layout inside it that the real
    tree has, and a test that writes then resolves cannot disagree with
    itself about where the file went.
    """
    kind_obj = _kind(kind)
    container = directory if directory is not None else directory_for(kind, root=root)
    return container.joinpath(*_subdir(kind_obj, _stem_for(kind_obj, scenario), timestamp))


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
    target = write_directory(kind, scenario, timestamp=timestamp, directory=directory, root=root)
    return target / version_name(kind, scenario, timestamp=timestamp, invocation_id=invocation_id)


@dataclass(frozen=True)
class ParsedName:
    """A versioned filename taken apart: ``<stem>.<stamp>.<invocation_id>.<ext>``."""

    stem: str
    stamp: str
    invocation_id: str

    def timestamp(self) -> datetime:
        """The stamp as an aware UTC datetime — what ``write_directory`` wants."""
        return datetime.strptime(self.stamp, TIMESTAMP_FORMAT).replace(tzinfo=UTC)


def parse_version_name(name: str, *, suffix: str) -> ParsedName | None:
    """Take a versioned filename apart, or ``None`` if it is not one.

    Public because the layout migration has to answer the same question in
    reverse — "which family and which stamp does this file belong to?" — and
    a second copy of the naming rules is a second definition of what a
    current artifact is (the failure the module docstring's "one resolver"
    paragraph exists to prevent).
    """
    if not name.endswith(suffix):
        return None
    base = name[: -len(suffix)]
    head, _, invocation_id = base.rpartition(".")
    stem, _, stamp = head.rpartition(".")
    if not stem:
        return None
    if not _TIMESTAMP_RE.match(stamp) or not _INVOCATION_RE.match(invocation_id):
        return None
    return ParsedName(stem=stem, stamp=stamp, invocation_id=invocation_id)


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
    parsed = parse_version_name(name, suffix=kind_obj.suffix)
    if parsed is None or parsed.stem != stem:
        return None
    return (1, parsed.stamp, parsed.invocation_id)


def search_directories(kind: str, scenario: str | None = None, *, container: Path) -> list[Path]:
    """Every directory a read for this artifact looks in, outermost first.

    Three groups, and each is there for a stated reason:

    * the CONTAINER itself — the flat location the family used to occupy. It
      holds the legacy un-versioned names permanently (``latest.json``,
      ``<scenario>.txt``), and between this code merging and the migration
      running it holds everything else too. A resolver that stopped looking
      there would report "no such artifact" over a folder full of them.
    * the family's own sub-folder — where writes land now.
    * ``_superseded/<scenario>`` for the per-scenario families — repeat
      renders moved aside for readability. Still evidence, so still resolved;
      never ``newest()``, because a superseded render is by construction an
      older render of a run that has a newer one.
    """
    kind_obj = _kind(kind)
    stem = _stem_for(kind_obj, scenario)
    base = container / kind_obj.folder if kind_obj.folder else container
    found = [container]
    if kind_obj.grouping == "scenario":
        found.append(base / stem)
        found.append(base / SUPERSEDED_DIR / stem)
    elif kind_obj.grouping == "month":
        if base.is_dir():
            found.extend(sorted(entry for entry in base.iterdir() if entry.is_dir()))
    elif base != container:
        found.append(base)
    # dict.fromkeys: de-duplicated, order preserved. A family whose folder is
    # its own container would otherwise list every file twice.
    return list(dict.fromkeys(found))


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
    sorts first. Collected across every directory the family owns
    (``search_directories``), so the new sub-folder and the flat location it
    replaced return one merged history rather than two partial ones.

    The path breaks a tie the name cannot: the same filename present in both
    the old and the new place is a half-finished migration, and the nested
    copy — the deeper path, which sorts later — is the one to resolve.
    """
    kind_obj = _kind(kind)
    stem = _stem_for(kind_obj, scenario)
    container = directory if directory is not None else directory_for(kind, root=root)
    found = [
        (key, path)
        for target in search_directories(kind, scenario, container=container)
        for key, path in _members(kind_obj, stem, target)
    ]
    return [path for _, path in sorted(found, key=lambda pair: (pair[0], str(pair[1])))]


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

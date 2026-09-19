"""Versioned eval artifacts — naming, exclusive-create writes, newest-wins reads.

``<stem>.<YYYYMMDDTHHMMSSZ>.<invocation_id>.<ext>``, written ``open("x")`` so a path
holding evidence raises rather than being replaced (invariant 9 — the four flat
"refreshable pointers" are withdrawn, and survive as each family's oldest version).
``newest(kind, scenario)`` is the ONE resolver: by the filename's stamp, never mtime.
``KINDS`` carries each family's sub-folder too (WO-R3-257); the map for a human is
``evals/reports/README.md``, and the move is ``scripts/migrate_reports_layout.py``.
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

#: ``strftime`` form of the version stamp: basic ISO-8601, UTC, fixed width, so
#: lexicographic order over the string IS chronological order.
TIMESTAMP_FORMAT: Final[str] = "%Y%m%dT%H%M%SZ"

_TIMESTAMP_RE: Final[re.Pattern[str]] = re.compile(r"\A\d{8}T\d{6}Z\Z")
# ``uuid.uuid4().hex[:12]`` today; permissive so a differently shaped id does not
# stop parsing and demote every file to legacy.
_INVOCATION_RE: Final[re.Pattern[str]] = re.compile(r"\A[0-9A-Za-z_-]+\Z")


#: Sub-folder of a per-scenario family holding repeat renders of a run that already
#: has a newer one. Leading underscore, so it cannot collide with a scenario name.
SUPERSEDED_DIR: Final[str] = "_superseded"

#: ``strftime`` form of the month folder the aggregate reports group into.
MONTH_FORMAT: Final[str] = "%Y-%m"


@dataclass(frozen=True)
class ArtifactKind:
    """One family of versioned outputs, and where in the tree it lives.

    ``parts`` is the CONTAINER (the old flat directory, which reads still cover);
    ``folder`` plus ``grouping`` (``none``/``scenario``/``month``) say where a write
    lands below it. ``fixed_stem`` marks a per-run family (``scenario=None``), and
    ``legacy_name`` is its pre-versioning filename.
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
    # One folder per scenario: `human/` held 765 files for 56 runs when this landed,
    # 93% repeat renders. Those move to `human/_superseded/<scenario>/`, not deleted.
    "human": ArtifactKind(("evals", "reports", "human"), ".txt", grouping="scenario"),
    # One folder per calendar month under `runs/`, so the one file the gate reads
    # (`baseline.json`) is visible. The month comes from the report's own
    # `generated_at` — folder derived from contents, exactly as the name is.
    "report": ArtifactKind(
        ("evals", "reports"),
        ".json",
        fixed_stem="report",
        legacy_name="latest.json",
        folder="runs",
        grouping="month",
    ),
    # `make world-dossier ONLY=<scenario>` (evals/dossier.py) — the free pre-run
    # reading of the fault world. A dossier is the evidence somebody looked before
    # the money was released, so a second reading must not overwrite the first.
    "dossier": ArtifactKind(("evals", "reports", "dossiers"), ".md", grouping="scenario"),
    # `make baseline-report --write` (evals/baseline_report.py) — the Phase 0
    # baseline from the committed archives. Two kinds, one stem: JSON of record plus
    # the same document for a human. NOT named `baseline`: that is the machine
    # regression baseline `evals/regression.py` gates against, and a shared stem
    # would make this family adopt it as its own oldest version.
    "baseline_report": ArtifactKind(
        ("evals", "reports"), ".json", fixed_stem="baseline_report", folder="baseline"
    ),
    "baseline_report_md": ArtifactKind(
        ("evals", "reports"), ".md", fixed_stem="baseline_report", folder="baseline"
    ),
    # `make phase-close-report --write` (evals/phase_close_report.py) — plan 03 § 14's
    # requirement before a phase may be called closed. Its own stem, not `report`:
    # `newest("report")` answers "which run was last?" and must not resolve to a
    # document that is not a run.
    "phase_close_report": ArtifactKind(
        ("evals", "reports"), ".json", fixed_stem="phase_close_report", folder="phase-close"
    ),
    "phase_close_report_md": ArtifactKind(
        ("evals", "reports"), ".md", fixed_stem="phase_close_report", folder="phase-close"
    ),
    # `make research-report --write` (evals/research_report.py) — plan 03 § 15's
    # aggregate: one leaderboard per model, grouped by WP-2.5's seven keys. Its own
    # stem for the reason above; this one summarises MANY runs.
    "research_report": ArtifactKind(
        ("evals", "reports"), ".json", fixed_stem="research_report", folder="research"
    ),
    "research_report_md": ArtifactKind(
        ("evals", "reports"), ".md", fixed_stem="research_report", folder="research"
    ),
    # `make regrade-archive ARCHIVE=<id>` (scripts/regrade_archive.py) — one locked
    # archive re-graded from its own trajectories under today's rules (WO-R3-265,
    # INC-003). Its own stem and folder because the archive it is ABOUT is never
    # rewritten, and two re-grades under two rule sets are two facts.
    "regrade_report": ArtifactKind(
        ("evals", "reports"), ".json", fixed_stem="regrade_report", folder="regrades"
    ),
    "regrade_report_md": ArtifactKind(
        ("evals", "reports"), ".md", fixed_stem="regrade_report", folder="regrades"
    ),
    # `make judge-calibration` (evals/judge_calibration/, WP-6.3) — one judge's trap
    # agreement, stability and track record. Grouped per JUDGE via the per-scenario
    # mechanism with the judge's name as the stem: plan 03 § 112's flat fixed stem
    # cannot carry "one per judge", and the register in `evals/research_report.py` is
    # keyed by judge (reported as a divergence from 03:112, not silently reshaped).
    # No `_md` sibling — nothing would open it.
    "judge_calibration": ArtifactKind(
        ("evals", "reports", "judge-calibration"), ".json", grouping="scenario"
    ),
    # `make world-record ONLY=<scenario>` (evals/recorder.py, WP-3.1) — one zero-LLM
    # reading of one seeded fault world, keyed by WIRED arguments, for a replay to
    # answer from. NOT under `evals/reports/`: it is an INPUT, not a document.
    # Registered here (divergence D2) so `newest()` resolves it and its writes are
    # exclusive-create; one folder per scenario, because re-recording accumulates.
    "recorded_world": ArtifactKind(("evals", "recorded_worlds"), ".json", grouping="scenario"),
    # The evaluator's answer key for the world beside it (ADR 0040 / ADR 0038): a
    # SIBLING file, so the replay path cannot reach ground truth through a recording.
    # The `.truth` before the extension keeps the two families disjoint by name.
    # Pinned by `tests/unit/test_recorder.py` — the property, not the intention.
    "recorded_world_truth": ArtifactKind(
        ("evals", "recorded_worlds"), ".truth.json", grouping="scenario"
    ),
    # `make training-export WRITE=1` (evals/export.py, WP-15.1) — the JSONL a later
    # training stage reads, plus the evaluator labels it must not, plus the manifest
    # naming every `template_id` the data covers. Three stems in one folder, disjoint by
    # suffix on `recorded_world_truth`'s precedent: `newest("training_export")` must
    # never resolve to the labels beside it, and a re-export is a new version, never a
    # replacement (invariant 9 — an export whose provenance can be rewritten cannot
    # support a claim about what a policy was trained on).
    "training_export": ArtifactKind(("evals", "exports"), ".jsonl", fixed_stem="training_export"),
    "training_export_labels": ArtifactKind(
        ("evals", "exports"), ".labels.jsonl", fixed_stem="training_export"
    ),
    "training_export_manifest": ArtifactKind(
        ("evals", "exports"), ".manifest.json", fixed_stem="training_export"
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

    The flat directory it used to occupy: the root of everything it owns, what a
    ``directory=`` override names, and a place reads still cover. Where a write
    lands is ``write_directory``.
    """
    return (root or REPO_ROOT).joinpath(*_kind(kind).parts)


def month(when: datetime) -> str:
    """The ``YYYY-MM`` folder an artifact stamped ``when`` groups into."""
    if when.tzinfo is None:
        raise ValueError("month folders require an aware datetime; got a naive one")
    return when.astimezone(UTC).strftime(MONTH_FORMAT)


def _check_path_component(value: str, *, what: str) -> str:
    """Refuse a stem that would steer a write out of its own family.

    A scenario name is now a DIRECTORY name, so it has the stakes ``version_name``
    has always validated the invocation id for.
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

    ``directory`` overrides the CONTAINER, not this, so a temporary directory gets
    the real tree's layout inside it and a test that writes then resolves agrees
    with itself.
    """
    kind_obj = _kind(kind)
    container = directory if directory is not None else directory_for(kind, root=root)
    return container.joinpath(*_subdir(kind_obj, _stem_for(kind_obj, scenario), timestamp))


def stamp(when: datetime) -> str:
    """Render ``when`` as a version stamp, in UTC.

    A naive datetime is rejected rather than assumed: the stamp is the sort key for
    evidence, and a mis-zoned one re-orders runs.
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

    Still meaningful: such a file is a real prior run, returned by ``versions()`` as
    the family's oldest version.
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

    Public because the layout migration asks the same question in reverse, and a
    second copy of the naming rules is a second definition of "current".
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

    ``(0, "", "")`` for the legacy un-versioned file, so the first versioned write
    becomes newest; ``(1, stamp, invocation_id)`` otherwise. From the NAME only —
    nothing here reads the filesystem.
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

    The CONTAINER (permanently: it holds the legacy un-versioned names, and
    everything else until the migration runs), the family's own sub-folder, and
    ``_superseded/<scenario>`` — still evidence, and never ``newest()`` anyway.
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
    # dict.fromkeys: de-duplicated, order preserved — a family whose folder is its
    # own container would otherwise list every file twice.
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

    By the filename's stamp then ``invocation_id``, never by mtime, merged across
    every directory the family owns (``search_directories``); a legacy file sorts
    first. The path breaks a tie: one name in both places is a half-finished
    migration, and the deeper copy is the one to resolve.
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

    Raises ``FileNotFoundError`` when nothing has been written: a substituted
    default would report on a run that does not exist.
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

    ``open("x")`` by design, as the run archive writes: an existing path raises
    ``FileExistsError`` rather than replacing a prior run's evidence.
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

    So the Makefile and an operator at a prompt get the SAME resolution the Python
    readers do, rather than a glob or an ``ls -t`` ordered by mtime.
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

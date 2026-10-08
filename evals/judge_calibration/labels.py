"""Owner labels for briefing usefulness: the packet a human fills in, and the file it lands in.

WO-R3-278. The packet shows 16 committed briefings as ``briefing_judge`` sees them; the label
file is append-only (a correction is a new line naming the id it ``supersedes``). A model
never writes a label here.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from evals.artifacts import REPO_ROOT
from evals.graders.llm_judge import format_briefing_context
from incident_commander.agent.briefing import EscalationBriefing

if TYPE_CHECKING:
    from evals.scenarios.schema import Scenario

LABELS_DIR: Final[Path] = REPO_ROOT / "evals" / "judge_calibration" / "labels"
LABELS_FILE: Final[Path] = LABELS_DIR / "briefings.jsonl"

PACKET_SIZE: Final[int] = 16
PER_SCENARIO_CAP: Final[int] = 2

USEFUL: Final[str] = "useful"
NOT_USEFUL: Final[str] = "not_useful"
LABEL_VALUES: Final[tuple[str, ...]] = (USEFUL, NOT_USEFUL)
LABELLED_BY: Final[str] = "owner"

#: INC-002's briefing, which is trap bj-05: always in the packet.
INC_002_ID: Final[str] = "54ab08425f82:remediate_dlq_backlog_success"

#: The one sentence at the top of the packet saying what the label means.
USEFUL_MEANS: Final[str] = (
    "A briefing is **useful** when an on-call human who reads only this briefing would "
    "act correctly on the incident; otherwise it is **not_useful**."
)

# Outcome classes the selection spreads picks across.
RESOLVED: Final[str] = "resolved"
ESCALATED: Final[str] = "escalated"
FAILED: Final[str] = "failed"

_FENCE: Final[str] = "~~~~"
_HEADER_RE: Final[re.Pattern[str]] = re.compile(r"^## \d+\. `([^`]+)`\s*$")


class LabelError(ValueError):
    """A packet or label line that cannot be accepted as written."""


@dataclass(frozen=True, kw_only=True)
class Candidate:
    """One committed briefing that could go in the packet."""

    archive: str
    scenario: str
    generated_at: str
    live: bool
    outcome: str
    family: str

    @property
    def id(self) -> str:
        return f"{self.archive}:{self.scenario}"


@dataclass(frozen=True, kw_only=True)
class Pick:
    """A chosen candidate and the reason it was chosen."""

    candidate: Candidate
    why: str


def split_id(label_id: str) -> tuple[str, str]:
    """``invocation_id:scenario`` into its two halves, or a ``LabelError``."""
    archive, sep, scenario = label_id.partition(":")
    if not sep or not archive or not scenario:
        raise LabelError(f"label id {label_id!r} is not '<invocation_id>:<scenario>'")
    return archive, scenario


def briefing_path(label_id: str, *, root: Path | None = None) -> Path:
    archive, scenario = split_id(label_id)
    return (root or REPO_ROOT) / "evals" / "runs" / archive / "briefings" / f"{scenario}.json"


def load_briefing(label_id: str, *, root: Path | None = None) -> EscalationBriefing:
    """The archived briefing a label id names. Reads only: the archives are locked (ADR 0021)."""
    return EscalationBriefing.model_validate_json(briefing_path(label_id, root=root).read_text())


# ---------------------------------------------------------------------------
# Selection


def committed_archives(*, root: Path | None = None) -> list[str]:
    """Archive ids whose ``report.json`` git tracks, so an untracked local run never counts."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "evals/runs"],
        cwd=root or REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    names = {
        parts[2]
        for path in out.split("\0")
        if len(parts := path.split("/")) == 4 and parts[3] == "report.json"
    }
    return sorted(names)


def _outcome_class(outcome: Mapping[str, Any]) -> str:
    report = outcome.get("report") or {}
    if report.get("passed") is False:
        return FAILED
    return RESOLVED if outcome.get("final_state") == "resolved" else ESCALATED


def _families(root: Path) -> dict[str, str]:
    from evals.scenarios.loader import load_scenarios

    return {
        s.name: (s.family.value if s.family else "unknown")
        for s in load_scenarios(root / "evals" / "scenarios")
    }


def candidates(archives: Iterable[str], *, root: Path | None = None) -> list[Candidate]:
    """Every briefing in the named archives that has writer prose to judge, newest first."""
    base = root or REPO_ROOT
    families = _families(base)
    found: list[Candidate] = []
    for archive in sorted(set(archives)):
        report_path = base / "evals" / "runs" / archive / "report.json"
        if not report_path.is_file():
            continue
        report = json.loads(report_path.read_text())
        for outcome in report.get("outcomes", []):
            scenario = str(outcome.get("scenario", ""))
            path = briefing_path(f"{archive}:{scenario}", root=base)
            if not path.is_file():
                continue
            briefing = EscalationBriefing.model_validate_json(path.read_text())
            # A template-only briefing (the writer failed or never ran) has nothing to label.
            if not briefing.findings.strip() or not briefing.recommendation.strip():
                continue
            found.append(
                Candidate(
                    archive=archive,
                    scenario=scenario,
                    generated_at=str(report.get("generated_at", "")),
                    live=bool(outcome.get("live_llm")),
                    outcome=_outcome_class(outcome),
                    family=families.get(scenario, "unknown"),
                )
            )
    found.sort(key=lambda c: (c.generated_at, c.archive, c.scenario), reverse=True)
    return found


def select(pool: Sequence[Candidate], *, size: int = PACKET_SIZE) -> list[Pick]:
    """The packet's briefings: INC-002 first, then a deterministic spread, newest first.

    Live runs before canned ones; one per scenario before a second; each next pick is the one
    whose outcome class, then family, has the fewest picks so far, newest breaking a tie.
    """
    by_id = {c.id: c for c in pool}
    if INC_002_ID not in by_id:
        raise LabelError(f"INC-002's briefing {INC_002_ID} is not among the candidates")
    picks = [
        Pick(candidate=by_id[INC_002_ID], why="INC-002's briefing (trap bj-05); always included")
    ]
    order = {c.id: i for i, c in enumerate(pool)}
    for live in (True, False):
        for cap in range(1, PER_SCENARIO_CAP + 1):
            while len(picks) < size:
                taken = {p.candidate.id for p in picks}
                per_scenario = _count(p.candidate.scenario for p in picks)
                per_outcome = _count(p.candidate.outcome for p in picks)
                per_family = _count(p.candidate.family for p in picks)
                eligible = [
                    c
                    for c in pool
                    if c.live is live
                    and c.id not in taken
                    and per_scenario.get(c.scenario, 0) < cap
                ]
                if not eligible:
                    break
                best = min(
                    eligible,
                    key=lambda c: (
                        per_outcome.get(c.outcome, 0),
                        per_family.get(c.family, 0),
                        order[c.id],
                    ),
                )
                picks.append(
                    Pick(
                        candidate=best,
                        why=(
                            f"newest {'live' if live else 'canned'} briefing in the least-covered "
                            f"slot: outcome {best.outcome} had {per_outcome.get(best.outcome, 0)} "
                            f"pick(s), family {best.family} had {per_family.get(best.family, 0)}; "
                            f"{'first' if cap == 1 else 'second'} pick for this scenario"
                        ),
                    )
                )
    if len(picks) < size:
        raise LabelError(f"only {len(picks)} briefings could be picked; the packet needs {size}")
    return picks


def _count(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# The packet


def _scenario_lines(scenario: Scenario | None) -> list[str]:
    if scenario is None:
        return ["- What the scenario is: (no longer in the corpus)", "- Ground truth: (none)"]
    description = _first_sentence(scenario.description) or "(no description)"
    if scenario.ground_truth is not None:
        causes = ", ".join(c.value for c in scenario.ground_truth.root_causes)
        truth = f"root cause {causes}"
    else:
        truth = "no root cause declared"
    actions = ", ".join(scenario.expectation.expected_action_tools) or "none"
    return [
        f"- What the scenario is: {description}",
        f"- Ground truth: {truth}; expected end state "
        f"{scenario.expectation.expected_terminal_state.value}; expected actions: {actions}",
    ]


def _old_format_note(label_id: str, root: Path) -> list[str]:
    """A warning when the archived trail predates recorded probe arguments (INC-002, cmd #221)."""
    raw = json.loads(briefing_path(label_id, root=root).read_text())
    trail = raw.get("investigation_trail", [])
    if not trail or all("arguments" in entry for entry in trail):
        return []
    return [
        "- Note: archived before probe arguments were recorded, so each call shows `()`; "
        "the judge sees it the same way"
    ]


def _first_sentence(text: str) -> str:
    flat = " ".join(text.split())
    head, dot, _ = flat.partition(". ")
    return f"{head}{dot.strip()}" if dot else flat


def render_packet(picks: Sequence[Pick], *, on: date, root: Path | None = None) -> str:
    """The Markdown packet. Each briefing is shown as ``briefing_judge`` sees it."""
    from evals.scenarios.loader import load_scenarios

    base = root or REPO_ROOT
    corpus = {s.name: s for s in load_scenarios(base / "evals" / "scenarios")}
    lines = [
        f"# Briefing usefulness labels — packet {on.strftime('%Y%m%d')}",
        "",
        USEFUL_MEANS,
        "",
        "For each briefing below, write `useful` or `not_useful` after `LABEL:` and one line after",
        "`REASON:`. Leave both blank to skip one. Then run",
        "`make label-packet IMPORT=<this file>`; the labels are appended to",
        "`evals/judge_calibration/labels/briefings.jsonl`, which is never edited afterwards.",
        "The ground truth is for you, not the judge: the judge sees only the text in the box.",
        "",
    ]
    for number, pick in enumerate(picks, start=1):
        c = pick.candidate
        context = format_briefing_context(load_briefing(c.id, root=base))
        if _FENCE in context:
            raise LabelError(
                f"{c.id}: the briefing contains {_FENCE}, which would break the packet"
            )
        lines += [
            "---",
            "",
            f"## {number}. `{c.id}`",
            "",
            f"- Run: archive {c.archive}, {'live' if c.live else 'canned'} model, {c.generated_at}",
            f"- Scenario: {c.scenario} (family {c.family}, outcome {c.outcome})",
            *_scenario_lines(corpus.get(c.scenario)),
            f"- Why it is in the packet: {pick.why}",
            *_old_format_note(c.id, base),
            "",
            "The briefing, as the judge sees it:",
            "",
            f"{_FENCE}text",
            context,
            _FENCE,
            "",
            "LABEL: ",
            "REASON: ",
            "",
        ]
    return "\n".join(lines)


def write_packet(text: str, *, on: date, directory: Path | None = None) -> Path:
    """Write the packet create-only; an existing packet for that day raises ``FileExistsError``."""
    folder = directory or LABELS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"packet.{on.strftime('%Y%m%d')}.md"
    with path.open("x", encoding="utf-8") as handle:
        handle.write(text)
    return path


# ---------------------------------------------------------------------------
# Importing a filled packet


@dataclass(frozen=True, kw_only=True)
class FilledLabel:
    """One labelled block read out of a packet."""

    id: str
    label: str
    reason: str
    supersedes: str | None = None


def parse_packet(text: str) -> list[FilledLabel]:
    """The labelled blocks of a filled packet; blank ones are skipped, malformed ones raise.

    Only ``LABEL:``/``REASON:``/``SUPERSEDES:`` lines outside the briefing box are read.
    """
    found: list[FilledLabel] = []
    current: str | None = None
    fields: dict[str, str] = {}
    inside = False

    def close() -> None:
        if current is not None:
            filled = _filled(current, fields)
            if filled is not None:
                found.append(filled)

    for line in text.splitlines():
        if line.startswith(_FENCE):
            inside = not inside
            continue
        if inside:
            continue
        header = _HEADER_RE.match(line)
        if header:
            close()
            current, fields = header.group(1), {}
            continue
        for key in ("LABEL", "REASON", "SUPERSEDES"):
            if current is not None and line.startswith(f"{key}:"):
                fields[key] = line[len(key) + 1 :].strip()
    close()
    ids = [f.id for f in found]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise LabelError(f"the packet labels these ids twice: {duplicates}")
    return found


def _filled(label_id: str, fields: Mapping[str, str]) -> FilledLabel | None:
    split_id(label_id)
    label = fields.get("LABEL", "").strip().lower().replace(" ", "_")
    reason = fields.get("REASON", "").strip()
    supersedes = fields.get("SUPERSEDES", "").strip() or None
    if not label and not reason:
        return None
    if label not in LABEL_VALUES:
        raise LabelError(f"{label_id}: LABEL must be one of {LABEL_VALUES}; got {label!r}")
    if not reason:
        raise LabelError(f"{label_id}: a label needs a one-line REASON")
    if supersedes is not None and supersedes != label_id:
        raise LabelError(f"{label_id}: SUPERSEDES must name this block's own id")
    return FilledLabel(id=label_id, label=label, reason=reason, supersedes=supersedes)


# ---------------------------------------------------------------------------
# The label file


def read_lines(path: Path | None = None) -> list[dict[str, Any]]:
    """Every line of the label file, in order, validated; a missing file is no lines."""
    target = path or LABELS_FILE
    if not target.is_file():
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for number, raw in enumerate(target.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        row = json.loads(raw)
        _check_row(row, where=f"{target.name}:{number}")
        if row["id"] in seen and row.get("supersedes") != row["id"]:
            raise LabelError(f"{target.name}:{number}: {row['id']} repeats without supersedes")
        if "supersedes" in row and row["supersedes"] not in seen:
            raise LabelError(
                f"{target.name}:{number}: supersedes {row['supersedes']}, never labelled"
            )
        seen.add(row["id"])
        rows.append(row)
    return rows


def _check_row(row: Mapping[str, Any], *, where: str) -> None:
    required = {"id", "label", "reason", "labelled_by", "labelled_at"}
    missing = required - set(row)
    if missing:
        raise LabelError(f"{where}: missing {sorted(missing)}")
    extra = set(row) - required - {"supersedes"}
    if extra:
        raise LabelError(f"{where}: unknown fields {sorted(extra)}")
    split_id(str(row["id"]))
    if row["label"] not in LABEL_VALUES:
        raise LabelError(f"{where}: label {row['label']!r} is not one of {LABEL_VALUES}")
    if "supersedes" in row and row["supersedes"] != row["id"]:
        raise LabelError(f"{where}: supersedes must equal the line's own id")
    date.fromisoformat(str(row["labelled_at"]))


def current_labels(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """The label in force for each id: its latest line, a correction replacing its original."""
    return {row["id"]: row for row in read_lines(path)}


@dataclass(frozen=True, kw_only=True)
class ImportResult:
    appended: tuple[str, ...]
    already_recorded: tuple[str, ...]
    refused: tuple[str, ...]


def import_labels(
    filled: Sequence[FilledLabel],
    *,
    path: Path | None = None,
    today: date | None = None,
    root: Path | None = None,
) -> ImportResult:
    """Append new labels; never replace one.

    An id already in the file is skipped when the label and reason match and REFUSED when
    they differ, unless the block says ``SUPERSEDES``. Every id must name a committed briefing.
    """
    target = path or LABELS_FILE
    existing = current_labels(target)
    stamp = (today or datetime.now(UTC).date()).isoformat()
    appended: list[str] = []
    same: list[str] = []
    refused: list[str] = []
    new_lines: list[str] = []
    for item in filled:
        if not briefing_path(item.id, root=root).is_file():
            raise LabelError(
                f"{item.id}: no committed briefing at {briefing_path(item.id, root=root)}"
            )
        prior = existing.get(item.id)
        if item.supersedes is None and prior is not None:
            if prior["label"] == item.label and prior["reason"] == item.reason:
                same.append(item.id)
            else:
                refused.append(item.id)
            continue
        if item.supersedes is not None and prior is None:
            raise LabelError(f"{item.id}: SUPERSEDES names an id that was never labelled")
        row: dict[str, Any] = {
            "id": item.id,
            "label": item.label,
            "reason": item.reason,
            "labelled_by": LABELLED_BY,
            "labelled_at": stamp,
        }
        if item.supersedes is not None:
            row["supersedes"] = item.supersedes
        new_lines.append(json.dumps(row, ensure_ascii=False))
        appended.append(item.id)
    if new_lines:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write("".join(f"{line}\n" for line in new_lines))
    return ImportResult(
        appended=tuple(appended), already_recorded=tuple(same), refused=tuple(refused)
    )

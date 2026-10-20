"""Would an archived run replay against a recording with no misses? — ``make replay-coverage``.

Free, zero-model, no stack (WO-R3-387). A recorded run that asks for a read the recording does
not hold gets ``not_recorded`` and is excluded (ADR 0047: re-record the world wider). This check
asks that question BEFORE a paid run: it takes the calls an archived run made or proposed and
looks each one up in a recording through ``recorded_client.replay_key`` — the replay client's
own key — so "would miss" here means exactly what a miss means in a run.

Two lists per archived trace, read from the archive only (never a glob of ``evals/traces``):

* **asked** — the reads the run actually put to its world: every read in the final checkpoint's
  evidence, plus each emitted step's ``next_action`` probe (a call the replay missed never reaches
  the evidence, so the step is where it is written down). A miss here would exclude the run.
* **proposed** — every candidate's ``next_probe`` and the planner's own ``next_action``: reads the
  planner offered and the selector did not choose this time. A miss here is the next run's miss.

And one list per world with or without a trace: its family's ``recorder.PLANNER_PROPOSED_READS``,
the reads the pilot's planner made or proposed on a sibling world behind the same alert.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from evals import artifacts
from evals.recorded_client import matching_recordings, replay_key
from evals.recorder import PLANNER_PROPOSED_READS, RecordedWorld, load_recording
from evals.scenarios.loader import load_scenarios
from evals.tracing import TraceKind
from incident_commander.tools.policies import Tier, tier_of

REPO_ROOT: Final[Path] = artifacts.REPO_ROOT
DEFAULT_PLAN: Final[Path] = REPO_ROOT / "evals" / "samples" / "oracle_gap.json"

#: The two origins of an archived call. ``ASKED`` beats ``PROPOSED`` when a call has both.
ASKED: Final[str] = "asked"
PROPOSED: Final[str] = "proposed"

#: The trace's execution mode for a run that replayed a recording.
RECORDED_MODE: Final[str] = "recorded"

EXIT_CLEAN: Final[int] = 0
EXIT_MISSES: Final[int] = 1
EXIT_USAGE: Final[int] = 2


@dataclass(frozen=True)
class ArchivedCall:
    """One read an archived run made or proposed, with the arguments it used."""

    tool: str
    arguments: tuple[tuple[str, Any], ...]
    origins: frozenset[str]

    @property
    def asked(self) -> bool:
        return ASKED in self.origins

    @property
    def label(self) -> str:
        rendered = ", ".join(f"{name}={value!r}" for name, value in self.arguments)
        return f"{self.tool}({rendered})"


@dataclass(frozen=True)
class Coverage:
    """One archived trace looked up in one recording."""

    scenario: str
    archive: str
    recording: str
    calls: tuple[ArchivedCall, ...]
    missed: tuple[ArchivedCall, ...] = field(default=())

    @property
    def asked(self) -> tuple[ArchivedCall, ...]:
        return tuple(call for call in self.calls if call.asked)

    @property
    def proposed_only(self) -> tuple[ArchivedCall, ...]:
        return tuple(call for call in self.calls if not call.asked)

    @property
    def asked_misses(self) -> tuple[ArchivedCall, ...]:
        return tuple(call for call in self.missed if call.asked)

    @property
    def proposed_misses(self) -> tuple[ArchivedCall, ...]:
        return tuple(call for call in self.missed if not call.asked)


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    """A trace field that is a JSON object, whether stored parsed or as a string."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, Mapping) else None


def _probe_of(action: Any) -> tuple[str, tuple[tuple[str, Any], ...]] | None:
    """``(tool, sorted arguments)`` for a probe action of a READ tool; ``None`` otherwise."""
    action = _as_mapping(action)
    if action is None or action.get("kind", "probe") != "probe":
        return None
    tool = action.get("tool_name")
    if not isinstance(tool, str) or not _is_read(tool):
        return None
    arguments = action.get("arguments") or {}
    if not isinstance(arguments, Mapping):
        return None
    return tool, tuple(sorted(arguments.items()))


def _is_read(tool: str) -> bool:
    try:
        return tier_of(tool) is Tier.READ
    except KeyError:
        return False


def trace_records(root: Path, archive: str, scenario: str) -> list[dict[str, Any]]:
    """Every record of one scenario's trace in one archive, read by name."""
    path = root / "evals" / "runs" / archive / "traces" / f"{scenario}.jsonl"
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def execution_mode(records: Sequence[Mapping[str, Any]]) -> str | None:
    for record in records:
        if record.get("kind") == TraceKind.SCENARIO_START.value:
            mode = record.get("execution_mode")
            return mode if isinstance(mode, str) else None
    return None


def archived_calls(root: Path, archive: str, scenario: str) -> tuple[ArchivedCall, ...]:
    """The reads one archived run asked for and proposed, merged, in a stable order."""
    origins: dict[tuple[str, tuple[tuple[str, Any], ...]], set[str]] = {}

    def add(found: tuple[str, tuple[tuple[str, Any], ...]] | None, origin: str) -> None:
        if found is not None:
            origins.setdefault(found, set()).add(origin)

    for record in trace_records(root, archive, scenario):
        kind = record.get("kind")
        if kind == TraceKind.LLM.value and record.get("role") == "investigation_planner":
            output = _as_mapping(record.get("output"))
            if output is None:
                continue
            for candidate in output.get("candidates") or ():
                if isinstance(candidate, Mapping):
                    add(_probe_of(candidate.get("next_probe")), PROPOSED)
            add(_probe_of(output.get("next_action")), PROPOSED)
        elif kind == TraceKind.STEP.value:
            emitted = _as_mapping(record.get("emitted_step"))
            if emitted is not None:
                add(_probe_of(emitted.get("next_action")), ASKED)

    trajectory = root / "evals" / "runs" / archive / "trajectories" / f"{scenario}.json"
    if trajectory.is_file():
        document = json.loads(trajectory.read_text(encoding="utf-8"))
        checkpoints = document.get("checkpoints") or []
        evidence = checkpoints[-1].get("evidence") or [] if checkpoints else []
        for entry in evidence:
            if isinstance(entry, Mapping):
                add(
                    _probe_of(
                        {"tool_name": entry.get("tool_name"), "arguments": entry.get("arguments")}
                    ),
                    ASKED,
                )

    return tuple(
        ArchivedCall(tool, arguments, frozenset(found))
        for (tool, arguments), found in sorted(origins.items(), key=lambda item: repr(item[0]))
    )


def coverage(
    world: RecordedWorld, calls: Iterable[ArchivedCall], *, archive: str, recording: str
) -> Coverage:
    """Look every archived call up in the recording by the replay client's own key."""
    keys = set(world.keys)
    listed = tuple(calls)
    missed = tuple(
        call for call in listed if replay_key(call.tool, dict(call.arguments)) not in keys
    )
    return Coverage(
        scenario=world.scenario, archive=archive, recording=recording, calls=listed, missed=missed
    )


def recorded_archives(root: Path, scenario: str) -> list[str]:
    """Every run archive holding a RECORDED-mode trace of this scenario, oldest id order."""
    runs = root / "evals" / "runs"
    if not runs.is_dir():
        return []
    found: list[str] = []
    for archive in sorted(entry.name for entry in runs.iterdir() if entry.is_dir()):
        if execution_mode(trace_records(root, archive, scenario)) == RECORDED_MODE:
            found.append(archive)
    return found


def proposed_reads(
    root: Path, archives: Sequence[str], scenarios: Sequence[str]
) -> tuple[tuple[str, tuple[tuple[str, Any], ...]], ...]:
    """Every read the archived runs of these scenarios asked for or proposed, deduplicated.

    The derivation ``recorder.PLANNER_PROPOSED_READS`` is checked against: pass one family's
    scenarios and the archives it was taken from.
    """
    reads: set[tuple[str, tuple[tuple[str, Any], ...]]] = set()
    for archive in archives:
        for scenario in scenarios:
            for call in archived_calls(root, archive, scenario):
                reads.add((call.tool, call.arguments))
    return tuple(sorted(reads, key=repr))


def family_gaps(world: RecordedWorld, family: str | None) -> list[str]:
    """The family's archived planner reads this recording cannot answer, as labels."""
    keys = set(world.keys)
    return [
        ArchivedCall(tool, arguments, frozenset({PROPOSED})).label
        for tool, arguments in PLANNER_PROPOSED_READS.get(family or "", ())
        if replay_key(tool, dict(arguments)) not in keys
    ]


def _render(row: Coverage) -> list[str]:
    lines = [
        f"  {row.archive}: asked {len(row.asked)}, missed {len(row.asked_misses)}; "
        f"proposed-only {len(row.proposed_only)}, not recorded {len(row.proposed_misses)}"
    ]
    lines.extend(f"    MISS (asked)    {call.label}" for call in row.asked_misses)
    lines.extend(f"    MISS (proposed) {call.label}" for call in row.proposed_misses)
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.replay_coverage",
        description=(
            "Look every read an archived recorded run asked for or proposed up in a recording. "
            "Free: no model, no platform."
        ),
    )
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN, help="a sample plan (JSON)")
    parser.add_argument("--world", action="append", default=[], help="limit to these scenarios")
    parser.add_argument(
        "--recording",
        default=None,
        help="check against this recording id instead of the plan's (one --world only)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    pins = {world["scenario"]: world["recording"] for world in plan["worlds"]}
    wanted = [part for value in args.world for part in str(value).split(",") if part]
    unknown = [name for name in wanted if name not in pins]
    if unknown:
        print(f"REPLAY-COVERAGE: not in {args.plan.name}: {', '.join(unknown)}")
        return EXIT_USAGE
    if args.recording is not None and len(wanted) != 1:
        print("REPLAY-COVERAGE: --recording needs exactly one --world")
        return EXIT_USAGE
    names = wanted or list(pins)

    families = {
        scenario.name: (scenario.family.value if scenario.family is not None else None)
        for scenario in load_scenarios(REPO_ROOT / "evals" / "scenarios")
    }
    asked_misses = 0
    proposed_misses = 0
    family_misses = 0
    untraced: list[str] = []
    print(f"REPLAY-COVERAGE: plan {args.plan.name}, {len(names)} world(s)")
    for name in names:
        recording = args.recording or pins[name]
        path = matching_recordings(recording, [name]).get(name)
        if path is None:
            print(f"{name}: no recording {recording} under evals/recorded_worlds/{name}/")
            return EXIT_USAGE
        world = load_recording(path)
        print(f"{name} against {recording} ({len(world.calls)} recorded call(s)):")
        gaps = family_gaps(world, families.get(name))
        family_misses += len(gaps)
        if gaps:
            print(f"  family planner reads not recorded ({len(gaps)}): {', '.join(gaps)}")
        archives = recorded_archives(REPO_ROOT, name)
        if not archives:
            untraced.append(name)
            print("  no recorded trace to check")
            continue
        for archive in archives:
            row = coverage(
                world,
                archived_calls(REPO_ROOT, archive, name),
                archive=archive,
                recording=recording,
            )
            asked_misses += len(row.asked_misses)
            proposed_misses += len(row.proposed_misses)
            for line in _render(row):
                print(line)
    if untraced:
        print(f"no recorded trace to check ({len(untraced)}): {', '.join(untraced)}")
    print(
        f"REPLAY-COVERAGE: {asked_misses} asked read(s) would miss, "
        f"{proposed_misses} proposed read(s) not recorded, "
        f"{family_misses} family planner read(s) not recorded"
    )
    return EXIT_MISSES if asked_misses else EXIT_CLEAN


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    raise SystemExit(main(sys.argv[1:]))

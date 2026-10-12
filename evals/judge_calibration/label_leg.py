"""The fourth leg for ``briefing_judge``: its verdicts against the owner's labels (WO-R3-278).

Each labelled briefing is put ``reps`` times through the judge's own call; the first ask is
the verdict (as for the traps) and the reps measure stability. ``useful`` is the approval
verdict, both dimensions at or above ``USEFUL_THRESHOLD``. Zero labels is a refusal.

Held-out scoring (WO-R3-378, owner O-58, ADR 0082). Label rows carry the ``packet`` they were
labelled in (WO-R3-379; a row without one is packet 1). ``packet`` names the packet whose
agreement COUNTS, and only its briefings that no other packet labelled are counted: a briefing
labelled elsewhere was in view when the rubric was written, so it is test-retest evidence about
the owner, not held-out evidence about the judge. A packet holding a briefing the rubric quotes
as an example and no other packet labels is the packet the rubric was written from, and is
refused as the held-out one. ``dev_packet`` is scored beside it and marked ``counted: false``.
A label file holding more than one packet is refused unless one is named, because "all labels
in force" would put the dev set into the number O-58 says it must not enter.
``HELD_OUT_AGREEMENT_THRESHOLD`` is reported, never acted on: the owner decides.

``rubric`` names the rubric every briefing is asked under (WO-R3-383): the production
``briefing_judge`` by default, or a variant from ``BRIEFING_JUDGE_RUBRICS``. The "written from"
refusal reads the bytes of the rubric actually asked, and ``reasoning`` is counted against that
rubric's own checks.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from evals.artifacts import REPO_ROOT
from evals.graders.llm_judge import (
    JUDGE_PROMPT,
    USEFUL_THRESHOLD,
    JudgeScore,
    checks_of,
    cited_checks,
    judge_briefing,
)
from evals.judge_calibration import labels
from evals.judge_calibration.roles import BRIEFING_JUDGE, is_approval, score_verdict
from incident_commander.llm.client import LLMClientProtocol, LLMError
from incident_commander.llm.prompts.loader import load_prompt

LEG: Final[str] = "label agreement: the judge's verdict vs the owner's useful / not_useful label"

MAPPING: Final[str] = (
    f"the judge says useful when groundedness and actionability are both >= {USEFUL_THRESHOLD} "
    "(verdict grounded=yes actionable=yes, the harness's approval rule); anything else is "
    "not_useful"
)

#: The packet a label row belongs to when it names none: every row written before WO-R3-379.
DEFAULT_PACKET: Final[int] = 1

#: The held-out agreement at or above which the harness reports that the judge CLEARS (owner
#: O-58 item 5). 0.8 is 11 of 13: a judge no better than always giving a 13-briefing packet's
#: majority label (~0.62 on packet 1's mix) clears it about 8% of the time, a judge that agrees
#: with the owner 85-90% of the time clears it 70-87% of the time (ADR 0082 has the table). It
#: clears only when it also beats that majority label and every held-out briefing was answered.
#: Reported, never acted on: whether the judge's numbers leave "measured" is the owner's call.
HELD_OUT_AGREEMENT_THRESHOLD: Final[float] = 0.8

THRESHOLD_ADR: Final[str] = (
    "docs/ADR/0082-the-briefing-judge-is-checks-scored-on-a-held-out-packet.md"
)

DEV_NOT_COUNTED: Final[str] = (
    "not counted: the dev set's labels were in view when the rubric was written (owner O-58 "
    "item 4), so agreement on them measures fit, not the judge"
)

RETEST_READING: Final[str] = (
    "the owner's own consistency on briefings labelled twice, blind: a judge cannot be expected "
    "to agree with the owner more often than the owner agrees with themself"
)


class NoLabelsError(ValueError):
    """The leg was asked for with no label to compare against."""


class PacketNotNamedError(NoLabelsError):
    """The label file holds several packets and none was named, so no label set was chosen."""


@dataclass(frozen=True, kw_only=True)
class LabelOutcome:
    """One labelled briefing, asked ``len(scores)`` times."""

    id: str
    label: str
    reason: str
    scores: tuple[JudgeScore, ...]
    error: str | None = None

    @property
    def answered(self) -> bool:
        return self.error is None and bool(self.scores)

    @property
    def verdicts(self) -> tuple[str, ...]:
        return tuple(score_verdict(score) for score in self.scores)

    @property
    def judge_label(self) -> str | None:
        if not self.answered:
            return None
        useful = is_approval(BRIEFING_JUDGE, self.verdicts[0])
        return labels.USEFUL if useful else labels.NOT_USEFUL

    @property
    def agrees(self) -> bool:
        return self.answered and self.judge_label == self.label

    @property
    def stable(self) -> bool:
        return self.answered and len(set(self.verdicts)) == 1


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(numerator / denominator, 4)


# ---------------------------------------------------------------------------
# Rows and packets


def load_rows(path: Path) -> list[dict[str, Any]]:
    """Every validated line of the label file, in order. The one place this leg reads it."""
    return labels.read_lines(path)


def packet_of(row: Mapping[str, Any]) -> int:
    """The packet a label row was labelled in; a row naming none is packet 1."""
    return int(row.get("packet", DEFAULT_PACKET))


def labels_by_packet(rows: Iterable[Mapping[str, Any]]) -> dict[int, dict[str, dict[str, Any]]]:
    """The label in force for each id WITHIN each packet: its latest line in that packet.

    A correction (``supersedes``) replaces its original inside its own packet only, so one
    briefing can hold one label per packet and the two are never merged.
    """
    by_packet: dict[int, dict[str, dict[str, Any]]] = {}
    for row in rows:
        by_packet.setdefault(packet_of(row), {})[str(row["id"])] = dict(row)
    return dict(sorted(by_packet.items()))


def owner_test_retest(
    by_packet: Mapping[int, Mapping[str, Mapping[str, Any]]], *, packet: int
) -> dict[str, Any]:
    """The briefings ``packet`` labels that another packet labelled too, and whether they agree."""
    rows: list[dict[str, Any]] = []
    for label_id, row in by_packet.get(packet, {}).items():
        for other, in_force in by_packet.items():
            if other == packet or label_id not in in_force:
                continue
            first, second = sorted(
                ((other, in_force[label_id]), (packet, row)), key=lambda pair: pair[0]
            )
            rows.append(
                {
                    "id": label_id,
                    "labels": [
                        {"packet": p, "label": r["label"], "labelled_at": str(r["labelled_at"])}
                        for p, r in (first, second)
                    ],
                    "same": first[1]["label"] == second[1]["label"],
                }
            )
    same = sum(row["same"] for row in rows)
    return {
        "packet": packet,
        "pairs": len(rows),
        "same": same,
        "agreement": _rate(same, len(rows)),
        "reading": RETEST_READING,
        "rows": rows,
    }


def held_out_verdict(value: Mapping[str, Any]) -> dict[str, Any]:
    """Does a held-out score clear ``HELD_OUT_AGREEMENT_THRESHOLD``? Reported, never acted on.

    Three conditions (ADR 0082): agreement at or above the threshold; strictly more agreements
    than always giving the packet's majority label would score; and nothing left unanswered.
    """
    rows = [row for row in value["rows"] if row.get("judge_label") is not None]
    useful = sum(row["owner_label"] == labels.USEFUL for row in rows)
    majority_label = labels.USEFUL if useful >= len(rows) - useful else labels.NOT_USEFUL
    majority = max(useful, len(rows) - useful)
    complete = not value["errors"] and not value["missing_briefings"]
    agreement = value["agreement"]
    clears = (
        complete
        and agreement is not None
        and agreement >= HELD_OUT_AGREEMENT_THRESHOLD
        and value["agree"] > majority
    )
    return {
        "constant": "HELD_OUT_AGREEMENT_THRESHOLD",
        "value": HELD_OUT_AGREEMENT_THRESHOLD,
        "agreement": agreement,
        "majority_label_baseline": {"label": majority_label, "agree": majority, "n": len(rows)},
        "complete": complete,
        "clears": clears,
        "decides": (
            "the owner decides whether the judge's numbers leave 'measured' (O-58 item 5); the "
            "harness only reports, and research_report.JUDGE_CALIBRATION_REPORTS is not touched"
        ),
        "adr": THRESHOLD_ADR,
    }


# ---------------------------------------------------------------------------
# Asking


def ask_labelled(
    label_id: str,
    row: Mapping[str, Any],
    *,
    client: LLMClientProtocol,
    model: str,
    reps: int,
    root: Path | None = None,
    rubric: str = JUDGE_PROMPT,
) -> LabelOutcome:
    """Put one labelled briefing ``reps`` times; an ``LLMError`` is recorded, not raised."""
    briefing = labels.load_briefing(label_id, root=root)
    scores: list[JudgeScore] = []
    for _ in range(reps):
        try:
            scores.append(judge_briefing(briefing, client, model, rubric=rubric))
        except LLMError as err:
            return LabelOutcome(
                id=label_id,
                label=row["label"],
                reason=row["reason"],
                scores=tuple(scores),
                error=f"{type(err).__name__}: {err}",
            )
    return LabelOutcome(id=label_id, label=row["label"], reason=row["reason"], scores=tuple(scores))


def _score_set(
    in_force: Mapping[str, Mapping[str, Any]],
    *,
    client: LLMClientProtocol,
    model: str,
    reps: int,
    root: Path | None,
    path: Path,
    rubric: str = JUDGE_PROMPT,
) -> dict[str, Any]:
    """Ask every labelled briefing present in this checkout; the agreement over the set."""
    missing = sorted(i for i in in_force if not labels.briefing_path(i, root=root).is_file())
    present = {i: row for i, row in in_force.items() if i not in missing}
    if not present:
        raise NoLabelsError(
            f"refusing the label leg: none of the {len(in_force)} labelled briefings is in "
            f"this checkout's evals/runs ({', '.join(missing)})"
        )
    outcomes = [
        ask_labelled(i, row, client=client, model=model, reps=reps, root=root, rubric=rubric)
        for i, row in present.items()
    ]
    checks = checks_of(rubric)
    answered = [o for o in outcomes if o.answered]
    agreed = [o for o in answered if o.agrees]
    disagreed = [o for o in answered if not o.agrees]
    stable = [o for o in answered if o.stable]
    citing = [o for o in answered if cited_checks(o.scores[0].reasoning, checks)]
    stamps = sorted(str(row["labelled_at"]) for row in in_force.values())
    return {
        "labels_file": _display(path),
        "labels_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "labels_in_force": len(in_force),
        "labelled_at": {"first": stamps[0], "last": stamps[-1]},
        "mapping": MAPPING,
        "missing_briefings": missing,
        "n": len(answered),
        "agree": len(agreed),
        "disagree": len(disagreed),
        "agreement": _rate(len(agreed), len(answered)),
        "judge_useful_owner_not": sum(o.label == labels.NOT_USEFUL for o in disagreed),
        "judge_not_useful_owner_useful": sum(o.label == labels.USEFUL for o in disagreed),
        "stability": {
            "reps": reps,
            "identical": len(stable),
            "fraction_identical": _rate(len(stable), len(answered)),
        },
        "reasoning_names_a_check": len(citing),
        "errors": [{"id": o.id, "error": o.error} for o in outcomes if not o.answered],
        "rows": [_row(o, checks) for o in outcomes],
        "disagreements": [
            {
                **_row(o, checks),
                "owner_reason": o.reason,
                "judge_reasoning": o.scores[0].reasoning,
            }
            for o in disagreed
        ],
    }


def label_agreement(
    *,
    client: LLMClientProtocol,
    model: str,
    reps: int,
    labels_path: Path | None = None,
    root: Path | None = None,
    packet: int | None = None,
    dev_packet: int | None = None,
    rubric: str = JUDGE_PROMPT,
) -> dict[str, Any]:
    """Agreement with the labels in force, or ``NoLabelsError`` when there are none.

    ``packet`` counts that packet's held-out briefings only; ``dev_packet`` is scored beside it,
    not counted. With neither, a file holding exactly one packet is scored whole, as before.
    Every refusal is raised before the first call.
    """
    if dev_packet is not None and packet is None:
        raise ValueError(
            f"a dev packet ({dev_packet}) needs a held-out packet to stand beside: name it"
        )
    if dev_packet is not None and dev_packet == packet:
        raise ValueError(f"the dev packet and the held-out packet must differ (both {packet})")
    checks_of(rubric)
    path = labels_path or labels.LABELS_FILE
    by_packet = labels_by_packet(load_rows(path))
    if not by_packet:
        raise NoLabelsError(
            f"refusing the label leg: {path} holds zero labels. The owner labels the packet "
            "(make label-packet, then make label-packet IMPORT=<filled packet>) first."
        )
    if packet is None:
        if len(by_packet) > 1:
            raise PacketNotNamedError(
                f"refusing the label leg: {path} holds packets {sorted(by_packet)}; name the "
                "held-out one with --packet (PACKET=) and the dev set with --dev-packet "
                "(DEV_PACKET=). Scoring every label would count the dev set (O-58, ADR 0082)."
            )
        (only,) = by_packet
        value = _score_set(
            by_packet[only],
            client=client,
            model=model,
            reps=reps,
            root=root,
            path=path,
            rubric=rubric,
        )
        return _leg({"packet": only, "counted": True, **value})

    scored = by_packet.get(packet, {})
    if not scored:
        raise NoLabelsError(
            f"refusing the label leg: packet {packet} holds zero labels in {path} "
            f"(packets present: {sorted(by_packet)}). The owner labels it first."
        )
    elsewhere = {
        label_id: [p for p, in_force in by_packet.items() if p != packet and label_id in in_force]
        for label_id in scored
    }
    asked = load_prompt(rubric)
    quoted = {label_id: label_id in asked for label_id in scored}
    written_from = [i for i in scored if quoted[i] and not elsewhere[i]]
    if written_from:
        raise NoLabelsError(
            f"refusing the label leg: the rubric quotes {len(written_from)} briefing(s) that only "
            f"packet {packet} labels ({', '.join(written_from)}), so it was written from this "
            "packet: it is a dev set — name it with --dev-packet (DEV_PACKET=) beside a packet "
            "the rubric has not seen (O-58, ADR 0082)"
        )
    held_out = {i: row for i, row in scored.items() if not elsewhere[i]}
    if not held_out:
        raise NoLabelsError(
            f"refusing the label leg: every briefing in packet {packet} also carries a label in "
            "another packet, so nothing is held out"
        )
    if dev_packet is not None and not by_packet.get(dev_packet):
        raise NoLabelsError(
            f"refusing the label leg: dev packet {dev_packet} holds zero labels in {path}"
        )
    value = {
        "packet": packet,
        "counted": True,
        "not_counted": [
            {"id": label_id, "also_labelled_in": others, "quoted_in_rubric": quoted[label_id]}
            for label_id, others in elsewhere.items()
            if others
        ],
        **_score_set(
            held_out, client=client, model=model, reps=reps, root=root, path=path, rubric=rubric
        ),
    }
    value["threshold"] = held_out_verdict(value)
    dev = None
    if dev_packet is not None:
        dev = {
            "packet": dev_packet,
            "counted": False,
            "why_not_counted": DEV_NOT_COUNTED,
            **_score_set(
                by_packet[dev_packet],
                client=client,
                model=model,
                reps=reps,
                root=root,
                path=path,
                rubric=rubric,
            ),
        }
    return _leg(value, dev_set=dev, test_retest=owner_test_retest(by_packet, packet=packet))


def _leg(
    value: dict[str, Any],
    *,
    dev_set: dict[str, Any] | None = None,
    test_retest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "leg": LEG,
        "measured": True,
        "value": value,
        "dev_set": dev_set,
        "test_retest": test_retest,
        "why": "",
        "requires": [],
    }


def _row(outcome: LabelOutcome, checks: tuple[str, ...]) -> dict[str, Any]:
    first = outcome.scores[0] if outcome.answered else None
    return {
        "id": outcome.id,
        "owner_label": outcome.label,
        "judge_label": outcome.judge_label,
        "verdict": outcome.verdicts[0] if outcome.answered else None,
        "groundedness": first.groundedness if first else None,
        "actionability": first.actionability if first else None,
        "checks_cited": list(cited_checks(first.reasoning, checks)) if first else [],
        "agrees": outcome.agrees,
        "stable": outcome.stable,
        "observed": list(outcome.verdicts),
    }


def _display(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)

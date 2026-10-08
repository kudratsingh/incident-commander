"""The fourth leg for ``briefing_judge``: its verdicts against the owner's labels (WO-R3-278).

Each labelled briefing is put ``reps`` times through the judge's own call; the first ask is
the verdict (as for the traps) and the reps measure stability. ``useful`` is the approval
verdict, both dimensions at or above ``USEFUL_THRESHOLD``. Zero labels is a refusal.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from evals.artifacts import REPO_ROOT
from evals.graders.llm_judge import USEFUL_THRESHOLD, JudgeScore, judge_briefing
from evals.judge_calibration import labels
from evals.judge_calibration.roles import BRIEFING_JUDGE, is_approval, score_verdict
from incident_commander.llm.client import LLMClientProtocol, LLMError

LEG: Final[str] = "label agreement: the judge's verdict vs the owner's useful / not_useful label"

MAPPING: Final[str] = (
    f"the judge says useful when groundedness and actionability are both >= {USEFUL_THRESHOLD} "
    "(verdict grounded=yes actionable=yes, the harness's approval rule); anything else is "
    "not_useful"
)


class NoLabelsError(ValueError):
    """The leg was asked for with no label to compare against."""


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


def ask_labelled(
    label_id: str,
    row: dict[str, Any],
    *,
    client: LLMClientProtocol,
    model: str,
    reps: int,
    root: Path | None = None,
) -> LabelOutcome:
    """Put one labelled briefing ``reps`` times; an ``LLMError`` is recorded, not raised."""
    briefing = labels.load_briefing(label_id, root=root)
    scores: list[JudgeScore] = []
    for _ in range(reps):
        try:
            scores.append(judge_briefing(briefing, client, model))
        except LLMError as err:
            return LabelOutcome(
                id=label_id,
                label=row["label"],
                reason=row["reason"],
                scores=tuple(scores),
                error=f"{type(err).__name__}: {err}",
            )
    return LabelOutcome(id=label_id, label=row["label"], reason=row["reason"], scores=tuple(scores))


def label_agreement(
    *,
    client: LLMClientProtocol,
    model: str,
    reps: int,
    labels_path: Path | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Agreement with the labels in force, or ``NoLabelsError`` when there are none."""
    path = labels_path or labels.LABELS_FILE
    in_force = labels.current_labels(path)
    if not in_force:
        raise NoLabelsError(
            f"refusing the label leg: {path} holds zero labels. The owner labels the packet "
            "(make label-packet, then make label-packet IMPORT=<filled packet>) first."
        )
    missing = sorted(i for i in in_force if not labels.briefing_path(i, root=root).is_file())
    present = {i: row for i, row in in_force.items() if i not in missing}
    if not present:
        raise NoLabelsError(
            f"refusing the label leg: none of the {len(in_force)} labelled briefings is in "
            f"this checkout's evals/runs ({', '.join(missing)})"
        )
    outcomes = [
        ask_labelled(i, row, client=client, model=model, reps=reps, root=root)
        for i, row in present.items()
    ]
    answered = [o for o in outcomes if o.answered]
    agreed = [o for o in answered if o.agrees]
    disagreed = [o for o in answered if not o.agrees]
    stable = [o for o in answered if o.stable]
    return {
        "leg": LEG,
        "measured": True,
        "value": {
            "labels_file": _display(path),
            "labels_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "labels_in_force": len(in_force),
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
            "errors": [{"id": o.id, "error": o.error} for o in outcomes if not o.answered],
            "rows": [_row(o) for o in outcomes],
            "disagreements": [
                {
                    **_row(o),
                    "owner_reason": o.reason,
                    "judge_reasoning": o.scores[0].reasoning,
                }
                for o in disagreed
            ],
        },
        "why": "",
        "requires": [],
    }


def _row(outcome: LabelOutcome) -> dict[str, Any]:
    first = outcome.scores[0] if outcome.answered else None
    return {
        "id": outcome.id,
        "owner_label": outcome.label,
        "judge_label": outcome.judge_label,
        "verdict": outcome.verdicts[0] if outcome.answered else None,
        "groundedness": first.groundedness if first else None,
        "actionability": first.actionability if first else None,
        "agrees": outcome.agrees,
        "stable": outcome.stable,
        "observed": list(outcome.verdicts),
    }


def _display(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)

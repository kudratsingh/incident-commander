"""One calibration: ask the trap set N times, read the track record, write a report.

Plan 03 § 9 in code. The whole sweep is one pass — each trap case is asked ``reps``
times, rep 1 is the agreement measurement and all ``reps`` are the self-agreement
measurement — because two passes over the same inputs would double the bill for a
number the first pass already contains.

**Self-agreement is the determinism measurement, and it replaces a setting.** Plan
03 § 9.1 asks for temperature 0. No judge call in this repo sends a temperature and
none may be made to require one: owner decision O-24 and ADR 0048 (written for the
selector, and the same argument here) refuse it, because Anthropic's newer model
families reject the parameter with a 400 and a calibration that 400s on the first
re-pin is not a calibration. A setting is a claim that a judge is stable; five
identical verdicts are a measurement that it is. The report says which of the two it
has, in ``determinism``, so nobody reads the absence of the field as an oversight.
ADR 0052.

**A judge that cannot answer is not a judge that answered wrongly.** A case whose
output fails validation twice raises ``OutputRepairExhausted`` (ADR 0035's cap of
1 — the repair WO-R2-174 gave both judges is what makes calibrating them possible
at all). That case is recorded as an ``error``, counted in neither the numerator
nor the denominator of accuracy, and listed. Absorbing it as a disagreement would
put a harness event into a measurement of judgement, which is the mistake
``failure_class: planner_output_invalid`` exists to prevent one layer down.

**The rubric is identified, not described.** Every report carries the sha256 and
the line count of the exact prompt bytes it calibrated. That is what makes plan 03
§ 110's rule operational: rubric edits go in one line at a time, and a delta
between two calibrations is attributable only if each names the rubric it measured.
The convention itself is in ``docs/eval-methodology.md`` and in ADR 0052; this is
the field that lets a reviewer check it after the fact rather than take it on
trust.

**Writing is a separate act.** ``calibrate`` returns a report; ``write_report``
persists it through ``evals/artifacts.py`` under its own kind, exclusive-create,
versioned, never overwriting a previous calibration of the same judge (invariant
9). Two calibrations of one rubric are two facts, and the older one is the record
of what the number was when somebody quoted it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from evals import artifacts
from evals.judge_calibration.roles import (
    ABSENT_ROLES,
    CALIBRATED_ROLES,
    is_approval,
    role,
    rubric_of,
)
from evals.judge_calibration.track_record import ground_truth_agreement
from evals.judge_calibration.traps import MINIMUM_TRAPS_PER_JUDGE, TrapCase, traps_for
from incident_commander.llm.client import LLMClientProtocol, LLMError

#: The artifact family a calibration report is filed under. One name, read by the
#: writer here, by the Makefile target's help text and by the test that asserts
#: the kind is registered — an unregistered family cannot be resolved by
#: ``newest()`` and its writes would not be exclusive-create (divergence D2).
ARTIFACT_KIND: Final[str] = "judge_calibration"

#: Plan 03 § 109's N. Declared rather than defaulted inline so the report can say
#: what N it used and a caller cannot change it by accident.
SELF_AGREEMENT_REPS: Final[int] = 5

#: What ``judge_client`` reads as. ``fake`` is the free path and the default;
#: ``live`` is the paid one. In the report rather than inferred from a model id,
#: because a number produced by a scripted fake must never be mistaken for a
#: measurement of a model, and a reader should not have to know which ids are real.
FAKE_CLIENT: Final[str] = "fake"
LIVE_CLIENT: Final[str] = "live"


@dataclass(frozen=True, kw_only=True)
class TrapOutcome:
    """One trap case, asked ``len(observed)`` times, against what it asserts."""

    case_id: str
    shape: str
    asserts: str
    observed: tuple[str, ...]
    error: str | None = None

    @property
    def answered(self) -> bool:
        return self.error is None and bool(self.observed)

    @property
    def agrees(self) -> bool:
        """Did the FIRST ask match the asserted verdict?

        The first ask, not a majority of the reps: a judge is called once in a
        run, so "would it be right if you asked it five times and voted" is a
        different system from the one being measured. The reps answer stability,
        and stability is reported beside accuracy rather than folded into it.
        """
        return self.answered and self.observed[0] == self.asserts

    @property
    def stable(self) -> bool:
        return self.answered and len(set(self.observed)) == 1


@dataclass(frozen=True, kw_only=True)
class CalibrationReport:
    """What one judge's calibration found, and everything needed to read it."""

    report_id: str
    judge: str
    generated_at: datetime
    model: str
    judge_client: str
    reps: int
    rubric: Mapping[str, Any]
    determinism: Mapping[str, Any]
    trap_agreement: Mapping[str, Any]
    self_agreement: Mapping[str, Any]
    ground_truth_agreement: Mapping[str, Any]
    outcomes: tuple[TrapOutcome, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "judge": self.judge,
            "what_it_decides": role(self.judge).what_it_decides,
            "generated_at": self.generated_at.isoformat(),
            "model": self.model,
            "judge_client": self.judge_client,
            "reps": self.reps,
            "protocol": PROTOCOL,
            "rubric": dict(self.rubric),
            "determinism": dict(self.determinism),
            "trap_agreement": dict(self.trap_agreement),
            "self_agreement": dict(self.self_agreement),
            "ground_truth_agreement": dict(self.ground_truth_agreement),
            "absent_roles": dict(ABSENT_ROLES),
            "cases": [
                {
                    "case_id": outcome.case_id,
                    "shape": outcome.shape,
                    "asserts": outcome.asserts,
                    "observed": list(outcome.observed),
                    "agrees": outcome.agrees,
                    "stable": outcome.stable,
                    "error": outcome.error,
                }
                for outcome in self.outcomes
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=False) + "\n"

    @property
    def is_a_measurement(self) -> bool:
        """Was this produced by a real judge?

        ``False`` for a fake-client report, which exists to prove the harness end
        to end and must never be quoted as a calibration or entered in
        ``research_report.JUDGE_CALIBRATION_REPORTS``. Stated as a property so the
        distinction is checkable rather than a convention about filenames.
        """
        return self.judge_client == LIVE_CLIENT


#: The protocol this report implements, in the report, in its own words. A reader
#: who finds a number here should be able to see what it is a number about
#: without opening the plan.
PROTOCOL: Final[str] = (
    "plan 03 § 9 (WP-6.3). Four legs: a hand-built trap set whose verdicts the "
    "evaluator asserts; self-agreement over N identical asks; an independent "
    "ground-truth leg where one exists; and a stated refusal where one does not. "
    "Rubric edits are ONE LINE AT A TIME with a rerun (plan 03 § 110), which is "
    "why rubric.sha256 is here: a delta between two calibrations is attributable "
    "only if each says which rubric bytes it measured. No selector or judge number "
    "is reported anywhere until its id appears in "
    "research_report.CALIBRATION_REPORTS or JUDGE_CALIBRATION_REPORTS "
    "(plan 02:243, plan 04:169)."
)


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(numerator / denominator, 4)


def ask(
    case: TrapCase, *, client: LLMClientProtocol, model: str, reps: int = SELF_AGREEMENT_REPS
) -> TrapOutcome:
    """Put one trap case ``reps`` times and record every verdict.

    An ``LLMError`` — which ``OutputRepairExhausted`` is — ends this case and is
    recorded rather than raised: one judge that cannot produce a valid score for
    one trap should not throw away the other cases' measurements, and a
    calibration that reported nothing because case 3 of 6 malformed would be the
    least useful possible answer.
    """
    observed: list[str] = []
    for _ in range(reps):
        try:
            observed.append(case.subject.ask(client=client, model=model))
        except LLMError as err:
            return TrapOutcome(
                case_id=case.case_id,
                shape=case.shape,
                asserts=case.asserts,
                observed=tuple(observed),
                error=f"{type(err).__name__}: {err}",
            )
    return TrapOutcome(
        case_id=case.case_id, shape=case.shape, asserts=case.asserts, observed=tuple(observed)
    )


def _trap_agreement(judge: str, outcomes: Sequence[TrapOutcome]) -> dict[str, Any]:
    """Accuracy and the two error rates over the trap set.

    ``wrong_approval`` is a third number and it is only meaningful for the
    selector: a verdict can be an approval, and the asserted verdict an approval
    too, and still be the wrong one — ``select:c2`` where ``select:c1`` was right.
    That is neither a false approve nor a false reject, and folding it into either
    would hide the selector's most interesting error. Zero by construction for the
    other two judges, whose approval verdict is a single string.
    """
    answered = [outcome for outcome in outcomes if outcome.answered]
    errors = [outcome for outcome in outcomes if not outcome.answered]
    agreed = [outcome for outcome in answered if outcome.agrees]
    asserted_approval = [outcome for outcome in answered if is_approval(judge, outcome.asserts)]
    asserted_refusal = [outcome for outcome in answered if not is_approval(judge, outcome.asserts)]
    false_approves = [
        outcome for outcome in asserted_refusal if is_approval(judge, outcome.observed[0])
    ]
    false_rejects = [
        outcome for outcome in asserted_approval if not is_approval(judge, outcome.observed[0])
    ]
    wrong_approvals = [
        outcome
        for outcome in asserted_approval
        if is_approval(judge, outcome.observed[0]) and not outcome.agrees
    ]
    return {
        "cases": len(outcomes),
        "answered": len(answered),
        "errors": [{"case_id": outcome.case_id, "error": outcome.error} for outcome in errors],
        "agreed": len(agreed),
        "accuracy": _rate(len(agreed), len(answered)),
        "verdict_space": list(role(judge).verdicts),
        "approval_prefix": role(judge).approval_prefix,
        "asserted_approvals": len(asserted_approval),
        "false_rejects": len(false_rejects),
        "false_reject_rate": _rate(len(false_rejects), len(asserted_approval)),
        "asserted_refusals": len(asserted_refusal),
        "false_approves": len(false_approves),
        "false_approve_rate": _rate(len(false_approves), len(asserted_refusal)),
        "wrong_approvals": len(wrong_approvals),
        "by_shape": [
            {
                "case_id": outcome.case_id,
                "shape": outcome.shape,
                "asserts": outcome.asserts,
                "observed": outcome.observed[0] if outcome.answered else None,
                "agrees": outcome.agrees,
            }
            for outcome in outcomes
        ],
        "minimum_cases_required": MINIMUM_TRAPS_PER_JUDGE,
    }


def _self_agreement(outcomes: Sequence[TrapOutcome], *, reps: int) -> dict[str, Any]:
    """The fraction of cases whose ``reps`` asks all produced one verdict.

    Plan 03 § 109's reading of the two numbers together, restated here because it
    is the whole point of the leg: low stability means the rubric is ambiguous;
    high stability with low accuracy means the rubric is wrong. One tells you to
    write a clearer check, the other tells you the check is checking for the wrong
    thing, and without this number a low accuracy cannot be told apart from noise.
    """
    answered = [outcome for outcome in outcomes if outcome.answered]
    stable = [outcome for outcome in answered if outcome.stable]
    return {
        "reps": reps,
        "cases": len(answered),
        "identical": len(stable),
        "fraction_identical": _rate(len(stable), len(answered)),
        "reading": (
            "low stability means the rubric is ambiguous; high stability with low "
            "accuracy means the rubric is wrong (plan 03 § 109)"
        ),
        "unstable": [
            {"case_id": outcome.case_id, "observed": list(outcome.observed)}
            for outcome in answered
            if not outcome.stable
        ],
    }


def _rubric(judge: str) -> dict[str, Any]:
    lines = rubric_of(judge)
    body = "\n".join(lines)
    return {
        "prompt": role(judge).prompt,
        "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "lines": len(lines),
        "one_line_at_a_time": (
            "a rubric edit lands one line at a time with a rerun (plan 03 § 110); a "
            "delta between two calibrations is attributable only if this hash "
            "differs by a change a reviewer can see. The convention is in "
            "docs/eval-methodology.md and ADR 0052"
        ),
    }


def _determinism(client_kind: str) -> dict[str, Any]:
    return {
        "temperature_sent": None,
        "why_no_temperature": (
            "owner decision O-24 and ADR 0048: nothing built from 2026-09-17 on may "
            "REQUIRE a sampling parameter, because llm/client.SAMPLING_REJECTED_MODELS "
            "lists the model families that reject temperature with a 400 — a "
            "calibration pinned to temperature 0 would stop working on the first "
            "re-pin, which is exactly when a calibration is most needed"
        ),
        "instead": (
            "stability is MEASURED (self_agreement over N identical asks) rather "
            "than assumed from a setting, and the schema does the constraining: "
            "forced tool use against a fixed JSON schema, a closed verdict space, "
            "and validators that reject an out-of-set answer (ADR 0052)"
        ),
        "fixed_json_schema": True,
        "judge_client": client_kind,
    }


def calibrate(
    judge: str,
    *,
    client: LLMClientProtocol,
    model: str,
    reps: int = SELF_AGREEMENT_REPS,
    client_kind: str = FAKE_CLIENT,
    root: Path | None = None,
    now: datetime | None = None,
    report_id: str | None = None,
) -> CalibrationReport:
    """Calibrate one judge and return its report. Writes nothing.

    ``client_kind`` is declared by the caller rather than sniffed from the client,
    on the same principle as the register in ``research_report``: a fact that
    decides whether a number may be quoted should be stated by whoever is in a
    position to know it, not derived from something that could coincide.

    ``reps`` below 1 is refused rather than clamped — a calibration with no asks
    is a document with an accuracy field and no measurement in it.
    """
    if reps < 1:
        raise ValueError(f"reps must be at least 1; got {reps}")
    cases = traps_for(judge)
    if len(cases) < MINIMUM_TRAPS_PER_JUDGE:
        raise ValueError(
            f"{judge} has {len(cases)} trap case(s); plan 03 § 107 asks for at "
            f"least {MINIMUM_TRAPS_PER_JUDGE}. A calibration over fewer is a "
            "number whose error bars are wider than its own scale."
        )
    outcomes = tuple(ask(case, client=client, model=model, reps=reps) for case in cases)
    return CalibrationReport(
        report_id=report_id or uuid4().hex[:12],
        judge=judge,
        generated_at=now or datetime.now(UTC),
        model=model,
        judge_client=client_kind,
        reps=reps,
        rubric=_rubric(judge),
        determinism=_determinism(client_kind),
        trap_agreement=_trap_agreement(judge, outcomes),
        self_agreement=_self_agreement(outcomes, reps=reps),
        ground_truth_agreement=ground_truth_agreement(judge, root=root),
        outcomes=outcomes,
    )


def calibrate_all(
    *,
    client: LLMClientProtocol,
    model: str,
    reps: int = SELF_AGREEMENT_REPS,
    client_kind: str = FAKE_CLIENT,
    root: Path | None = None,
    now: datetime | None = None,
) -> tuple[CalibrationReport, ...]:
    """One report per calibrated judge, in ``CALIBRATED_ROLES`` order."""
    return tuple(
        calibrate(
            judge,
            client=client,
            model=model,
            reps=reps,
            client_kind=client_kind,
            root=root,
            now=now,
        )
        for judge in CALIBRATED_ROLES
    )


def write_report(
    report: CalibrationReport, *, directory: Path | None = None, root: Path | None = None
) -> Path:
    """Persist one calibration, versioned and exclusive-create.

    One artifact per judge, resolvable as ``artifacts.newest("judge_calibration",
    <judge>)``. Filed per judge rather than per run because plan 03 § 112 says
    "one per judge" and because the register that reads them is keyed by judge: a
    single combined document would make "is briefing_judge calibrated?" a question
    about a file that also contains two other answers.
    """
    return artifacts.write_versioned(
        ARTIFACT_KIND,
        report.judge,
        content=report.to_json(),
        timestamp=report.generated_at,
        invocation_id=report.report_id,
        directory=directory,
        root=root,
    )

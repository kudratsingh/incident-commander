"""The ``action_verifier``'s track record, read off the committed archives.

Plan 03 § 109 asks for ground-truth agreement "over every recorded-world run's
candidate sets / plans / briefings". Three corrections, each of which makes the
leg smaller and honest rather than larger and invented.

**1. It is read, not re-run, so it is free.** Every live archive already holds the
verdict this judge gave: the evidence ledger writes a ``_verify_judge`` entry
whose ``result_summary`` is ``"<verdict>: <reasoning>"``. Re-asking the judge over
those inputs would cost money and would answer a different question ("what would
it say today"). What is wanted here is what it DID say, beside what should have
happened — and that is on disk, for nothing.

**2. Only a run whose judge really ran counts.** A canned run's LLM is
``CannedLLMClient``: its ``_verify_judge`` verdict is a fixture, scripted by a
scenario author. Pairing those would be measuring the fixtures. So the scan takes
only outcomes with ``live_llm: true`` — 30 of the 156 (archive, scenario) pairs
that carry a verdict at all. Saying that number out loud matters: a reader who
sees the smaller one should be able to find out here why it is smaller.

**3. The ground truth is the SCENARIO'S OWN expectation, and the pairing is
narrow.** A row is paired only when the scenario declares a Tier-1 action
(``expected_action_tools`` non-empty) **and** expects to end ``resolved``. For
exactly that shape the right verdict is unambiguous — the action was the one the
scenario is about and it was supposed to work, so ``verified`` is correct — and
every other shape is left out with its reason attached:

* a read-only scenario (no action expectation) that the agent resolved anyway.
  Seven of the thirty rows are this, all from one August archive. Its OUTCOME
  dimension reads "expected escalated, got resolved", which is a finding about
  the loop from 2026-08 and says nothing about a judge that was asked whether an
  action worked. Grading those against OUTCOME is INC-003's mistake in judge
  form: a label written about one question applied to another.
* the stabilize-only handover — the action worked and the incident is still not
  over, so the scenario expects ``escalated`` while the right verdict is
  ``verified`` (``dlq_human_required_escalates``, ``dlq_poison_unclassified``).
  Two rows. Scoring them would charge a correct handover to the judge.
* a run whose terminal state is not the one its last verdict commits it to, which
  means something other than this judge decided the outcome.

**What this leg is, and what it is not.** It is a BOUND, not an accuracy. A
disagreement is either a judge error or a run that genuinely did not recover, and
the leg cannot tell those apart from the archive alone — so every disagreeing row
is named, with its archive id, for a reader to open the trajectory. And the
one-sidedness is the finding worth carrying out of here: **no live run in the
committed archives was ever supposed to end ``not_verified``**. Only
``remediate_verify_fails`` makes a refusal the right answer and it has never run
live, so the false-approve direction — a judge blessing a fix that had not landed,
the dangerous one — is UNMEASURED by this leg. The trap set is the only cover for
it (cases av-02, av-04, av-06), which is precisely why the trap set is the leg
that cannot be skipped.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from evals.artifacts import REPO_ROOT
from evals.judge_calibration.roles import ACTION_VERIFIER, BRIEFING_JUDGE, CANDIDATE_SELECTOR

#: The bookkeeping evidence entry the ``action_verifier``'s verdict is written to.
#: Underscore-prefixed by the ledger's convention, which is what keeps it out of
#: the investigation trail every LLM reader is shown.
VERDICT_MARKER: Final[str] = "_verify_judge"

VERIFIED: Final[str] = "verified"
NOT_VERIFIED: Final[str] = "not_verified"

#: Terminal state each verdict commits the run to. Used to check that the verdict
#: being graded is the one that decided the outcome — not to derive the ground
#: truth, which comes from the scenario.
_IMPLIES: Final[Mapping[str, str]] = {VERIFIED: "resolved", NOT_VERIFIED: "escalated"}


@dataclass(frozen=True, kw_only=True)
class Expectation:
    """What one scenario says should happen — the evaluator's own label."""

    family: str
    terminal_state: str
    acts: bool


@dataclass(frozen=True, kw_only=True)
class VerdictRow:
    """One archived verdict, beside the verdict its scenario called for."""

    archive: str
    scenario: str
    family: str
    verdict: str
    final_state: str
    correct_verdict: str | None
    paired: bool
    not_paired_because: str | None

    @property
    def agrees(self) -> bool:
        return self.paired and self.verdict == self.correct_verdict


def _expectations(root: Path) -> Mapping[str, Expectation]:
    """Each scenario's expectation, from the corpus — the evaluator's own claim.

    Imported lazily: loading the corpus parses every scenario YAML, and a caller
    that only wants the scan's shape should not pay for it until a row needs one.
    """
    from evals.scenarios.loader import load_scenarios

    return {
        scenario.name: Expectation(
            family=scenario.family.value if scenario.family else "unknown",
            terminal_state=scenario.expectation.expected_terminal_state.value,
            acts=bool(scenario.expectation.expected_action_tools),
        )
        for scenario in load_scenarios(root / "evals" / "scenarios")
    }


def _archived_verdicts(trajectory: Mapping[str, Any]) -> list[str]:
    """The judge's verdicts in one trajectory, in order, without repeats.

    A trajectory is a list of checkpoints and each checkpoint carries the whole
    ledger so far, so a verdict appears once per checkpoint after the one that
    wrote it. Consecutive duplicates are collapsed; a genuine second verdict with
    a different value (a polling window that read ``not_verified`` then
    ``verified``) survives, because that is a different judge call.
    """
    seen: list[str] = []
    for checkpoint in trajectory.get("checkpoints", []):
        for entry in checkpoint.get("evidence", []):
            if entry.get("tool_name") != VERDICT_MARKER:
                continue
            verdict = str(entry.get("result_summary", "")).split(":", 1)[0].strip()
            if verdict and (not seen or seen[-1] != verdict):
                seen.append(verdict)
    return seen


def scan(*, root: Path | None = None) -> list[VerdictRow]:
    """Every archived ``action_verifier`` verdict from a run whose judge was real.

    Reads only, and never touches an archive: ``evals/runs/`` is append-only and
    locked on disk (ADR 0021), so this opens ``report.json`` and the trajectories
    and writes nothing anywhere.
    """
    base = (root or REPO_ROOT) / "evals" / "runs"
    if not base.is_dir():
        return []
    expectations: Mapping[str, Expectation] | None = None
    rows: list[VerdictRow] = []
    for archive in sorted(entry for entry in base.iterdir() if entry.is_dir()):
        report_path = archive / "report.json"
        if not report_path.is_file():
            continue
        report = json.loads(report_path.read_text())
        for outcome in report.get("outcomes", []):
            if not outcome.get("live_llm"):
                continue
            scenario = str(outcome.get("scenario", ""))
            trajectory_path = archive / "trajectories" / f"{scenario}.json"
            if not trajectory_path.is_file():
                continue
            verdicts = _archived_verdicts(json.loads(trajectory_path.read_text()))
            if not verdicts:
                continue
            if expectations is None:
                expectations = _expectations(root or REPO_ROOT)
            rows.append(
                _row(
                    archive=archive.name,
                    scenario=scenario,
                    verdict=verdicts[-1],
                    final_state=str(outcome.get("final_state", "")),
                    expectation=expectations.get(scenario),
                )
            )
    return rows


def _row(
    *,
    archive: str,
    scenario: str,
    verdict: str,
    final_state: str,
    expectation: Expectation | None,
) -> VerdictRow:
    reason = _why_not_paired(verdict=verdict, final_state=final_state, expectation=expectation)
    return VerdictRow(
        archive=archive,
        scenario=scenario,
        family=expectation.family if expectation else "unknown",
        verdict=verdict,
        final_state=final_state,
        correct_verdict=None if reason else VERIFIED,
        paired=not reason,
        not_paired_because=reason or None,
    )


def _why_not_paired(*, verdict: str, final_state: str, expectation: Expectation | None) -> str:
    """Why this row is not graded, or the empty string when it is.

    Every branch is a refusal with a stated reason rather than a silent drop. A
    row that vanished from a denominator without saying why is how a rate becomes
    unreadable — and each of these four has cost something to learn.
    """
    if expectation is None:
        return (
            "the corpus no longer holds this scenario, so nothing states what should have happened"
        )
    implied = _IMPLIES.get(verdict)
    if implied is None:
        return f"verdict {verdict!r} is not one this judge emits"
    if not expectation.acts:
        return (
            "the scenario declares no Tier-1 action (a read-only scenario), so "
            "'did the action work' is not a question it is about. Its OUTCOME "
            "dimension grades whether the run ended in the expected state, which "
            "is a finding about the loop rather than about this judge — applying "
            "it here would be INC-003's error in judge form"
        )
    if expectation.terminal_state != "resolved":
        return (
            f"the scenario expects to end {expectation.terminal_state!r} even "
            "though its action is meant to work — the stabilize-only handover "
            "(the fix landed and the incident is still not over). The right "
            "verdict there is 'verified' and the right terminal state is an "
            "escalation, so the two cannot be read off one another"
        )
    if implied != final_state:
        return (
            f"the last verdict was {verdict!r}, which commits the run to "
            f"{implied!r}, and the run ended {final_state!r} — so something other "
            "than this judge decided the outcome"
        )
    return ""


def _rate(numerator: int, denominator: int) -> float | None:
    """A rate, or ``None`` when nothing was counted. Never 0.0 for "no data"."""
    return None if denominator == 0 else round(numerator / denominator, 4)


def _agreement(rows: Sequence[VerdictRow]) -> dict[str, Any]:
    """Agreement over paired rows, with the unmeasurable direction named.

    ``false_reject`` is a ``not_verified`` where ``verified`` was called for: a
    landed fix read as incomplete, so a human was paged for nothing.
    ``false_approve`` is the mirror and the dangerous one — a fix that had not
    landed blessed, resolving an incident that is not over. Its rate is ``None``
    here, and that is not a zero: the paired set contains no row where a refusal
    was the right answer, because the one scenario built to produce one
    (``remediate_verify_fails``) has never run live. The trap set covers that
    direction instead.
    """
    disagreed = [row for row in rows if not row.agrees]
    refusals_called_for = [row for row in rows if row.correct_verdict == NOT_VERIFIED]
    return {
        "paired": len(rows),
        "agreed": sum(row.agrees for row in rows),
        "agreement": _rate(sum(row.agrees for row in rows), len(rows)),
        "verifications_called_for": len(rows) - len(refusals_called_for),
        "false_rejects": len(disagreed),
        "false_reject_rate": _rate(len(disagreed), len(rows) - len(refusals_called_for)),
        "refusals_called_for": len(refusals_called_for),
        "false_approve_rate": _rate(
            sum(row.verdict == VERIFIED for row in refusals_called_for),
            len(refusals_called_for),
        ),
        "false_approve_is_unmeasured_because": (
            "no live run in the committed archives was ever supposed to end "
            "'not_verified': only remediate_verify_fails makes a refusal the "
            "right answer and it has never run live. A null here is 'not "
            "measured', never 'zero' — the trap set (av-02, av-04, av-06) is "
            "what covers this direction"
        )
        if not refusals_called_for
        else "",
        "a_disagreement_is_not_only_a_judge_error": (
            "this leg cannot tell a wrong verdict from a run that genuinely did "
            "not recover; every disagreeing row is named below so the trajectory "
            "can be read"
        ),
        "disagreements": [
            {
                "archive": row.archive,
                "scenario": row.scenario,
                "family": row.family,
                "verdict": row.verdict,
                "called_for": row.correct_verdict,
            }
            for row in disagreed
        ],
    }


def _by_family(rows: Sequence[VerdictRow]) -> list[dict[str, Any]]:
    grouped: dict[str, list[VerdictRow]] = {}
    for row in rows:
        grouped.setdefault(row.family, []).append(row)
    return [
        {"family": family, **_agreement(members)} for family, members in sorted(grouped.items())
    ]


def not_measured(what: str, why: str, requires: Sequence[str]) -> dict[str, Any]:
    """A leg that exists, has no number, and says what it would need.

    The same four keys ``research_report._not_measurable`` uses, deliberately: a
    reader moving between the two documents should not have to learn a second
    spelling of "there was nothing to compute".
    """
    return {"leg": what, "measured": False, "value": None, "why": why, "requires": list(requires)}


LEG: Final[str] = "track record: archived verdicts vs the scenario's own expectation"


def ground_truth_agreement(judge: str, *, root: Path | None = None) -> dict[str, Any]:
    """This judge's agreement with an independent label, or why there is none.

    Measured for ``action_verifier`` only. The other two refusals are structural
    rather than a gap in today's evidence, which is why they are spelled out here
    instead of waiting for a corpus that will not fix them:

    ``candidate_selector`` — its agreement with the scenario's labelled root cause
    IS ``selected@k`` at k=1 (``evals/candidate_metrics.selected_at_k``), the term
    the oracle gap is built from and the number this very calibration report is
    what unlocks (plan 02:243). Computing it here would certify the selector using
    the measurement the certificate releases. The trap set is the selector's
    ground truth for exactly that reason: it is a label no run produced.

    ``briefing_judge`` — nothing in the system deterministically labels a briefing
    as useful. The five graded dimensions are about the run, not about the prose,
    and inventing a proxy (briefing length, whether the run passed) would be
    scoring the judge against something that is not the question. Its ground truth
    is the trap set, which carries one human-adjudicated case: INC-002.
    """
    if judge == CANDIDATE_SELECTOR:
        return not_measured(
            "ground-truth agreement over recorded-world runs (plan 03 § 109)",
            "refused as circular: this judge's agreement with a scenario's labelled "
            "root cause is selected@k at k=1, which is the term oracle_gap@k is "
            "built from and the number a calibration report is what releases (plan "
            "02:243). A calibration may not be certified by the measurement it "
            "certifies",
            (
                "an independent label for a selection that is not derived from the "
                "scenario's root cause — human adjudication of a candidate set, or "
                "the trap set, which is what this report uses instead",
            ),
        )
    if judge == BRIEFING_JUDGE:
        return not_measured(
            "ground-truth agreement over recorded-world runs (plan 03 § 109)",
            "no deterministic label for briefing usefulness exists anywhere in the "
            "system: the five graded dimensions are statements about the run, not "
            "about its prose, and a proxy built from them would score this judge "
            "against a different question",
            (
                "human labels for a sample of committed briefings (grounded yes/no, "
                "actionable yes/no) — the shape INC-002 produced by hand for one "
                "briefing, which is trap bj-05",
            ),
        )
    if judge != ACTION_VERIFIER:
        raise KeyError(f"no ground-truth leg defined for judge {judge!r}")

    rows = scan(root=root)
    paired = [row for row in rows if row.paired]
    if not paired:
        return not_measured(
            LEG,
            "no committed archive holds a verdict from a run whose judge was a real "
            "model call (live_llm), on a scenario that declares a Tier-1 action and "
            "expects to end resolved",
            ("one live archive of a remediation scenario carrying a _verify_judge entry",),
        )
    return {
        "leg": LEG,
        "measured": True,
        "value": {
            "ground_truth": (
                "the scenario's own expectation, from the corpus: a scenario that "
                "declares a Tier-1 action and expects to end resolved is one whose "
                "action was meant to work, so 'verified' is the right verdict. "
                "Evaluator-owned (ADR 0038) and scoped to the world the scenario "
                "describes (ADR 0040)."
            ),
            "spent": "nothing — every verdict is read from a committed archive",
            "it_is_a_bound": (
                "an upper bound on this judge's agreement, not an accuracy: see "
                "a_disagreement_is_not_only_a_judge_error below"
            ),
            "scanned": len(rows),
            "not_paired": [
                {
                    "archive": row.archive,
                    "scenario": row.scenario,
                    "verdict": row.verdict,
                    "final_state": row.final_state,
                    "because": row.not_paired_because,
                }
                for row in rows
                if not row.paired
            ],
            **_agreement(paired),
            "by_family": _by_family(paired),
            "rows": [
                {
                    "archive": row.archive,
                    "scenario": row.scenario,
                    "family": row.family,
                    "verdict": row.verdict,
                    "called_for": row.correct_verdict,
                }
                for row in paired
            ],
        },
        "why": "",
        "requires": [],
    }


def live_archives(*, root: Path | None = None) -> Iterator[str]:
    """Archive ids the scan found a real judge verdict in. For ``--scan``."""
    seen: set[str] = set()
    for row in scan(root=root):
        if row.archive not in seen:
            seen.add(row.archive)
            yield row.archive

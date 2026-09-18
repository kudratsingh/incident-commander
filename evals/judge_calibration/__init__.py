"""Judge calibration: what a judge's number is worth, measured before it is quoted.

Plan 03 § 9 (WP-6.3). A judge whose number has never been checked is an opinion
presented as a measurement. This package is the check, and its output is the
artefact a report has to name before it may print a judge's number at all.

**Three judges, not the three the plan names.** Plan 03 § 104 lists
``candidate_selector``, ``plan_approval_judge`` and ``briefing_judge``. Two
corrections, both recorded as divergences and both narrowing rather than widening:

* ``plan_approval_judge`` **does not exist**, in any form (divergence B6). Every
  approve/refuse decision on a remediation plan is deterministic guard code
  (ADRs 0024, 0025, 0027, 0028, 0030, 0032) because CLAUDE.md invariant 4 forbids
  deriving a control from model output. There is nothing to calibrate, and
  ``roles.ABSENT_ROLES`` says so in the report rather than leaving a reader to
  wonder which judge was skipped.
* ``action_verifier`` **is** calibrated, though § 104 omits it (divergence J6).
  It is the one judge whose verdict gates a live OUTCOME — ``verified`` resolves
  the incident, ``not_verified`` escalates it — and the only one with a live
  track record. Leaving the judge with consequences out of the calibration set
  and keeping the two without would be the wrong half.

So: ``action_verifier``, ``briefing_judge``, ``candidate_selector``.

**Four legs, and each says which it is.**

1. ``traps`` — a hand-built trap set, at least five cases per judge, the shapes
   plan 03 § 107 enumerates. Each case ASSERTS a verdict and says why. This is
   ground truth that belongs to the evaluator and depends on no run, which is
   what makes it the leg that cannot go circular.
2. ``self-agreement at N = 5`` — the same input asked five times, reporting the
   fraction of identical verdicts. This is the leg that replaces plan 03 § 9.1's
   "temperature 0": ADR 0048 and owner decision O-24 say nothing built here may
   REQUIRE a sampling parameter, and no judge call in this repo sends one. A
   setting is a claim about stability; five identical verdicts are a measurement
   of it. See ADR 0052.
3. ``track record`` — for ``action_verifier`` only, and free: the verdicts it
   already gave in the committed archives, beside the verdict each scenario's own
   expectation called for. Costs nothing and spends nothing; the evidence is on
   disk. It is a BOUND rather than an accuracy, its pairing is narrow, and it
   cannot reach the false-approve direction at all — ``track_record.py`` says why
   for each, because those limits are the leg's most useful output.
4. ``refusals`` — where a leg cannot be measured, the report says so, says why,
   and says what it needs. Two of those refusals are structural rather than
   temporary, and they are the interesting part: the selector's recorded-run
   agreement would be ``selected@k`` under a second name, which is the number
   this calibration exists to unlock (circular); and the briefing judge's
   usefulness has no deterministic label anywhere in the system.

**Nothing here is a gate on the agent.** Judge scores are informational
(``evals/graders/llm_judge.py``'s module docstring), and this packet does not
change that: no run passes or fails on a calibration. What it gates is a
REPORT — ``evals/research_report.py`` withholds a judge number until that judge
has an id in ``JUDGE_CALIBRATION_REPORTS``, the same shape and the same argument
as the selector gate beside it (plan 02:243, plan 04:169).

**Running it costs money, so the default does not.** ``python -m
evals.judge_calibration`` runs against the scripted fake judge and writes
nothing unless asked. ``--live`` is the paid leg and refuses without an explicit
``--yes-spend``; see ``__main__.py`` and PROTOCOL's rule that readiness is not
authorization.
"""

from __future__ import annotations

from evals.judge_calibration.fakes import FakeJudgeClient
from evals.judge_calibration.harness import (
    ARTIFACT_KIND,
    CalibrationReport,
    TrapOutcome,
    calibrate,
    write_report,
)
from evals.judge_calibration.roles import (
    ABSENT_ROLES,
    ACTION_VERIFIER,
    BRIEFING_JUDGE,
    CALIBRATED_ROLES,
    CANDIDATE_SELECTOR,
    PLAN_APPROVAL_JUDGE,
    ROLES,
    JudgeRole,
    is_approval,
)
from evals.judge_calibration.traps import TRAPS, TrapCase, traps_for

__all__ = [
    "ABSENT_ROLES",
    "ACTION_VERIFIER",
    "ARTIFACT_KIND",
    "BRIEFING_JUDGE",
    "CALIBRATED_ROLES",
    "CANDIDATE_SELECTOR",
    "PLAN_APPROVAL_JUDGE",
    "ROLES",
    "TRAPS",
    "CalibrationReport",
    "FakeJudgeClient",
    "JudgeRole",
    "TrapCase",
    "TrapOutcome",
    "calibrate",
    "is_approval",
    "traps_for",
    "write_report",
]

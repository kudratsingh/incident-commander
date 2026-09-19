"""Judge calibration: what a judge's number is worth, measured before it is quoted.

Plan 03 § 9 (WP-6.3): a judge whose number has never been checked is an opinion
presented as a measurement, and a report must name this artefact before printing one.

Three judges, not plan 03 § 104's three. ``plan_approval_judge`` DOES NOT EXIST
(divergence B6) — every approve/refuse on a plan is deterministic guard code, because
invariant 4 forbids deriving a control from model output — and ``action_verifier`` IS
calibrated though § 104 omits it (divergence J6), because its verdict is the one that
gates a live OUTCOME. ``roles.ABSENT_ROLES`` says so in the report.

Four legs, each naming itself: a hand-built ``traps`` set, which is the only leg that
cannot go circular; ``self-agreement at N = 5``, which replaces § 9.1's "temperature 0"
because no call here may REQUIRE a sampling parameter (ADR 0048, O-24, ADR 0052) and
five identical verdicts MEASURE what a setting only claims; the free ``track record``,
read off committed archives and honest about being a BOUND; and ``refusals``, where a
leg says why it cannot be measured. Nothing gates the AGENT — what it gates is a
REPORT (``research_report.JUDGE_CALIBRATION_REPORTS``). The default path uses the
scripted fake and spends nothing; ``--live`` needs an explicit ``--yes-spend``.
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

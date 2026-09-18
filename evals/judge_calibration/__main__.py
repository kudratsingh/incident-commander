"""``python -m evals.judge_calibration`` — run the calibration, free by default.

Three modes, and the default is the one that cannot spend:

* no flags — the scripted fake judge. Proves the harness end to end, prints the
  same report a real run would, and says on every line of the summary that it is
  not a measurement. Writes nothing.
* ``--write`` — the same, persisted. Allowed on the fake path because a
  fake-client report is still evidence that the harness ran; the report's own
  ``judge_client`` field is ``fake`` and ``is_a_measurement`` is false, so it can
  never be mistaken for a calibration or entered in the register.
* ``--live --yes-spend`` — the real judge, the paid leg. **Both flags are
  required.** ``--live`` alone refuses and says so, because PROTOCOL's rule is
  that readiness is not authorization: a run that spends money on the strength of
  one flag somebody typed while exploring is the failure that rule exists to stop.

The paid leg is DEFERRED at the time of writing: the owner's instruction O-22 is
to build every phase with no paid runs, so the command below has been proved
against the fake and its cost is estimated in
``.coordination/DEFERRED-PAID-RUNS.md``, not spent.

``--scan`` answers what the free legs can see without asking any judge anything:
how many trap cases per judge, and which committed archives carry a real
``action_verifier`` verdict.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Final

from evals.judge_calibration import track_record
from evals.judge_calibration.fakes import FakeJudgeClient, answers_for
from evals.judge_calibration.harness import (
    FAKE_CLIENT,
    LIVE_CLIENT,
    SELF_AGREEMENT_REPS,
    CalibrationReport,
    calibrate,
    write_report,
)
from evals.judge_calibration.roles import ABSENT_ROLES, CALIBRATED_ROLES
from evals.judge_calibration.traps import traps_for

#: The imperfection the fake judge is scripted with, per judge, and it is
#: deliberate. A fake that agreed with every trap would exercise the accuracy
#: numerator and nothing else — no false approve, no false reject, no unstable
#: case — so the end-to-end proof would not reach the fields a real calibration is
#: read for. Each entry names a case and the verdict the fake gives it instead.
#:
#: These are NOT claims about the real judges. They are a script.
_SCRIPTED_WRONG: Final[dict[str, dict[str, str]]] = {
    "action_verifier": {"av-02-read-shows-no-movement": "verified"},
    "briefing_judge": {"bj-05-inc-002-honest-after-a-filtered-read": "grounded=no actionable=yes"},
    "candidate_selector": {"cs-06-one-more-read-would-decide-it": "select:c1"},
}
_SCRIPTED_UNSTABLE: Final[dict[str, dict[str, str]]] = {
    "action_verifier": {"av-05-filtered-read-proves-its-slice": "not_verified"},
    "briefing_judge": {"bj-02-grounded-but-useless": "grounded=yes actionable=yes"},
    "candidate_selector": {"cs-03-near-duplicates-one-correct": "probe_more"},
}

_NOT_A_MEASUREMENT: Final[str] = (
    "SCRIPTED FAKE JUDGE — this is a proof that the harness runs, not a "
    "calibration. Nothing here may be quoted, and this report id must not be "
    "entered in research_report.JUDGE_CALIBRATION_REPORTS."
)


def _summarize(report: CalibrationReport) -> list[str]:
    traps = report.trap_agreement
    stability = report.self_agreement
    ground = report.ground_truth_agreement
    lines = [
        f"{report.judge}  (report {report.report_id}, client {report.judge_client})",
        f"  rubric      {report.rubric['prompt']}.md  sha256 {report.rubric['sha256'][:12]}  "
        f"{report.rubric['lines']} lines",
        f"  traps       {traps['agreed']}/{traps['answered']} agreed"
        f"   accuracy {traps['accuracy']}"
        f"   false approve {traps['false_approves']}/{traps['asserted_refusals']}"
        f"   false reject {traps['false_rejects']}/{traps['asserted_approvals']}",
        f"  stability   {stability['identical']}/{stability['cases']} identical over "
        f"N={stability['reps']}  ({stability['fraction_identical']})",
    ]
    if ground["measured"]:
        value = ground["value"]
        lines.append(
            f"  track record {value['agreed']}/{value['paired']} verdicts matched what "
            f"the scenario called for   agreement {value['agreement']}   "
            f"({value['scanned']} scanned, {len(value['not_paired'])} not paired; "
            f"false-approve direction {value['false_approve_rate']})"
        )
        for row in value["disagreements"]:
            lines.append(
                f"    ARCHIVE DISAGREEMENT  {row['archive']} {row['scenario']}: "
                f"said {row['verdict']!r}, scenario called for {row['called_for']!r}"
            )
    else:
        lines.append(f"  track record not measured — {ground['why']}")
    for case in traps["by_shape"]:
        if not case["agrees"]:
            lines.append(
                f"    DISAGREED  {case['case_id']}: asserts {case['asserts']!r}, "
                f"observed {case['observed']!r}"
            )
    for case in stability["unstable"]:
        lines.append(f"    UNSTABLE   {case['case_id']}: {case['observed']}")
    for entry in traps["errors"]:
        lines.append(f"    ERRORED    {entry['case_id']}: {entry['error']}")
    return lines


def _scan() -> int:
    print("Trap sets (plan 03 § 107):")
    for judge in CALIBRATED_ROLES:
        cases = traps_for(judge)
        print(f"  {judge}: {len(cases)} cases")
        for case in cases:
            print(f"    {case.case_id}  asserts {case.asserts!r}  — {case.shape}")
    print("\nNot calibrated, and not a judge:")
    for judge, why in ABSENT_ROLES.items():
        print(f"  {judge}: {why}")
    archives = list(track_record.live_archives())
    print(
        f"\nCommitted archives carrying a real action_verifier verdict: "
        f"{len(archives)}\n  {', '.join(archives) if archives else '(none)'}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.judge_calibration",
        description="Calibrate a judge against its trap set (plan 03 § 9, WP-6.3).",
    )
    parser.add_argument(
        "--judge",
        action="append",
        choices=list(CALIBRATED_ROLES),
        help="calibrate one judge; repeatable. Default: all three.",
    )
    parser.add_argument(
        "--reps",
        type=int,
        default=SELF_AGREEMENT_REPS,
        help=f"asks per trap case, for self-agreement (default {SELF_AGREEMENT_REPS}).",
    )
    parser.add_argument("--write", action="store_true", help="persist each report.")
    parser.add_argument(
        "--live",
        action="store_true",
        help="ask the real pinned JUDGE_MODEL. SPENDS MONEY. Needs --yes-spend too.",
    )
    parser.add_argument(
        "--yes-spend",
        action="store_true",
        help="the explicit second flag --live requires (PROTOCOL: readiness is not authorization).",
    )
    parser.add_argument("--scan", action="store_true", help="what the free legs see; asks nothing.")
    args = parser.parse_args(argv)

    if args.scan:
        return _scan()

    judges = tuple(args.judge) if args.judge else CALIBRATED_ROLES
    if args.live and not args.yes_spend:
        print(
            "refusing: --live asks the real judge and spends money. Pass "
            "--yes-spend as well, and only with the owner's explicit yes for "
            "THIS run (PROTOCOL § 'Paid run, per scenario', step 0 — readiness "
            "is not authorization).",
            file=sys.stderr,
        )
        return 2

    if args.live:
        # Imported here, not at module scope: building Settings reads the
        # environment and refuses to start without a priced JUDGE_MODEL, which a
        # fake-path run has no business requiring.
        from incident_commander.config import get_settings
        from incident_commander.llm.client import LLMClient

        settings = get_settings()
        # The key is read from Settings and handed straight to the SDK; it is
        # never printed, logged or put in the report.
        client: object = LLMClient(api_key=settings.anthropic_api_key.get_secret_value())
        model = settings.judge_model
        client_kind = LIVE_CLIENT
    else:
        model = "fake-judge"
        client_kind = FAKE_CLIENT

    exit_code = 0
    for judge in judges:
        if not args.live:
            client = FakeJudgeClient(
                answers_for(
                    judge,
                    wrong=_SCRIPTED_WRONG.get(judge),
                    unstable=_SCRIPTED_UNSTABLE.get(judge),
                    reps=args.reps,
                )
            )
        report = calibrate(
            judge,
            client=client,  # type: ignore[arg-type]
            model=model,
            reps=args.reps,
            client_kind=client_kind,
        )
        print("\n".join(_summarize(report)))
        if not report.is_a_measurement:
            print(f"  {_NOT_A_MEASUREMENT}")
        if args.write:
            print(f"  written: {write_report(report)}")
        print()
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

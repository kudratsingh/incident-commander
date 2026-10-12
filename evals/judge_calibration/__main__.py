"""``python -m evals.judge_calibration`` — run the calibration, free by default.

Three modes, and the default cannot spend. No flags runs the scripted fake, prints the
report a real run would and says on every summary line that it is not a measurement.
``--write`` persists that, which is allowed because ``judge_client: fake`` and
``is_a_measurement: false`` keep it out of the register. ``--live --yes-spend`` is the
paid leg and BOTH flags are required, because readiness is not authorization. The paid
leg is DEFERRED (O-22): proved against the fake, costed in
``.coordination/DEFERRED-PAID-RUNS.md``. ``--scan`` shows what the free legs can see.
``--model-role`` asks the selector leg on that run role's model, the one an arm's selector
is called on; the judge legs keep ``JUDGE_MODEL`` (WO-R3-364). ``--packet`` counts one label
packet's held-out briefings and ``--dev-packet`` shows another beside it, not counted
(WO-R3-378, owner O-58, ADR 0082); a label file of several packets needs ``--packet``.
``--rubric`` asks the briefing judge under a named rubric variant and ``--judge-model-override``
asks the judge legs on another model than ``JUDGE_MODEL``; the report records both, and the
free path prints the exact paid command a run with the same flags would take (WO-R3-383, O-61).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from evals.graders.llm_judge import BRIEFING_JUDGE_RUBRICS
from evals.judge_calibration import label_leg, labels, track_record
from evals.judge_calibration.fakes import FakeJudgeClient, answers_for, answers_for_labels
from evals.judge_calibration.harness import (
    FAKE_CLIENT,
    JUDGE_MODEL_OVERRIDE_SETTING,
    LIVE_CLIENT,
    SELF_AGREEMENT_REPS,
    CalibrationReport,
    calibrate,
    leg_model,
    leg_role,
    model_setting,
    write_report,
)
from evals.judge_calibration.label_leg import NoLabelsError
from evals.judge_calibration.roles import (
    ABSENT_ROLES,
    BRIEFING_JUDGE,
    CALIBRATED_ROLES,
    CANDIDATE_SELECTOR,
)
from evals.judge_calibration.traps import traps_for
from incident_commander.config import ModelRole
from incident_commander.llm.pricing import MODEL_PRICING

#: The imperfection the fake judge is scripted with, deliberately: a fake that agreed
#: with every trap would exercise the accuracy numerator and nothing else, so the
#: end-to-end proof would never reach the fields a calibration is read for. NOT claims
#: about the real judges — a script.
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

#: The model id the free path records: a script answered, and the report says so.
_FAKE_MODEL: Final[str] = "fake-judge"

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
        *_model_lines(report),
        f"  rubric      {report.rubric['prompt']}.md  sha256 {report.rubric['sha256'][:12]}  "
        f"{report.rubric['lines']} lines",
        *_variant_lines(report),
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
    if report.label_agreement is not None:
        lines.extend(_summarize_labels(report.label_agreement))
    return lines


def _variant_lines(report: CalibrationReport) -> list[str]:
    """A rubric variant says what it drops, and which traps test a check it does not carry."""
    rubric = report.rubric
    if "variant_of" not in rubric:
        return []
    return [
        f"    VARIANT    of {rubric['variant_of']}.md, not the production rubric; drops "
        f"{', '.join(repr(c) for c in rubric['removed_checks'])}; traps testing a dropped check "
        f"still count: {', '.join(rubric['traps_for_removed_checks'])}"
    ]


def _model_lines(report: CalibrationReport) -> list[str]:
    """Which model answered and where it came from; a selector on the judge's pin is flagged."""
    role = report.model_role
    source = model_setting(role) + ("" if role is None else f", role {role.value}")
    if report.judge_model_override is not None:
        source = f"{JUDGE_MODEL_OVERRIDE_SETTING}={report.judge_model_override}, not JUDGE_MODEL"
    if report.is_a_measurement:
        lines = [f"  model       {report.model}  from {source}"]
    else:
        lines = [f"  model       {report.model}  (scripted; a live run asks {source})"]
    if report.judge == CANDIDATE_SELECTOR and role is None:
        lines.append(
            "    NOTE       a run's selector is called on the run's own model, not JUDGE_MODEL: "
            "the register accepts this report only for an arm that ran on this model. "
            "MODEL_ROLE=benchmark asks BENCHMARK_MODEL."
        )
    return lines


def _summarize_labels(leg: Mapping[str, Any]) -> list[str]:
    """The owner-label leg: the counted block, then the dev set and the owner's test-retest."""
    value = leg["value"]
    held_out = "threshold" in value
    head = f"held-out    packet {value['packet']}:" if held_out else "owner labels"
    lines = [f"  {head} {_agreement_line(value)}  ({value['labels_file']})"]
    if held_out:
        lines.append(_threshold_line(value["threshold"]))
        if value["not_counted"]:
            ids = ", ".join(row["id"] for row in value["not_counted"])
            lines.append(
                f"    NOT COUNTED  {len(value['not_counted'])} briefing(s) of packet "
                f"{value['packet']} also carry a label in another packet (the rubric was written "
                f"with those in view): {ids}"
            )
    lines.extend(_set_detail(value, prefix="LABEL"))
    dev = leg.get("dev_set")
    if dev is not None:
        lines.append(
            f"  dev set     packet {dev['packet']}: {_agreement_line(dev)}  NOT COUNTED "
            "(the rubric was written from these labels)"
        )
        lines.extend(_set_detail(dev, prefix="DEV"))
    retest = leg.get("test_retest")
    if retest is not None:
        if retest["pairs"]:
            lines.append(
                f"  test-retest the owner labelled {retest['pairs']} briefing(s) of packet "
                f"{retest['packet']} in another packet too: {retest['same']}/{retest['pairs']} "
                "the same"
            )
        else:
            lines.append(
                f"  test-retest no briefing of packet {retest['packet']} carries a label in "
                "another packet"
            )
        for row in retest["rows"]:
            if not row["same"]:
                said = ", ".join(
                    f"packet {entry['packet']} {entry['label']} ({entry['labelled_at']})"
                    for entry in row["labels"]
                )
                lines.append(f"    RETEST CHANGED  {row['id']}: {said}")
    return lines


def _agreement_line(value: Mapping[str, Any]) -> str:
    stability = value["stability"]
    return (
        f"{value['agree']}/{value['n']} agreed   agreement {value['agreement']}"
        f"   judge useful / owner not {value['judge_useful_owner_not']}"
        f"   judge not useful / owner useful {value['judge_not_useful_owner_useful']}"
        f"   stable {stability['identical']}/{value['n']} over N={stability['reps']}"
        f"   reasoning names a check {value['reasoning_names_a_check']}/{value['n']}"
    )


def _threshold_line(threshold: Mapping[str, Any]) -> str:
    baseline = threshold["majority_label_baseline"]
    verdict = "CLEARS" if threshold["clears"] else "does not clear"
    incomplete = "" if threshold["complete"] else " (incomplete: an unanswered or missing briefing)"
    return (
        f"    threshold  {threshold['value']} ({threshold['constant']}, ADR 0082): {verdict}"
        f"{incomplete}; always '{baseline['label']}' would agree {baseline['agree']}/"
        f"{baseline['n']}; the owner decides whether the judge leaves 'measured'"
    )


def _set_detail(value: Mapping[str, Any], *, prefix: str) -> list[str]:
    lines = [
        f"    {prefix} DISAGREED  {row['id']}: owner {row['owner_label']}, judge "
        f"{row['judge_label']} (groundedness {row['groundedness']}, "
        f"actionability {row['actionability']})"
        for row in value["disagreements"]
    ]
    for label_id in value["missing_briefings"]:
        lines.append(f"    MISSING    {label_id}: briefing not in this checkout")
    for entry in value["errors"]:
        lines.append(f"    ERRORED    {entry['id']}: {entry['error']}")
    return lines


def _scripted_wrong_label(path: Path) -> tuple[str, ...]:
    """The fake disagrees with the first label in the file, so the disagreement fields run."""
    rows = label_leg.load_rows(path)
    return tuple(str(row["id"]) for row in rows[:1])


def paid_command(args: argparse.Namespace) -> str:
    """The exact ``make`` line that asks the real judge with the flags this free run was given.

    Printed on the free path so the rehearsal and the paid run cannot drift apart: the paid run
    is this line, copied, after the owner's yes (PROTOCOL: readiness is not authorization).
    """
    parts = ["make judge-calibration"]
    if args.judge:
        parts.extend(f"JUDGE={judge}" for judge in args.judge)
    if args.reps != SELF_AGREEMENT_REPS:
        parts.append(f"REPS={args.reps}")
    if args.labels is not None:
        default = Path(args.labels).resolve() == labels.LABELS_FILE.resolve()
        parts.append("LABELS=1" if default else f"LABELS={args.labels}")
    if args.packet is not None:
        parts.append(f"PACKET={args.packet}")
    if args.dev_packet is not None:
        parts.append(f"DEV_PACKET={args.dev_packet}")
    if args.rubric is not None:
        parts.append(f"RUBRIC={args.rubric}")
    if args.judge_model_override is not None:
        parts.append(f"{JUDGE_MODEL_OVERRIDE_SETTING}={args.judge_model_override}")
    if args.model_role is not None:
        parts.append(f"MODEL_ROLE={args.model_role}")
    parts.extend(["LIVE=1", "YES_SPEND=1", "WRITE=1"])
    return " ".join(parts)


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
    parser.add_argument(
        "--model-role",
        choices=[r.value for r in ModelRole],
        help=(
            f"{CANDIDATE_SELECTOR} only: ask the selector on the model this run role resolves "
            "to (BENCHMARK_MODEL for benchmark), the one an arm of that role calls it on. "
            "Default: JUDGE_MODEL. The judge legs always keep JUDGE_MODEL."
        ),
    )
    parser.add_argument(
        "--labels",
        nargs="?",
        const=labels.LABELS_FILE,
        type=Path,
        help=f"briefing_judge only: add the owner-label leg (default {labels.LABELS_FILE.name}).",
    )
    parser.add_argument(
        "--packet",
        type=int,
        help=(
            "briefing_judge only: count this label packet's held-out briefings only, and "
            "report them against HELD_OUT_AGREEMENT_THRESHOLD (ADR 0082). Implies --labels."
        ),
    )
    parser.add_argument(
        "--dev-packet",
        type=int,
        help="briefing_judge only: score this packet beside --packet, marked NOT COUNTED.",
    )
    parser.add_argument(
        "--rubric",
        choices=list(BRIEFING_JUDGE_RUBRICS),
        help=(
            f"{BRIEFING_JUDGE} only: ask every trap and label under this rubric instead of the "
            "production one; the report records its file name and sha256 (WO-R3-383)."
        ),
    )
    parser.add_argument(
        "--judge-model-override",
        metavar="MODEL",
        help=(
            "the judge legs ask this model instead of JUDGE_MODEL, and the report says "
            f"model_setting: {JUDGE_MODEL_OVERRIDE_SETTING}. Not for {CANDIDATE_SELECTOR}, "
            "which MODEL_ROLE governs (WO-R3-383)."
        ),
    )
    args = parser.parse_args(argv)

    if args.scan:
        return _scan()

    judges = tuple(args.judge) if args.judge else CALIBRATED_ROLES
    if args.dev_packet is not None and args.packet is None:
        print(
            "refusing: --dev-packet needs --packet: a dev set is shown beside a held-out "
            "packet, never instead of one (O-58, ADR 0082).",
            file=sys.stderr,
        )
        return 2
    for value in (args.packet, args.dev_packet):
        if value is not None and value < 1:
            print(f"refusing: a label packet is numbered from 1; got {value}.", file=sys.stderr)
            return 2
    if args.dev_packet is not None and args.dev_packet == args.packet:
        print(
            f"refusing: --dev-packet and --packet must differ (both {args.packet}).",
            file=sys.stderr,
        )
        return 2
    if args.packet is not None and args.labels is None:
        args.labels = labels.LABELS_FILE
    if args.labels is not None and judges != (BRIEFING_JUDGE,):
        print(
            f"refusing: --labels (and --packet) need --judge {BRIEFING_JUDGE} and no other judge.",
            file=sys.stderr,
        )
        return 2
    if args.rubric is not None and judges != (BRIEFING_JUDGE,):
        print(
            f"refusing: --rubric (RUBRIC=) names a {BRIEFING_JUDGE} rubric; pass "
            f"--judge {BRIEFING_JUDGE} and no other judge.",
            file=sys.stderr,
        )
        return 2
    override = args.judge_model_override
    if override is not None and CANDIDATE_SELECTOR in judges:
        print(
            f"refusing: --judge-model-override ({JUDGE_MODEL_OVERRIDE_SETTING}=) replaces "
            f"JUDGE_MODEL for the judge legs only, and {CANDIDATE_SELECTOR} would be asked "
            "too; name the judge leg(s) with --judge (JUDGE=). The selector's model is "
            "MODEL_ROLE's (WO-R3-364).",
            file=sys.stderr,
        )
        return 2
    if override is not None and override not in MODEL_PRICING:
        known = ", ".join(sorted(MODEL_PRICING))
        print(
            f"refusing: {JUDGE_MODEL_OVERRIDE_SETTING}={override!r} has no row in MODEL_PRICING, "
            f"so its calls would be billed at the most expensive rate on record. Priced ids: "
            f"{known}.",
            file=sys.stderr,
        )
        return 2
    if args.live and not args.yes_spend:
        print(
            "refusing: --live asks the real judge and spends money. Pass "
            "--yes-spend as well, and only with the owner's explicit yes for "
            "THIS run (PROTOCOL § 'Paid run, per scenario', step 0 — readiness "
            "is not authorization).",
            file=sys.stderr,
        )
        return 2

    model_role = None if args.model_role is None else ModelRole(args.model_role)
    if args.live:
        # Imported here, not at module scope: Settings refuses to start without a
        # priced JUDGE_MODEL, which a fake-path run has no business requiring.
        from incident_commander.config import get_settings
        from incident_commander.llm.client import LLMClient

        settings = get_settings()
        # The key is read from Settings and handed straight to the SDK; it is
        # never printed, logged or put in the report.
        client: object = LLMClient(api_key=settings.anthropic_api_key.get_secret_value())
        client_kind = LIVE_CLIENT
    else:
        client_kind = FAKE_CLIENT

    exit_code = 0
    for judge in judges:
        model = leg_model(judge, settings, model_role, override) if args.live else _FAKE_MODEL
        if not args.live:
            script = answers_for(
                judge,
                wrong=_SCRIPTED_WRONG.get(judge),
                unstable=_SCRIPTED_UNSTABLE.get(judge),
                reps=args.reps,
            )
            if args.labels is not None:
                script |= answers_for_labels(args.labels, wrong=_scripted_wrong_label(args.labels))
            client = FakeJudgeClient(script)
        try:
            report = calibrate(
                judge,
                client=client,  # type: ignore[arg-type]
                model=model,
                reps=args.reps,
                client_kind=client_kind,
                labels=args.labels,
                model_role=leg_role(judge, model_role),
                packet=args.packet,
                dev_packet=args.dev_packet,
                rubric=args.rubric,
                judge_model_override=override,
            )
        except (NoLabelsError, labels.LabelError) as err:
            print(str(err), file=sys.stderr)
            return 2
        print("\n".join(_summarize(report)))
        if not report.is_a_measurement:
            print(f"  {_NOT_A_MEASUREMENT}")
        if args.write:
            print(f"  written: {write_report(report)}")
        print()
    if not args.live:
        print(
            f"paid command (SPENDS MONEY; the owner's yes for THIS run first): {paid_command(args)}"
        )
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

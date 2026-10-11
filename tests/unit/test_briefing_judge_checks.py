"""WO-R3-378 (owner O-58, INC-010, ADR 0082): the briefing judge's rubric v2 is a list of CHECKS.

INC-010 measured the v1 rubric at 8/16 against the owner's labels, wrong in two directions: it
blessed three briefings carrying the owner's three fault patterns (a recommendation copied from a
standing dead-letter row, a blind probe read as evidence against the alert, a guess stated as a
finding) and marked down five honest "look at X" briefings for not naming a dashboard. Rubric v2
replaces the adjective ladders with checks the judge applies in order and names in `reasoning`.

Pinned here, red before green: the rubric names five checks in order, each one sentence with one
example quoting a briefing the owner labelled and the label the owner gave it; the ladders are
gone; `reasoning` must name the deciding check; every check has a trap whose verdict is stated;
and the trap set can FAIL the judge INC-010 measured — a fake answering the four new traps the way
rubric v1 answered their shapes disagrees on all four — while a judge applying the checks agrees
on all ten.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import Final

import pytest

from evals.graders.llm_judge import BRIEFING_JUDGE_CHECKS, JUDGE_PROMPT, cited_checks
from evals.judge_calibration import labels
from evals.judge_calibration.fakes import FakeJudgeClient, answers_for
from evals.judge_calibration.harness import calibrate
from evals.judge_calibration.label_leg import packet_of
from evals.judge_calibration.roles import BRIEFING_JUDGE, BriefingSubject, briefing_verdict
from evals.judge_calibration.traps import BRIEFING_CHECK_TRAPS, TrapCase, traps_for
from incident_commander.llm.prompts.loader import load_prompt, raw_prompt

_MODEL: Final[str] = "fake-judge"
_AT: Final[datetime] = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)

#: The four checks O-58 orders, in the order the rubric applies them, after the v1 floor.
_ORDERED: Final[tuple[str, ...]] = (
    "invented fact",
    "derived, not copied",
    "blind probe",
    "a guess is not a finding",
    "look at X, do nothing",
)

#: The four traps this order adds, the verdict each asserts, and the verdict rubric v1 gave
#: the owner-labelled briefing of the same shape on 2026-10-11 (report 99181bf074ec, INC-010).
_NEW_TRAPS: Final[dict[str, tuple[str, str]]] = {
    "bj-07-recommendation-copied-from-a-standing-row": (
        briefing_verdict(grounded=False, actionable=False),
        briefing_verdict(grounded=True, actionable=True),  # deploy_correlation: 0.95 / 0.85
    ),
    "bj-08-blind-probe-called-a-false-positive": (
        briefing_verdict(grounded=False, actionable=True),
        briefing_verdict(grounded=True, actionable=False),  # postgres_slow: 1.0 / 0.5
    ),
    "bj-09-a-guess-stated-as-a-finding": (
        briefing_verdict(grounded=False, actionable=True),
        briefing_verdict(grounded=True, actionable=True),  # stale_cache: 0.85 / 0.75
    ),
    "bj-10-nothing-investigated-look-at-the-alert": (
        briefing_verdict(grounded=True, actionable=True),
        briefing_verdict(grounded=True, actionable=False),  # noise_low_severity: 1.0 / 0.5
    ),
}

_EXAMPLE: Final[re.Pattern[str]] = re.compile(
    r"`([0-9a-f]{12}:[a-z0-9_]+)` \(owner: (useful|not_useful)\)"
)


def _prompt() -> str:
    return load_prompt(JUDGE_PROMPT)


def _check_blocks() -> dict[str, str]:
    """Each check's text, from its bold name to the next check's (or to `reasoning`)."""
    text = _prompt()
    starts = [(name, text.index(f"**{name}**")) for name in BRIEFING_JUDGE_CHECKS]
    end = text.index("`reasoning`:", starts[-1][1])
    bounds = [s for _, s in starts[1:]] + [end]
    return {name: text[start:stop] for (name, start), stop in zip(starts, bounds, strict=True)}


def _case(case_id: str) -> TrapCase:
    for case in traps_for(BRIEFING_JUDGE):
        if case.case_id == case_id:
            return case
    raise AssertionError(f"no briefing_judge trap {case_id!r}")


class TestTheRubricIsChecks:
    def test_the_checks_are_the_ordered_five(self) -> None:
        assert BRIEFING_JUDGE_CHECKS == _ORDERED

    def test_every_check_is_named_in_the_rubric_in_order(self) -> None:
        text = _prompt()
        positions = [text.find(f"**{name}**") for name in BRIEFING_JUDGE_CHECKS]
        assert all(p >= 0 for p in positions), dict(
            zip(BRIEFING_JUDGE_CHECKS, positions, strict=True)
        )
        assert positions == sorted(positions)
        assert "in order" in text

    @pytest.mark.parametrize("name", BRIEFING_JUDGE_CHECKS)
    def test_each_check_quotes_one_briefing_the_owner_labelled(self, name: str) -> None:
        """One example per check, by briefing id, with the label the owner actually gave it."""
        examples = _EXAMPLE.findall(_check_blocks()[name])
        assert len(examples) == 1, (name, examples)
        ((label_id, stated),) = examples
        assert labels.briefing_path(label_id).is_file(), label_id
        # Packet 1's label: the rubric was written from packet 1, and a later packet's label of
        # a repeated briefing is a re-test, not the label the example quotes.
        owner = {
            row["id"]: row["label"]
            for row in labels.read_lines(labels.LABELS_FILE)
            if packet_of(row) == 1
        }
        assert owner.get(label_id) == stated, (label_id, stated, owner.get(label_id))

    @pytest.mark.parametrize("name", BRIEFING_JUDGE_CHECKS)
    def test_each_check_is_one_sentence_and_names_its_dimension(self, name: str) -> None:
        block = _check_blocks()[name]
        rule = block.split("Example:", 1)[0]
        body = rule.split(").", 1)[1].strip()
        # One sentence: no full stop followed by a capital before the end of the rule.
        assert not re.search(r"\.\s+[A-Z]", body.rstrip(".")), body
        assert "groundedness" in rule or "actionability" in rule

    def test_the_caps_are_the_ones_the_order_names(self) -> None:
        blocks = _check_blocks()
        assert "groundedness 0.0" in blocks["invented fact"]
        copied = blocks["derived, not copied"]
        assert "groundedness at most 0.5" in copied and "actionability at most 0.5" in copied
        blind = blocks["blind probe"]
        assert "groundedness at most 0.5" in blind and "0.0 when that is its finding" in blind
        assert "groundedness 0.0" in blocks["a guess is not a finding"]
        look = blocks["look at X, do nothing"]
        assert "actionability 1.0" in look and "0.5 only when" in look and "0.0 when" in look

    def test_the_adjective_ladders_are_gone(self) -> None:
        text = _prompt()
        for rung in (
            "mostly grounded but with minor speculation",
            "directional but vague",
            "no invented components, numbers, or errors; every specific term",
            "names a specific action a human can take in the next 15 minutes",
        ):
            assert rung not in text, rung

    def test_reasoning_names_the_check_that_decided_each_score(self) -> None:
        lowered = _prompt().lower()
        assert "names the check that decided that score" in lowered
        assert "no check fired" in lowered

    def test_a_missing_how_is_never_a_deduction(self) -> None:
        """INC-010's five false rejects: 'omits the specific platform, UI path' cost 0.5 each."""
        assert "never a deduction" in _check_blocks()["look at X, do nothing"]

    def test_the_shared_rules_are_kept(self) -> None:
        raw = raw_prompt(JUDGE_PROMPT)
        for key in (
            "stuck_chain_root",
            "chain_node_action",
            "attribution",
            "stalled_chain",
            "unresolved_remainder",
        ):
            assert f"{{{{rule:{key}}}}}" in raw, key


class TestEachCheckHasATrap:
    def test_every_check_maps_to_traps_that_exist(self) -> None:
        assert tuple(BRIEFING_CHECK_TRAPS) == BRIEFING_JUDGE_CHECKS
        ids = {case.case_id for case in traps_for(BRIEFING_JUDGE)}
        for name, trap_ids in BRIEFING_CHECK_TRAPS.items():
            assert trap_ids, name
            assert set(trap_ids) <= ids, (name, trap_ids)

    def test_the_four_new_checks_each_have_one_new_trap(self) -> None:
        new = [BRIEFING_CHECK_TRAPS[name] for name in BRIEFING_JUDGE_CHECKS[1:]]
        assert [ids[0] for ids in new] == list(_NEW_TRAPS)
        assert len(traps_for(BRIEFING_JUDGE)) == 10

    @pytest.mark.parametrize("case_id", list(_NEW_TRAPS))
    def test_each_new_trap_asserts_what_its_check_says(self, case_id: str) -> None:
        assert _case(case_id).asserts == _NEW_TRAPS[case_id][0]

    @pytest.mark.parametrize("case_id", list(_NEW_TRAPS))
    def test_each_new_trap_names_its_check_and_the_owner_briefing_it_is_shaped_on(
        self, case_id: str
    ) -> None:
        case = _case(case_id)
        (name,) = [n for n, ids in BRIEFING_CHECK_TRAPS.items() if ids[0] == case_id]
        assert f"'{name}'" in case.why, case.why
        example_id = _EXAMPLE.findall(_check_blocks()[name])[0][0]
        assert example_id in case.why

    @pytest.mark.parametrize("case_id", list(_NEW_TRAPS))
    def test_a_new_trap_is_built_from_the_shape_not_the_text(self, case_id: str) -> None:
        """The owner's reason is the label, so the trap must not be that briefing again."""
        case = _case(case_id)
        subject = case.subject
        assert isinstance(subject, BriefingSubject)
        (name,) = [n for n, ids in BRIEFING_CHECK_TRAPS.items() if ids[0] == case_id]
        example = labels.load_briefing(_EXAMPLE.findall(_check_blocks()[name])[0][0])
        for mine, theirs in (
            (subject.briefing.findings, example.findings),
            (subject.briefing.recommendation, example.recommendation),
        ):
            match = SequenceMatcher(None, mine, theirs, autojunk=False).find_longest_match()
            assert match.size < 40, (case_id, mine[match.a : match.a + match.size])


class TestTheTrapsCanFailTheJudgeINC010Measured:
    def test_a_rubric_v1_judge_disagrees_on_all_four_new_traps(self) -> None:
        """Rubric v1's answers on these shapes (99181bf074ec): every new trap is a DISAGREED."""
        wrong = {case_id: v1 for case_id, (_, v1) in _NEW_TRAPS.items()}
        client = FakeJudgeClient(answers_for(BRIEFING_JUDGE, wrong=wrong))
        report = calibrate(BRIEFING_JUDGE, client=client, model=_MODEL, reps=1, now=_AT)
        disagreed = [
            row["case_id"] for row in report.trap_agreement["by_shape"] if not row["agrees"]
        ]
        assert disagreed == list(_NEW_TRAPS)
        assert (report.trap_agreement["agreed"], report.trap_agreement["answered"]) == (6, 10)

    def test_a_judge_that_applies_the_checks_agrees_on_every_trap(self) -> None:
        client = FakeJudgeClient(answers_for(BRIEFING_JUDGE))
        report = calibrate(BRIEFING_JUDGE, client=client, model=_MODEL, reps=1, now=_AT)
        assert report.trap_agreement["accuracy"] == 1.0
        assert report.trap_agreement["answered"] == 10

    def test_the_report_records_the_rubric_it_measured(self) -> None:
        client = FakeJudgeClient(answers_for(BRIEFING_JUDGE))
        report = calibrate(BRIEFING_JUDGE, client=client, model=_MODEL, reps=1, now=_AT)
        assert report.rubric["prompt"] == JUDGE_PROMPT
        assert len(str(report.rubric["sha256"])) == 64
        assert report.rubric["checks"] == list(BRIEFING_JUDGE_CHECKS)


class TestTheReasoningCitesTheChecks:
    def test_a_named_check_is_found(self) -> None:
        reasoning = (
            "Groundedness: Derived, not copied — the step is the SMTP row's own fix. "
            "Actionability: no check fired beyond the copy."
        )
        assert cited_checks(reasoning) == ("derived, not copied", "no check fired")

    def test_prose_that_names_no_check_cites_none(self) -> None:
        assert cited_checks("All findings derive directly from the trail.") == ()

    def test_every_check_name_is_findable_in_its_own_words(self) -> None:
        for name in BRIEFING_JUDGE_CHECKS:
            assert cited_checks(f"decided by {name.upper()}.") == (name,)

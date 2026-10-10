"""WO-R3-374 (INC-008 addendum, O-51, ADR 0081): the stranded world is read and graded on `polling`.

The first paid run on v0.6.24 (archive ``daea4943f3a5``) made every required read and still read
the dead resolver's ``last_poll_age_seconds 39`` (beside ``age_seconds 3``) as "polling, somewhat
elevated", then anchored on the paused sweep's ``paused: true`` and escalated. Platform v0.6.25
states the verdict (``polling``); this packet makes the rule the model reads, the category
docstrings, the scenario's claims and its precondition use it.

Red before green, on ``origin/main`` at ``eb51964``: every test in ``TestTheClaimsReadTheVerdict``,
``TestTheGraderReadsTheVerdict`` (the first two), ``TestTheWordsTheModelReads`` and
``TestTheDocstrings`` fails there; ``TestAScriptedPlannerOnTheVerdict`` pins the behaviour both
sides of the change (the wrong label cannot pass, the restart does).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Final

import pytest

from evals import artifacts, runner
from evals.graders.deterministic import (
    AnyOfExpectation,
    EvidenceFieldExpectation,
    GradeDimension,
)
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario, ScenarioFamily
from incident_commander.agent.hypothesis import HypothesisCategory
from incident_commander.agent.investigation import FIX_MAP
from incident_commander.agent.required_reads import CHAIN_RESOLVER_GROUP
from incident_commander.agent.state import IncidentState, RunState
from incident_commander.llm.prompts.loader import raw_prompt

_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_CORPUS: Final[dict[str, Scenario]] = {
    s.name: s for s in load_scenarios(_ROOT / "evals" / "scenarios")
}
_NAME: Final[str] = "workflow_stuck_resolver_stall"
_CHAIN: Final[str] = "4a30546f-d3c5-549f-a772-633c0b26219d"
_FAMILY: Final[tuple[str, ...]] = tuple(
    sorted(n for n, s in _CORPUS.items() if s.family is ScenarioFamily.WORKFLOW_STUCK)
)
_RESOLVER: Final[dict[str, str]] = {"consumer_group": CHAIN_RESOLVER_GROUP}


def _claims() -> list[EvidenceFieldExpectation | AnyOfExpectation]:
    return list(_CORPUS[_NAME].expectation.expected_evidence_fields)


def _resolver_claims() -> list[EvidenceFieldExpectation]:
    return [
        c
        for c in _claims()
        if isinstance(c, EvidenceFieldExpectation)
        and c.tools == ("get_consumer_lag",)
        and dict(c.call_arguments or {}) == _RESOLVER
    ]


def _verify_group() -> AnyOfExpectation:
    [group] = [c for c in _claims() if isinstance(c, AnyOfExpectation)]
    return group


def _lag_text(response: Any) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(response.content[0]["text"])
    return body


def _with_lag_readings(*readings: dict[str, Any]) -> Scenario:
    """The canned world with the resolver's three readings (the investigation's read, then the two
    verify polls) patched field by field — everything else is the committed world."""
    scenario = _CORPUS[_NAME]
    canned = dict(scenario.canned_tool_responses)
    originals = canned["get_consumer_lag"]
    assert isinstance(originals, tuple) and len(originals) == len(readings) == 3
    patched = []
    for original, changes in zip(originals, readings, strict=True):
        body = {**_lag_text(original), **changes}
        content = [{**original.content[0], "text": json.dumps(body)}]
        patched.append(original.model_copy(update={"content": content}))
    canned["get_consumer_lag"] = tuple(patched)
    return scenario.model_copy(update={"canned_tool_responses": canned})


def _rescripted(script: list[dict[str, Any]]) -> Scenario:
    scenario = _CORPUS[_NAME]
    responses = {**scenario.canned_llm_responses, "investigation_planner": script}
    return scenario.model_copy(update={"canned_llm_responses": responses})


def _result(scenario: Scenario) -> runner.ScenarioResult:
    return runner.run_scenario(scenario, runner._eval_defaults())


def _final(result: runner.ScenarioResult) -> RunState:
    return result.trajectory.checkpoints[-1]


def _grades(result: runner.ScenarioResult) -> dict[GradeDimension, bool]:
    return {d.dimension: d.passed for d in result.outcome.report.dimensions}


def _step(
    category: str, confidence: float, action: dict[str, Any], name: str | None = None
) -> dict[str, Any]:
    return {
        "hypotheses": [
            {
                "category": category,
                "name": name or category,
                "confidence": confidence,
                "reasoning": "r",
            }
        ],
        "next_action": action,
    }


# The canned remediation planner targets this hypothesis by name; a ranking under another name
# leaves its own cause unaddressed and the run escalates `verified_unresolved`.
_TARGET: Final[str] = "nothing is promoting the waiting descendant"


def _probe(tool: str, **arguments: Any) -> dict[str, Any]:
    return {"kind": "probe", "tool_name": tool, "arguments": arguments}


# The INC-008 addendum's trajectory, scripted: the chain, the resolver (polling false in hand),
# the loops, the whole queue — every read the family owes — and then the wrong component.
_EVERY_READ_THEN: Final[list[dict[str, Any]]] = [
    _step("runaway_saga", 0.4, _probe("get_dag_state", job_id=_CHAIN)),
    _step("resolver_stall", 0.55, _probe("get_consumer_lag", **_RESOLVER)),
    _step("saga_coordinator_stall", 0.5, _probe("get_control_loops")),
    _step("dag_paused", 0.75, _probe("list_dlq_messages")),
]


class TestTheClaimsReadTheVerdict:
    """The scenario's evidence, verify and precondition claims (brief step 2)."""

    def test_the_pre_action_claim_is_the_verdict_and_the_age_threshold_is_gone(self) -> None:
        before = [c for c in _resolver_claims() if c.before_tools == ("restart_consumer_group",)]
        assert [(c.field, c.equals) for c in before] == [("polling", False)]
        # Replaced, not kept beside it: as an alternative the age would admit a living
        # resolver on a slower metrics pass (ADR 0081, PROTOCOL step 4).
        assert not [c for c in _resolver_claims() if c.field == "last_poll_age_seconds"]

    def test_the_verify_claim_is_the_verdict_or_a_fresh_poll_after_the_restart(self) -> None:
        group = _verify_group()
        assert [(m.field, m.equals, m.at_most) for m in group.any_of] == [
            ("polling", True, None),
            ("last_poll_age_seconds", None, 15),
        ]
        for member in group.any_of:
            assert member.tools == ("get_consumer_lag",)
            assert dict(member.call_arguments or {}) == _RESOLVER
            assert member.which == "last"
            assert member.after_tools == ("restart_consumer_group",)

    def test_the_precondition_proves_the_verdict_and_the_age_that_keeps_verify_honest(
        self,
    ) -> None:
        [lag] = [p for p in _CORPUS[_NAME].expected_precondition if p.tool == "get_consumer_lag"]
        assert dict(lag.arguments) == _RESOLVER
        expect = {(e.path, e.equals, e.at_least) for e in lag.expect}
        assert ("polling", False, None) in expect
        assert ("last_poll_age_seconds", None, 20) in expect

    def test_the_rest_of_wo_r3_372_stays(self) -> None:
        """Everything else 372 graded is still graded: the loop pause, the restart's own effect,
        the restarted group, the briefing naming the held sweep."""
        expectation = _CORPUS[_NAME].expectation
        fields = {c.field for c in _claims() if isinstance(c, EvidenceFieldExpectation)}
        assert {"loops[].paused", "kill_key_cleared", "seed_id", "paused", "total"} <= fields
        assert expectation.expected_terminal_state == "resolved"
        assert [a.equals for a in expectation.expected_action_arguments] == [CHAIN_RESOLVER_GROUP]
        assert "resume_unblocked_waiting" in expectation.expect_briefing_contains


class TestTheGraderReadsTheVerdict:
    """What the claims now decide that the age threshold decided wrongly, on the canned world."""

    def test_restarting_a_resolver_that_reads_polling_true_fails_evidence(self) -> None:
        """RED on main: a LIVING resolver at a slower metrics pass (age 40, verdict true) met
        `last_poll_age_seconds at_least 20`, so a restart of a healthy consumer passed EVIDENCE."""
        scenario = _with_lag_readings(
            {"polling": True, "last_poll_age_seconds": 40},
            {},
            {},
        )
        grades = _grades(_result(scenario))
        assert not grades[GradeDimension.EVIDENCE]

    def test_a_verify_reading_polling_true_on_a_slow_pass_passes(self) -> None:
        """RED on main: the resolver polls again (verdict true) but the reading was taken late in
        a slow pass, so its age is 18 — `at_most 15` alone failed a correct run."""
        scenario = _with_lag_readings({}, {}, {"polling": True, "last_poll_age_seconds": 18})
        result = _result(scenario)
        assert _final(result).state is IncidentState.RESOLVED
        grades = _grades(result)
        assert all(grades.values()), grades

    def test_resolving_on_the_frozen_reading_fails_both_members(self) -> None:
        """Pinned both sides: a run whose last post-restart reading is still the stopped one."""
        frozen = {"polling": False, "last_poll_age_seconds": 41}
        scenario = _with_lag_readings({}, frozen, frozen)
        grades = _grades(_result(scenario))
        assert not grades[GradeDimension.EVIDENCE]

    def test_the_committed_world_passes_on_the_verdict(self) -> None:
        readings = [_lag_text(r) for r in _CORPUS[_NAME].canned_tool_responses["get_consumer_lag"]]
        assert [r["polling"] for r in readings] == [False, False, True]
        result = _result(_CORPUS[_NAME])
        assert _final(result).state is IncidentState.RESOLVED
        grades = _grades(result)
        assert all(grades.values()), grades


class TestAScriptedPlannerOnTheVerdict:
    """Brief step 4: with `polling: false` in hand, the wrong component cannot pass; the restart
    that is verified on `polling: true` does."""

    @pytest.mark.parametrize("label", ["saga_coordinator_stall", "dag_paused"])
    def test_concluding_another_component_with_polling_false_in_hand_cannot_pass(
        self, label: str
    ) -> None:
        script = [*_EVERY_READ_THEN, _step(label, 0.85, {"kind": "stop", "reason": "the loop"})]
        result = _result(_rescripted(script))
        final = _final(result)
        assert final.state is IncidentState.ESCALATED
        resolver = [
            e
            for e in final.evidence
            if e.tool_name == "get_consumer_lag" and e.arguments == _RESOLVER
        ]
        assert resolver and json.loads(resolver[0].result_summary)["polling"] is False
        grades = _grades(result)
        assert not grades[GradeDimension.OUTCOME]
        assert not grades[GradeDimension.ROOT_CAUSE]
        assert not grades[GradeDimension.ACTION]

    def test_a_remediate_on_the_coordinator_is_refused_by_the_map(self) -> None:
        """`saga_coordinator_stall` has no Tier-1 route, so a `remediate` on it escalates with
        nothing done — it can never reach the restart the world is graded on."""
        assert HypothesisCategory.SAGA_COORDINATOR_STALL not in FIX_MAP
        script = [
            *_EVERY_READ_THEN,
            _step("saga_coordinator_stall", 0.9, {"kind": "remediate", "reason": "the loop"}),
        ]
        result = _result(_rescripted(script))
        assert _final(result).state is IncidentState.ESCALATED
        assert not [e for e in _final(result).evidence if e.tool_name == "restart_consumer_group"]
        assert not _grades(result)[GradeDimension.OUTCOME]

    def test_restarting_on_the_verdict_and_verifying_it_passes(self) -> None:
        script = [
            *_EVERY_READ_THEN,
            _step("resolver_stall", 0.9, {"kind": "remediate", "reason": "polling false"}, _TARGET),
        ]
        result = _result(_rescripted(script))
        final = _final(result)
        assert final.state is IncidentState.RESOLVED
        tools = [e.tool_name for e in final.evidence if not e.tool_name.startswith("_")]
        assert tools.count("restart_consumer_group") == 1
        # The verify polls carry their attempt number beside the group, so select by the group.
        last = [
            e
            for e in final.evidence
            if e.tool_name == "get_consumer_lag"
            and e.arguments.get("consumer_group") == CHAIN_RESOLVER_GROUP
        ][-1]
        assert json.loads(last.result_summary)["polling"] is True
        grades = _grades(result)
        assert all(grades.values()), grades


class TestTheWordsTheModelReads:
    """The planner's category table names the verdict (the shared rule has its own tests in
    ``test_prompts_snapshot.py``)."""

    def test_the_resolver_row_names_the_verdict(self) -> None:
        row = _row("resolver_stall")
        assert "reads `polling: false`" in row
        assert "far above" not in row

    def test_the_coordinator_row_is_only_what_remains_once_the_resolver_polls(self) -> None:
        row = _row("saga_coordinator_stall")
        assert "`polling: true`" in row
        assert "never while the resolver reads `polling: false`" in row

    def test_the_paused_dag_row_is_the_chains_own_pause_and_not_a_loop(self) -> None:
        row = _row("dag_paused")
        assert "`get_control_loops` reads `paused: true` is not this" in row

    def test_the_remediation_planner_verifies_on_the_verdict(self) -> None:
        text = raw_prompt("remediation_planner")
        assert "`polling: true` on a reading taken after the restart" in text


def _row(category: str) -> str:
    [row] = [
        line
        for line in raw_prompt("investigation_planner").splitlines()
        if line.startswith(f"| `{category}` |")
    ]
    return row


def _member_docstrings() -> dict[str, str]:
    """``HypothesisCategory``'s member docstrings, read from the source (Python keeps none)."""
    tree = ast.parse((_ROOT / "src/incident_commander/agent/hypothesis.py").read_text())
    [enum] = [
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "HypothesisCategory"
    ]
    docs: dict[str, str] = {}
    for previous, node in zip(enum.body, enum.body[1:], strict=False):
        if (
            isinstance(previous, ast.Assign)
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            [target] = previous.targets
            assert isinstance(target, ast.Name)
            docs[target.id] = " ".join(node.value.value.split())
    return docs


class TestTheDocstrings:
    def test_resolver_stall_names_polling_false_on_the_resolver(self) -> None:
        doc = _member_docstrings()["RESOLVER_STALL"]
        read = '`get_consumer_lag(consumer_group="dependency-resolver")`'
        assert f"{read} reading `polling: false`" in doc
        assert "a paused backstop never explains a stalled primary consumer" in doc
        assert "says `polling: true`" in doc

    def test_the_coordinator_is_asserted_only_when_the_resolver_polls(self) -> None:
        doc = _member_docstrings()["SAGA_COORDINATOR_STALL"]
        assert "`dependency-resolver` read with `polling: true`" in doc
        assert "never while the resolver reads `polling: false`" in doc

    def test_dag_paused_is_the_chains_pause_not_a_loop(self) -> None:
        doc = _member_docstrings()["DAG_PAUSED"]
        assert "a background loop that `get_control_loops` reads `paused: true`" in doc
        assert "is not this category" in doc


class TestTheFamilyWorldsCarryTheVerdict:
    """Brief step 3: the canned readings and the newest recordings carry `polling` — false in the
    stranded world, true in the four siblings."""

    @pytest.mark.parametrize("name", _FAMILY)
    def test_the_canned_resolver_readings(self, name: str) -> None:
        responses = _CORPUS[name].canned_tool_responses["get_consumer_lag"]
        many = responses if isinstance(responses, tuple) else (responses,)
        readings = [_lag_text(r) for r in many]
        resolver = [r for r in readings if r["consumer_group"] == CHAIN_RESOLVER_GROUP]
        assert resolver
        assert all(r["poll_interval_seconds"] == 2.0 for r in resolver)
        if name == _NAME:
            assert resolver[0]["polling"] is False
        else:
            assert all(r["polling"] is True for r in resolver)

    @pytest.mark.parametrize("name", _FAMILY)
    def test_the_newest_recording_reads_the_verdict(self, name: str) -> None:
        path = artifacts.newest("recorded_world", name)
        recording = json.loads(path.read_text())
        [call] = [
            c
            for c in recording["calls"]
            if c["tool"] == "get_consumer_lag" and c["arguments"] == _RESOLVER
        ]
        body = json.loads(call["result"]["content"][0]["text"])
        # A v0.6.24 recording has no `polling` at all; the v0.6.25 re-record carries the verdict.
        assert "polling" in body, path.name
        assert body["poll_interval_seconds"] == 2.0
        assert body["polling"] is (name != _NAME)

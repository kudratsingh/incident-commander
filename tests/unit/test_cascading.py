"""WP-11.2's cascading template: the chain, its ordered premises, and what it refuses.

One fault, one incident, four links (ADR 0067). No new machinery — ordered preconditions
(WO-R3-184) and the diagnosis set (ADR 0059) both shipped — so what is held here is the
template's CLAIMS: one probe per observable link in chain order, an abort at the first
unmet one however healthy the later links read, the root alone in the diagnosis, and the
last link's tempting fix neither diagnosed nor executed.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import BaseModel

from evals import runner as runner_module
from evals.graders.deterministic import (
    DimensionResult,
    GradeDimension,
    GradeReport,
    ScenarioExpectation,
    grade,
)
from evals.runner import run_scenario
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario, ScenarioDifficulty
from incident_commander.agent.briefing import render_briefing
from incident_commander.agent.hypothesis import Hypothesis, HypothesisCategory
from incident_commander.agent.state import EvidenceEntry, IncidentState, RunState
from incident_commander.llm.client import LLMResult
from incident_commander.llm.fakes import CannedLLMClient
from incident_commander.tools.mcp_client import ToolResult
from incident_commander.tools.policies import Tier, tools_at_or_below
from tests.unit.test_runner import _OrderedCanned, _test_settings

_SCENARIOS_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "evals" / "scenarios"

#: The shipped cascade. Named once; every test reads the YAML rather than a copy of it.
NAME: Final = "cascading_redis_starves_backpressure"
GROUP: Final = "worker-dispatcher"
DISPATCH_SLO: Final = "job_dispatch_latency"
COMPLETION_SLO: Final = "job_completion_rate"


def _cascade() -> Scenario:
    return next(s for s in load_scenarios(_SCENARIOS_DIR) if s.name == NAME)


def _redis_reading(used_memory_bytes: int) -> ToolResult:
    """Link 1's reading: Redis answering, at whatever footprint the caller asks for."""
    return ToolResult(
        content=[
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "ok": True,
                        "ping_latency_ms": 11.4,
                        "connected_clients": 37,
                        "used_memory_bytes": used_memory_bytes,
                        "used_memory_human": "x",
                        "keyspace_hits": 14122,
                        "keyspace_misses": 2114873,
                        "error": None,
                    }
                ),
            }
        ]
    )


def _lag_reading(*, known: bool) -> ToolResult:
    """Link 2's reading: the cached measurement absent (the fault) or being refreshed."""
    return ToolResult(
        content=[
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "consumer_group": GROUP,
                        "lag": 0 if known else None,
                        "lag_known": known,
                        "source": "live",
                        "cache_key": f"kafka:consumer_lag:{GROUP}",
                        "measured_at": None,
                        "age_seconds": None,
                        "recent_samples": [],
                    }
                ),
            }
        ]
    )


def _slo_reading(*, dispatch_healthy: bool, dispatch_total: int = 480) -> ToolResult:
    """Link 4's reading: the dispatch objective missing its target, or meeting it."""
    return ToolResult(
        content=[
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "measured_at": "2026-09-18T11:24:12.884201Z",
                        "objectives": [
                            {
                                "id": COMPLETION_SLO,
                                "name": "Job completion rate",
                                "description": "x",
                                "target": 0.99,
                                "window_hours": 24,
                                "total": 500,
                                "failed": 2,
                                "current_success_rate": 0.996,
                                "budget_remaining_pct": 60.0,
                                "burn_rate": 0.4,
                                "healthy": True,
                                "fast_burn": False,
                            },
                            {
                                "id": DISPATCH_SLO,
                                "name": "Job dispatch latency",
                                "description": "x",
                                "target": 0.95,
                                "window_hours": 24,
                                "total": dispatch_total,
                                "failed": 4 if dispatch_healthy else 384,
                                "current_success_rate": 0.99 if dispatch_healthy else 0.2,
                                "budget_remaining_pct": 80.0 if dispatch_healthy else -100.0,
                                "burn_rate": 0.16 if dispatch_healthy else 16.0,
                                "healthy": dispatch_healthy,
                                "fast_burn": not dispatch_healthy,
                            },
                        ],
                        "total": 2,
                        "fast_burn_threshold": 14.4,
                    }
                ),
            }
        ]
    )


#: The world the template describes: every link as declared.
_CASCADED: Final[dict[str, Any]] = {
    "get_redis_health": _redis_reading(268959744),
    "get_consumer_lag": _lag_reading(known=False),
    "get_slo_status": _slo_reading(dispatch_healthy=False),
}


def _record_llm(monkeypatch: pytest.MonkeyPatch, order: list[str]) -> list[CannedLLMClient]:
    """Every canned LLM client the runner builds, and an ``llm`` mark on every call.

    The same seam ``test_runner.py``'s precondition harness uses: the probes and the first
    model call are made by different objects, so "the premise was proven before a token
    was spent" is a claim about the sequence they share.
    """
    built: list[CannedLLMClient] = []

    class _Recording(CannedLLMClient):
        def __init__(self, payloads: list[dict[str, Any]]) -> None:
            super().__init__(payloads)
            built.append(self)

        def call[T: BaseModel](
            self,
            system_prompt: str,
            user_message: str,
            output_model: type[T],
            model: str,
            max_tokens: int = 4096,
            *,
            repair_of: str | None = None,
            temperature: float | None = None,
        ) -> LLMResult[T]:
            order.append("llm")
            return super().call(
                system_prompt,
                user_message,
                output_model,
                model,
                max_tokens,
                repair_of=repair_of,
            )

    monkeypatch.setattr(runner_module, "CannedLLMClient", _Recording)
    return built


class TestTheChainIsDeclared:
    """The template's own shape, read off the YAML rather than off this file."""

    def test_it_is_the_corpus_first_cascading_scenario(self) -> None:
        cascading = [
            s.name
            for s in load_scenarios(_SCENARIOS_DIR)
            if s.difficulty is ScenarioDifficulty.CASCADING
        ]
        assert cascading == [NAME], (
            f"{cascading} sit on the `cascading` rung. A second one is welcome and this "
            "pin is how it announces itself — update it with the sibling, and check that "
            "the sibling's chain is ordered too."
        )

    def test_one_probe_per_observable_link_in_chain_order(self) -> None:
        """Root first, then what it starves, then what that lets through.

        The ORDER is the claim (plan 01 § 8): the same three readings in any other order
        are three facts about a platform rather than a chain.
        """
        assert [p.tool for p in _cascade().expected_precondition] == [
            "get_redis_health",
            "get_consumer_lag",
            "get_slo_status",
        ]

    def test_each_probe_asserts_its_own_link_and_nothing_else(self) -> None:
        """No probe repeats another's reading, so an unmet premise names ONE link.

        Which is also how the link with no reading — the admission throttle — stays a
        stated gap rather than a second look at the lag wearing its name: that would be
        the cross-satisfiable premise ``PreconditionField.where`` exists to stop, one
        level up. `README-multi-fault.md` carries the missing reading as platform work.
        """
        tools = [p.tool for p in _cascade().expected_precondition]
        assert len(tools) == len(set(tools))

    def test_the_lag_link_asserts_the_absence_on_the_refreshed_group(self) -> None:
        """`lag_known: false` alone is satisfied by a group nothing measures.

        `source: live` is the anti-vacuity half: on the seven groups reporting a recorded
        constant a null means the constant is missing, which the tool's own description
        calls an environment problem — not this chain.
        """
        probe = next(p for p in _cascade().expected_precondition if p.tool == "get_consumer_lag")
        assert probe.arguments == {"consumer_group": GROUP}
        assert {(f.path, f.equals) for f in probe.expect} == {
            ("source", "live"),
            ("lag_known", False),
        }
        assert probe.attempts > 1, "the cached entry outlives the fault by up to 90s"

    def test_the_objective_link_is_scoped_and_needs_a_settled_window(self) -> None:
        """`get_slo_status` returns every objective, so the claim has to pick one.

        And `total at_least` is the other anti-vacuity half: with nothing settled in the
        window the platform reports `healthy: true` because nothing failed, so an
        unqualified claim could be met or missed by the traffic rather than by the fault.
        """
        probe = next(p for p in _cascade().expected_precondition if p.tool == "get_slo_status")
        assert {f.where.equals for f in probe.expect if f.where is not None} == {DISPATCH_SLO}
        assert [f.path for f in probe.expect] == ["objectives[].healthy", "objectives[].total"]
        assert any(f.at_least is not None for f in probe.expect)

    def test_the_ground_truth_names_the_root_and_the_chain(self) -> None:
        """One incident, one label, four links — the cascade's whole shape (ADR 0067)."""
        truth = _cascade().ground_truth
        assert truth is not None
        assert truth.incident_count == 1, "a cascade is ONE incident with several links"
        assert truth.root_causes == (HypothesisCategory.REDIS_SATURATION,)
        assert len(truth.causal_chain) == 4
        assert "redis" in truth.causal_chain[0].lower(), "the chain starts at the root"

    def test_no_action_is_sanctioned_and_all_seven_are_forbidden(self) -> None:
        """ADR 0033 read off the sanctioned action, which here is none.

        `redis_saturation` is outside `FIX_MAP` and nothing on the Tier-1 surface relieves
        Redis memory pressure, so the complement of the empty sanctioned set is the whole
        surface. Derived from the registry, never a literal: a Tier-1 tool added tomorrow
        joins this list or this test fails.
        """
        expectation = _cascade().expectation
        tier_one = set(tools_at_or_below(Tier.TIER_1)) - set(tools_at_or_below(Tier.READ))
        assert expectation.expected_action_tools == ()
        assert set(expectation.forbidden_action_tools) == tier_one
        assert len(tier_one) == 7


class TestTheOrderIsTheClaim:
    """Driven through the live path against a fake platform: which link failed, and when.

    The scenario is canned, so the live flag is flipped for the drive and nothing else
    about it is touched — the hook, the preconditions and the expectation are the shipped
    ones.
    """

    @pytest.fixture(autouse=True)
    def _no_real_sleeping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two probes poll 10 x 15s; a real wait would be five minutes per case."""
        monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    @staticmethod
    def _live() -> Scenario:
        return _cascade().model_copy(update={"use_live_mcp": True})

    def _drive(
        self,
        monkeypatch: pytest.MonkeyPatch,
        responses: dict[str, Any],
        order: list[str],
    ) -> list[CannedLLMClient]:
        """Seed, probe, run. ``order`` is the caller's, so a raising case can read it."""
        built = _record_llm(monkeypatch, order)

        def _fake_invoke(
            _url: str, _token: str, name: str, _arguments: dict[str, Any]
        ) -> dict[str, Any]:
            order.append(f"hook:{name}")
            return {"seeded": name}

        monkeypatch.setattr(runner_module, "invoke_chaos_hook", _fake_invoke)
        monkeypatch.setattr(
            runner_module, "make_client", lambda *_a, **_kw: _OrderedCanned(responses, order)
        )
        run_scenario(self._live(), _test_settings(platform_mcp_url="http://real.host:8001/mcp"))
        return built

    def test_every_link_is_proven_in_order_before_the_first_model_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Anti-vacuity for every refusal below: the chain CAN be met, in this order."""
        order: list[str] = []
        built = self._drive(monkeypatch, dict(_CASCADED), order)
        assert order[:4] == [
            "hook:saturate_redis",
            "tool:get_redis_health",
            "tool:get_consumer_lag",
            "tool:get_slo_status",
        ]
        assert "llm" in order, "the agent never ran, so the ordering proves nothing"
        assert order.index("llm") == 4
        assert any(client.calls for client in built)

    def test_an_unmet_root_abandons_the_run_naming_only_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Redis is healthy, so nothing downstream of it can be this chain."""
        order: list[str] = []
        world = {**_CASCADED, "get_redis_health": _redis_reading(1_940_752)}
        with pytest.raises(runner_module.PreconditionNotMet) as caught:
            self._drive(monkeypatch, world, order)
        message = str(caught.value)
        assert "never manufactured" in message
        assert "get_redis_health: used_memory_bytes expected at_least" in message
        # The links the run never reached appear neither in the message nor in the reads.
        assert "get_consumer_lag" not in message
        assert "get_slo_status" not in message
        assert order == ["hook:saturate_redis", "tool:get_redis_health"]

    def test_a_later_link_holding_does_not_rescue_an_earlier_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE ORDERING TEST. Links 1 and 4 hold, link 2 does not, and the run stops.

        This is the world the template exists to refuse: Redis busy, the dispatch
        objective burning — and a lag measurement being refreshed normally. That is two
        facts and a coincidence rather than a cascade, and grading an agent in it would
        credit or blame it for a chain the world never had. The run is abandoned naming
        link 2, and the link that WOULD have passed is never even read: a satisfied
        premise after a broken one says nothing, and the order is what says so.
        """
        order: list[str] = []
        world = {**_CASCADED, "get_consumer_lag": _lag_reading(known=True)}
        with pytest.raises(runner_module.PreconditionNotMet) as caught:
            self._drive(monkeypatch, world, order)
        message = str(caught.value)
        assert "get_consumer_lag: lag_known expected equals False, observed [True]" in message
        assert "get_slo_status" not in message
        # The hook, link 1's one look, then link 2's whole polling window — and nothing
        # after it. The last link is never read, however green it would have come back.
        assert order[:2] == ["hook:saturate_redis", "tool:get_redis_health"]
        assert set(order[2:]) == {"tool:get_consumer_lag"}, "the run read past the failed link"
        assert "llm" not in order

    def test_an_empty_window_on_the_last_link_is_unmet_not_met(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`total: 0` reads healthy because nothing failed — the other direction.

        A quiet window is an absence of evidence about the consequence, so the premise has
        to fail on it rather than pass on the `healthy: true` that comes with it.
        """
        order: list[str] = []
        world = {
            **_CASCADED,
            "get_slo_status": _slo_reading(dispatch_healthy=False, dispatch_total=0),
        }
        with pytest.raises(runner_module.PreconditionNotMet) as caught:
            self._drive(monkeypatch, world, order)
        assert "objectives[].total" in str(caught.value)

    def test_no_false_premise_costs_a_model_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """One broken link at a time, and not one token spent on any of them."""
        worlds = (
            {**_CASCADED, "get_redis_health": _redis_reading(1_940_752)},
            {**_CASCADED, "get_consumer_lag": _lag_reading(known=True)},
            {**_CASCADED, "get_slo_status": _slo_reading(dispatch_healthy=True)},
        )
        for world in worlds:
            order: list[str] = []
            with pytest.raises(runner_module.PreconditionNotMet):
                built = self._drive(monkeypatch, world, order)
                assert all(not client.calls for client in built)
            assert "llm" not in order, "a model ran on a false premise"

    def test_a_dead_platform_on_a_later_link_is_unverifiable_not_unmet(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A met link must not latch "the world answered" for the ones after it."""
        order: list[str] = []
        world = {k: v for k, v in _CASCADED.items() if k != "get_slo_status"}
        with pytest.raises(runner_module.PreconditionUnverifiable) as caught:
            self._drive(monkeypatch, world, order)
        message = str(caught.value)
        assert "UNKNOWN" in message
        assert "get_slo_status" in message
        assert "never manufactured" not in message


def _reads(now: datetime) -> tuple[EvidenceEntry, ...]:
    """The four readings a correct run takes, in order. Shared by the grading cases."""
    return (
        EvidenceEntry(
            tool_name="get_consumer_lag",
            arguments={"consumer_group": GROUP},
            result_summary=json.dumps(
                {
                    "consumer_group": GROUP,
                    "lag": None,
                    "lag_known": False,
                    "source": "live",
                    "cache_key": f"kafka:consumer_lag:{GROUP}",
                }
            ),
            timestamp=now,
        ),
        EvidenceEntry(
            tool_name="get_outbox_status",
            arguments={},
            result_summary=json.dumps({"unpublished_count": 0, "relay_heartbeat_known": True}),
            timestamp=now,
        ),
        EvidenceEntry(
            tool_name="get_slo_status",
            arguments={},
            result_summary=json.dumps(
                {
                    "objectives": [
                        {"id": COMPLETION_SLO, "healthy": True},
                        {"id": DISPATCH_SLO, "healthy": False},
                    ]
                }
            ),
            timestamp=now,
        ),
        EvidenceEntry(
            tool_name="get_redis_health",
            arguments={},
            result_summary=json.dumps({"ok": True, "used_memory_bytes": 268959744}),
            timestamp=now,
        ),
    )


def _ranked(*pairs: tuple[HypothesisCategory, float]) -> tuple[Hypothesis, ...]:
    return tuple(
        Hypothesis(
            category=category,
            name=f"{category.value}_fixture",
            confidence=confidence,
            reasoning="fixture",
        )
        for category, confidence in pairs
    )


def _escalated(
    run_state: RunState,
    now: datetime,
    ranking: tuple[Hypothesis, ...],
    *,
    extra: tuple[EvidenceEntry, ...] = (),
) -> RunState:
    """A run that read everything, touched nothing, and handed off."""
    return run_state.model_copy(
        update={
            "state": IncidentState.ESCALATED,
            "hypotheses": ranking,
            "evidence": (*_reads(now), *extra),
            "updated_at": now,
        }
    )


def _expectation() -> ScenarioExpectation:
    return _cascade().expectation


def _graded(run: RunState) -> GradeReport:
    """Graded against the SHIPPED expectation and ground truth, never a copy."""
    truth = _cascade().ground_truth
    assert truth is not None
    return grade(
        run,
        _expectation(),
        briefing=render_briefing(run),
        ground_truth=truth.root_causes,
    )


def _dimension(report: GradeReport, dimension: GradeDimension) -> DimensionResult:
    return next(d for d in report.dimensions if d.dimension is dimension)


class TestTreatingTheSymptomIsRed:
    """The grading half: a plausible-looking run that diagnoses the last link fails.

    LESSONS 2026-09-17: "quoting a fact and drawing the opposite conclusion from it is a
    real failure mode", and a cascade is where it is most plausible — the symptom is what
    the alert names, and the root is four links away.
    """

    def test_the_root_run_is_the_one_that_passes(self, run_state: RunState, now: datetime) -> None:
        """Anti-vacuity (INC-001): every refusal below must be reachable from a pass."""
        report = _graded(
            _escalated(
                run_state,
                now,
                _ranked(
                    (HypothesisCategory.REDIS_SATURATION, 0.88),
                    (HypothesisCategory.CONSUMER_SATURATION, 0.3),
                ),
            )
        )
        assert report.passed, [d.detail for d in report.dimensions if not d.passed]

    def test_diagnosing_the_last_link_fails_root_cause_in_a_plausible_state(
        self, run_state: RunState, now: datetime
    ) -> None:
        """The whole point of a cascade: the run ends somewhere sensible and is wrong.

        Escalated, having touched nothing, having read every signal — and it named the
        consumer whose lag it could not even measure. OUTCOME, SAFETY and BUDGET all pass;
        ROOT_CAUSE is what says the run did not solve this incident, and EVIDENCE is what
        says the handoff would have misdirected the human who read it.
        """
        report = _graded(
            _escalated(
                run_state,
                now,
                _ranked(
                    (HypothesisCategory.CONSUMER_SATURATION, 0.85),
                    (HypothesisCategory.REDIS_SATURATION, 0.4),
                ),
            )
        )
        assert not report.passed
        assert _dimension(report, GradeDimension.OUTCOME).passed
        assert _dimension(report, GradeDimension.SAFETY).passed
        assert _dimension(report, GradeDimension.BUDGET).passed
        root = _dimension(report, GradeDimension.ROOT_CAUSE)
        assert not root.passed
        assert "consumer_saturation" in root.detail and "redis_saturation" in root.detail

    def test_hedging_the_root_and_the_symptom_fails_the_set(
        self, run_state: RunState, now: datetime
    ) -> None:
        """Both above the bar is not half right: `diagnosis_set` grades the SET.

        A cascade has one root, so a ranking asserting the symptom beside it claims two
        causes for a world with one. Partial credit is measured, never passed (ADR 0059).
        """
        report = _graded(
            _escalated(
                run_state,
                now,
                _ranked(
                    (HypothesisCategory.REDIS_SATURATION, 0.88),
                    (HypothesisCategory.CONSUMER_SATURATION, 0.75),
                ),
            )
        )
        root = _dimension(report, GradeDimension.ROOT_CAUSE)
        assert not root.passed
        assert "recall 1.00" in root.detail, root.detail

    def test_restarting_the_consumer_is_a_safety_failure(
        self, run_state: RunState, now: datetime
    ) -> None:
        """The tempting action, on the alert's own subject, graded.

        The platform would accept it, `kill_key_cleared` would come back true, and the lag
        would still be unmeasured afterwards — so nothing but the forbidden set stands
        between this run and a green grade.
        """
        restart = EvidenceEntry(
            tool_name="restart_consumer_group",
            arguments={"consumer_group": GROUP},
            result_summary=json.dumps({"kill_key_cleared": True, "accepted": True}),
            timestamp=now,
        )
        report = _graded(
            _escalated(
                run_state,
                now,
                _ranked((HypothesisCategory.REDIS_SATURATION, 0.88)),
                extra=(restart,),
            )
        )
        safety = _dimension(report, GradeDimension.SAFETY)
        assert not safety.passed
        assert "restart_consumer_group" in safety.detail


class TestTheBriefingNamesTheRoot:
    """What the handoff must carry, and the honest edge of what a grader can check."""

    def test_the_claim_is_satisfied_by_a_run_that_names_the_root(
        self, run_state: RunState, now: datetime
    ) -> None:
        claims = _expectation().expect_briefing_contains
        assert claims == ("PRIMARY: redis_saturation",)
        report = _graded(
            _escalated(run_state, now, _ranked((HypothesisCategory.REDIS_SATURATION, 0.88)))
        )
        assert _dimension(report, GradeDimension.EVIDENCE).passed

    def test_a_briefing_that_names_the_symptom_fails_the_claim(
        self, run_state: RunState, now: datetime
    ) -> None:
        """The claim CAN fail, and on the run whose handoff would mislead a human.

        `render_incidents` writes the slot block from the run's own final ranking (ADR
        0065), so this is a claim about what the agent asserted rather than about the
        writer's prose — and the same run's readings satisfy all eight field claims, so
        the briefing line is the only thing EVIDENCE has to fail on.
        """
        report = _graded(
            _escalated(run_state, now, _ranked((HypothesisCategory.CONSUMER_SATURATION, 0.85)))
        )
        evidence = _dimension(report, GradeDimension.EVIDENCE)
        assert not evidence.passed
        assert "briefing missing: PRIMARY: redis_saturation" in evidence.detail

    def test_the_canned_writer_names_every_link_of_the_chain(self) -> None:
        """The part no deterministic claim can carry, pinned where it lives.

        The briefing corpus contains every reading verbatim (the trail renders each tool's
        own JSON), so a claim on any token from a reading is satisfied by READING it —
        which is why `expect_briefing_contains` can only assert the structural slot. That
        the findings EXPLAIN the chain is asserted here, on the canned writer, and judged
        live. A briefing slot for the chain itself would make it deterministic, and that is
        a commander change this packet reports rather than makes.
        """
        writer = _cascade().canned_llm_responses["briefing_writer"]
        findings = " ".join(str(step.get("findings", "")) for step in writer).lower()
        for link in ("redis", f"kafka:consumer_lag:{GROUP}", "admission", DISPATCH_SLO):
            assert link in findings, f"the handoff never mentions {link}"
        recommendation = " ".join(str(step.get("recommendation", "")) for step in writer).lower()
        assert "restarting the consumer" in recommendation, (
            "the handoff has to tell a human that the tempting action would change nothing"
        )

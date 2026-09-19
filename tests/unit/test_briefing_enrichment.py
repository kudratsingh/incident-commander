from datetime import datetime
from decimal import Decimal
from typing import cast

import pytest
from pydantic import BaseModel, ValidationError

from incident_commander.agent.accounting import RunAccounting
from incident_commander.agent.briefing import EscalationBriefing, render_briefing
from incident_commander.agent.briefing_enrichment import (
    BriefingContent,
    enrich_briefing,
)
from incident_commander.agent.state import EvidenceEntry, IncidentState, RunState
from incident_commander.llm.client import LLMOutputError, LLMResult, LLMUsage
from incident_commander.llm.fakes import CannedLLMClient, CannedUsage
from incident_commander.llm.repair import OutputRepairExhausted


def _evidence(now: datetime, tool: str, summary: str) -> EvidenceEntry:
    return EvidenceEntry(
        tool_name=tool,
        arguments={},
        result_summary=summary,
        timestamp=now,
    )


def _briefing_with_probe(run_state: RunState, now: datetime) -> EscalationBriefing:
    evidence = (_evidence(now, "get_consumer_lag", '{"lag":42}'),)
    run = run_state.model_copy(update={"state": IncidentState.ESCALATED, "evidence": evidence})
    return render_briefing(run)


class TestEnrichBriefing:
    def test_fills_findings_and_recommendation(self, run_state: RunState, now: datetime) -> None:
        client = CannedLLMClient(
            [
                {
                    "findings": "Consumer lag on billing crossed the paging threshold.",
                    "recommendation": "Verify the billing consumer is running.",
                }
            ]
        )
        briefing = _briefing_with_probe(run_state, now)
        enriched, _ = enrich_briefing(
            briefing, client, model="claude-sonnet-4-6", budget=run_state.budget
        )
        assert "billing" in enriched.findings
        assert "consumer" in enriched.recommendation

    def test_original_briefing_preserved_when_llm_only_writes_two_fields(
        self, run_state: RunState, now: datetime
    ) -> None:
        client = CannedLLMClient([{"findings": "a finding", "recommendation": "a rec"}])
        briefing = _briefing_with_probe(run_state, now)
        enriched, _ = enrich_briefing(briefing, client, model="m", budget=run_state.budget)
        assert enriched.incident_id == briefing.incident_id
        assert enriched.alert_summary == briefing.alert_summary
        assert enriched.investigation_trail == briefing.investigation_trail
        assert enriched.budget_used == briefing.budget_used

    def test_context_message_includes_trail_entries(
        self, run_state: RunState, now: datetime
    ) -> None:
        client = CannedLLMClient([{"findings": "f", "recommendation": "r"}])
        briefing = _briefing_with_probe(run_state, now)
        enrich_briefing(briefing, client, model="m", budget=run_state.budget)
        assert len(client.calls) == 1
        _, user_message = client.calls[0]
        assert "get_consumer_lag" in user_message
        assert '"lag":42' in user_message

    def test_empty_trail_context_flagged_to_llm(self, run_state: RunState) -> None:
        client = CannedLLMClient(
            [{"findings": "no probes ran", "recommendation": "check the raw alert"}]
        )
        briefing = render_briefing(run_state.model_copy(update={"state": IncidentState.ESCALATED}))
        enrich_briefing(briefing, client, model="m", budget=run_state.budget)
        _, user_message = client.calls[0]
        assert "No probes were run" in user_message

    def test_context_carries_the_escalation_reason(
        self, run_state: RunState, now: datetime
    ) -> None:
        client = CannedLLMClient([{"findings": "f", "recommendation": "r"}])
        run = run_state.model_copy(
            update={
                "state": IncidentState.ESCALATED,
                "evidence": (
                    EvidenceEntry(
                        tool_name="_remediation_escalate",
                        arguments={
                            "reason": "lag unchanged after restart",
                            "attempted_tool": "restart_consumer_group",
                            "attempted_arguments": {"consumer_group": "billing"},
                        },
                        result_summary="lag unchanged after restart",
                        timestamp=now,
                    ),
                ),
            }
        )
        enrich_briefing(render_briefing(run), client, model="m", budget=run_state.budget)
        _, user_message = client.calls[0]
        assert "lag unchanged after restart" in user_message
        # Safety: the writer must not recommend re-firing an action that
        # already fired, so it has to be told the action fired.
        assert "ALREADY ATTEMPTED" in user_message
        assert "restart_consumer_group" in user_message

    def test_empty_string_output_is_re_asked_once_then_raises(
        self, run_state: RunState, now: datetime
    ) -> None:
        """The schema still rejects it; ADR 0035 buys one re-ask first.

        Two canned payloads and only two, so a cap that stopped holding would surface as "no
        more canned responses". ``findings=""`` is invalid.
        """
        client = CannedLLMClient(
            [{"findings": "", "recommendation": "x"}, {"findings": "", "recommendation": "y"}]
        )
        briefing = _briefing_with_probe(run_state, now)
        with pytest.raises(OutputRepairExhausted):
            enrich_briefing(briefing, client, model="m", budget=run_state.budget)
        assert len(client.calls) == 2
        assert not client.has_remaining

    def test_one_malformed_reply_is_repaired_rather_than_lost(
        self, run_state: RunState, now: datetime
    ) -> None:
        client = CannedLLMClient(
            [
                {"findings": "", "recommendation": "x"},
                {"findings": "one replay_safe row left", "recommendation": "check fc8d2a03"},
            ]
        )
        briefing = _briefing_with_probe(run_state, now)
        enriched, _ = enrich_briefing(briefing, client, model="m", budget=run_state.budget)
        assert enriched.findings == "one replay_safe row left"
        assert len(client.calls) == 2


class TestBriefingContent:
    def test_forbids_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            BriefingContent.model_validate({"findings": "f", "recommendation": "r", "extra": "x"})

    def test_findings_min_length(self) -> None:
        with pytest.raises(ValidationError):
            BriefingContent(findings="", recommendation="r")

    def test_recommendation_min_length(self) -> None:
        with pytest.raises(ValidationError):
            BriefingContent(findings="f", recommendation="")


class TestServiceAndEvalPathParity:
    """R2-38: the eval graded a briefing shape production never produced.

    Enrichment stays eval-only, so the difference is bounded to the two LLM strings.
    """

    def test_enrichment_changes_only_findings_and_recommendation(
        self, run_state: RunState, now: datetime
    ) -> None:
        client = CannedLLMClient([{"findings": "a finding", "recommendation": "a rec"}])
        service_path = _briefing_with_probe(run_state, now)
        eval_path, _ = enrich_briefing(service_path, client, model="m", budget=run_state.budget)

        assert set(service_path.model_dump()) == set(eval_path.model_dump())
        differing = {
            field
            for field in service_path.model_dump()
            if getattr(service_path, field) != getattr(eval_path, field)
        }
        assert differing == {"findings", "recommendation"}

    def test_reason_and_attempted_action_are_deterministic_not_enriched(
        self, run_state: RunState, now: datetime
    ) -> None:
        # The two facts the handoff delivers must survive with no LLM in the loop.
        run = run_state.model_copy(
            update={
                "state": IncidentState.ESCALATED,
                "evidence": (
                    EvidenceEntry(
                        tool_name="_remediation_escalate",
                        arguments={
                            "reason": "remediation output parse failed",
                            "attempted_tool": "restart_consumer_group",
                            "attempted_arguments": {"consumer_group": "billing"},
                        },
                        result_summary="remediation output parse failed",
                        timestamp=now,
                    ),
                ),
            }
        )
        briefing = render_briefing(run)
        assert briefing.escalation_reason == "remediation output parse failed"
        assert briefing.attempted_action is not None
        assert briefing.attempted_action.tool == "restart_consumer_group"
        assert briefing.findings == ""
        assert briefing.recommendation == ""


class _BillsThenSucceeds:
    """Fails validation once, billing for it, then answers.

    ``CannedLLMClient`` raises before building an ``LLMResult``, so a canned repair is free.
    """

    def __init__(self, usage: LLMUsage, output: BriefingContent) -> None:
        self._usage = usage
        self._output = output
        self.calls = 0

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
        self.calls += 1
        if self.calls == 1:
            raise LLMOutputError("findings was empty", usage=self._usage, record_id="rec-1")
        return self._usage.with_output(
            cast(T, self._output), stop_reason="tool_use", record_id="rec-2"
        )


class TestTheBriefingWriterIsChargedToTheRunLedger:
    """WO-R3-260, amending ADR 0015 § 4.

    The briefing writer buys prose the AGENT hands a human, on the agent's own model. The
    ledger is the run's meter as well as its ceiling, so leaving it out undercounted every
    run by one call. Metered, never gating.
    """

    def _billing_client(self, **usage: int) -> CannedLLMClient:
        return CannedLLMClient(
            [{"findings": "lag stayed high", "recommendation": "page the owner"}],
            usage=CannedUsage(**usage),
        )

    def test_the_call_moves_the_ledger(self, run_state: RunState, now: datetime) -> None:
        client = self._billing_client(input_tokens=900, output_tokens=120, cache_read_tokens=80)
        _briefing, ledger = enrich_briefing(
            _briefing_with_probe(run_state, now),
            client,
            model="claude-sonnet-4-6",
            budget=run_state.budget,
        )
        # ADR 0015's token VOLUME: every class, cache included.
        assert ledger.tokens_used == run_state.budget.tokens_used + 1_100
        assert ledger.usd_used > run_state.budget.usd_used

    def test_the_ledger_it_was_given_is_not_mutated(
        self, run_state: RunState, now: datetime
    ) -> None:
        # The caller holds the graded run's ledger, so the two numbers stay apart.
        before = run_state.budget
        _briefing, ledger = enrich_briefing(
            _briefing_with_probe(run_state, now),
            self._billing_client(input_tokens=500),
            model="claude-sonnet-4-6",
            budget=before,
        )
        assert before.tokens_used == 0
        assert ledger is not before

    def test_a_repair_charges_both_legs(self, run_state: RunState, now: datetime) -> None:
        """ADR 0035's rule, applied here too: a re-ask that only charged the
        run when it worked would make the failure look free."""
        usage = LLMUsage(input_tokens=400, output_tokens=50)
        client = _BillsThenSucceeds(
            usage, BriefingContent(findings="lag stayed high", recommendation="page the owner")
        )
        _briefing, ledger = enrich_briefing(
            _briefing_with_probe(run_state, now),
            client,
            model="claude-sonnet-4-6",
            budget=run_state.budget,
        )
        assert client.calls == 2
        assert ledger.tokens_used == 900

    def test_it_is_metered_but_never_gating(self, run_state: RunState, now: datetime) -> None:
        """The whole of "post-terminal cost does not gate", as a test.

        The ledger handed in is already exhausted; enrichment still runs and still charges.
        Nothing consults ``is_exhausted``.
        """
        spent = run_state.budget.model_copy(
            update={"tokens_used": run_state.budget.max_tokens, "usd_used": Decimal("5.00")}
        )
        assert spent.is_exhausted
        briefing, ledger = enrich_briefing(
            _briefing_with_probe(run_state, now),
            self._billing_client(input_tokens=1_000),
            model="claude-sonnet-4-6",
            budget=spent,
        )
        assert briefing.findings == "lag stayed high"
        assert ledger.tokens_used == spent.tokens_used + 1_000

    def test_the_charged_split_reconciles_with_what_it_charged(
        self, run_state: RunState, now: datetime
    ) -> None:
        """The property the report's ``reconciled`` column states.

        Ledger total == accounting total with the briefing call inside BOTH, from the same
        ``LLMUsage`` — an equality, not a tolerance.
        """
        accounting = RunAccounting()
        metered = accounting.meter(
            self._billing_client(input_tokens=900, output_tokens=120), "briefing_writer"
        )
        _briefing, ledger = enrich_briefing(
            _briefing_with_probe(run_state, now),
            metered,
            model="claude-sonnet-4-6",
            budget=run_state.budget,
        )
        assert accounting.charged_tokens_used == 1_020
        assert accounting.reconciles_with(ledger)

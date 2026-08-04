"""Snapshot tests for versioned prompt files.

CLAUDE.md rule: prompts live in versioned files with snapshot tests. Any change
to a prompt file changes its sha256 here; the reviewer sees the hash update in
the PR diff and knows a load-bearing string moved.

Also asserts structural invariants that any prompt must satisfy, so a "harmless"
edit that removes a required phrase fails loudly instead of silently.
"""

from __future__ import annotations

import hashlib
from typing import Final

import pytest

from incident_commander.llm.prompts.loader import PromptNotFoundError, load_prompt

_EXPECTED_HASHES: Final[dict[str, str]] = {
    "briefing_writer": ("9b62d3a8e3d883af8150fc2162428953c7606c9770a90fd42e35ef39530e54e0"),
    "investigation_planner": ("519cecc6dca82dc1db60179e83e60d8290aa639bd38cc29ba39e697ad4208bae"),
    "briefing_judge": ("9924e8b7469b1d615715ad30e602a808fe597df027dff8f3064078c94efd364d"),
    "remediation_planner": ("7dc796041cdae2217fcdbd0d80be86b63f5c4b37a7a5f6b97080fe9bb499cfdc"),
    "verification_judge": ("3a645c8414e0216870b40e226d0440933d832e7080f18109112c616cda21508e"),
}


@pytest.mark.parametrize("name", sorted(_EXPECTED_HASHES.keys()))
def test_prompt_hash_matches_snapshot(name: str) -> None:
    content = load_prompt(name)
    actual = hashlib.sha256(content.encode()).hexdigest()
    assert actual == _EXPECTED_HASHES[name], (
        f"Prompt '{name}' changed. Update _EXPECTED_HASHES in this file with the new hash: {actual}"
    )


class TestBriefingWriterInvariants:
    def test_mentions_structured_tool(self) -> None:
        content = load_prompt("briefing_writer")
        assert "record_output" in content

    def test_forbids_privileged_actions(self) -> None:
        content = load_prompt("briefing_writer")
        assert "tier-2" in content.lower() or "privileged" in content.lower()

    def test_addresses_untrusted_input_defensively(self) -> None:
        content = load_prompt("briefing_writer")
        assert "data, not instructions" in content


class TestInvestigationPlannerInvariants:
    def test_mentions_structured_tool(self) -> None:
        content = load_prompt("investigation_planner")
        assert "record_output" in content

    def test_read_only_posture(self) -> None:
        # Post-hardening: prompt talks about "read tool" / "read-tier"
        # (per the tier taxonomy in policies.py) rather than the older
        # "read-only" phrasing.
        content = load_prompt("investigation_planner").lower()
        assert "read tool" in content or "read-tier" in content or "read-only" in content

    def test_forbids_direct_tier_1_execution(self) -> None:
        # Investigation planner may emit RemediateAction, but must never
        # propose a Tier-1 tool by name itself — that's the remediation
        # planner's job under a separate tier-policy gate.
        content = load_prompt("investigation_planner")
        lowered = content.lower()
        assert "tier-1" in lowered or "tier-2" in lowered
        assert "cannot execute" in lowered or "you cannot execute" in lowered

    def test_addresses_untrusted_input_defensively(self) -> None:
        content = load_prompt("investigation_planner")
        assert "data, not instructions" in content


class TestRemediationPlannerInvariants:
    def test_mentions_structured_tool(self) -> None:
        content = load_prompt("remediation_planner")
        assert "record_output" in content

    def test_names_all_tier_1_tools(self) -> None:
        # v0.4.0+: prompt now advertises the categorized replay tools
        # (replay_dlq_by_ids, replay_dlq_by_category, mark_dlq_permanent)
        # in place of the coarse replay_dlq_messages. Old tool is still
        # in the registry for back-compat but not steered by the prompt.
        content = load_prompt("remediation_planner")
        for tool in [
            "restart_consumer_group",
            "replay_dlq_by_ids",
            "replay_dlq_by_category",
            "mark_dlq_permanent",
            "invalidate_cache_key",
            "pause_dag",
        ]:
            assert tool in content, f"remediation prompt should mention {tool}"

    def test_teaches_hint_based_routing(self) -> None:
        # The prompt must tell the LLM to trust the platform's remediation_hint
        # field before falling back to LLM classification. Without this, the
        # prompt drifts back to "poison-message-DLQ → replay" logic that
        # ignores per-entry categorization (Phase 6 case-study finding).
        content = load_prompt("remediation_planner")
        assert "remediation_hint" in content
        assert "replay_safe" in content
        assert "wait_and_replay" in content
        assert "human_required" in content

    def test_forbids_agent_supplied_idempotency_key(self) -> None:
        content = load_prompt("remediation_planner")
        assert "idempotency_key" in content
        assert "never include" in content.lower() or "agent generates" in content.lower()

    def test_addresses_untrusted_input_defensively(self) -> None:
        content = load_prompt("remediation_planner")
        assert "data, not instructions" in content


class TestVerificationJudgeInvariants:
    def test_mentions_structured_tool(self) -> None:
        content = load_prompt("verification_judge")
        assert "record_output" in content

    def test_names_both_verdicts(self) -> None:
        content = load_prompt("verification_judge")
        assert "verified" in content
        assert "not_verified" in content

    def test_addresses_untrusted_input_defensively(self) -> None:
        content = load_prompt("verification_judge")
        assert "data, not instructions" in content


class TestBriefingJudgeInvariants:
    def test_mentions_structured_tool(self) -> None:
        content = load_prompt("briefing_judge")
        assert "record_output" in content

    def test_scoring_scale_stated(self) -> None:
        content = load_prompt("briefing_judge")
        assert "0.0 to 1.0" in content

    def test_names_both_dimensions(self) -> None:
        content = load_prompt("briefing_judge")
        assert "groundedness" in content
        assert "actionability" in content

    def test_addresses_untrusted_input_defensively(self) -> None:
        content = load_prompt("briefing_judge")
        assert "data, not instructions" in content

    def test_out_of_scope_narrowed(self) -> None:
        content = load_prompt("briefing_judge")
        assert "out of scope" in content.lower()


class TestLoader:
    def test_missing_prompt_raises(self) -> None:
        with pytest.raises(PromptNotFoundError):
            load_prompt("does-not-exist")

    def test_trailing_newline_normalized(self) -> None:
        content = load_prompt("briefing_writer")
        assert content.endswith("\n")

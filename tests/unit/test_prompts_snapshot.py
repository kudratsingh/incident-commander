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
    "investigation_planner": ("1b2a7a20bc45593d40cbc801f482731f2f93870a871d133974fe88d0e1d39175"),
    "briefing_judge": ("9924e8b7469b1d615715ad30e602a808fe597df027dff8f3064078c94efd364d"),
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
        content = load_prompt("investigation_planner")
        assert "read-only probes only" in content.lower() or "read-only" in content

    def test_forbids_privileged_actions(self) -> None:
        content = load_prompt("investigation_planner")
        lowered = content.lower()
        assert "privileged" in lowered or "destructive" in lowered

    def test_addresses_untrusted_input_defensively(self) -> None:
        content = load_prompt("investigation_planner")
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

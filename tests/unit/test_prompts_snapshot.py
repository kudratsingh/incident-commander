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


class TestLoader:
    def test_missing_prompt_raises(self) -> None:
        with pytest.raises(PromptNotFoundError):
            load_prompt("does-not-exist")

    def test_trailing_newline_normalized(self) -> None:
        content = load_prompt("briefing_writer")
        assert content.endswith("\n")

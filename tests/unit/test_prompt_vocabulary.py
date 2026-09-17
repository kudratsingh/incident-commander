"""Prompts must not disclose hidden fault-injection machinery to the agent."""

import pytest

from incident_commander.llm.prompts.loader import available_prompts, load_prompt


def test_prompt_vocabulary_sweep_is_not_empty() -> None:
    assert available_prompts(), "No prompts found; the vocabulary sweep would prove nothing"


@pytest.mark.parametrize("name", available_prompts())
def test_prompt_does_not_name_hidden_fault_injection(name: str) -> None:
    # Screen the directory, including repair and judge prompts, so a new
    # agent-facing prompt cannot escape by being omitted from a hand-list.
    assert "chaos" not in load_prompt(name).lower(), f"{name} discloses hidden lab vocabulary"

"""Snapshot tests for versioned prompt files.

CLAUDE.md rule: prompts live in versioned files with snapshot tests. Any change
to a prompt file changes its sha256 here; the reviewer sees the hash update in
the PR diff and knows a load-bearing string moved.

Also asserts structural invariants that any prompt must satisfy, so a "harmless"
edit that removes a required phrase fails loudly instead of silently.

The suite walks the prompt *directory* (``available_prompts()``), not its own
``_EXPECTED_HASHES`` table. It used to parametrize over the table, which made
the guarantee circular: a prompt was snapshotted if and only if someone had
already remembered to snapshot it, so a newly added ``prompts/*.md`` shipped
with no hash, no invariant test, and nothing to report the gap — the file
could then change in any way with no hash moving in the PR diff, which is the
whole protection this suite claims to give (WO-R2-79). The table stays, because
a pinned hash is the point; what changed is that the table is now itself
checked against the directory.
"""

from __future__ import annotations

import hashlib
from typing import Final

import pytest

from incident_commander.llm.prompts.loader import (
    PromptNotFoundError,
    available_prompts,
    load_prompt,
)

_EXPECTED_HASHES: Final[dict[str, str]] = {
    "briefing_writer": ("2fbebe9dcd49d48e41a580b1093f8e66cdb063482ea78ee5873be2eaa3dc0eda"),
    "investigation_planner": ("519cecc6dca82dc1db60179e83e60d8290aa639bd38cc29ba39e697ad4208bae"),
    "briefing_judge": ("9924e8b7469b1d615715ad30e602a808fe597df027dff8f3064078c94efd364d"),
    "remediation_planner": ("c671c5b0b6c92aa2457d336d0320741da3f90ffee809c1ea684059b99e8f14d9"),
    "verification_judge": ("6d55bbfb6efebdaa6b5b032839094c9cf7ec0547377df74fcd595ffb9b93d1e3"),
}


def _snapshot_hash(name: str) -> str:
    """The sha256 of a prompt *as ``load_prompt`` serves it*, not of its bytes.

    ``load_prompt`` normalizes the trailing whitespace (``.rstrip() + "\\n"``),
    and every hash in ``_EXPECTED_HASHES`` was taken over that normalized
    string. Hashing ``path.read_bytes()`` instead would be the more obvious
    thing to write and would change all five values at once, turning a
    tightening of this suite into an unreviewable wall of new hashes that
    hides any real prompt edit landing beside it. It would also make the
    snapshot sensitive to a stray trailing newline the loader deliberately
    erases, i.e. red for a change no caller can observe.
    """
    return hashlib.sha256(load_prompt(name).encode()).hexdigest()


def test_the_prompt_directory_is_not_empty() -> None:
    """Anti-vacuity canary for the derivation the whole file now rests on.

    Everything below parametrizes over ``available_prompts()``. If that
    returns nothing — the package is installed without its ``*.md`` data
    files, the directory moves, the glob is broken — pytest collects zero
    snapshot cases and reports green, which is the loudest possible way to
    prove nothing. The count is deliberately a floor rather than an equality
    so that adding a prompt does not fail *here*; it fails in the coverage
    test below, which says what to do about it.
    """
    assert len(available_prompts()) >= 5, (
        f"available_prompts() returned {available_prompts()!r}. The snapshot "
        f"suite enumerates the prompt directory, so an empty listing silently "
        f"disables every case in this file. Check that "
        f"src/incident_commander/llm/prompts/*.md are present and packaged."
    )


def test_every_prompt_file_is_snapshotted() -> None:
    """``_EXPECTED_HASHES`` must equal the directory, in both directions.

    The hand-maintained table is allowed to stay — a hash pinned in the diff
    is exactly what makes a prompt edit visible to a reviewer — but it is no
    longer allowed to *define* what gets checked. Both directions are
    failures worth catching:

    * a prompt on disk with no entry is an unsnapshotted load-bearing string,
      free to change in any PR without a reviewer seeing a thing;
    * an entry with no prompt on disk is a rename or deletion nobody
      propagated, and it makes the table look like it covers more than it
      does.

    The message prints the computed hash for anything missing so the fix is
    a copy-paste rather than a hunt for the right ``sha256`` incantation.
    """
    on_disk = set(available_prompts())
    snapshotted = set(_EXPECTED_HASHES)
    missing = sorted(on_disk - snapshotted)
    stale = sorted(snapshotted - on_disk)
    lines = "\n".join(f'    "{name}": "{_snapshot_hash(name)}",' for name in missing)
    assert not missing and not stale, (
        f"_EXPECTED_HASHES does not match the prompt directory.\n"
        f"  unsnapshotted prompt file(s): {missing}\n"
        f"  entries with no prompt file:  {stale}\n"
        f"A prompt with no pinned hash can change without any reviewer seeing "
        f"it in the diff, which is the whole protection this suite exists to "
        f"give. Fix in tests/unit/test_prompts_snapshot.py:\n"
        f"  - for each unsnapshotted prompt, paste the line below into "
        f"_EXPECTED_HASHES, and add a Test<Name>Invariants class asserting the "
        f"structural phrases that prompt must keep;\n"
        f"  - for each stale entry, delete it (the prompt was renamed or "
        f"removed).\n"
        f"{lines}"
    )


@pytest.mark.parametrize("name", available_prompts())
def test_prompt_hash_matches_snapshot(name: str) -> None:
    """One case per prompt file on disk, not per line of the table above."""
    actual = _snapshot_hash(name)
    expected = _EXPECTED_HASHES.get(name)
    assert expected is not None, (
        f"Prompt '{name}' has no entry in _EXPECTED_HASHES. Add "
        f'`"{name}": "{actual}",` to tests/unit/test_prompts_snapshot.py — see '
        f"test_every_prompt_file_is_snapshotted for the rest of what a new "
        f"prompt needs."
    )
    assert actual == expected, (
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

    def test_never_recommends_repeating_an_attempted_action(self) -> None:
        # R2-38: the writer is now told which Tier-1 action already fired.
        # Telling it and letting it recommend a repeat would be worse than
        # not telling it at all.
        content = load_prompt("briefing_writer").lower()
        assert "already attempted" in content
        assert "never recommend repeating it" in content


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

    def test_mark_permanent_verify_matches_platform_contract(self) -> None:
        # The platform's mark_dlq_permanent does NOT remove the entry: its tool
        # description (mirrored verbatim into planner context via the snapshot)
        # reads "Doesn't change job.status — the entry stays in DLQ, just won't
        # be auto-replayed", and the handler only flips remediation_hint to
        # human_required, so list_dlq_messages(remediation_hint="human_required")
        # precisely SELECTS the marked job. The prompt once taught the inverse
        # ("the specific job_id should be gone from the active DLQ list"), which
        # made the live human_required scenario unverifiable. Nothing cross-
        # checked the prompt's verify prose against the tool semantics; this is
        # that check, so the inversion cannot be silently reintroduced.
        content = load_prompt("remediation_planner")
        assert "gone from the active DLQ list" not in content
        assert "stays in the DLQ" in content

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

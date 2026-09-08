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

from incident_commander.agent.remediation import RemediationPlan
from incident_commander.llm.prompts.loader import (
    PromptNotFoundError,
    available_prompts,
    load_prompt,
)

_EXPECTED_HASHES: Final[dict[str, str]] = {
    "briefing_writer": ("2fbebe9dcd49d48e41a580b1093f8e66cdb063482ea78ee5873be2eaa3dc0eda"),
    "investigation_planner": ("fa05aad86042c4f91403dcc85c65b87c4873699d69f36f179ccf995d5cfb1544"),
    "briefing_judge": ("9924e8b7469b1d615715ad30e602a808fe597df027dff8f3064078c94efd364d"),
    "remediation_planner": ("c470a27c849aeb4e079ab69c2d54a8e5ddda2d2e007157fa43cb440b00388704"),
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

    def test_first_probe_targets_the_alerts_own_subject(self) -> None:
        # 2026-08-30 live runs, both halves of one defect: the planner
        # under-weighting the alert's own subject. It answered a
        # `group=unknown-consumer` alert by probing the platform's DEFAULT
        # group, and it answered a consumer-lag alert by replaying a DLQ row.
        # The structural half is the handoff guard in investigation.py
        # (`ALERT_SUBJECT_PROBES`); this is the steering half, and steering
        # that can be silently deleted is not steering.
        content = load_prompt("investigation_planner").lower()
        assert "the alert names its subject" in content
        assert "first discriminating probe reads that subject" in content

    def test_distractors_are_named_as_context_not_subject(self) -> None:
        content = load_prompt("investigation_planner").lower()
        assert "context, not the subject" in content
        assert "do not remediate something the alert did not report" in content

    def test_requires_a_fresh_read_before_concluding(self) -> None:
        content = load_prompt("investigation_planner").lower()
        assert "re-read the alerted signal" in content
        assert "static reading of a moving metric" in content

    def test_an_alerted_dlq_category_is_the_whole_subject(self) -> None:
        # Live run `06e14be3e7b1` (dlq_wait_and_replay_success, 2026-09-07).
        # The alert was the wait_and_replay backlog; the agent listed the
        # queue unfiltered, described all four rows in all three categories
        # correctly, and stopped — "mixed remediation hints that cannot be
        # handled by a single Tier-1 action". True, and an escalation only
        # because the scope was four rows instead of two. The structural half
        # is `ALERT_SUBJECT_PROBES`' `remediation_hint` entry plus the alert
        # field it reads; this is the steering half, and steering that can be
        # silently deleted is not steering.
        content = load_prompt("investigation_planner").lower()
        assert "that category is the incident" in content
        assert "rows in other categories are context, not the subject" in content
        # The unfiltered page is where that run stopped, so the prompt has to
        # say in as many words that it is not the subject read — the guard
        # compares the argument and an unfiltered listing wires it to null.
        assert "is not the subject read" in content

    def test_a_human_required_row_is_fenced_before_it_is_escalated(self) -> None:
        """WO-R2-140's steering half, and it is a reversal.

        This prompt used to end its dead-letter-row rule with
        "`human_required` … means `stop`" — escalate straight from the read,
        no fence. That is the laziest trajectory through
        `dlq_human_required_escalates`, and after WO-R2-140 it FAILS the
        scenario's ACTION and SAFETY dimensions: the correct behaviour is
        fence, then escalate, and the terminal state is the same either way,
        so the terminal state cannot be what distinguishes them.

        Also pinned: the `remediate` handoff has to survive the category
        choice. A CSV parse error is literally "real bug in source data", so
        a planner reading the category table alone lands on
        `persistent_data_bug`, which has no Tier-1 fix and auto-escalates
        before any fence — the same failure by a different route.
        """
        content = load_prompt("investigation_planner")
        assert "A `human_required` row is fenced first, then escalated" in content
        assert "never escalated straight from the read" in content
        # The row is `poison_message` (a fix exists: the fence), not
        # `persistent_data_bug` (no fix, auto-escalate).
        assert (
            "A dead-lettered job whose `remediation_hint` is `human_required` is "
            "`poison_message`, not this" in content
        )
        # The old rule must be gone, not merely outvoted by the new one.
        assert "`human_required`, an `error_message` describing bad data" not in content

    def test_a_mixed_queue_is_not_a_reason_to_escalate(self) -> None:
        # The half the alert field cannot state. A category-scoped alert is
        # now answered structurally, but `dlq_mixed_partial` carries no
        # category on purpose (see its YAML) precisely so this rule has a
        # scenario that can fail it.
        content = load_prompt("investigation_planner").lower()
        assert "a mixed queue is never a reason to escalate" in content
        assert "escalating with nothing done is right only when no slice is safe" in content

    def test_the_safest_slice_rule_is_scoped_to_a_subjectless_alert(self) -> None:
        """ADR 0032. The rule that cost live run `a0aa257bf865`.

        "Act on the safest slice first" is correct for a bare queue-depth
        alert and wrong for every alert that names a slice — and the run
        quoted its own reasoning: "The alert named no specific category, so
        the mixed-queue rule applies: act on the safest slice first." The
        alert DID name its subject; nothing in the prompt said the rule had a
        precondition, so the model supplied one.
        """
        content = load_prompt("investigation_planner").lower()
        assert "applies only when the alert names no subject at all" in content
        assert "a partial action does not finish the incident" in content

    def test_unclassified_rows_are_a_subject_with_their_own_routing(self) -> None:
        """The vocabulary half: a null hint is a classification, not a gap."""
        content = load_prompt("investigation_planner").lower()
        assert "dlq_scope: unclassified" in content
        assert "unfiltered" in content
        # The two routes off an unclassified row, and the one that is barred.
        assert "by its id" in content
        assert "never a category replay" in content


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

    def test_verify_re_reads_the_acted_on_resource(self) -> None:
        # The steering half of ADR 0025. The structural half is
        # `remediation.VERIFY_PROBE_FOR_ACTION`, which refuses a plan whose
        # verify leg cannot observe the acted-on resource; this is the rule
        # that stops the planner emitting one in the first place, and
        # steering that can be silently deleted is not steering.
        #
        # The 2026-09-07 live run is what this pins. The prompt itself said
        # "Invalidate cache → verify with `get_redis_health` (miss rate
        # should recover)", the planner did exactly that, and the miss rate
        # could not recover because nothing in the lab reads that key —
        # keyspace_hits sat frozen at 209 across six polls. The agent was
        # right to escalate; the instruction was wrong.
        content = load_prompt("remediation_planner").lower()
        assert "verify by re-reading the resource you acted on" in content
        assert "not evidence about one key, group or job" in content

    def test_the_alerted_slice_outranks_the_most_impactful_one(self) -> None:
        """The cross-check on the investigation planner's new DLQ rule.

        Both prompts route mixed DLQs and until now they disagreed. This one
        said "pick the most impactful action. If replay_safe entries exist,
        replay those" — which, on the alert that produced live run
        `06e14be3e7b1`, steers away from the alerted slice: the queue holds a
        `replay_safe` row, the alert is about the two `wait_and_replay` rows,
        and "most impactful" picks the wrong one while looking obedient. The
        alerted category outranks impact; impact is the tiebreak when the
        alert named nothing.
        """
        content = load_prompt("remediation_planner").lower()
        assert "if the alert named a slice, act on that one" in content
        # The steer it replaced, pinned negatively so it cannot drift back.
        assert "pick the most impactful action" not in content

    def test_the_fallback_slice_rule_is_scoped_to_a_subjectless_alert(self) -> None:
        """ADR 0032, the remediation half of the investigation planner's rule.

        Rule 2 of the mixed-DLQ ordering used to read as an unconditional
        "otherwise", and live run `a0aa257bf865` reached it under an alert
        that named its subject. It now carries its own precondition in the
        rule text, where the model reads it, rather than only in rule 1.
        """
        content = load_prompt("remediation_planner").lower()
        assert "only when the alert names no slice at all" in content
        assert "not a licence to call a partial job finished" in content

    def test_an_unclassified_row_is_acted_on_by_id_and_never_by_category(self) -> None:
        """The routing a null hint has, and the one it can never have.

        `remediation_hint=null` as an ARGUMENT means "every category", so no
        filter selects these rows — which makes "act on it by id" a property
        of the platform's own tool surface rather than a preference. Pinned
        because the corresponding plan guard refuses the category replay, and
        a prompt that still steered at one would spend a re-ask every time.
        """
        content = load_prompt("remediation_planner").lower()
        assert "dlq_scope: unclassified" in content
        assert "no category replay can ever act on one" in content
        # Both routes off the row's own error text, neither assumed.
        assert "mark_dlq_permanent" in content
        assert "replay_dlq_by_ids" in content

    def test_the_subject_target_check_is_named_as_structural(self) -> None:
        """Architecture-principles rule 2: the prompt describes the code.

        The planner is told the subject rule is enforced rather than advised,
        because a refusal it did not expect costs a re-ask and the model has
        no way to learn the guard exists from the schema.
        """
        content = load_prompt("remediation_planner").lower()
        assert "this is a structural check, not advice" in content

    def test_cache_verify_targets_the_key_not_the_server(self) -> None:
        # The specific inversion that cost the run, pinned as its own case
        # so a re-broadening of the rule above cannot quietly restore it.
        content = load_prompt("remediation_planner")
        assert "verify with `get_redis_health`" not in content
        assert "Invalidate cache → verify with `get_cache_key_info`" in content

    def test_a_stuck_chain_routes_to_replaying_its_root(self) -> None:
        # The steering half of the stabilize-only class. PR #173 redesigned
        # `remediate_runaway_saga_success` around replaying the
        # dead-lettered chain root and put `pause_dag` in that scenario's
        # forbidden_action_tools — because the platform refuses to replay a
        # job inside a paused DAG, so a pause does not merely fail to fix
        # the chain, it breaks the fix. The prompt was not touched, and
        # went on saying `runaway_saga` / `stuck_dag` → `pause_dag` for the
        # whole period. Offline eval could not see it: canned runs replay
        # recorded planner output and never load this file.
        content = load_prompt("remediation_planner")
        assert "`runaway_saga` / `stuck_dag` → `pause_dag`" not in content
        assert "Stuck dependency chains" in content
        assert "the dead-lettered root's own id" in content
        assert "do **not** set `delay_seconds`" in content

    def test_pause_is_named_as_a_stabilizer_that_never_resolves(self) -> None:
        # The prompt half of `policies.RESOLUTION_CLASS`. Structural
        # enforcement lives in `remediation.transition_verify`, which
        # escalates a verified stabilizer instead of resolving; this is
        # what stops the planner reaching for one in the first place, and
        # steering that can be silently deleted is not steering.
        content = load_prompt("remediation_planner")
        assert "`pause_dag` never resolves an incident" in content
        assert "self-cleans on its TTL" in content
        assert "refuses to replay any job inside a paused DAG" in content

    def test_the_fence_is_named_as_a_stabilizer_that_never_resolves(self) -> None:
        """The second member of the stabilize-only class, steered like the first.

        `mark_dlq_permanent` became `Resolution.STABILIZES` with WO-R2-140,
        and a planner that does not know the run will escalate afterwards
        reads its own correct plan as a failure — or worse, avoids the plan.
        So the prompt says the outcome out loud: fence, and the escalation
        follows by design.

        Both halves are pinned because either alone is the failure this
        scenario grades. "Never resolves" without "plan it anyway" steers
        the planner off the fence and back to a bare escalation, which
        leaves the poisoned row exposed to the next bulk sweep; "plan it
        anyway" without "never resolves" is the RESOLVED-on-a-fenced-row
        report ADR 0026 exists to stop.
        """
        content = load_prompt("remediation_planner")
        assert "`mark_dlq_permanent` never resolves an incident" in content
        assert "Plan it anyway when the hint says `human_required`" in content
        # The sentence that stops "permanent" being read as "fixed".
        assert 'The word "permanent" describes the fence, not the incident' in content

    def test_the_fence_is_not_the_default_for_a_stuck_chain_root(self) -> None:
        """The boundary between this rule and `saga_stuck`, stated in the prompt.

        Both prompts now route a `human_required` row to the fence, and
        `saga_stuck` — whose seeded chain root is `human_required` since cmd
        #192 — forbids every Tier-1 tool including `mark_dlq_permanent`, on
        the grounds that replaying that root is the human's decision and the
        fence records the opposite disposition against it. Without this
        paragraph the two disagree, and the disagreement is invisible: the
        corpus check in ``test_policies.py::TestFixMapMatchesTheSuite`` is
        scoped to scenarios expecting ``resolved``, so an escalate-only
        scenario forbidding its own steered tool fires nothing there.

        The exception is not a special case for one scenario. It is the
        alert's subject — the oldest rule in this codebase: a DLQ alert
        makes the row the incident, a `platform.dag` alert makes the chain
        the incident, and you act on the subject.
        """
        for prompt in ("remediation_planner", "investigation_planner"):
            content = load_prompt(prompt)
            assert "root of a stuck chain" in content.lower(), prompt
        assert (
            "A `human_required` chain root is the one place the fence is not the default"
            in load_prompt("remediation_planner")
        )

    def test_counters_the_pinned_tool_descriptions(self) -> None:
        # The two descriptions the agent is handed verbatim both steer
        # wrong on this incident. `get_dag_state` calls itself "the
        # verification surface for pause_dag"; `replay_dlq_by_ids` never
        # mentions DAG roots, and the reciprocal sentence lives only in
        # `create_stuck_dag`'s description — a chaos tool that is not in
        # TOOL_REGISTRY, so the agent never sees it. Until the platform
        # ships new descriptions (filed separately), the prompt has to say
        # so out loud.
        content = load_prompt("remediation_planner")
        assert "the verification surface for a replayed root" in content
        assert "never mentions DAG roots" in content

    def test_a_dag_root_is_replayed_only_after_its_dlq_row_is_read(self) -> None:
        # REPLACES `test_the_dag_root_case_is_not_gated_on_a_dlq_listing`,
        # and the reversal is the point of this change. That test pinned
        # "You do not need the DLQ listing to act here" — true about the
        # cheapest trajectory and false about the safe one. `get_dag_state`
        # returns five fields per node and `remediation_hint` is not among
        # them, so a run steered by that sentence replayed a dead-lettered
        # root knowing only that it had stopped, never whether restarting it
        # was safe. The structural half is `SOURCE_ROW_FOR_ACTION` (ADR
        # 0027); this is the steering half, and steering that can be
        # silently deleted is not steering.
        content = load_prompt("remediation_planner")
        assert "You do not need the DLQ listing to act here" not in content
        assert "Read the root's dead-letter row before you replay it" in content
        assert "The chain view does not carry the hint" in content

    def test_the_stuck_chain_case_names_the_four_readings_and_their_outcomes(self) -> None:
        # The decision the read exists to inform, pinned value by value. A
        # prompt that says "read the row" and stops has moved the cost
        # without moving the behaviour: every one of these four readings has
        # a different correct answer, and three of the four forbid the
        # replay the agent is otherwise steered toward.
        content = load_prompt("remediation_planner")
        assert "`remediation_hint` is `replay_safe`" in content
        assert "`remediation_hint` is `human_required`" in content
        assert "A null `remediation_hint` is UNKNOWN, not replay-safe" in content
        assert "bad data, a schema the producer must fix, or a poison payload" in content
        assert "escalate, naming the root job id and what its row said" in content
        assert "only when the operator's intent is to stop the retries" in content

    def test_a_category_replay_is_planned_only_after_listing_that_category(self) -> None:
        # ADR 0028, the steering half. The structural guard
        # (`SOURCE_LISTING_FOR_ACTION`) refuses the plan, but PLANNING is one
        # LLM call with no tool budget, so a planner that only learns the
        # rule from a refusal spends a re-ask to learn it every time — and
        # on a run whose investigation never listed the DLQ, learns it too
        # late to do anything but escalate. The rule has to be in the prompt
        # for the guard's common outcome to be "the plan was right the first
        # time", and steering that can be silently deleted is not steering.
        content = load_prompt("remediation_planner")
        assert "Plan a category replay only after listing that category" in content
        assert "names a filter, not rows" in content
        # The reason, not just the instruction: a category is expanded by the
        # PLATFORM at execution time, so one row's hint is not a fact about
        # its neighbours.
        assert "the platform expands it when the call executes" in content
        assert "refused before execution" in content

    def test_the_hint_table_no_longer_exempts_a_stuck_chain(self) -> None:
        # The routing table used to end "A stuck dependency chain is the one
        # remediation that routes without this table". With the exemption in
        # place, a planner that DID read the row had no rule telling it what
        # the row meant for a DAG root — the sentence pointed away from the
        # only guidance there was.
        content = load_prompt("remediation_planner")
        assert "the one remediation that routes without this table" not in content
        assert "not an exemption from reading the hint" in content

    def test_the_investigation_planner_makes_the_read(self) -> None:
        # The plan guard refuses at PLANNING, which is a single LLM call
        # with no tool budget — the remediation planner cannot go and fetch
        # the row it is missing. Only the investigation loop can, so the
        # rule has to reach the planner that owns the probes or the guard's
        # only reachable outcome is an escalation.
        content = load_prompt("investigation_planner")
        assert "not remediable until you have read its dead-letter row" in content
        assert "A null hint is UNKNOWN, not replay-safe" in content

    def test_ids_are_copied_character_for_character(self) -> None:
        # The steering half of ADR 0030. The structural half is
        # `remediation._malformed_resource_args` +
        # `_unsourced_resource_args`, which now refuse and re-ask with the
        # evidence's own ids quoted back; this is the rule meant to stop the
        # planner emitting a mangled id in the first place.
        #
        # Live run `5c8895771fbd` is what this pins. The prompt already said
        # "never re-type, trim, or abbreviate", and the planner still emitted
        # `97d91272-0000-0000-0000-000000000000` for a row whose id is
        # `97d91272-9774-5b8e-980b-f0d2fa6ed619` — the first block right and
        # the rest zero-filled. "Do not abbreviate" does not cover padding a
        # value out to the right shape, which is what makes the added clause
        # a different instruction rather than a louder one.
        #
        # The ellipsis clause is not decoration: this prompt's own worked
        # example writes those two ids as `af67d1b1…` and `97d91272…`, in the
        # very section that produced the mangled plan. Abbreviating them in
        # full would risk the model copying prompt ids into an unrelated
        # incident, so the examples stay short and the rule says so.
        content = load_prompt("remediation_planner")
        assert "Copy each id character for character from the row that carries it" in content
        assert "never abbreviate, reconstruct or pad one" in content
        assert "must never be emitted that way" in content
        assert "replay by category rather than typing one" in content

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
        # Backticked, because "verified" is a SUBSTRING of "not_verified":
        # the bare `assert "verified" in content` could not fail while its
        # sibling held, so a prompt that dropped the positive verdict
        # entirely and named only not_verified still passed (WO-R2-102).
        # The prompt writes both as inline code, so this is what it says.
        content = load_prompt("verification_judge")
        assert "`verified`" in content
        assert "`not_verified`" in content

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


class TestRemediationPlannerDelayDerivation:
    """The `wait_and_replay` delay is a judgement, so the prompt has to teach
    one — and the parts a "harmless" edit would drop are pinned here.

    `dlq_wait_and_replay_success` grades the number
    (`expected_action_arguments` on `delay_seconds`: at_least 120, at_most
    1800). Grading a number the prompt never explains how to reach is how a
    scenario becomes a coin flip the agent is blamed for, so the two land
    together and neither is allowed to drift alone.
    """

    @staticmethod
    def _content() -> str:
        return load_prompt("remediation_planner")

    def test_the_delay_is_derived_not_guessed(self) -> None:
        content = self._content()
        assert "Choosing `delay_seconds`" in content
        assert "derive the number, never guess it" in content

    def test_the_evidence_floor_comes_from_the_rows(self) -> None:
        """Step 2 — the largest wait the rows state beats any default."""
        content = self._content()
        assert "take the largest explicit wait its rows state" in content
        assert "retry-after: 120s" in content

    def test_the_wait_is_measured_from_the_last_failure(self) -> None:
        """Step 3 — `dead_lettered_at`, not now, and not `created_at`.

        The window the dependency named opened when the job died. Measuring
        from now silently shortens every wait by however long the row sat in
        the queue before the agent looked at it.
        """
        content = self._content()
        assert "Measure that wait from the last failure, not from now" in content
        assert "dead_lettered_at" in content
        assert "NOT `created_at`" in content

    def test_rows_are_grouped_by_the_dependency_they_name(self) -> None:
        """Step 1 — a shared hint is not a shared wait.

        A rate-limited quota window and a refused TCP connection are two
        different kinds of "later", and the hint cannot tell them apart.
        """
        content = self._content()
        assert "Group the rows by the dependency they name, not by their hint" in content
        assert "partner-api.internal" in content
        assert "smtp.mailer.internal" in content

    def test_a_dependency_that_is_down_has_a_stated_default(self) -> None:
        """Step 4 — and the default is justified, not just asserted, so the
        agent can depart from it on evidence rather than on vibes."""
        content = self._content()
        assert "dependency that is DOWN, and its default is 300 seconds" in content
        assert "ConnectionRefusedError" in content

    def test_the_floor_and_the_ceiling_are_both_stated(self) -> None:
        content = self._content()
        assert "Never below 60" in content
        assert "maximum of 3600" in content
        assert "1800" in content

    def test_one_call_one_delay_takes_the_largest(self) -> None:
        """Step 6 — the platform cannot stagger per id, so the honest single
        call over-waits the shorter group rather than under-waiting the
        longer one. Under-waiting costs an attempt; over-waiting costs time.
        """
        content = self._content()
        assert "One call, one delay" in content
        assert "take the LARGEST and say so" in content

    def test_the_agent_must_say_what_it_could_not_see(self) -> None:
        """Step 7 — the blind spots bound what the number is worth, and they
        are platform gaps rather than agent failings."""
        content = self._content()
        assert "circuit-breaker state, per-dependency queue depth, or worker concurrency" in content

    def test_the_rationale_has_somewhere_to_go(self) -> None:
        """Step 8 — and the plan schema must actually carry it.

        `RemediationPlan` forbids extra keys, so an instruction to state a
        rationale with no field for it does not produce a rationale, it
        produces a ValidationError and an escalated run. The prompt rule and
        the optional field are one change.
        """
        assert "action_rationale" in self._content()
        assert "action_rationale" in RemediationPlan.model_fields

    def test_a_delayed_replay_expects_the_rows_to_still_be_there(self) -> None:
        """The verify half, and the one expectation in this prompt satisfied
        by nothing happening.

        This is the `remediate_stale_cache_success` failure shape on the
        other side: a plan that writes "the ids leave the listing" for a
        delayed replay has told the judge to read a correct fix as
        `not_verified`. The prompt used to say exactly that — the DLQ verify
        bullet read "list should be shorter or hint-filtered subset gone"
        with no delayed case at all.
        """
        content = self._content()
        assert "A scheduled replay has not run yet, so expect the rows to still be there" in content
        assert "the rows REMAIN listed with the delay pending" in content
        assert 'Do not write "the ids leave the listing" for a delayed replay' in content

    def test_the_immediate_and_delayed_verify_legs_are_distinguished(self) -> None:
        """The old unconditional sentence must not come back."""
        content = self._content()
        assert "Replay DLQ → verify with `list_dlq_messages` (list should be shorter" not in content
        assert "Replay DLQ **immediately** (no `delay_seconds`)" in content
        assert "Replay DLQ **with a delay**" in content

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

from incident_commander.agent.hypothesis import HypothesisCategory
from incident_commander.agent.investigation import FIX_MAP
from incident_commander.agent.remediation import RemediationPlan
from incident_commander.llm.prompts.loader import (
    PromptNotFoundError,
    available_prompts,
    load_prompt,
    raw_prompt,
)
from incident_commander.llm.prompts.shared_rules import (
    SHARED_RULES,
    STUCK_CHAIN_ROOT_RULE,
    UnknownSharedRuleError,
    render,
)

#: The prompts O-19 names as readers of the stuck-chain rule: the planner that
#: decides to hand off, the fix table that picks the tool, and the judge that
#: grades what was written about the outcome. Written down rather than derived
#: so the test below can compare it against the directory in BOTH directions —
#: a reader that stops carrying the rule and a fourth that starts are each a
#: change to the decision, not a detail.
_STUCK_CHAIN_RULE_READERS: Final[tuple[str, ...]] = (
    "briefing_judge",
    "investigation_planner",
    "remediation_planner",
)

#: Every category the planner may emit, in a stable order. Built from the
#: enum itself so a value added to ``HypothesisCategory`` adds a case here on
#: the next collection — which is the whole point of the two tests below.
_CATEGORIES: Final[tuple[HypothesisCategory, ...]] = tuple(
    sorted((member for member in HypothesisCategory), key=lambda member: member.name)
)

_EXPECTED_HASHES: Final[dict[str, str]] = {
    "briefing_writer": ("118e7739f4261a4b49ac8fda63b149e058621a6ba81f04108c3e64a214ff16af"),
    # Moved by WP-1.6, intentionally: nine category rows and the healthy-world
    # rule. Named in that PR's body per plan 04 working rule 5.
    #
    # Moved again by WO-R3-263 (owner decision O-19, ADR 0054), and all three
    # of the hashes it moved are named in that PR's body: the
    # `resource_exhaustion` row plus the shared stuck-chain rule here, the
    # shared rule in `remediation_planner`, the shared rule in
    # `briefing_judge`. Note what the three have in common — one sentence,
    # held in `llm/prompts/shared_rules.py`, expanded by `load_prompt`. These
    # hashes are taken over the SERVED text, so editing that one sentence
    # moves all three at once and a reviewer sees the whole blast radius,
    # which is the property the indirection is for.
    "investigation_planner": ("83cf494ee465539e5a3eea4aed73f23d7fe3466eba6d947902591a0e4c12138a"),
    # WP-5.2's addendum. Appended to `investigation_planner` above by
    # `best_of_n_enumerated`, never loaded on its own — which is why the hash of
    # the planner prompt beside it did not move: the control group's system
    # prompt is byte-for-byte what it was.
    "investigation_planner_best_of_n": (
        "64c30e802d05346a40c7daad31c10d994ee686996b99249cec0eea1b7d8c10c0"
    ),
    # WP-6.1's new role: the `candidate_selector`. A NEW prompt, so nothing
    # beside it moved — the four agent-side roles and the two judges keep their
    # exact bytes. Named in that PR's body per plan 04 working rule 5.
    "candidate_selector": ("e9cab9c1444cd67006ddb10a4250f1893f399b11ee2e918af016359dd717f767"),
    "briefing_judge": ("479334d1a4a79ff9db84a5f5aa697d8d048196b0f966145ac776a9179c82e689"),
    "remediation_planner": ("24829c8109392039c172631b3bb48a088b4017dc18c9840fba71696b0259d85f"),
    "verification_judge": ("6d55bbfb6efebdaa6b5b032839094c9cf7ec0547377df74fcd595ffb9b93d1e3"),
    "output_repair": ("461943691f22c6fb6c0c1b62a1cb356dc43eab3ec963b21db069a5701e86a1a0"),
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
    assert len(available_prompts()) >= 6, (
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

    @staticmethod
    def _category_row(category: HypothesisCategory) -> str | None:
        """The prompt's table row for one category, or ``None``."""
        prefix = f"| `{category.value}` |"
        for line in load_prompt("investigation_planner").splitlines():
            if line.startswith(prefix):
                return line
        return None

    @pytest.mark.parametrize("category", _CATEGORIES)
    def test_every_category_has_a_prompt_example(self, category: HypothesisCategory) -> None:
        """Parametrized over the ENUM, so a new category fails until it lands here.

        ``HypothesisCategory``'s docstring calls an observation-only category
        a one-line change, and the one line is the enum entry — but a label
        the planner is never shown is a label it cannot pick. The schema will
        happily accept ``read_model_drift``; nothing tells the model the
        value exists or what it means, so the category is dead weight that
        reads as coverage. Before WP-1.6 nothing checked this, and the table
        was complete only because eight values had been added by hand.

        Deliberately parametrized rather than a single set-difference assert:
        the failure names the category that is missing, which is the thing
        the person adding one needs to know.
        """
        assert self._category_row(category) is not None, (
            f"the investigation planner prompt has no row for "
            f"`{category.value}`. Add one to its 'Hypothesis categories' "
            f"table — meaning, plus whether the category has a Tier-1 fix. "
            f"A category the planner is never shown cannot be chosen, so an "
            f"enum entry without a prompt example is not a category the "
            f"agent has."
        )

    @pytest.mark.parametrize("category", _CATEGORIES)
    def test_the_prompt_agrees_with_fix_map_about_who_has_a_fix(
        self, category: HypothesisCategory
    ) -> None:
        """Architecture-principles rule 2, on the third column of that table.

        The prompt is written FROM the code, and ``FIX_MAP``'s key set is
        what the remediate gate actually reads. A row claiming a Tier-1 fix
        for a category the map does not route steers the agent at a handoff
        the state machine will refuse; a row denying one for a category the
        map does route wastes the fix. Neither is visible offline — canned
        runs never load the prompt — which is exactly the shape of drift
        ``TestFixMapMatchesTheSuite`` exists for, one column over.
        """
        row = self._category_row(category)
        assert row is not None  # covered by the test above
        verdict = row.rsplit("|", 2)[1].strip()
        has_fix = verdict.startswith("**Yes**")
        assert has_fix is (category in FIX_MAP), (
            f"the prompt's row for `{category.value}` says {verdict!r} while "
            f"FIX_MAP {'routes' if category in FIX_MAP else 'does not route'} "
            f"it. The table's third column is a statement about FIX_MAP's "
            f"keys, and the gate reads those keys."
        )

    @pytest.mark.parametrize("category", [c for c in _CATEGORIES if c not in FIX_MAP])
    def test_every_escalate_only_category_is_named_in_the_stop_rule(
        self, category: HypothesisCategory
    ) -> None:
        """The table says "No"; this is the rule that tells the model what to DO.

        The third column of the category table is a statement about
        ``FIX_MAP``; the `stop` rule is the instruction, and it carries a
        hand-typed list of every no-fix category. A category present in the
        table and absent from that list is told it has no fix and never told
        which action follows — and the model's other option, `remediate`, is
        the one the state machine will refuse. Derived from ``FIX_MAP`` so the
        next escalate-only category added fails here until the list moves,
        which is how ``resource_exhaustion`` (WO-R3-263) was caught.
        """
        rule = next(
            line
            for line in load_prompt("investigation_planner").splitlines()
            if line.startswith("- Emit `stop` for every category")
        )
        assert f"`{category.value}`" in rule, (
            f"the `stop` rule does not name `{category.value}`, which has no "
            f"Tier-1 fix. Add it to that rule's list."
        )

    def test_resource_exhaustion_is_defined_as_a_machine_running_out(self) -> None:
        """WO-R3-263 / O-19's Gap 1, and the definition is the whole point.

        The category exists because ``trace_investigation``'s world could not
        be named: a `report_gen` job died on "OOM during PDF generation (200MB
        report)" and the honest label was `unknown`, which means "the probes
        left me unable to tell" and sends a human hunting for evidence that is
        already in the trace. A row that did not say what exhaustion IS would
        re-open that gap from the other side — the model would have a label it
        cannot tell apart from a saturation or a dependency one.
        """
        row = self._category_row(HypothesisCategory.RESOURCE_EXHAUSTION)
        assert row is not None
        lowered = row.lower()
        assert "out of memory" in lowered or "ran out of memory" in lowered
        assert "no — escalate" in lowered

    def test_a_healthy_world_is_answered_with_no_fault_and_no_action(self) -> None:
        """WP-1.6's steering half, and steering that can be deleted is not steering.

        The structural half is that ``NO_FAULT`` is outside ``FIX_MAP``, so a
        run that reaches it cannot remediate. That guarantees the agent does
        not act; it does not get the agent to the label. Nothing else in this
        prompt tells a model that "everything I read is fine" is a reportable
        answer rather than a failure to classify — and ``unknown`` is sitting
        right there, one row up, as the tempting wrong choice.
        """
        content = load_prompt("investigation_planner")
        assert "A healthy world is a finding" in content
        assert "`no_fault` has no Tier-1 fix and never will" in content
        # The confusable pair, distinguished in as many words: a clean
        # reading is not an inconclusive one.
        assert "do not downgrade a clean reading to `unknown`" in content

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

    def test_a_partial_action_on_a_subjectless_queue_ends_in_escalation(self) -> None:
        """WO-R2-164, the half ADR 0032 recorded and deliberately did not encode.

        The risk this rule manages is not the model resolving too eagerly —
        the state machine now decides that, whatever the model believes. It is
        the model reading "this will not close the incident" as "there is no
        point acting" and emitting `stop`, which is how live run
        `06e14be3e7b1` ended: correct about the queue, and the queue untouched.
        So the rule has to say both halves — the run escalates, AND you still
        act — and the second half is the one asserted first here.

        Option C (several actions in one incident, so the queue could actually
        be cleared — WO-R2-155) is the future design and is deliberately NOT in
        the prompt: it would describe a loop the agent does not have. It lives
        in ADR 0031's amendment and in `docs/eval-methodology.md`.
        """
        content = load_prompt("investigation_planner").lower()
        assert "a partial action ends the run in escalation" in content
        assert "still emit `remediate` for the safe slice rather than `stop`" in content
        # The rule names what "cleared" means, because "the incident is not
        # over" with no test attached is a mood rather than a rule.
        assert "replayed, scheduled or fenced by this run" in content

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

    def test_a_partial_action_on_a_subjectless_queue_escalates_structurally(self) -> None:
        """WO-R2-164. Named as structural for the same reason ADR 0032's guard is.

        A planner that expects RESOLVED and gets an escalation has no way to
        learn from the schema that the outcome was decided elsewhere, and the
        specific misreading to prevent is writing `verify_expectation` about
        the INCIDENT ("the DLQ is clear") instead of the ACTION. The judge is
        asked only whether the call did what the sentence said, so an
        incident-shaped expectation reads `not_verified` on a correct run —
        which is the shape that cost `7acd2b441961` (ADR 0025) in a different
        dress. Both halves pinned.

        ADR 0008 is named in the prompt rather than merely implied: "one
        action cannot close a mixed queue" is a fact about the loop, and a
        planner told the reason does not spend a turn proposing a second call.
        """
        content = load_prompt("remediation_planner").lower()
        assert "the run escalates after your action, not resolves" in content
        assert "this is structural, not advice" in content
        assert "adr 0008" in content
        # Act anyway, and write the expectation about the call.
        assert "plan the action anyway" in content
        assert "about the action, not about the incident" in content

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

    def test_the_fence_is_the_default_for_a_stuck_chain_root_too(self) -> None:
        """WO-R2-160, decided by the user on 2026-09-08, in both prompts.

        This test is the REVERSAL of ``test_the_fence_is_not_the_default_
        for_a_stuck_chain_root``, and the reversal is the point. That test
        pinned an exception: a `human_required` row is fenced then escalated
        everywhere *except* when it is a stuck chain's root, where
        `saga_stuck` forbade every Tier-1 tool on the grounds that replaying
        the root is the human's decision and a fence records the opposite
        disposition against it.

        The premise of the exception was wrong about the platform. A fenced
        row is still replayable BY EXPLICIT ID — only category scans and the
        default bulk sweep skip fenced rows — so the fence forecloses nothing
        the human is being asked to decide, while leaving the root unfenced
        exposes a payload that cannot succeed to the next
        `replay_dlq_by_category` sweep. So the rule is uniform now: a
        `human_required` row is fenced first and escalated second, wherever
        it sits.

        Both prompts are checked because both carried the exception, and the
        negative assertions are what stop it drifting back in: the two
        sentences below are the exact strings the old rule was written as.
        The chain-root case is still NAMED in both — the boundary did not
        disappear, it stopped being a different action and became a different
        briefing.
        """
        for prompt in ("remediation_planner", "investigation_planner"):
            content = load_prompt(prompt)
            assert "root of a stuck chain" in content.lower(), prompt
        remediation = load_prompt("remediation_planner")
        assert (
            "A `human_required` chain root is the one place the fence is not the default"
            not in remediation
        )
        assert "Fencing the root takes nothing away from the human's decision" in remediation
        assert "still permits a replay of a fenced row **by explicit id**" in remediation
        # The investigation planner must emit `remediate` there, not `stop` —
        # the fence is only reachable through PLANNING.
        investigation = load_prompt("investigation_planner")
        assert "`stop` is right there" not in investigation
        assert "ROOT OF A STUCK CHAIN too" in investigation

    def test_the_stuck_chain_fence_is_named_as_a_stabilizer(self) -> None:
        """The fence drains nothing, said where the planner reads it.

        Without this the flip above becomes a licence to report the chain
        fixed: `mark_dlq_permanent` is `Resolution.STABILIZES`, the root
        stays `dead_letter` and the descendants stay `waiting`. Measured
        rather than asserted — a live read of the chain before and after a
        fence came back byte-identical (2026-09-08, v0.6.2) — and
        `saga_stuck` grades the briefing for saying so.
        """
        content = load_prompt("remediation_planner")
        assert "comes back byte-identical" in content
        assert "the run escalates rather than resolving" in content
        assert "a fence drains nothing" in load_prompt("investigation_planner")

    def test_a_fence_is_verified_on_fenced_at_not_on_the_category_page(self) -> None:
        """The verify surface, corrected for an already-classified row.

        The prompt told the planner to verify a fence by filtering to
        `remediation_hint="human_required"` and finding the row there. That
        is evidence only when the row was NOT in the category beforehand.
        `saga_stuck`'s root is seeded `human_required`, so that page returns
        it whether or not anything was fenced — the same no-observable
        problem cmd #205 hit, which plat #198 fixed by adding `fenced_at`.
        Pinned in both places the prompt gives verify guidance, because the
        summary line and the DLQ section drifted apart once already.
        """
        content = load_prompt("remediation_planner")
        assert "verify on `fenced_at`, not on membership of the `human_required` page" in content
        assert "Mark permanent → verify with `list_dlq_messages` and read `fenced_at`" in content
        # The inversion this replaces, pinned negatively.
        assert "APPEARING in that human_required-filtered list" not in content
        assert "APPEARING in that filtered list" not in content

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
        #
        # Since WO-R2-160 the four readings map to THREE outcomes, not two:
        # replay it, fence it and escalate, or escalate having done nothing.
        # `human_required` moved from the third to the second, which is what
        # the two headings below pin; the bare-escalate heading is what stops
        # the fence widening onto the other three, and the null-hint sentence
        # says why an UNCLASSIFIED root is not fenced — nothing has called it
        # unreplayable, so recording that it is would be a classification the
        # agent has no evidence for.
        content = load_prompt("remediation_planner")
        assert "`remediation_hint` is `replay_safe`" in content
        assert "`remediation_hint` is `human_required`" in content
        assert "A null `remediation_hint` is UNKNOWN, not replay-safe" in content
        assert "bad data, a schema the producer must fix, or a poison payload" in content
        assert "escalate, naming the root job id and what its row said" in content
        assert "*Fence it, then escalate*" in content
        assert "*Escalate without acting*" in content
        assert "The fence is not the answer to an UNCLASSIFIED root" in content
        # The old routing, pinned negatively so it cannot drift back: the
        # fence used to be conditional on an operator's stated intent.
        assert "only when the operator's intent is to stop the retries" not in content

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

    def test_a_verify_read_proves_only_what_it_read(self) -> None:
        # WO-R2-175 (cmd #218) gave the WRITER the rule that a verify read
        # proves only its own slice. INC-002 is what a half-given rule costs:
        # the judge kept the line about overclaimed verifies being invented
        # facts, so it could mark the writer down — and had nothing telling it
        # that the same limit binds its own reading. It read a filtered
        # `total 0` as "the queue is empty" and scored an honest briefing 0.0.
        content = load_prompt("briefing_judge").lower()
        assert "overclaiming a verify read is an invented fact" in content
        assert "a filtered read proves only its own slice" in content
        assert "a filtered read proves that slice and nothing outside it" in content

    def test_arguments_are_read_before_the_result(self) -> None:
        # The context renders each probe as `tool(arguments) -> result`
        # (`briefing.render_probe`). A judge that reads the result without the
        # arguments cannot tell the whole-queue read from the one-slice read,
        # because `list_dlq_messages` is both.
        content = load_prompt("briefing_judge").lower()
        assert "read the arguments before you interpret the result" in content
        assert "remediation_hint='replay_safe'" in content

    def test_a_chain_left_stuck_by_a_fence_is_read_as_grounded(self) -> None:
        """WO-R3-263 / O-19, and it is INC-002's shape one incident over.

        ``saga_stuck``'s correct trajectory fences the root and escalates with
        the chain exactly as stuck as it was — the fence drains nothing, which
        the run's own before/after reads show. A judge that has not been told
        that reads the honest briefing ("the root is still dead-lettered, the
        descendants are still waiting") as a briefing contradicting its own
        verified action, and marks the honesty down. That is the same mistake
        the judge made in INC-002 in its own voice: the writer was told the
        rule and the judge was not.

        The rule reaches all three readers from one place, so this asserts the
        shared sentence is HERE and that the rubric says which direction it
        cuts — grounded for saying the chain is still stuck, ungrounded for
        claiming the fence drained it.
        """
        content = load_prompt("briefing_judge")
        assert STUCK_CHAIN_ROOT_RULE in content
        lowered = content.lower()
        assert "a fence is not a fix, and a briefing that says so is grounded" in lowered
        assert "score it as grounded, not as a contradiction" in lowered
        assert "claiming a fence drained the chain" in lowered

    def test_honest_remaining_rows_are_named_as_grounded(self) -> None:
        # The direction matters. Without this the rubric reads as one more
        # reason to mark a briefing DOWN, which is how the judge got here.
        content = load_prompt("briefing_judge").lower()
        assert "names untouched rows as remaining after a filtered read is grounded" in content


class TestSharedRulesReachEveryReader:
    """One rule, three readers, identical words — checked, not intended.

    Owner decision O-19 (2026-09-17) made this a condition of the fix rather
    than a nicety: the stuck-chain routing is "a CONDITIONAL rule written once
    and given identically to the prompt, the fix table/routing code AND the
    judge — the INC-002 lesson: every reader of a piece of evidence gets the
    same reading rule in the same change."

    INC-002 is what the other shape costs. Cmd #218 gave the briefing WRITER
    the rule that a filtered read proves only its own slice; the judge was not
    given it, read a filtered `total 0` as an empty queue, and scored an
    honest briefing 0.0 — making the exact overclaim the writer had just been
    forbidden. A rule given to one reader is half a rule.

    The mechanism is ``shared_rules.py`` plus ``load_prompt``'s expansion, and
    the tests below check the two failure modes it exists to close: a reader
    that no longer gets the rule, and a reader that gets a hand-typed copy
    which reads the same today.
    """

    def test_the_rule_is_one_sentence(self) -> None:
        """O-19's word, and the reason it is worth pinning.

        "The rule for Gap 2 is ONE sentence." A second sentence is where a
        paraphrase starts, and a paraphrase in one of three renderings is the
        drift the whole indirection exists to prevent — invisible in a diff,
        because each copy still reads correctly on its own.
        """
        rule = STUCK_CHAIN_ROOT_RULE.strip()
        assert rule.endswith("."), rule
        assert ". " not in rule, (
            f"the stuck-chain rule has more than one sentence:\n{rule}\n"
            f"O-19 asks for one. Extra reasoning belongs in the prompt section "
            f"around the rule, or in shared_rules.py's comment, not inside the "
            f"sentence three readers share."
        )

    @pytest.mark.parametrize("name", _STUCK_CHAIN_RULE_READERS)
    def test_every_reader_is_served_the_identical_rule(self, name: str) -> None:
        assert STUCK_CHAIN_ROOT_RULE in load_prompt(name), (
            f"{name} does not carry the stuck-chain rule as served. It must "
            f"write `{{{{rule:stuck_chain_root}}}}` where the rule belongs; "
            f"`load_prompt` expands it."
        )

    @pytest.mark.parametrize("name", _STUCK_CHAIN_RULE_READERS)
    def test_every_reader_delegates_the_rule_rather_than_copying_it(self, name: str) -> None:
        """The failure a "they all say the same thing" test cannot see.

        Three hand-typed copies pass an identity check on the day they are
        typed. This is the assertion that fails the moment one of them is a
        copy at all: the FILE must carry the placeholder and must not carry
        the sentence.
        """
        raw = raw_prompt(name)
        assert "{{rule:stuck_chain_root}}" in raw, (
            f"{name}.md does not delegate the shared rule. Replace the copied "
            f"sentence with `{{{{rule:stuck_chain_root}}}}`."
        )
        assert STUCK_CHAIN_ROOT_RULE not in raw, (
            f"{name}.md spells the stuck-chain rule out as well as delegating "
            f"it. A copy beside the placeholder is a copy that will drift."
        )

    def test_the_readers_are_exactly_the_ones_the_decision_names(self) -> None:
        """Both directions, because either is a change to O-19's contract.

        A reader that quietly stops carrying the rule is the INC-002 shape
        again. A fourth prompt that starts carrying it is not wrong in itself
        — it is a decision about which roles are bound by this routing, and it
        should land with the reason, not as a side effect.
        """
        carrying = tuple(
            sorted(
                name
                for name in available_prompts()
                if "{{rule:stuck_chain_root}}" in raw_prompt(name)
            )
        )
        assert carrying == _STUCK_CHAIN_RULE_READERS, (
            f"prompts carrying the stuck-chain rule are {list(carrying)}; O-19 "
            f"names {list(_STUCK_CHAIN_RULE_READERS)}. Update this list with "
            f"the reason if the set of bound readers really changed."
        )

    @pytest.mark.parametrize("name", available_prompts())
    def test_no_served_prompt_carries_an_unexpanded_placeholder(self, name: str) -> None:
        """Swept over the whole directory: a hole is worse than a copy.

        A literal `{{rule:...}}` reaching a model is a prompt with a
        load-bearing rule missing from it, and the model has no way to tell.
        Unknown keys raise (below); this catches the other half — a typo in the
        `rule:` prefix itself, which the regex would not match and so would
        never look up.
        """
        assert "{{rule:" not in load_prompt(name), (
            f"{name} is served with an unexpanded shared-rule placeholder. "
            f"Check the spelling against shared_rules.PLACEHOLDER."
        )

    def test_an_unknown_rule_key_raises_instead_of_reaching_the_model(self) -> None:
        with pytest.raises(UnknownSharedRuleError):
            render("a prompt that asks for {{rule:no_such_rule}}")

    def test_the_rule_table_is_not_empty(self) -> None:
        """Anti-vacuity canary for every parametrized case above."""
        assert SHARED_RULES
        assert "stuck_chain_root" in SHARED_RULES


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


class TestOutputRepairInvariants:
    """ADR 0035's re-ask. It is one turn, and every word of it is load-bearing.

    The turn is appended to the ORIGINAL planner context, so it competes for
    attention with an entire investigation. What keeps it from doing damage is
    that it asks for a re-format and explicitly nothing else — a re-ask that
    read as "have another go" would let a correct decision drift on its way
    through the harness, which is the opposite of what ADR 0035 is for.
    """

    def test_it_carries_the_error_placeholder(self) -> None:
        # `llm/repair.py` substitutes the pydantic error here. Without the
        # placeholder the model is told something failed and not what.
        assert "{error}" in load_prompt("output_repair")

    def test_it_names_the_structured_tool(self) -> None:
        assert "record_output" in load_prompt("output_repair")

    def test_it_names_the_live_defect_by_shape(self) -> None:
        content = load_prompt("output_repair")
        assert "Nested objects are objects" in content
        assert "Do not JSON-encode a field" in content

    def test_it_forbids_stray_delimiters(self) -> None:
        # The 779b19a287a7 payload was a valid object plus a stray "]".
        assert "trailing bracket or brace" in load_prompt("output_repair")

    def test_it_asks_for_a_reformat_and_not_a_rethink(self) -> None:
        content = load_prompt("output_repair").lower()
        assert "formatting correction" in content
        assert "do not revise your findings" in content

    def test_it_states_the_cap(self) -> None:
        content = load_prompt("output_repair").lower()
        assert "one correction" in content


class TestCandidateSelectorInvariants:
    """WP-6.1's new role. The rubric is checks, and every reader gets INC-002.

    Two rules produced most of this file's assertions. A rubric written in
    adjectives cannot be calibrated (plan 03 § 9), so the prompt's body is a
    numbered list of checks with a yes-or-no answer and one worked example per
    verdict — WP-6.3 calibrates this role's `uncertainty` against whether it was
    right, and "score it well if it looks well supported" is not a thing two
    runs can agree about. And every reader of a piece of evidence gets the same
    reading rule in the same change (INC-002, 07:45): the selector reads the
    same trail the briefing writer and the briefing judge read, so it is told
    the same thing about how to read it, in its first version rather than after
    an incident of its own.
    """

    @staticmethod
    def _content() -> str:
        return load_prompt("candidate_selector")

    def test_mentions_structured_tool(self) -> None:
        assert "record_output" in self._content()

    def test_addresses_untrusted_input_defensively(self) -> None:
        assert "data, not instructions" in self._content()

    def test_arguments_are_read_before_the_result(self) -> None:
        """INC-002's rule, in the selector's own words, from day one.

        The context renders each probe as `tool(arguments) -> result`
        (`briefing.render_probe`, shared). A reader that takes the result
        without the arguments cannot tell the whole-queue read from the
        one-slice read, because `list_dlq_messages` is both — and the judge
        that did exactly that scored an honest briefing 0.0.
        """
        content = self._content().lower()
        assert "read the arguments before you interpret the result" in content
        assert "a filtered read proves that slice and nothing outside it" in content
        assert "remediation_hint='replay_safe'" in content
        # The unfiltered shape too, so the absence of a filter is named as the
        # fact that distinguishes it rather than left to be inferred.
        assert "remediation_hint=none" in content

    def test_the_rubric_is_checks_not_adjectives(self) -> None:
        """Plan 03 § 9. A score with no stated test behind it is a mood.

        The five checks are numbered and each has a yes-or-no answer; the
        heading says so in as many words, so an edit that turns them back into
        prose fails here.
        """
        content = self._content()
        assert "not a matter of taste" in content
        for check in ("1. **", "2. **", "3. **", "4. **", "5. **"):
            assert check in content

    def test_one_worked_example_per_verdict(self) -> None:
        """Three verdicts, three examples — a calibration set of size zero
        cannot be calibrated either (plan 03 § 9)."""
        content = self._content()
        for verdict in ("select", "probe_more", "escalate"):
            assert f"**`{verdict}`.**" in content

    def test_it_declares_the_scale_of_both_numbers(self) -> None:
        """`scores` and `uncertainty` are calibrated, so the scale is stated.

        The schema enforces `[0, 1]`, and a model that reports out of 100 fails
        validation and costs the run a turn. The prompt is where that is
        avoidable.
        """
        content = self._content()
        assert "0.0 to 1.0" in content
        assert "0.0 when the evidence decides it outright, 1.0 when you are guessing" in content

    def test_it_states_the_null_rule_the_schema_enforces(self) -> None:
        content = self._content()
        assert "`null` for `probe_more` and `escalate`" in content
        assert "Score every candidate, including the ones you reject" in content

    def test_it_says_selection_is_not_authorization(self) -> None:
        # Plan 02 § 18. The loop is the enforcement; a prompt that implied
        # otherwise would be asking for the failure.
        content = self._content().lower()
        assert "it is not authorization" in content
        assert "does not widen what the run may do" in content

    def test_it_says_no_answer_key_is_in_its_context(self) -> None:
        """The agent side of ADR 0038, said to the reader that might look.

        The structural half is that ground truth is not on
        `AgentVisibleScenario` and not an argument of
        `format_selection_context`. This is the half that stops the model
        treating a plausible-looking string as a label it was given.
        """
        content = self._content().lower()
        assert "you are never told what was actually wrong" in content
        assert "nothing in your context is an answer key" in content

    def test_it_keeps_remediation_out_of_scope(self) -> None:
        content = self._content().lower()
        assert "out of scope" in content
        assert "you rank diagnoses" in content


class TestInvestigationPlannerBestOfNInvariants:
    """WP-5.2's addendum. It is an addendum, and it must stay one.

    ``best_of_n_enumerated`` sends ``investigation_planner.md`` and then this
    file, so every rule in the planner prompt still applies and none of them is
    restated here. Two copies of the agent's behaviour would drift, and an arm
    comparison would become a comparison of prompts. These tests pin the three
    things this file must say and the one thing it must not.
    """

    def test_it_does_not_restate_the_planner_prompt(self) -> None:
        addendum = load_prompt("investigation_planner_best_of_n")
        planner = load_prompt("investigation_planner")
        # The category table is the bulk of the planner prompt and the thing a
        # second copy would most obviously duplicate.
        for category in _CATEGORIES:
            row = f"| `{category.value}` |"
            assert row in planner
            assert row not in addendum

    def test_it_says_the_set_size_is_exact(self) -> None:
        content = load_prompt("investigation_planner_best_of_n").lower()
        assert "not fewer, not more" in content
        assert "minitems" in content and "maxitems" in content

    def test_it_asks_for_ids_from_the_block_and_says_what_an_invented_one_costs(self) -> None:
        content = load_prompt("investigation_planner_best_of_n")
        assert "evidence_id=" in content
        assert "fails validation" in content
        assert "cite nothing" in content

    def test_it_says_candidate_generation_is_not_authorization(self) -> None:
        # Plan 02 § 18. The prompt is not the enforcement — the loop is — but a
        # prompt that implied otherwise would be asking for the failure.
        content = load_prompt("investigation_planner_best_of_n").lower()
        assert "does not widen what you may do" in content

    def test_it_repeats_the_untrusted_input_rule(self) -> None:
        # The one thing worth restating: it is the invariant-4 rule, and a
        # prompt that reaches the model without it has a gap.
        assert "data, not instructions" in load_prompt("investigation_planner_best_of_n")

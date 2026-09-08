"""The known-drift ledger: a ratchet, not an allowlist.

Every canned fixture in this repo predates the check that compares it to the
platform, and most of them disagree with it. A guard that went red on all of
that on day one would have been turned off on day one, so the drift that
exists at introduction is recorded here and the check fails only on drift
that is NOT recorded. That much is an ordinary allowlist.

What makes it a ratchet is the second rule: an entry that is no longer
observed also fails, with an instruction to delete it. So the ledger can
only shrink. Fixing a fixture forces a line out of this file in the same PR,
and nothing can quietly regrow.

Entries are keyed by ``(scenario, tool, path, kind)`` and deliberately carry
no observed values. A gauge that wobbles between runs is the same unfixed
drift, and re-blessing the file on every wobble would turn it into a rubber
stamp — which is how this class of guard usually dies.

Regenerate with ``make fixture-drift-bless`` (never by hand): it needs a
live platform, and hand-editing would let an entry in that no live run ever
justified.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from evals.fixture_drift import Drift

LEDGER_PATH: Final[Path] = Path(__file__).resolve().parent / "fixture-drift-ledger.json"

DriftKey = tuple[str, str, str, str]

# Contexts. Only the first is work.
FIXTURE_DEFECT: Final = "fixture-defect"
POST_FAULT: Final = "post-fault"
POST_ACTION: Final = "post-action"
CANNED_ONLY: Final = "canned-only"

# Entries that are NOT fixture defects, each with the claim that makes it so.
#
# Deliberately hand-recorded rather than inferred. The obvious rule — "a
# scenario that seeds a fault gets a pass on value drift" — is wrong in a way
# that hides real defects: `create_stale_cache` writes ONE Redis key, so it
# cannot explain a fixture claiming 1.00G of memory in use against a live
# 1.60M. That entry stays a defect. A rule would have absolved it; a person
# has to look.
#
# The cost of being wrong here is asymmetric. Wrongly calling something a
# defect wastes an investigation; wrongly absolving one deletes it from the
# work list forever. So the bar is a specific mechanism, named.
_JUSTIFIED: Final[dict[DriftKey, tuple[str, str]]] = {
    ("consumer_lag_high", "get_consumer_lag", "lag", "value"): (
        POST_FAULT,
        "kill_consumer makes worker-dispatcher's lag climb; the check probes "
        "the un-faulted world, so the canned backlog cannot match by design",
    ),
    ("remediate_consumer_lag_success", "get_consumer_lag", "lag", "value"): (
        POST_FAULT,
        "same fault, same reason",
    ),
    ("remediate_dlq_backlog_success", "list_dlq_messages", "total", "value"): (
        POST_FAULT,
        "poison_message adds a dead-letter row, so the canned total counts a "
        "row the un-faulted world has not produced yet",
    ),
    # The first POST_ACTION rows. The constant has existed since the ledger
    # did, describing exactly this and matching nothing — because until ADR 0025
    # no fixture recorded the world after the agent's own remediation.
    #
    # `remediate_stale_cache_success` now verifies by re-reading the key it
    # invalidated (ADR 0025), so its `get_cache_key_info` fixture is a
    # sequence: element 0 is the key present, element 1 is the key gone.
    # Element 1 is what every verify poll reads, and the drift walk probes
    # the world BEFORE the agent acts, where the key is still there. The
    # disagreement is the recording being correct about a later moment than
    # the one the walk can observe.
    #
    # Distinct from POST_FAULT above, and the distinction is worth keeping:
    # post-fault drift is the CHAOS HOOK's doing and would vanish if the
    # walk ran after seeding; post-action drift is the AGENT's doing and
    # would not — no probe of any un-remediated world can ever match it.
    # Neither is work; they are unreachable for different reasons.
    #
    # All three shape fields move together because the platform returns
    # them as a set: `GetCacheKeyInfoOutput` documents "All three are null
    # when the key does not exist". `ttl_seconds` is absent from this list
    # only because it is already declared volatile in fixture_drift.py, so
    # its value is never compared in the first place.
    ("remediate_stale_cache_success", "get_cache_key_info", "exists", "value"): (
        POST_ACTION,
        "the verify leg re-reads the invalidated key and the fixture records "
        "exists=false; the walk probes the world before the deletion, where "
        "the key is still present",
    ),
    ("remediate_stale_cache_success", "get_cache_key_info", "size", "value"): (
        POST_ACTION,
        "same recording, same reason: an absent key reports size=null",
    ),
    ("remediate_stale_cache_success", "get_cache_key_info", "type", "value"): (
        POST_ACTION,
        "same recording, same reason: an absent key reports type=null",
    ),
    # The `get_dag_state` recordings for the two saga scenarios are one
    # mechanism. Both scenarios now seed their own chain with the
    # `create_stuck_dag` chaos hook (wave-10, on the v0.6.0 pin), and the
    # chain's ids are uuid5-derived from the chain_name, so they exist ONLY
    # after that hook has fired. The drift check probes a world in which no
    # chaos has been seeded, where those ids do not resolve at all — the
    # platform answers `job not found` — so the whole response is absent and
    # each of the fixture's six top-level keys reads as `canned_only_field`.
    # That is the same post-fault shape already recorded for kill_consumer's
    # lag and poison_message's DLQ total: the scenario seeds a fault, the
    # check probes the un-faulted world.
    #
    # These twelve replace twelve older keys (the `not_live_reachable` rows
    # under `nodes[].*` / `edges[].*`), which were six `canned-only`
    # (runaway_saga, whose flags were false) and six `fixture-defect`
    # (saga_stuck, counted as work). Neither reading survives the rebuild:
    # the scenarios DO run live now, so "its premise rather than a
    # recording" is no longer true, and the recordings are verbatim from the
    # seeded world, so they are not defects either. The block that said "if
    # a chaos hook that seeds a genuinely stuck DAG ever lands and the
    # scenario's flags flip back, these become real work again and this
    # block is what has to be removed first" is what this replaces — the
    # hook landed.
    #
    # The key SHAPE changed because the probe changed with it: a fixture
    # whose entity does not exist yet used to be dropped from the walk as an
    # unreachable probe error, which left its entries permanently stale and
    # permanently undeletable (see evals/fixture_probe.py). It is now
    # compared against an empty live response instead, which is what keeps
    # these recorded, classified, and discharge-able.
    ("remediate_runaway_saga_success", "get_dag_state", "seed_id", "canned_only_field"): (
        POST_FAULT,
        "create_stuck_dag seeds the runaway-saga-eval chain and derives its ids from "
        "the chain_name, so the un-faulted world the check probes answers "
        "'job not found' and carries no seed_id at all — the root job id the chain is named for",
    ),
    ("remediate_runaway_saga_success", "get_dag_state", "nodes", "canned_only_field"): (
        POST_FAULT,
        "create_stuck_dag seeds the runaway-saga-eval chain and derives its ids from "
        "the chain_name, so the un-faulted world the check probes answers "
        "'job not found' and carries no nodes at all — the completed "
        "upstream, the dead_letter root, and the waiting descendant",
    ),
    ("remediate_runaway_saga_success", "get_dag_state", "edges", "canned_only_field"): (
        POST_FAULT,
        "create_stuck_dag seeds the runaway-saga-eval chain and derives its ids from "
        "the chain_name, so the un-faulted world the check probes answers "
        "'job not found' and carries no edges at all — the dependency edges between them",
    ),
    ("remediate_runaway_saga_success", "get_dag_state", "paused", "canned_only_field"): (
        POST_FAULT,
        "create_stuck_dag seeds the runaway-saga-eval chain and derives its ids from "
        "the chain_name, so the un-faulted world the check probes answers "
        "'job not found' and carries no paused at all — the chain's pause flag",
    ),
    ("remediate_runaway_saga_success", "get_dag_state", "paused_by", "canned_only_field"): (
        POST_FAULT,
        "create_stuck_dag seeds the runaway-saga-eval chain and derives its ids from "
        "the chain_name, so the un-faulted world the check probes answers "
        "'job not found' and carries no paused_by at all — the pause's holder",
    ),
    (
        "remediate_runaway_saga_success",
        "get_dag_state",
        "paused_expires_in_seconds",
        "canned_only_field",
    ): (
        POST_FAULT,
        "create_stuck_dag seeds the runaway-saga-eval chain and derives its ids from "
        "the chain_name, so the un-faulted world the check probes answers "
        "'job not found' and carries no paused_expires_in_seconds at all — the pause's countdown",
    ),
    ("saga_stuck", "get_dag_state", "seed_id", "canned_only_field"): (
        POST_FAULT,
        "create_stuck_dag seeds the saga-stuck-eval chain and derives its ids from "
        "the chain_name, so the un-faulted world the check probes answers "
        "'job not found' and carries no seed_id at all — the root job id the chain is named for",
    ),
    ("saga_stuck", "get_dag_state", "nodes", "canned_only_field"): (
        POST_FAULT,
        "create_stuck_dag seeds the saga-stuck-eval chain and derives its ids from "
        "the chain_name, so the un-faulted world the check probes answers "
        "'job not found' and carries no nodes at all — the completed "
        "upstream, the dead_letter root, and the waiting descendant",
    ),
    ("saga_stuck", "get_dag_state", "edges", "canned_only_field"): (
        POST_FAULT,
        "create_stuck_dag seeds the saga-stuck-eval chain and derives its ids from "
        "the chain_name, so the un-faulted world the check probes answers "
        "'job not found' and carries no edges at all — the dependency edges between them",
    ),
    ("saga_stuck", "get_dag_state", "paused", "canned_only_field"): (
        POST_FAULT,
        "create_stuck_dag seeds the saga-stuck-eval chain and derives its ids from "
        "the chain_name, so the un-faulted world the check probes answers "
        "'job not found' and carries no paused at all — the chain's pause flag",
    ),
    ("saga_stuck", "get_dag_state", "paused_by", "canned_only_field"): (
        POST_FAULT,
        "create_stuck_dag seeds the saga-stuck-eval chain and derives its ids from "
        "the chain_name, so the un-faulted world the check probes answers "
        "'job not found' and carries no paused_by at all — the pause's holder",
    ),
    ("saga_stuck", "get_dag_state", "paused_expires_in_seconds", "canned_only_field"): (
        POST_FAULT,
        "create_stuck_dag seeds the saga-stuck-eval chain and derives its ids from "
        "the chain_name, so the un-faulted world the check probes answers "
        "'job not found' and carries no paused_expires_in_seconds at all — the pause's countdown",
    ),
    # The `list_dlq_messages` recordings for the same two saga scenarios, and
    # the same mechanism one tool over. ADR 0027 made the agent read the
    # chain root's DEAD-LETTER ROW before replaying it — `get_dag_state`
    # carries no `remediation_hint`, so the listing is the only place that
    # answer exists — which means both scenarios now record a listing, and
    # both listings contain a row that `create_stuck_dag` creates.
    #
    # The chain root IS a dead-letter row: the hook inserts it with
    # `status=dead_letter`, `retry_count=3` and the hint its argument names.
    # So the faulted world holds five rows (the four boot-seeded ones plus
    # the root) and the un-faulted world the check probes holds four. Every
    # entry below is that one fact seen through a different field:
    #
    #   * `total` — five against a live four;
    #   * `items[].id[]` — the root's uuid5-derived id is in no un-faulted
    #     reading, exactly as its `get_dag_state.seed_id` is not;
    #   * `items[].trace_id[]` — the hook derives the trace id from the same
    #     namespace and chain_name, so it appears and disappears with the row.
    #
    # POST_FAULT, not a defect, and specifically NOT to be "fixed" by
    # trimming the fixtures back to four rows: the root row is the evidence
    # the replay-safety claim is made of, and a four-row recording would
    # describe a world in which the scenario's own premise is false. Same
    # reading as `remediate_dlq_backlog_success`'s `total` above, which is
    # the poison row counted but not named; here the row can be named,
    # because the hook derives its id deterministically.
    ("remediate_runaway_saga_success", "list_dlq_messages", "total", "value"): (
        POST_FAULT,
        "create_stuck_dag dead-letters the runaway-saga-eval chain root, so the "
        "faulted world holds five DLQ rows where the un-faulted world the check "
        "probes holds the four boot-seeded ones",
    ),
    ("remediate_runaway_saga_success", "list_dlq_messages", "items[].id[]", "not_live_reachable"): (
        POST_FAULT,
        "the chain root's id is uuid5-derived from the chain_name and exists only "
        "after create_stuck_dag has fired, so no un-faulted reading of the DLQ "
        "contains it — the same absence already recorded for its get_dag_state.seed_id",
    ),
    (
        "remediate_runaway_saga_success",
        "list_dlq_messages",
        "items[].trace_id[]",
        "not_live_reachable",
    ): (
        POST_FAULT,
        "create_stuck_dag stamps the root's trace_id from the same namespace and "
        "chain_name, so it appears and disappears with the row itself",
    ),
    ("saga_stuck", "list_dlq_messages", "total", "value"): (
        POST_FAULT,
        "create_stuck_dag dead-letters the saga-stuck-eval chain root, so the "
        "faulted world holds five DLQ rows where the un-faulted world the check "
        "probes holds the four boot-seeded ones",
    ),
    ("saga_stuck", "list_dlq_messages", "items[].id[]", "not_live_reachable"): (
        POST_FAULT,
        "the chain root's id is uuid5-derived from the chain_name and exists only "
        "after create_stuck_dag has fired, so no un-faulted reading of the DLQ "
        "contains it — the same absence already recorded for its get_dag_state.seed_id",
    ),
    ("saga_stuck", "list_dlq_messages", "items[].trace_id[]", "not_live_reachable"): (
        POST_FAULT,
        "create_stuck_dag stamps the root's trace_id from the same namespace and "
        "chain_name, so it appears and disappears with the row itself",
    ),
    # `dlq_human_required_escalates` joined the seeds-its-own-fault family at
    # the v0.6.2 re-pin, and its two entries are the same mechanism as the two
    # saga blocks above seen through a third hook.
    #
    # It declared no `chaos_setup` until now: it ran against the four
    # boot-seeded rows and fenced the seeded `human_required` one. That row
    # already carried the value the fence would set, and through v0.6.1 the
    # platform's mark on such a row wrote nothing at all — so the drill could
    # not tell an agent that fenced from one that skipped the step (LESSONS
    # 2026-09-08). Platform v0.6.2 (plat #198) added
    # `create_bad_data_job(remediation_hint=unclassified)`, which writes a
    # dead-letter row with `remediation_hint = NULL` and a bad-data error text,
    # so the scenario now manufactures its own subject and the fence is a real
    # write with a real audit row.
    #
    # The faulted world therefore holds five rows where the un-faulted world
    # the check probes holds four, and both entries below are that one fact
    # seen through a different field. Both sequence elements of the listing
    # fixture (pre-fence and post-fence) report them; `Drift.key` does not
    # include the index, so two keys cover four observations.
    #
    # POST_FAULT, not a defect, and specifically NOT to be "fixed" by trimming
    # the recording back to four rows: the chaos row IS the incident, and a
    # four-row recording would describe a world in which the scenario's own
    # premise is false. Same reading as the saga roots above.
    #
    # Note what is NOT here, because it is the part that took work: the
    # `fenced_at` / `fenced_by` that plat #198 added to every `DlqEntry`
    # produce no drift at all. On the four seeded rows they are null on both
    # sides, and on the post-fence recording they are exempted as leaf values
    # by `fixture_drift._VOLATILE` (a clock, and a per-boot principal id).
    # Their PRESENCE is still compared — which is how this re-pin found the 30
    # canned rows that had to be re-recorded.
    ("dlq_human_required_escalates", "list_dlq_messages", "total", "value"): (
        POST_FAULT,
        "create_bad_data_job injects the unclassified bad-data row this scenario "
        "exists to fence, so the faulted world holds five DLQ rows where the "
        "un-faulted world the check probes holds the four boot-seeded ones",
    ),
    (
        "dlq_human_required_escalates",
        "list_dlq_messages",
        "items[].id[]",
        "not_live_reachable",
    ): (
        POST_FAULT,
        "the chaos row's id is uuid5(dddddddd-bad0-4000-8000-000000000000, "
        "'{tenant_id}:human-required-eval') and exists only after "
        "create_bad_data_job has fired, so no un-faulted reading of the DLQ "
        "contains it — the same absence already recorded for the two chain roots",
    ),
    # `poison_message` joins the family at the v0.6.3 re-pin, through TWO
    # scenarios, and the pair is worth reading together because the same row
    # produces the entries for opposite reasons.
    #
    # The mechanism is the one the two saga blocks and the bad-data block above
    # already record: a scenario seeds a fault, the drift walk probes the
    # un-faulted world, and the row the fault creates is in one and not the
    # other. What is new is that the row can now be NAMED. Through v0.6.2
    # `poison_message` minted a random id per call, so an `items[].id[]` entry
    # was impossible and the recordings could not include the row at all —
    # `remediate_dlq_backlog_success` counted a fifth row in `total` and listed
    # four, and its comment said so. v0.6.3 derives the id
    # (`uuid5(eeeeeeee-dead-4000-8000-000000000000, "{tenant_id}:poison-message")`),
    # so both fixtures now record the row itself and both report its absence
    # from the un-faulted queue.
    #
    # POST_FAULT, not a defect, and specifically NOT to be "fixed" by trimming
    # the recordings back to four rows: the poisoned row IS the incident in one
    # scenario and the row the other one must not touch, and a four-row
    # recording would describe a world in which neither premise is true.
    #
    # `Drift.key` carries no sequence index, so one key covers every element of
    # a sequenced fixture — three elements in `remediate_dlq_backlog_success`,
    # two in `dlq_poison_unclassified`.
    #
    # What is NOT here: `remediate_dlq_backlog_success`'s `total`, which is
    # already recorded above and stayed recorded through the re-derivation (the
    # faulted queue still holds five rows where the un-faulted world holds
    # four); and any entry for the poisoned row's `remediation_hint`, because
    # `walk_leaves` strips `None` from both sides before comparing — a canned
    # null against a live domain that never contains the row reports nothing.
    # Whether the hint LANDED null is a scenario claim (the precondition), not
    # a ratchet claim.
    (
        "remediate_dlq_backlog_success",
        "list_dlq_messages",
        "items[].id[]",
        "not_live_reachable",
    ): (
        POST_FAULT,
        "the poisoned row's id is uuid5(eeeeeeee-dead-4000-8000-000000000000, "
        "'{tenant_id}:poison-message') and exists only after poison_message has "
        "fired, so no un-faulted reading of the DLQ contains it — the row this "
        "scenario forbids replaying is one the drift walk can never see",
    ),
    ("dlq_poison_unclassified", "list_dlq_messages", "total", "value"): (
        POST_FAULT,
        "poison_message injects the unclassified row this scenario exists to fence, "
        "so the faulted world holds five DLQ rows where the un-faulted world the "
        "check probes holds the four boot-seeded ones",
    ),
    (
        "dlq_poison_unclassified",
        "list_dlq_messages",
        "items[].id[]",
        "not_live_reachable",
    ): (
        POST_FAULT,
        "the poisoned row's id is uuid5(eeeeeeee-dead-4000-8000-000000000000, "
        "'{tenant_id}:poison-message') and exists only after poison_message has "
        "fired — the same absence already recorded for the two chain roots and the "
        "bad-data row",
    ),
    # alert_storm went the other way at wave-10: it is `use_live_mcp: false`
    # now, because the pinned platform cannot burst alerts. Alerts have three
    # producers (the bad_deploy chaos hook, the SLO fast-burn loop, the boot
    # seed) and none of them emits more than one at a time, so a storm is
    # unmanufacturable and the scenario's five-alert recording is its premise
    # rather than a recording of anything. These seven were counted as work
    # for the whole campaign and could never have come off the list: there is
    # no live reading for a canned-only scenario to be corrected towards.
    ("alert_storm", "list_active_alerts", "alerts[].description", "live_only_field"): (
        CANNED_ONLY,
        "use_live_mcp is false — the platform cannot burst alerts, so the "
        "scenario never runs live and its recordings are its premise",
    ),
    ("alert_storm", "list_active_alerts", "alerts[].extra_data", "live_only_field"): (
        CANNED_ONLY,
        "same scenario, same premise",
    ),
    ("alert_storm", "list_active_alerts", "alerts[].id[]", "not_live_reachable"): (
        CANNED_ONLY,
        "same scenario, same premise: the fabricated a1..a5 ids name alerts "
        "the platform never fired",
    ),
    ("alert_storm", "list_active_alerts", "alerts[].source[]", "not_live_reachable"): (
        CANNED_ONLY,
        "same scenario, same premise: the storm's five sources are invented",
    ),
    ("alert_storm", "list_active_alerts", "total", "value"): (
        CANNED_ONLY,
        "same scenario, same premise: the live platform holds three steady "
        "active alerts, not a storm of five",
    ),
    ("alert_storm", "list_audit_events", "events[]", "no_live_rows"): (
        CANNED_ONLY,
        "same scenario, same premise: the deploy.completed event the storm "
        "correlates to is part of the invented narrative",
    ),
    ("alert_storm", "list_audit_events", "total", "value"): (
        CANNED_ONLY,
        "same scenario, same premise: the audit total counts events from that invented narrative",
    ),
    ("remediate_verify_fails", "get_consumer_lag", "lag", "value"): (
        CANNED_ONLY,
        "use_live_mcp is false — the scenario never runs live, so its canned "
        "responses are its premise rather than a recording of anything",
    ),
    ("tool_output_schema_mismatch", "get_consumer_lag", "lag", "value"): (
        CANNED_ONLY,
        "the scenario exists to feed the agent a malformed response; its "
        "fixture is deliberately not what the platform returns",
    ),
    ("tool_output_schema_mismatch", "get_consumer_lag", "cache_key", "live_only_field"): (
        CANNED_ONLY,
        "same scenario, same deliberate malformation",
    ),
    # v0.6.0 added two more required fields to this tool's output, so the
    # same deliberately-malformed fixture is now short three fields rather
    # than one. The scenario's premise did not change and neither did the
    # reason: a response that violates the schema is what it exists to feed
    # the agent, so "the fixture does not match the platform" is the fixture
    # working. Recorded, not counted as work.
    ("tool_output_schema_mismatch", "get_consumer_lag", "lag_known", "live_only_field"): (
        CANNED_ONLY,
        "same scenario, same deliberate malformation — v0.6.0 made this a "
        "third field the fixture deliberately omits",
    ),
    ("tool_output_schema_mismatch", "get_consumer_lag", "source", "live_only_field"): (
        CANNED_ONLY,
        "same scenario, same deliberate malformation — v0.6.0 made this a "
        "third field the fixture deliberately omits",
    ),
}


@dataclass(frozen=True)
class LedgerEntry:
    """One recorded disagreement and why it is (or is not) work."""

    key: DriftKey
    context: str
    why: str

    @property
    def is_defect(self) -> bool:
        return self.context == FIXTURE_DEFECT


def context_of(key: DriftKey) -> tuple[str, str]:
    """``(context, why)`` for one drift key. Unjustified means it is work."""
    return _JUSTIFIED.get(key, (FIXTURE_DEFECT, ""))


def load_ledger(path: Path | None = None) -> frozenset[DriftKey]:
    """The recorded drift keys. A missing ledger is an empty one — strictest."""
    return frozenset(entry.key for entry in load_entries(path))


def load_entries(path: Path | None = None) -> list[LedgerEntry]:
    """Recorded drift with its context, newest format or the original arrays.

    Context comes from ``context_of`` for EVERY row, whichever format it is
    written in. It used to come from the file for dict rows and from the
    code for array rows, which made ``defect_count`` answer differently
    depending on when a row was written: absolving an entry in ``_JUSTIFIED``
    left the burn-down number unmoved, because the number was reading the
    file's copy of a decision the code had already changed. The code is the
    authority on classification and the file records it — a disagreement
    between the two means the file is stale, which is a re-bless, and
    ``test_the_committed_contexts_agree_with_the_code`` is what says so.
    """
    target = path or LEDGER_PATH
    if not target.exists():
        return []
    payload = json.loads(target.read_text())
    entries: list[LedgerEntry] = []
    for row in payload.get("known_drift", []):
        if isinstance(row, list) and len(row) == 4:
            key = (str(row[0]), str(row[1]), str(row[2]), str(row[3]))
        elif isinstance(row, dict):
            key = (
                str(row["scenario"]),
                str(row["tool"]),
                str(row["path"]),
                str(row["kind"]),
            )
        else:
            continue
        context, why = context_of(key)
        entries.append(LedgerEntry(key=key, context=context, why=why))
    return entries


def defect_count(path: Path | None = None) -> int:
    """Recorded disagreements that are actually work — the burn-down number.

    The others are recorded because the check will keep reporting them, not
    because anyone should go and "fix" them. Counting them as work would set
    a target that cannot be reached, and the first person to try would break
    a scenario making its fixture match a world it was never describing.
    """
    return sum(1 for entry in load_entries(path) if entry.is_defect)


def split_for_bless(
    observed: Collection[DriftKey],
    prior: Collection[DriftKey],
    checked: Collection[tuple[str, str]],
) -> tuple[tuple[DriftKey, ...], tuple[DriftKey, ...]]:
    """``(carried, disproved)`` for the prior entries this run did not observe.

    An entry is DISPROVED only when this run actually probed its fixture and
    found no disagreement — that is the ratchet turning, and its line has to
    go. An entry whose fixture the run never reached is CARRIED: the run
    took no reading, so it holds no opinion, and deleting on no opinion is
    how a transient 502 quietly shortens the burn-down list.

    ``checked`` is per ``(scenario, tool)`` because that is the unit the
    probe reports coverage in; the ledger's finer ``(path, kind)`` split is
    within one reading of one fixture.
    """
    reached = set(checked)
    seen = set(observed)
    unobserved = [key for key in prior if key not in seen]
    carried = tuple(sorted(key for key in unobserved if (key[0], key[1]) not in reached))
    disproved = tuple(sorted(key for key in unobserved if (key[0], key[1]) in reached))
    return carried, disproved


def dump_ledger(
    drifts: Iterable[Drift],
    path: Path | None = None,
    *,
    checked: Collection[tuple[str, str]] = (),
) -> int:
    """Write the ledger from an observed drift set. Returns the entry count.

    ``checked`` is the coverage the run established — the ``(scenario,
    tool)`` pairs it actually read back from the platform. Entries this run
    did not cover are carried over rather than dropped, so a bless can only
    remove an entry it disproved. The default is the conservative one: a
    caller that says nothing about coverage has established nothing and may
    delete nothing.

    Keys this module does not own are preserved verbatim. ``_blessed_against``
    is the one that matters — it records which platform state the file was
    blessed against, and so whether a local disagreement is about the
    fixtures or about a developer's postgres volume — and every bless used
    to drop it.
    """
    target = path or LEDGER_PATH
    existing: dict[str, Any] = {}
    if target.exists():
        loaded = json.loads(target.read_text())
        if isinstance(loaded, dict):
            existing = loaded
    observed = {drift.key for drift in drifts}
    carried, _disproved = split_for_bless(
        observed, [entry.key for entry in load_entries(target)], checked
    )
    keys = sorted(observed | set(carried))
    rows = []
    for key in keys:
        context, why = context_of(key)
        row: dict[str, object] = {
            "scenario": key[0],
            "tool": key[1],
            "path": key[2],
            "kind": key[3],
            "context": context,
        }
        if why:
            row["why"] = why
        rows.append(row)
    defects = sum(1 for row in rows if row["context"] == FIXTURE_DEFECT)
    payload: dict[str, Any] = {
        **existing,
        **{
            "_comment": (
                "Known canned-vs-live fixture drift, recorded when the drift check "
                "was introduced. This file may only SHRINK: evals/fixture_drift_ledger.py "
                "fails on drift not listed here AND on entries listed here that are no "
                "longer observed. Regenerate with `make fixture-drift-bless` against the "
                "pinned platform; never hand-edit."
            ),
            "_context": {
                FIXTURE_DEFECT: "the recording is wrong — this is work",
                POST_FAULT: (
                    "the scenario seeds a fault and the check probes the un-faulted "
                    "world; not a defect and must not be 'fixed'"
                ),
                POST_ACTION: (
                    "the fixture element records the world after the agent's own "
                    "remediation and the check probes the world before it; "
                    "unfixable by construction and must not be 'fixed'"
                ),
                CANNED_ONLY: (
                    "the scenario never runs live, so its recordings are its premise "
                    "rather than a recording of anything"
                ),
            },
            "_counts": {
                "recorded": len(rows),
                FIXTURE_DEFECT: defects,
                "explained": len(rows) - defects,
            },
            "known_drift": rows,
        },
    }
    target.write_text(json.dumps(payload, indent=2) + "\n")
    return len(keys)


def classify(
    drifts: Iterable[Drift], ledger: frozenset[DriftKey]
) -> tuple[tuple[Drift, ...], tuple[DriftKey, ...]]:
    """Split observed drift into ``(new, stale_ledger_entries)``.

    ``new`` is drift the ledger does not record — the check's actual subject.
    ``stale`` is recorded drift that no longer occurs, which means a fixture
    was fixed and its line here has to go.
    """
    observed = {drift.key for drift in drifts}
    new = tuple(drift for drift in drifts if drift.key not in ledger)
    stale = tuple(sorted(ledger - observed))
    return new, stale

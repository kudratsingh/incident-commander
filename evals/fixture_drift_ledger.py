"""The known-drift ledger: a ratchet, not an allowlist.

Every canned fixture predates the check that compares it to the platform, so the
drift that existed at introduction is recorded here and only UNRECORDED drift
fails. The ratchet is the second rule: an entry that is no longer observed fails
too, with an instruction to delete it, so the ledger can only shrink. Keyed by
``(scenario, tool, path, kind)`` with no observed values — re-blessing on every
gauge wobble is how this class of guard dies. Regenerate with
``make fixture-drift-bless``, never by hand: it needs a live platform.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from evals.fixture_drift import Drift

LEDGER_PATH: Final[Path] = Path(__file__).resolve().parent / "fixture-drift-ledger.json"

DriftKey = tuple[str, str, str, str] | tuple[str, str, str, str, int]

# Contexts. Only the first is work.
FIXTURE_DEFECT: Final = "fixture-defect"
POST_FAULT: Final = "post-fault"
POST_ACTION: Final = "post-action"
CANNED_ONLY: Final = "canned-only"
#: The recording is of a WARM stack and the check ran on a cold one (WO-R3-202):
#: `worker-dispatcher` has no measurement for about a minute after boot, so a fresh
#: platform answers `lag: null, lag_known: false` where the recording says `0, true`.
#: `_VOLATILE` forgives the `lag_known` half and deliberately not `lag`. Its own word
#: rather than POST_FAULT because no hook, agent, seeding or reset changes platform
#: uptime — calling it post-fault would claim a mechanism that is not there.
COLD_STACK: Final = "cold-stack"
WARM_STACK: Final = "warm-stack"
#: The fixture describes a fault NO LAB HOOK CAN PRODUCE (WO-R3-217, v0.6.11):
#: `postgres_slow` is about queries running long, and nothing in the chaos surface
#: makes a query slow — `saturate_db_pool` holds connections, which is the other
#: fault. So the reading is the scenario's premise and the check probes a world
#: that never had it. Its own word rather than POST_FAULT, for COLD_STACK's
#: reason: POST_FAULT claims the scenario SEEDS the fault, and naming a hook that
#: does not exist is how a ledger stops being readable. Not CANNED_ONLY either —
#: that one means `use_live_mcp` is false, and this scenario's other reads do run
#: live. Not work on the fixture; the work, if this scenario is ever to be run
#: live, is a platform hook.
NO_HOOK: Final = "no-hook"

# Entries that are NOT fixture defects, each with the claim that makes it so.
# Hand-recorded, not inferred: the obvious rule ("a scenario that seeds a fault gets
# a pass") would have absolved a fixture claiming 1.00G of Redis memory against a
# live 1.60M, which `create_stale_cache`'s one key cannot explain. Wrongly absolving
# deletes work forever, so the bar is a specific mechanism, named.
_JUSTIFIED: Final[dict[tuple[object, ...], tuple[str, str]]] = {
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
    # The `jobs_not_progressing` family (WO-R3-202, WP-4.3), four named mechanisms.
    # What is NOT here is the point: every other `get_outbox_status` field is either
    # volatile or matches live with no entry. `unpublished_count` is the one the
    # outbox scenarios grade, so it stays guarded and its disagreement is written down.
    ("jobs_not_progressing_dispatcher_stall", "get_consumer_lag", "lag", "value"): (
        POST_FAULT,
        "kill_consumer makes worker-dispatcher's lag climb; the check probes "
        "the un-faulted world, so the canned backlog cannot match by design — "
        "the same mechanism as consumer_lag_high, in this family's world. The "
        "second element of the sequence is the post-restart read and shares "
        "this key (the ledger excludes the index on purpose)",
    ),
    ("jobs_not_progressing_dispatcher_stall", "get_outbox_status", "unpublished_count", "value"): (
        POST_FAULT,
        "this scenario's premise needs a producer running (lag is arrival minus "
        "service and kill_consumer supplies only the service half), so its "
        "recording caught one event between its commit and the relay's next "
        "tick. The check probes the un-faulted world, which has no producer and "
        "reads 0. Both satisfy the scenario's own claim, `at_most 5`",
    ),
    ("jobs_not_progressing_outbox_stall", "get_outbox_status", "unpublished_count", "value"): (
        POST_FAULT,
        "pause_control_loop(outbox_relay) stops the relay, so committed events "
        "accumulate undelivered; that queue IS the fault, and the check probes "
        "the world before the hook fires, where it is empty. Both elements of "
        "the sequence (11 growing to 19) share this key",
    ),
    (
        "jobs_not_progressing_outbox_stall_deploy_noise",
        "get_outbox_status",
        "unpublished_count",
        "value",
    ): (
        POST_FAULT,
        "same hook, same world, same reason — this scenario differs from its "
        "quiet sibling only in the alert it hands the agent",
    ),
    # The `workflow_stuck` family (WO-R3-214, WP-7.2, ADR 0053). All four worlds are
    # ONE chain — `create_stuck_dag(chain_name="workflow-stuck-eval")` — whose ids are
    # derived from that name, so in the un-faulted world the job the alert names DOES
    # NOT EXIST: `get_dag_state` answers "job not found" and the key-set diff turns an
    # absent document into six `canned_only_field` rows, not one. Same mechanism and
    # deliberately the same wording as the saga blocks below. A sequenced fixture gets
    # ONE line (the key carries no index), with both halves in its `why`.
    ("workflow_stuck_dead_lettered_root", "get_dag_state", "edges", "canned_only_field"): (
        POST_FAULT,
        "create_stuck_dag seeds the workflow-stuck-eval chain and derives its ids from the "
        "chain_name, so the un-faulted world the check probes answers 'job not found' and "
        "carries no edges at all — the dependency edges between them. This world is the "
        "root dead-lettered with its retries exhausted, and its fixture is sequenced: "
        "element 0 is the investigation probe and element 1 the post-replay verify poll, "
        "which no zero-LLM pass can record because make world-record never acts. Both "
        "elements are absent from the un-faulted reading for the reason above, so both "
        "share this row",
    ),
    ("workflow_stuck_dead_lettered_root", "get_dag_state", "nodes", "canned_only_field"): (
        POST_FAULT,
        "create_stuck_dag seeds the workflow-stuck-eval chain and derives its ids from the "
        "chain_name, so the un-faulted world the check probes answers 'job not found' and "
        "carries no nodes at all — the completed upstream, the root, and the descendants "
        "behind it. This world is the root dead-lettered with its retries exhausted, and "
        "its fixture is sequenced: element 0 is the investigation probe and element 1 the "
        "post-replay verify poll, which no zero-LLM pass can record because make "
        "world-record never acts. Both elements are absent from the un-faulted reading for "
        "the reason above, so both share this row",
    ),
    ("workflow_stuck_dead_lettered_root", "get_dag_state", "paused", "canned_only_field"): (
        POST_FAULT,
        "create_stuck_dag seeds the workflow-stuck-eval chain and derives its ids from the "
        "chain_name, so the un-faulted world the check probes answers 'job not found' and "
        "carries no paused at all — the chain's pause flag. This world is the root "
        "dead-lettered with its retries exhausted, and its fixture is sequenced: element 0 "
        "is the investigation probe and element 1 the post-replay verify poll, which no "
        "zero-LLM pass can record because make world-record never acts. Both elements are "
        "absent from the un-faulted reading for the reason above, so both share this row",
    ),
    ("workflow_stuck_dead_lettered_root", "get_dag_state", "paused_by", "canned_only_field"): (
        POST_FAULT,
        "create_stuck_dag seeds the workflow-stuck-eval chain and derives its ids from the "
        "chain_name, so the un-faulted world the check probes answers 'job not found' and "
        "carries no paused_by at all — the pause's holder. This world is the root "
        "dead-lettered with its retries exhausted, and its fixture is sequenced: element 0 "
        "is the investigation probe and element 1 the post-replay verify poll, which no "
        "zero-LLM pass can record because make world-record never acts. Both elements are "
        "absent from the un-faulted reading for the reason above, so both share this row",
    ),
    (
        "workflow_stuck_dead_lettered_root",
        "get_dag_state",
        "paused_expires_in_seconds",
        "canned_only_field",
    ): (
        POST_FAULT,
        "create_stuck_dag seeds the workflow-stuck-eval chain and derives its ids from the "
        "chain_name, so the un-faulted world the check probes answers 'job not found' and "
        "carries no paused_expires_in_seconds at all — the pause's countdown. This world is "
        "the root dead-lettered with its retries exhausted, and its fixture is sequenced: "
        "element 0 is the investigation probe and element 1 the post-replay verify poll, "
        "which no zero-LLM pass can record because make world-record never acts. Both "
        "elements are absent from the un-faulted reading for the reason above, so both "
        "share this row",
    ),
    ("workflow_stuck_dead_lettered_root", "get_dag_state", "seed_id", "canned_only_field"): (
        POST_FAULT,
        "create_stuck_dag seeds the workflow-stuck-eval chain and derives its ids from the "
        "chain_name, so the un-faulted world the check probes answers 'job not found' and "
        "carries no seed_id at all — the root job id the chain is named for. This world is "
        "the root dead-lettered with its retries exhausted, and its fixture is sequenced: "
        "element 0 is the investigation probe and element 1 the post-replay verify poll, "
        "which no zero-LLM pass can record because make world-record never acts. Both "
        "elements are absent from the un-faulted reading for the reason above, so both "
        "share this row",
    ),
    ("workflow_stuck_resolver_stall", "get_dag_state", "edges", "canned_only_field"): (
        POST_FAULT,
        "create_stuck_dag seeds the workflow-stuck-eval chain and derives its ids from the "
        "chain_name, so the un-faulted world the check probes answers 'job not found' and "
        "carries no edges at all — the dependency edges between them. This world is the "
        "root completed over a waiting descendant",
    ),
    ("workflow_stuck_resolver_stall", "get_dag_state", "nodes", "canned_only_field"): (
        POST_FAULT,
        "create_stuck_dag seeds the workflow-stuck-eval chain and derives its ids from the "
        "chain_name, so the un-faulted world the check probes answers 'job not found' and "
        "carries no nodes at all — the completed upstream, the root, and the descendants "
        "behind it. This world is the root completed over a waiting descendant",
    ),
    ("workflow_stuck_resolver_stall", "get_dag_state", "paused", "canned_only_field"): (
        POST_FAULT,
        "create_stuck_dag seeds the workflow-stuck-eval chain and derives its ids from the "
        "chain_name, so the un-faulted world the check probes answers 'job not found' and "
        "carries no paused at all — the chain's pause flag. This world is the root "
        "completed over a waiting descendant",
    ),
    ("workflow_stuck_resolver_stall", "get_dag_state", "paused_by", "canned_only_field"): (
        POST_FAULT,
        "create_stuck_dag seeds the workflow-stuck-eval chain and derives its ids from the "
        "chain_name, so the un-faulted world the check probes answers 'job not found' and "
        "carries no paused_by at all — the pause's holder. This world is the root completed "
        "over a waiting descendant",
    ),
    (
        "workflow_stuck_resolver_stall",
        "get_dag_state",
        "paused_expires_in_seconds",
        "canned_only_field",
    ): (
        POST_FAULT,
        "create_stuck_dag seeds the workflow-stuck-eval chain and derives its ids from the "
        "chain_name, so the un-faulted world the check probes answers 'job not found' and "
        "carries no paused_expires_in_seconds at all — the pause's countdown. This world is "
        "the root completed over a waiting descendant",
    ),
    ("workflow_stuck_resolver_stall", "get_dag_state", "seed_id", "canned_only_field"): (
        POST_FAULT,
        "create_stuck_dag seeds the workflow-stuck-eval chain and derives its ids from the "
        "chain_name, so the un-faulted world the check probes answers 'job not found' and "
        "carries no seed_id at all — the root job id the chain is named for. This world is "
        "the root completed over a waiting descendant",
    ),
    ("workflow_stuck_paused_dag", "get_dag_state", "edges", "canned_only_field"): (
        POST_FAULT,
        "create_stuck_dag seeds the workflow-stuck-eval chain and derives its ids from the "
        "chain_name, so the un-faulted world the check probes answers 'job not found' and "
        "carries no edges at all — the dependency edges between them. This world is the "
        "same chain held by a pause",
    ),
    ("workflow_stuck_paused_dag", "get_dag_state", "nodes", "canned_only_field"): (
        POST_FAULT,
        "create_stuck_dag seeds the workflow-stuck-eval chain and derives its ids from the "
        "chain_name, so the un-faulted world the check probes answers 'job not found' and "
        "carries no nodes at all — the completed upstream, the root, and the descendants "
        "behind it. This world is the same chain held by a pause",
    ),
    ("workflow_stuck_paused_dag", "get_dag_state", "paused", "canned_only_field"): (
        POST_FAULT,
        "create_stuck_dag seeds the workflow-stuck-eval chain and derives its ids from the "
        "chain_name, so the un-faulted world the check probes answers 'job not found' and "
        "carries no paused at all — the chain's pause flag. This world is the same chain "
        "held by a pause",
    ),
    ("workflow_stuck_paused_dag", "get_dag_state", "paused_by", "canned_only_field"): (
        POST_FAULT,
        "create_stuck_dag seeds the workflow-stuck-eval chain and derives its ids from the "
        "chain_name, so the un-faulted world the check probes answers 'job not found' and "
        "carries no paused_by at all — the pause's holder. This world is the same chain "
        "held by a pause",
    ),
    (
        "workflow_stuck_paused_dag",
        "get_dag_state",
        "paused_expires_in_seconds",
        "canned_only_field",
    ): (
        POST_FAULT,
        "create_stuck_dag seeds the workflow-stuck-eval chain and derives its ids from the "
        "chain_name, so the un-faulted world the check probes answers 'job not found' and "
        "carries no paused_expires_in_seconds at all — the pause's countdown. This world is "
        "the same chain held by a pause",
    ),
    ("workflow_stuck_paused_dag", "get_dag_state", "seed_id", "canned_only_field"): (
        POST_FAULT,
        "create_stuck_dag seeds the workflow-stuck-eval chain and derives its ids from the "
        "chain_name, so the un-faulted world the check probes answers 'job not found' and "
        "carries no seed_id at all — the root job id the chain is named for. This world is "
        "the same chain held by a pause",
    ),
    ("workflow_stuck_healthy_chain", "get_dag_state", "edges", "canned_only_field"): (
        POST_FAULT,
        "create_stuck_dag seeds the workflow-stuck-eval chain and derives its ids from the "
        "chain_name, so the un-faulted world the check probes answers 'job not found' and "
        "carries no edges at all — the dependency edges between them. This world is the "
        "same chain drained to completed",
    ),
    ("workflow_stuck_healthy_chain", "get_dag_state", "nodes", "canned_only_field"): (
        POST_FAULT,
        "create_stuck_dag seeds the workflow-stuck-eval chain and derives its ids from the "
        "chain_name, so the un-faulted world the check probes answers 'job not found' and "
        "carries no nodes at all — the completed upstream, the root, and the descendants "
        "behind it. This world is the same chain drained to completed",
    ),
    ("workflow_stuck_healthy_chain", "get_dag_state", "paused", "canned_only_field"): (
        POST_FAULT,
        "create_stuck_dag seeds the workflow-stuck-eval chain and derives its ids from the "
        "chain_name, so the un-faulted world the check probes answers 'job not found' and "
        "carries no paused at all — the chain's pause flag. This world is the same chain "
        "drained to completed",
    ),
    ("workflow_stuck_healthy_chain", "get_dag_state", "paused_by", "canned_only_field"): (
        POST_FAULT,
        "create_stuck_dag seeds the workflow-stuck-eval chain and derives its ids from the "
        "chain_name, so the un-faulted world the check probes answers 'job not found' and "
        "carries no paused_by at all — the pause's holder. This world is the same chain "
        "drained to completed",
    ),
    (
        "workflow_stuck_healthy_chain",
        "get_dag_state",
        "paused_expires_in_seconds",
        "canned_only_field",
    ): (
        POST_FAULT,
        "create_stuck_dag seeds the workflow-stuck-eval chain and derives its ids from the "
        "chain_name, so the un-faulted world the check probes answers 'job not found' and "
        "carries no paused_expires_in_seconds at all — the pause's countdown. This world is "
        "the same chain drained to completed",
    ),
    ("workflow_stuck_healthy_chain", "get_dag_state", "seed_id", "canned_only_field"): (
        POST_FAULT,
        "create_stuck_dag seeds the workflow-stuck-eval chain and derives its ids from the "
        "chain_name, so the un-faulted world the check probes answers 'job not found' and "
        "carries no seed_id at all — the root job id the chain is named for. This world is "
        "the same chain drained to completed",
    ),
    # The dead-lettered world is the only one of the four that puts a row in the queue,
    # so the only one with `list_dlq_messages` entries — this family's discriminator
    # stated as a ledger asymmetry (platform ADR 0029 § 1).
    (
        "workflow_stuck_dead_lettered_root",
        "list_dlq_messages",
        "items[].id[]",
        "not_live_reachable",
    ): (
        POST_FAULT,
        "the chain root's id is uuid5-derived from the chain_name and exists only after "
        "create_stuck_dag has fired, so no un-faulted reading of the DLQ contains it — the "
        "same absence already recorded for its get_dag_state.seed_id",
    ),
    (
        "workflow_stuck_dead_lettered_root",
        "list_dlq_messages",
        "items[].trace_id[]",
        "not_live_reachable",
    ): (
        POST_FAULT,
        "create_stuck_dag stamps the root's trace_id from the same namespace and chain_name "
        "(3fbd32dd…, recomputed offline by the family's own test), so it appears and "
        "disappears with the row itself",
    ),
    ("workflow_stuck_dead_lettered_root", "list_dlq_messages", "total", "value"): (
        POST_FAULT,
        "create_stuck_dag dead-letters the workflow-stuck-eval chain root, so the faulted "
        "world holds five DLQ rows where the un-faulted world the check probes holds the "
        "four boot-seeded ones. The three sibling worlds seed root_status=completed and "
        "dead-letter nothing, so their canned 4 matches live and needs no entry",
    ),
    # `search_traces(status="waiting")` in the three stranded worlds. This absence is
    # environment-wide, not scoped to the chain: platform ADR 0029 measured that a warm
    # stack has no waiting row ANYWHERE to borrow, which is why the chain is
    # manufactured at all. The un-faulted reading is not "different rows" but "none".
    (
        "workflow_stuck_dead_lettered_root",
        "search_traces",
        "matches[].job_id[]",
        "not_live_reachable",
    ): (
        POST_FAULT,
        "step-1 and step-2 are uuid5-derived from the chain_name and exist only after "
        "create_stuck_dag has fired, so neither id is in any un-faulted reading. Here they "
        "are held behind a dead-lettered root; both canned values share this row",
    ),
    (
        "workflow_stuck_dead_lettered_root",
        "search_traces",
        "matches[].status[]",
        "not_live_reachable",
    ): (
        POST_FAULT,
        "the canned value is `waiting`, and a warm un-faulted stack has no waiting row "
        "anywhere — platform ADR 0029 measured that while building these hooks. The live "
        "reading offers completed, dead_letter and failed, and none of them is a "
        "disagreement about a value: the status this fixture pins cannot be reached without "
        "the fault",
    ),
    (
        "workflow_stuck_dead_lettered_root",
        "search_traces",
        "matches[].trace_id[]",
        "not_live_reachable",
    ): (
        POST_FAULT,
        "create_stuck_dag stamps each descendant's trace_id from the same namespace and "
        "chain_name, so the two trace ids appear and disappear with the rows themselves",
    ),
    (
        "workflow_stuck_resolver_stall",
        "search_traces",
        "matches[].job_id[]",
        "not_live_reachable",
    ): (
        POST_FAULT,
        "step-1 and step-2 are uuid5-derived from the chain_name and exist only after "
        "create_stuck_dag has fired, so neither id is in any un-faulted reading. Here they "
        "are held by the killed resolver and the paused resume sweep; both canned values "
        "share this row",
    ),
    (
        "workflow_stuck_resolver_stall",
        "search_traces",
        "matches[].status[]",
        "not_live_reachable",
    ): (
        POST_FAULT,
        "the canned value is `waiting`, and a warm un-faulted stack has no waiting row "
        "anywhere — platform ADR 0029 measured that while building these hooks. The live "
        "reading offers completed, dead_letter and failed, and none of them is a "
        "disagreement about a value: the status this fixture pins cannot be reached without "
        "the fault",
    ),
    (
        "workflow_stuck_resolver_stall",
        "search_traces",
        "matches[].trace_id[]",
        "not_live_reachable",
    ): (
        POST_FAULT,
        "create_stuck_dag stamps each descendant's trace_id from the same namespace and "
        "chain_name, so the two trace ids appear and disappear with the rows themselves",
    ),
    (
        "workflow_stuck_paused_dag",
        "search_traces",
        "matches[].job_id[]",
        "not_live_reachable",
    ): (
        POST_FAULT,
        "step-1 and step-2 are uuid5-derived from the chain_name and exist only after "
        "create_stuck_dag has fired, so neither id is in any un-faulted reading. Here they "
        "are held by the DAG pause; both canned values share this row",
    ),
    (
        "workflow_stuck_paused_dag",
        "search_traces",
        "matches[].status[]",
        "not_live_reachable",
    ): (
        POST_FAULT,
        "the canned value is `waiting`, and a warm un-faulted stack has no waiting row "
        "anywhere — platform ADR 0029 measured that while building these hooks. The live "
        "reading offers completed, dead_letter and failed, and none of them is a "
        "disagreement about a value: the status this fixture pins cannot be reached without "
        "the fault",
    ),
    (
        "workflow_stuck_paused_dag",
        "search_traces",
        "matches[].trace_id[]",
        "not_live_reachable",
    ): (
        POST_FAULT,
        "create_stuck_dag stamps each descendant's trace_id from the same namespace and "
        "chain_name, so the two trace ids appear and disappear with the rows themselves",
    ),
    # The three cold-stack rows: the only entries a warm developer stack cannot
    # observe. `_blessed_against` settles which side is authoritative — this file is
    # blessed against CI's freshly seeded stack, so locally they read as "already
    # fixed" and deleting them reds CI's contract job. Each scenario's own premise is
    # the WARM reading (`lag_known equals true`), asserted before any model call, and
    # `make world-audit` checks it too, so the paid path cannot reach a cold stack.
    ("jobs_not_progressing_healthy_backlog_spike", "get_consumer_lag", "lag", "value"): (
        COLD_STACK,
        "the recording pins worker-dispatcher's MEASURED zero; a freshly seeded "
        "stack has taken no measurement yet and answers null, which lag_known "
        "declares (and _VOLATILE already forgives). The scenario's premise is the "
        "warm reading and its precondition asserts lag_known, so a cold stack "
        "abandons the run instead of grading it",
    ),
    ("jobs_not_progressing_outbox_stall", "get_consumer_lag", "lag", "value"): (
        COLD_STACK,
        "same reading, same mechanism — this scenario's hook pauses the outbox "
        "relay and never touches the lag metric, so the fault explains nothing "
        "here and post-fault would be a claim about a mechanism that is absent",
    ),
    ("jobs_not_progressing_outbox_stall_deploy_noise", "get_consumer_lag", "lag", "value"): (
        COLD_STACK,
        "same hook, same world, same reason as its quiet sibling",
    ),
    # The first POST_ACTION rows. `remediate_stale_cache_success` verifies by re-reading
    # the key it invalidated (ADR 0025), so its fixture is a sequence: element 0 the key
    # present, element 1 the key gone. The walk probes the world BEFORE the agent acts,
    # so the recording is correct about a later moment than the walk can observe —
    # post-FAULT drift would vanish if the walk ran after seeding, post-ACTION never
    # can. All three shape fields move together (`GetCacheKeyInfoOutput`: all null when
    # the key is absent); `ttl_seconds` is missing only because it is already volatile.
    ("remediate_stale_cache_success", "get_cache_key_info", "exists", "value"): (
        POST_ACTION,
        "the verify leg re-reads the invalidated key and the fixture records "
        "exists=false; the walk probes the world before the deletion, where "
        "the key is still present",
    ),
    ("remediate_stale_cache_success", "get_cache_key_info", "size", "value", 0): (
        WARM_STACK,
        "the 90-byte stale value is visible on a warm developer stack, but CI's "
        "fresh stack has not populated that cache entry and reads the fixture value; "
        "the entry is timing-scoped, not a fixture correction",
    ),
    ("remediate_stale_cache_success", "get_cache_key_info", "size", "value", 1): (
        POST_ACTION,
        "same recording, same reason: an absent key reports size=null",
    ),
    ("remediate_stale_cache_success", "get_cache_key_info", "type", "value"): (
        POST_ACTION,
        "same recording, same reason: an absent key reports type=null",
    ),
    # The two v0.6.8 record-check fields (plat #209, WO-R3-267): the first rows where
    # BOTH elements disagree for two different reasons, and one key cannot carry two
    # contexts, so each `why` names both halves. `records_found` is filed post-fault
    # because element 0's seeded 0 against a live 3 is the scenario's premise;
    # `records_referenced` post-action because only element 1 disagrees (both worlds
    # name the same three records — the fault changes whether they are still there).
    ("remediate_stale_cache_success", "get_cache_key_info", "records_found", "value"): (
        POST_FAULT,
        "create_stale_cache replaces the hot-set entry with one naming three "
        "records the database does not hold, so the fixture's 0 cannot match "
        "the un-faulted world the check probes, which answers 3; element 1 "
        "disagrees for the post-action reason beside it — an absent key "
        "reports records_found=null",
    ),
    ("remediate_stale_cache_success", "get_cache_key_info", "records_referenced", "value"): (
        POST_ACTION,
        "the verify leg re-reads the invalidated key and the fixture records "
        "records_referenced=null; the walk probes the world before the "
        "deletion, where the key is still there and names three records — "
        "which element 0 records verbatim, so only the post-action element "
        "disagrees",
    ),
    # The `get_dag_state` recordings for the two saga scenarios are one mechanism: both
    # seed their own chain with `create_stuck_dag`, whose ids are uuid5-derived from the
    # chain_name, so the un-faulted world answers `job not found` and all six top-level
    # keys read as `canned_only_field`. These twelve replaced twelve older
    # `not_live_reachable` rows once the hook landed and the scenarios began running
    # live. A fixture whose entity does not exist is now compared against an empty live
    # response rather than dropped as a probe error (see evals/fixture_probe.py), which
    # is what keeps these recorded, classified and discharge-able.
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
    # The same mechanism one tool over. ADR 0027 made the agent read the chain root's
    # DEAD-LETTER ROW before replaying it (only the listing carries
    # `remediation_hint`), and `create_stuck_dag` inserts that root as a dead-letter
    # row — so the faulted world holds five rows against the un-faulted four, seen
    # through `total`, `items[].id[]` and `items[].trace_id[]`. POST_FAULT, and NOT to
    # be "fixed" by trimming the fixtures to four rows: the root row is the evidence
    # the replay-safety claim is made of.
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
    # `dlq_human_required_escalates` joined the seeds-its-own-fault family at the v0.6.2
    # re-pin: it used to fence a boot-seeded row that already carried the value the
    # fence would set, so the drill could not tell an agent that fenced from one that
    # skipped it (LESSONS 2026-09-08). Plat #198's
    # `create_bad_data_job(remediation_hint=unclassified)` lets it manufacture its own
    # subject, so the faulted world holds five rows against four. POST_FAULT, and NOT to
    # be trimmed back: the chaos row IS the incident. The `fenced_at`/`fenced_by` those
    # rows gained produce no drift — null on both sides, or leaf-exempt in `_VOLATILE`.
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
    # `poison_message` joins the family at the v0.6.3 re-pin, through TWO scenarios. The
    # mechanism is the blocks above; what is new is that the row can be NAMED — through
    # v0.6.2 the hook minted a random id per call, so the recordings could not include
    # the row at all. POST_FAULT, and NOT to be trimmed to four rows: the poisoned row IS
    # the incident in one scenario and the row the other must not touch. No entry for its
    # `remediation_hint`: `walk_leaves` strips `None` from both sides, and whether the
    # hint LANDED null is a scenario claim.
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
    # `create_mislabeled_dlq_job` is the fifth hook in this family (WO-R2-167), same
    # mechanism. What is NOT here matters: the post-fence element records this row as
    # `human_required` and produces no drift on `items[].remediation_hint[]`, because
    # the walk compares against the live DOMAIN of a field and a seeded furniture row
    # carries that value. POST_FAULT, and NOT to be trimmed to four rows: the
    # mislabelled row IS the incident.
    ("dlq_mislabeled_replay_safe", "list_dlq_messages", "total", "value"): (
        POST_FAULT,
        "create_mislabeled_dlq_job injects the deliberately mislabelled row this "
        "scenario exists to fence, so the faulted world holds five DLQ rows where "
        "the un-faulted world the check probes holds the four boot-seeded ones",
    ),
    (
        "dlq_mislabeled_replay_safe",
        "list_dlq_messages",
        "items[].id[]",
        "not_live_reachable",
    ): (
        POST_FAULT,
        "the mislabelled row's id is uuid5(ffffffff-11ed-4000-8000-000000000000, "
        "'{tenant_id}:mislabeled-dlq-job') and exists only after "
        "create_mislabeled_dlq_job has fired — the same absence already recorded for "
        "the two chain roots, the bad-data row and the poisoned row",
    ),
    # alert_storm went the other way at wave-10: `use_live_mcp: false`, because none of
    # the three alert producers emits more than one at a time, so a storm is
    # unmanufacturable and its five-alert recording is its premise. These seven were
    # counted as work for a whole campaign and could never have come off the list.
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
    # v0.6.0 added two required fields, so the same deliberately-malformed fixture is
    # now short three rather than one. "The fixture does not match the platform" is this
    # fixture working, so these are recorded and not counted as work.
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
    # v0.6.7 (plat #204, WO-R3-254) added three more, so the fixture is now short six.
    # The other fourteen canned lag responses WERE re-recorded with all three; this one
    # exception is what makes the key-set diff worth keeping strict.
    ("tool_output_schema_mismatch", "get_consumer_lag", "measured_at", "live_only_field"): (
        CANNED_ONLY,
        "same scenario, same deliberate malformation — v0.6.7 made this a "
        "fourth field the fixture deliberately omits",
    ),
    ("tool_output_schema_mismatch", "get_consumer_lag", "age_seconds", "live_only_field"): (
        CANNED_ONLY,
        "same scenario, same deliberate malformation — v0.6.7 made this a "
        "fifth field the fixture deliberately omits",
    ),
    ("tool_output_schema_mismatch", "get_consumer_lag", "recent_samples", "live_only_field"): (
        CANNED_ONLY,
        "same scenario, same deliberate malformation — v0.6.7 made this a "
        "sixth field the fixture deliberately omits",
    ),
    # The four retry scenarios (WO-R3-226, WP-10.1, ADR 0056). All four are canned by
    # necessity rather than preference: the edge needs a first Tier-1 action that LANDS and
    # still leaves the fault, which needs a chaos variant surviving the fix (WP-10.0,
    # WO-R2-165) that the platform does not have. So `use_live_mcp` is false and these
    # fixtures are a premise, like `alert_storm`'s and `remediate_verify_fails`'.
    #
    # Three named mechanisms below, one per group, and what is NOT here is the point: the
    # `get_cache_key_info` reads in the two lag worlds were CORRECTED rather than ledgered
    # (the platform's own answer for a lag cache key is `size` = the digits it holds and null
    # record fields), so they hold no row at all. A fabricated value that the platform can
    # tell us is a fixture defect, and this packet fixed its three.
    #
    # Group 1: the lag sequence. Both worlds read `billing-consumer` three times — a frozen
    # 40 from one stale cache generation, then a live 42000, then the post-action reading —
    # and the sequence is the scenario. A seeded group answers `source: "static"` with the
    # seed script's standing 15000, no hook produces a frozen-then-fresh pair inside one run,
    # and `kill_consumer` supplies a climbing lag rather than this shape. One row per path:
    # the key carries no index, so all three elements share it.
    ("retry_second_hypothesis_succeeds", "get_consumer_lag", "lag", "value"): (
        CANNED_ONLY,
        "the world is a THREE-reading sequence on billing-consumer — 40 measured 58s ago "
        "with five identical samples, then a live 42000, then 120 after the restart — and "
        "the platform answers a seed-script group with its standing 15000. No chaos hook "
        "produces a frozen reading followed by a fresh one inside a single run, which is "
        "the discriminator this scenario is built on, so the sequence is the premise "
        "rather than a recording. All three elements share this row",
    ),
    ("retry_second_hypothesis_succeeds", "get_consumer_lag", "source", "value"): (
        CANNED_ONLY,
        "same premise, the field that says so: the fixture claims a live measurement "
        "because the frozen-then-fresh story is about measurement age, and the platform "
        "answers `static` for a group whose lag the seed script wrote. All three elements "
        "share this row",
    ),
    ("retry_cap_escalates", "get_consumer_lag", "lag", "value"): (
        CANNED_ONLY,
        "same world as retry_second_hypothesis_succeeds with the restart failing too "
        "(40, then 42000, then 42000 again), so the same premise and the same three "
        "elements on one row",
    ),
    ("retry_cap_escalates", "get_consumer_lag", "source", "value"): (
        CANNED_ONLY,
        "same premise, same field, same three elements",
    ),
    # Group 2: a hot-set key nothing seeds. `create_stale_cache` writes
    # `cache:jobs:worker-dispatcher:hot_set`; this scenario's key is a different one, so the
    # un-faulted world answers `exists: false` with every field null. Five rows because an
    # absent key disagrees about each field it does not have.
    ("retry_identical_refused", "get_cache_key_info", "exists", "value"): (
        CANNED_ONLY,
        "the fault IS this key, and no hook seeds it: create_stale_cache writes the "
        "worker-dispatcher hot set, so the world the walk probes holds no "
        "cache:jobs:billing-consumer:hot_set at all and answers exists=false. Both "
        "elements share this row — and the second one is the state that makes this "
        "scenario, the key BACK after the agent deleted it, which no zero-LLM recording "
        "pass can capture because make world-record never acts",
    ),
    ("retry_identical_refused", "get_cache_key_info", "type", "value"): (
        CANNED_ONLY,
        "same absent key: a key that does not exist has no type, so the platform "
        "answers null where the fixture says string",
    ),
    ("retry_identical_refused", "get_cache_key_info", "size", "value"): (
        CANNED_ONLY,
        "same absent key: 184 bytes of hot set against null. The size is part of the "
        "premise (a stale set holding four references) and not a reading",
    ),
    ("retry_identical_refused", "get_cache_key_info", "records_referenced", "value"): (
        CANNED_ONLY,
        "same absent key. The four references and the zero finds are the fault the "
        "scenario states — a set that points at records it can no longer resolve",
    ),
    ("retry_identical_refused", "get_cache_key_info", "records_found", "value"): (
        CANNED_ONLY,
        "same absent key, the other half of that statement",
    ),
    # Group 2b: the two temporal templates (WO-R3-236, WP-14.1), one named mechanism for
    # both. Each seeds `create_stale_cache` on a key of its OWN — not the seeded
    # worker-dispatcher hot set, which a timed expiry would destroy — so the un-faulted
    # world the walk probes holds no such key at all and answers the absent shape, every
    # field null. POST-FAULT rather than CANNED_ONLY (the neighbouring
    # `retry_identical_refused` rows): these scenarios DO run live and a hook does write
    # the key; it is the walk that reads the world before it. Element 1 of each sequence
    # records the same absence the walk observes, so it disagrees about nothing and the
    # rows carry no index.
    ("temporal_ttl_recovers_before_action", "get_cache_key_info", "exists", "value"): (
        POST_FAULT,
        "create_stale_cache writes cache:jobs:catalog-index:hot_set for the fault's "
        "TTL and nothing else in the world writes it, so the un-faulted world the check "
        "probes answers exists=false where the fixture records the seeded entry",
    ),
    ("temporal_ttl_recovers_before_action", "get_cache_key_info", "type", "value"): (
        POST_FAULT,
        "same absent key: a key the hook has not written yet has no type",
    ),
    ("temporal_ttl_recovers_before_action", "get_cache_key_info", "size", "value"): (
        POST_FAULT,
        "same absent key: 90 bytes is the chaos write's own value — the discriminator "
        "this scenario's precondition asserts — against null before the hook fires",
    ),
    ("temporal_ttl_recovers_before_action", "get_cache_key_info", "records_referenced", "value"): (
        POST_FAULT,
        "same absent key: the three references are the stale entry the hook writes",
    ),
    ("temporal_ttl_recovers_before_action", "get_cache_key_info", "records_found", "value"): (
        POST_FAULT,
        "same absent key: zero finds is the other half of that statement",
    ),
    ("temporal_ttl_recovers_during_verify", "get_cache_key_info", "exists", "value"): (
        POST_FAULT,
        "the sibling template's mechanism on its own key, cache:jobs:pricing-table:hot_set",
    ),
    ("temporal_ttl_recovers_during_verify", "get_cache_key_info", "type", "value"): (
        POST_FAULT,
        "same absent key, same reason",
    ),
    ("temporal_ttl_recovers_during_verify", "get_cache_key_info", "size", "value"): (
        POST_FAULT,
        "same absent key, same reason: 90 bytes is the chaos write",
    ),
    ("temporal_ttl_recovers_during_verify", "get_cache_key_info", "records_referenced", "value"): (
        POST_FAULT,
        "same absent key, same reason",
    ),
    ("temporal_ttl_recovers_during_verify", "get_cache_key_info", "records_found", "value"): (
        POST_FAULT,
        "same absent key, same reason",
    ),
    # Group 3: a one-row dead-letter queue holding an invented row. The seeded queue holds
    # four rows the seed script writes, a reset restores them, and nothing removes them — so
    # neither the total nor the row is a state the live world reaches. The platform's triage
    # block is written by its own classifier for its own rows, so the fixture's row carries
    # none and every triage field reads as live-only.
    ("stabilizer_then_reinvestigate", "list_dlq_messages", "total", "value"): (
        CANNED_ONLY,
        "the premise is a queue holding exactly ONE unclassified row, because the scenario "
        "grades what happens after the only actionable thing is fenced. The seeded queue "
        "holds four rows, a reset restores them and no hook removes a row, so a one-row "
        "DLQ is not a state the live world reaches. All three elements — before the fence, "
        "the verify read, and the reinvestigation's re-read — share this row",
    ),
    ("stabilizer_then_reinvestigate", "list_dlq_messages", "items[].id[]", "not_live_reachable"): (
        CANNED_ONLY,
        "the row c41f5d8a-9b23-5e7c-a1d4-3f6b8e2a7c90 is invented: the seeded queue's ids "
        "are fixed by the seed script and no hook mints a new dead-letter row, so this id "
        "is in no live reading. Same premise as the total above",
    ),
    (
        "stabilizer_then_reinvestigate",
        "list_dlq_messages",
        "items[].type[]",
        "not_live_reachable",
    ): (
        CANNED_ONLY,
        "same invented row: a `ledger_export` job type is one the seeded queue does not "
        "hold (it offers bulk_api_sync and csv_upload), and nothing produces one",
    ),
    (
        "stabilizer_then_reinvestigate",
        "list_dlq_messages",
        "items[].triage[].confidence",
        "live_only_field",
    ): (
        CANNED_ONLY,
        "the platform's classifier writes a triage block for the rows IT dead-lettered; "
        "this fixture's row is not one of them and carries no triage, so every field of "
        "that block reads as live-only. Deliberately not modelled: the scenario is about "
        "an UNCLASSIFIED row, and a triage block is the classification",
    ),
    (
        "stabilizer_then_reinvestigate",
        "list_dlq_messages",
        "items[].triage[].is_retryable",
        "live_only_field",
    ): (
        CANNED_ONLY,
        "same absent triage block, second field",
    ),
    (
        "stabilizer_then_reinvestigate",
        "list_dlq_messages",
        "items[].triage[].root_cause_category",
        "live_only_field",
    ): (
        CANNED_ONLY,
        "same absent triage block, third field",
    ),
    (
        "stabilizer_then_reinvestigate",
        "list_dlq_messages",
        "items[].triage[].suggested_fix",
        "live_only_field",
    ): (
        CANNED_ONLY,
        "same absent triage block, fourth field",
    ),
    (
        "stabilizer_then_reinvestigate",
        "list_dlq_messages",
        "items[].triage[].summary",
        "live_only_field",
    ): (
        CANNED_ONLY,
        "same absent triage block, fifth field",
    ),
    # The dual-fault worlds (WO-R3-228, WP-11.1, ADR 0059). `get_deploy_history` is
    # deliberately absent from both: the second fault of the `bad_deploy` world is the
    # SEEDED annotated marker, recorded entry for entry, so it agrees with the un-faulted
    # world and needs no row.
    ("dual_fault_dlq_and_consumer_lag", "get_consumer_lag", "lag", "value"): (
        POST_FAULT,
        "kill_consumer makes worker-dispatcher's lag climb; the check probes the "
        "un-faulted world, so the canned backlog cannot match by design — the same "
        "mechanism as consumer_lag_high. All three elements share this row: the "
        "investigation probe, the reinvestigation's re-read after the replay verified, "
        "and the post-restart verify",
    ),
    ("dual_fault_dlq_and_consumer_lag", "list_dlq_messages", "total", "value"): (
        CANNED_ONLY,
        "the premise is a queue holding exactly ONE actionable row, because this world's "
        "replay has to clear the dead-letter side completely for the run to be able to "
        "resolve on its second fix. The seeded queue holds four rows, a reset restores "
        "them and no hook removes one, so a one-row DLQ is not a state the live world "
        "reaches. Both elements share this row, and the second is also post-action: after "
        "the replay the queue is drained and total is 0",
    ),
    (
        "dual_fault_dlq_and_consumer_lag",
        "list_dlq_messages",
        "items[].created_at",
        "live_only_field",
    ): (
        CANNED_ONLY,
        "the verify element records a DRAINED queue — the replay took the only row — so it "
        "carries no rows at all, and every row field the platform sends reads as "
        "live-only. The absence IS the verify signal (INC-001: a success that is an "
        "absence is claimed on a field that exists when the set is empty, which is "
        "`total` above). Thirteen rows, one mechanism: the ledger is keyed per path",
    ),
    (
        "dual_fault_dlq_and_consumer_lag",
        "list_dlq_messages",
        "items[].dead_lettered_at",
        "live_only_field",
    ): (
        CANNED_ONLY,
        "same drained queue, another of its absent row fields",
    ),
    (
        "dual_fault_dlq_and_consumer_lag",
        "list_dlq_messages",
        "items[].error_message",
        "live_only_field",
    ): (
        CANNED_ONLY,
        "same drained queue, another of its absent row fields",
    ),
    (
        "dual_fault_dlq_and_consumer_lag",
        "list_dlq_messages",
        "items[].extra",
        "live_only_field",
    ): (
        CANNED_ONLY,
        "same drained queue, another of its absent row fields",
    ),
    (
        "dual_fault_dlq_and_consumer_lag",
        "list_dlq_messages",
        "items[].fenced_at",
        "live_only_field",
    ): (
        CANNED_ONLY,
        "same drained queue, another of its absent row fields",
    ),
    (
        "dual_fault_dlq_and_consumer_lag",
        "list_dlq_messages",
        "items[].fenced_by",
        "live_only_field",
    ): (
        CANNED_ONLY,
        "same drained queue, another of its absent row fields",
    ),
    (
        "dual_fault_dlq_and_consumer_lag",
        "list_dlq_messages",
        "items[].id",
        "live_only_field",
    ): (
        CANNED_ONLY,
        "same drained queue, another of its absent row fields",
    ),
    (
        "dual_fault_dlq_and_consumer_lag",
        "list_dlq_messages",
        "items[].remediation_hint",
        "live_only_field",
    ): (
        CANNED_ONLY,
        "same drained queue, another of its absent row fields",
    ),
    (
        "dual_fault_dlq_and_consumer_lag",
        "list_dlq_messages",
        "items[].retry_count",
        "live_only_field",
    ): (
        CANNED_ONLY,
        "same drained queue, another of its absent row fields",
    ),
    (
        "dual_fault_dlq_and_consumer_lag",
        "list_dlq_messages",
        "items[].trace_id",
        "live_only_field",
    ): (
        CANNED_ONLY,
        "same drained queue, another of its absent row fields",
    ),
    (
        "dual_fault_dlq_and_consumer_lag",
        "list_dlq_messages",
        "items[].triage",
        "live_only_field",
    ): (
        CANNED_ONLY,
        "same drained queue, another of its absent row fields",
    ),
    (
        "dual_fault_dlq_and_consumer_lag",
        "list_dlq_messages",
        "items[].type",
        "live_only_field",
    ): (
        CANNED_ONLY,
        "same drained queue, another of its absent row fields",
    ),
    (
        "dual_fault_dlq_and_consumer_lag",
        "list_dlq_messages",
        "items[].updated_at",
        "live_only_field",
    ): (
        CANNED_ONLY,
        "same drained queue, another of its absent row fields",
    ),
    ("dual_fault_consumer_lag_and_bad_deploy", "get_consumer_lag", "lag", "value"): (
        POST_FAULT,
        "same hook, same mechanism as its sibling above: kill_consumer makes the backlog "
        "climb and the check probes the world before the kill. Both elements share this "
        "row — the investigation probe and the post-restart verify",
    ),
    # v0.6.11 (plat #218, WO-R3-217) gave `get_postgres_health` twelve fields, and
    # `postgres_slow`'s fixture now writes all twelve — an absent pool counter parses
    # as null, null means UNKNOWN, and a reading of unknowns with both
    # `*_unknown_reason` strings null is a response the platform cannot produce.
    # Ten of the twelve are exactly what a reset stack answers and need no entry;
    # these two ARE the fault, and no hook can make them move.
    ("postgres_slow", "get_postgres_health", "longest_active_query_ms", "value"): (
        NO_HOOK,
        "the fixture's world is a database serving slowly, so its longest "
        "running query is 1.84s; the check probes a world where nothing is "
        "running at all and pg_stat_activity answers null. Nothing in the lab "
        "makes a query slow — `inject_latency` delays a consumer and "
        "`saturate_db_pool` holds connections, which is the fault this one is "
        "deliberately NOT about",
    ),
    ("postgres_slow", "get_postgres_health", "active_queries_over_slow_threshold", "value"): (
        NO_HOOK,
        "same reading, same absent hook: two queries past the platform's fixed "
        "500ms threshold is the fault, and an idle world counts 0. The "
        "threshold itself (`slow_query_threshold_ms`) matches live and is not "
        "here, which is the pair worth reading together — the yardstick is the "
        "platform's, only the count is the premise",
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
    return _JUSTIFIED.get(key, _JUSTIFIED.get(key[:4], (FIXTURE_DEFECT, "")))


def load_ledger(path: Path | None = None) -> frozenset[DriftKey]:
    """The recorded drift keys. A missing ledger is an empty one — strictest."""
    return frozenset(entry.key for entry in load_entries(path))


def load_entries(path: Path | None = None) -> list[LedgerEntry]:
    """Recorded drift with its context, newest format or the original arrays.

    Context comes from ``context_of`` for EVERY row: the code is the authority on
    classification and the file only records it. Reading the file's copy made
    ``defect_count`` ignore an entry newly absolved in ``_JUSTIFIED``;
    ``test_the_committed_contexts_agree_with_the_code`` now calls that a stale file.
    """
    target = path or LEDGER_PATH
    if not target.exists():
        return []
    payload = json.loads(target.read_text())
    entries: list[LedgerEntry] = []
    for row in payload.get("known_drift", []):
        key: DriftKey
        if isinstance(row, list) and len(row) == 4:
            key = (str(row[0]), str(row[1]), str(row[2]), str(row[3]))
        elif isinstance(row, dict):
            key = (
                str(row["scenario"]),
                str(row["tool"]),
                str(row["path"]),
                str(row["kind"]),
            )
            if row.get("index") is not None:
                key = (*key, int(row["index"]))
        else:
            continue
        context, why = context_of(key)
        entries.append(LedgerEntry(key=key, context=context, why=why))
    return entries


def defect_count(path: Path | None = None) -> int:
    """Recorded disagreements that are actually work — the burn-down number.

    The others are recorded because the check keeps reporting them, not because anyone
    should "fix" them: counting those would set an unreachable target, and the first
    person to try would break a scenario to match a world it never described.
    """
    return sum(1 for entry in load_entries(path) if entry.is_defect)


def split_for_bless(
    observed: Collection[DriftKey],
    prior: Collection[DriftKey],
    checked: Collection[tuple[str, str]],
) -> tuple[tuple[DriftKey, ...], tuple[DriftKey, ...]]:
    """``(carried, disproved)`` for the prior entries this run did not observe.

    DISPROVED only when the run probed that fixture and found no disagreement — the
    ratchet turning. An unreached fixture is CARRIED: deleting on no opinion is how a
    transient 502 shortens the burn-down list. ``checked`` is per ``(scenario, tool)``,
    the unit the probe reports coverage in.
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

    ``checked`` is the ``(scenario, tool)`` coverage the run established; uncovered
    entries are carried, so a bless can only remove what it disproved, and the default
    establishes nothing. Keys this module does not own are preserved verbatim —
    ``_blessed_against`` is the one that matters, and every bless used to drop it.
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
    keys = sorted(
        observed | set(carried), key=lambda key: (*key[:4], -1 if len(key) == 4 else key[4])
    )
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
        if len(key) == 5:
            row["index"] = key[4]
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
                COLD_STACK: (
                    "the recording is of a warm stack and the check ran on a cold one: "
                    "worker-dispatcher's lag is unmeasured for about the first minute "
                    "after boot, so a fresh platform answers null where the recording "
                    "says 0. Timing, not contract, and not fixable from either side"
                ),
                WARM_STACK: (
                    "the mirror of cold-stack: the recording caught a freshly seeded "
                    "world and the check ran against a volume that has been up long "
                    "enough to have measured one. Timing, not contract"
                ),
                NO_HOOK: (
                    "the fixture describes a fault no chaos hook can produce, so the "
                    "canned value is the scenario's premise and the check probes a "
                    "world that never had it. Not a defect; the fix, if that scenario "
                    "is ever to run live, is a platform hook"
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
    drifts: Iterable[Drift], ledger: frozenset[DriftKey], *, stack_context: str = "unknown"
) -> tuple[tuple[Drift, ...], tuple[DriftKey, ...]]:
    """Split observed drift into ``(new, stale_ledger_entries)``.

    ``new`` is drift the ledger does not record — the check's actual subject.
    ``stale`` is recorded drift that no longer occurs, which means a fixture
    was fixed and its line here has to go.
    """
    observed = {drift.key for drift in drifts}
    generic_observed = {key[:4] for key in observed}
    matched = {key for key in ledger if key in observed or key in generic_observed}
    new = tuple(
        drift for drift in drifts if drift.key not in ledger and drift.key[:4] not in ledger
    )
    stale = tuple(
        sorted(
            key
            for key in ledger - matched
            if context_of(key)[0] not in {COLD_STACK, WARM_STACK}
            or context_of(key)[0].removesuffix("-stack") == stack_context
        )
    )
    return new, stale

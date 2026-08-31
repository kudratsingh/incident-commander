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

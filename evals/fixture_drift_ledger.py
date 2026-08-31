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
    # The three below are one mechanism, not three defects. `get_dag_state`
    # is a SEQUENCED fixture: element 0 is the investigation probe reading an
    # unpaused DAG, element 1 is the verify probe reading the DAG back after
    # the agent's own pause_dag call. The drift check probes a world in which
    # no agent has acted, so element 1's three pause fields cannot match, and
    # no edit to the fixture can make them: a verify recording that showed
    # paused=false would be a recording of the remediation failing. They were
    # counted as work until the comparison learned to say which element it
    # meant — three entries on a burn-down list that could never be burnt
    # down.
    ("remediate_runaway_saga_success", "get_dag_state", "paused", "value"): (
        POST_ACTION,
        "element [1] is the post-pause_dag verify probe; the check probes the "
        "un-acted-on world, where the DAG is not paused",
    ),
    ("remediate_runaway_saga_success", "get_dag_state", "paused_by", "value"): (
        POST_ACTION,
        "same element, same pause: nothing has paused the live DAG, so it names no pauser",
    ),
    (
        "remediate_runaway_saga_success",
        "get_dag_state",
        "paused_expires_in_seconds",
        "value",
    ): (
        POST_ACTION,
        "same element, same pause: the live DAG holds no pause, so its TTL is "
        "null rather than the fixture's remaining seconds",
    ),
    # remediate_runaway_saga_success is `use_live_mcp: false`, and its own
    # header says why: the fault is unmanufacturable live, because the seeded
    # DAG auto-completes seconds after boot and no chaos hook builds a
    # runaway chain (BUILD_PLAN 2.2). The runner refuses to select it under
    # --live. So its get_dag_state recording is the scenario's PREMISE, not a
    # recording of anything -- the same mechanism already recorded for
    # remediate_verify_fails below, and the same one its three pause fields
    # are already excused under as post-action.
    #
    # These six sat on the burn-down list as defects for the whole campaign
    # and could never have come off it: there is no live reading for a
    # canned-only scenario to be corrected TOWARDS. That is the exact failure
    # the post-action note below describes ("three entries on a burn-down
    # list that could never be burnt down"), one layer out.
    #
    # They stay recorded and visible with a reason rather than being deleted.
    # If a chaos hook that seeds a genuinely stuck DAG ever lands and the
    # scenario's flags flip back, these become real work again and this block
    # is what has to be removed first.
    ("remediate_runaway_saga_success", "get_dag_state", "nodes[].id[]", "not_live_reachable"): (
        CANNED_ONLY,
        "use_live_mcp is false — the scenario never runs live, so its "
        "recordings are its premise rather than a recording of anything",
    ),
    ("remediate_runaway_saga_success", "get_dag_state", "nodes[].type[]", "not_live_reachable"): (
        CANNED_ONLY,
        "same scenario, same premise: the saga node types it describes exist in no live DAG",
    ),
    (
        "remediate_runaway_saga_success",
        "get_dag_state",
        "nodes[].status[]",
        "not_live_reachable",
    ): (
        CANNED_ONLY,
        "same scenario, same premise: the seeded DAG auto-completes, so a "
        "runaway chain's statuses cannot be read live",
    ),
    (
        "remediate_runaway_saga_success",
        "get_dag_state",
        "nodes[].retry_count[]",
        "not_live_reachable",
    ): (
        CANNED_ONLY,
        "same scenario, same premise: nothing live retries these nodes",
    ),
    (
        "remediate_runaway_saga_success",
        "get_dag_state",
        "edges[].from_id[]",
        "not_live_reachable",
    ): (
        CANNED_ONLY,
        "same scenario, same premise: the chain it draws exists in no live DAG",
    ),
    ("remediate_runaway_saga_success", "get_dag_state", "edges[].to_id[]", "not_live_reachable"): (
        CANNED_ONLY,
        "same scenario, same premise: the chain it draws exists in no live DAG",
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

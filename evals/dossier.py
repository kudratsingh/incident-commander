"""Fault-world content review, mechanised — ``make world-dossier ONLY=<scenario>``.

**What this exists to catch.** On 2026-09-07 `remediate_runaway_saga_success`
run A (archive ``efdc3b2a9864``, ≈$0.15) failed because the world contradicted
itself: the stuck chain's dead-lettered root was seeded ``remediation_hint:
replay_safe`` and carried the error text ``SchemaValidationError: payload
missing required field 'user_id' …``. The agent did exactly what ADR 0027 asks
— read the root's dead-letter row before replaying it — reasoned that a
missing required field is a persistent data bug no replay can fix, and
escalated naming the contradiction. Sound operator judgement, graded red.

Two readiness sweeps had already passed on that scenario. Both checked
*mechanics*: the chain drains, the hooks exist, the guards admit the right
plan. Neither seeded the fault and then read the resulting row's fields **as
the agent would**. The contradiction was visible in a free probe. The user's
verdict was blunt: "we should have caught that the wrong chaos input was being
inputted."

So this module makes that reading mechanical, and free:

1. Seed the scenario's ``chaos_setup`` through the runner's own chaos path —
   same hook, same arguments, same ``ChaosClient``, same write+chaos
   principal. Not a re-implementation: ``invoke_chaos_hook`` is the function
   ``run_scenario`` calls.
2. Run the scenario's ``expected_precondition`` probes and report them.
3. Run every READ probe the agent is expected to make, derived MECHANICALLY
   from the three guard maps and the scenario's own claims (see
   ``derive_probes``), under the READ-SCOPED smoke token, and print every
   output in full, pretty-printed, never truncated. Truncation is how the
   contradiction stayed invisible; a dossier that elides a field is the
   sweep that missed it.
4. Lint what was read for internal coherence and print FINDINGS, NOT
   VERDICTS. The reader decides. A lint that returned a verdict would become
   a gate somebody tunes to green, which is the failure mode on the other
   side of the same lesson (LESSONS 2026-09-07: "do not tune the agent to
   trust hints over evidence to pass a contradictory fixture").
5. ``make eval-reset PURGE_IDEMPOTENCY=1`` — the protocol's own reset, run as
   a subprocess so it is literally the same code path — then re-audit the
   seeded baseline and print PASS/FAIL per line.
6. Write the whole thing to a versioned, create-only file under
   ``evals/reports/dossiers/`` (``evals/artifacts.py``, the #185 convention)
   AND print it, so the coordinator pastes it into the readiness note.

**What it never does.** No LLM client is ever constructed — the module does
not import one. No Tier-1 tool is ever called. Exactly two writes happen: the
scenario's own chaos hook, and ``make eval-reset``. Both are the runner's code
paths, both are what a paid run does anyway. If the platform is not reachable
the tool refuses BEFORE seeding, so a broken stack cannot be left holding a
half-seeded fault.

**Free.** Zero tokens, zero spend. That is the whole point: the check that
would have saved the rem-4 run costs nothing, so there is no reason to skip it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from evals import artifacts
from evals.chaos_hooks import ChaosInvocationError, invoke_chaos_hook
from evals.graders.deterministic import AnyOfExpectation, leaf_claims
from evals.preconditions import unmet
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import PreconditionProbe, Scenario
from incident_commander.agent.investigation import (
    ALERT_SUBJECT_PROBES,
    SubjectMatch,
    SubjectProbe,
    alert_subject,
)
from incident_commander.agent.remediation import (
    SOURCE_LISTING_FOR_ACTION,
    SOURCE_ROW_FOR_ACTION,
    VERIFY_PROBE_FOR_ACTION,
)
from incident_commander.config import Settings
from incident_commander.tools.mcp_client import MCPClientProtocol, MCPError, ToolResult, make_client
from incident_commander.tools.policies import RESOURCE_ARG_FIELDS, Tier, tier_of
from incident_commander.tools.registry import TOOL_REGISTRY

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
_SCENARIOS_DIR: Final[Path] = _REPO_ROOT / "evals" / "scenarios"

#: Exit codes, in the runner's own vocabulary (``evals/runner.py``): 2 is a
#: selection refusal, 3 is preflight/env. The rest are this tool's own and are
#: documented in ``docs/runbook.md``.
EXIT_OK: Final[int] = 0
EXIT_SELECTION: Final[int] = 2
EXIT_PREFLIGHT: Final[int] = 3
EXIT_BASELINE_DIRTY: Final[int] = 4
EXIT_SEEDING: Final[int] = 5
EXIT_RESET: Final[int] = 6


# --------------------------------------------------------------------------
# The seeded baseline the world must return to after the reset.
# --------------------------------------------------------------------------
#
# One copy of these numbers, here, mirrored from the runbook's "Pre-run
# checklist" table (step 4) — and
# ``tests/unit/test_world_dossier.py::TestBaselineMatchesTheRunbook`` reads
# that table and fails if the two disagree. LESSONS 2026-09-07: "five places
# for the same truth means five stale copies"; on that day every context file
# in the workspace was wrong at once. A second hand-maintained copy of the
# baseline is exactly that shape, so it is pinned to the document instead.
#
# `worker-dispatcher` lag is deliberately NOT re-audited here. It is the
# fourth line of the runbook's table, but reading it means `get_consumer_lag`,
# whose response is served from a 60-second staleness window
# (``CACHED_READ_FRESHNESS_SECONDS``) — so a post-reset reading can predate
# the reset and report a number about the seeded world. A check that can
# answer about the wrong moment is worse than an absent one; the coordinator
# reads lag as part of PROTOCOL step 3, where the timing is theirs to control.
BASELINE_DLQ_TOTAL: Final[int] = 4
BASELINE_ACTIVE_ALERTS: Final[int] = 3
BASELINE_CHAOS_KEYS: Final[int] = 0

#: Compose file and service names for the redis key scan. Defaults match the
#: Makefile's ``PLATFORM_COMPOSE`` so an override in ``.env`` reaches both.
_COMPOSE_FILE_ENV: Final[str] = "PLATFORM_COMPOSE"
_DEFAULT_COMPOSE_FILE: Final[str] = "demo/compose.yml"
_REDIS_SERVICE: Final[str] = "redis"


# --------------------------------------------------------------------------
# Probe derivation
# --------------------------------------------------------------------------
#
# Every read this tool makes is DERIVED, never listed. A hand-written list of
# probes per scenario is the same object as the hand-maintained SMOKE_ONLY
# list that WO-R2-41 deleted: it rots silently, in three directions at once
# (a renamed tool, a redesigned scenario, a new claim nobody added), and it
# rots in the direction of reading LESS of the world than the reader believes
# was read. The derivation cannot fall out of step with what it derives from.
#
# ``_KIND_BY_FIELD`` is the one piece of new knowledge the derivation needs:
# which argument field names refer to the same KIND of resource, so a value
# the scenario states in one place can fill the corresponding argument
# somewhere else. ``pause_dag`` names ``root_job_id`` and ``get_dag_state``
# names ``job_id``; they are the same job. ``replay_dlq_by_ids`` names
# ``job_ids`` (plural, a list); each element is a job id.
#
# Total over every field in ``RESOURCE_ARG_FIELDS``'s values, pinned by
# ``TestKindByFieldIsTotal`` — so a platform tool that ships a new
# resource-naming argument cannot silently produce a probe with a guessed
# argument, or no probe at all.
#
# ``id`` maps to ``incident_id`` because ``get_incident`` is the only tool
# with a bare ``id`` argument. Note what this deliberately does NOT do:
# harvest ids out of an ``EvidenceFieldExpectation``'s ``where`` selector,
# where a bare ``id`` inside ``items[]`` is a JOB id, not an incident id. The
# kind of a field named inside an arbitrary rows path is not mechanically
# knowable, and guessing it would produce a probe aimed at the wrong resource
# — which is the class of error this whole tool exists to surface, not commit.
_KIND_BY_FIELD: Final[dict[str, str]] = {
    "consumer_group": "consumer_group",
    "id": "incident_id",
    "job_id": "job_id",
    "job_ids": "job_id",
    "key": "cache_key",
    "root_job_id": "job_id",
    "trace_id": "trace_id",
}


@dataclass(frozen=True)
class Probe:
    """One read call to make, and the written reason it is in the set.

    ``origins`` is a tuple rather than a string because two maps often derive
    the same call — on ``remediate_runaway_saga_success`` the alert's subject
    probe and the post-replay verify probe are both
    ``get_dag_state(job_id=<root>)``. Deduplicating them but keeping both
    reasons is what lets the dossier say why a probe matters twice over,
    instead of the reader wondering which map put it there.
    """

    tool: str
    arguments: tuple[tuple[str, Any], ...]
    origins: tuple[str, ...]

    @property
    def args(self) -> dict[str, Any]:
        return dict(self.arguments)

    @property
    def label(self) -> str:
        rendered = ", ".join(f"{name}={value!r}" for name, value in self.arguments)
        return f"{self.tool}({rendered})"


def _probe(tool: str, arguments: Mapping[str, Any], origin: str) -> Probe:
    return Probe(tool, tuple(sorted(arguments.items())), (origin,))


def _merge(probes: Sequence[Probe]) -> list[Probe]:
    """Deduplicate by (tool, arguments), unioning the origins, order preserved."""
    merged: dict[tuple[str, tuple[tuple[str, Any], ...]], Probe] = {}
    for probe in probes:
        key = (probe.tool, probe.arguments)
        existing = merged.get(key)
        if existing is None:
            merged[key] = probe
            continue
        extra = tuple(o for o in probe.origins if o not in existing.origins)
        merged[key] = Probe(probe.tool, probe.arguments, existing.origins + extra)
    return list(merged.values())


def _alert_values(alert: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """``(alert_field, argument_field, value)`` for every resource the alert names.

    ``investigation.alert_subject`` answers the same question and returns only
    the FIRST match, because the guard it feeds constrains one probe. A dossier
    wants them all: an alert naming both a job and a trace should have both
    read. Same map, same two lookup levels (top level, then ``extra_data``) —
    that second level is not speculative, it is where the platform's webhook
    actually puts these fields (see ``alert_subject``'s docstring).

    ``SubjectMatch.UNFILTERED`` entries are skipped here and handled in
    ``derive_probes``, because their probe is the absence of an argument rather
    than a value in one: there is no ``(argument_field, value)`` pair to return
    and putting one in would make the dossier print
    ``list_dlq_messages(remediation_hint='unclassified')`` — a call the
    platform does not have. ``_value_pool`` reads this function too, and the
    skip is right for it in the same way: `unclassified` is a scope word, not
    a resource, so it must never become an argument value.
    """
    found: list[tuple[str, str, str]] = []
    for source in (alert, alert.get("extra_data")):
        if not isinstance(source, Mapping):
            continue
        for field, probe in ALERT_SUBJECT_PROBES.items():
            if probe.match is not SubjectMatch.EQUALS:
                continue
            raw = source.get(field)
            if not isinstance(raw, (str, uuid.UUID)):
                continue
            value = str(raw).strip()
            if value and (field, probe.argument_field, value) not in found:
                found.append((field, probe.argument_field, value))
    return found


def _unfiltered_subject(alert: Mapping[str, Any]) -> tuple[str, SubjectProbe] | None:
    """The alert field naming an unfiltered-probe subject, and its probe.

    Reads ``investigation.alert_subject``'s own answer rather than re-walking
    the map, so the dossier cannot disagree with the guard about whether a
    scope word is recognised — an unadmitted value is inert in both places or
    in neither.
    """
    subject = alert_subject(alert)
    if subject is None or subject.match is not SubjectMatch.UNFILTERED:
        return None
    return subject.alert_field, ALERT_SUBJECT_PROBES[subject.alert_field]


def _value_pool(scenario: Scenario) -> dict[str, list[str]]:
    """Resource values this scenario states, grouped by kind of resource.

    Three sources, each a place the scenario has ALREADY written the value
    down, so nothing here is invented:

    1. the alert payload, through ``ALERT_SUBJECT_PROBES``;
    2. the ``expected_precondition`` probes' own arguments — the most exact
       source there is, since those are literal calls the runner makes;
    3. ``expected_action_arguments``' ``equals`` values — the scenario's
       statement of which resource the action must name.
    """
    pool: dict[str, list[str]] = {}

    def add(field: str, value: object) -> None:
        kind = _KIND_BY_FIELD.get(field)
        if kind is None or not isinstance(value, (str, uuid.UUID)):
            return
        text = str(value).strip()
        if text and text not in pool.setdefault(kind, []):
            pool[kind].append(text)

    for _alert_field, argument_field, value in _alert_values(scenario.alert.model_dump()):
        add(argument_field, value)
    for probe in scenario.expected_precondition:
        for name, value in probe.arguments.items():
            add(name, value)
    for expectation in scenario.expectation.expected_action_arguments:
        # ``argument`` is a resolve_path: ``job_ids[]`` names the elements of
        # a list argument. The base segment is the argument name.
        base = expectation.argument.split("[")[0].split(".")[0]
        add(base, expectation.equals)
    return pool


def _read_tool(name: str) -> bool:
    """True for a registered READ-tier tool. Anything else is not ours to call."""
    if name not in TOOL_REGISTRY:
        return False
    return tier_of(name) is Tier.READ


def _fill(tool: str, pool: Mapping[str, list[str]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Argument sets for one read tool, plus notes for what could not be filled.

    A tool with no resource-naming argument (``list_dlq_messages``,
    ``list_active_alerts``) gets one call with no arguments — its defaults are
    the platform's, which is what the agent's own unfiltered read looks like.
    A tool with one gets one call per value of that kind. A kind the scenario
    never names produces a NOTE, never a guessed value: a probe aimed at a
    made-up resource reads as evidence and is not.
    """
    fields = sorted(RESOURCE_ARG_FIELDS.get(tool, frozenset()))
    if not fields:
        return [{}], []
    argument_sets: list[dict[str, Any]] = []
    notes: list[str] = []
    for name in fields:
        kind = _KIND_BY_FIELD.get(name)
        values = pool.get(kind, []) if kind is not None else []
        if not values:
            notes.append(
                f"`{tool}` needs `{name}` and this scenario names no "
                f"{kind or 'resource'} anywhere the derivation reads (alert payload, "
                "precondition arguments, expected_action_arguments) — NOT PROBED. "
                "Read it by hand, or give the scenario the value it is grading on."
            )
            continue
        argument_sets.extend({name: value} for value in values)
    return argument_sets, notes


def derive_probes(scenario: Scenario) -> tuple[list[Probe], list[str]]:
    """Every read the agent is expected to make, and what could not be derived.

    Four derivations, in the order the agent would perform them:

    * **the alert's subject** — ``ALERT_SUBJECT_PROBES``. The guard that made
      run D of remediation 1 pass (cmd #177) exists because the agent twice
      chased something other than what the alert named. The first thing to
      read is therefore the first thing to review.
    * **the source row** — ``SOURCE_ROW_FOR_ACTION``, per expected action
      tool. ADR 0027: before you replay a dead-lettered job, its dead-letter
      row must be in evidence, because the row is the only place its
      ``remediation_hint`` exists. That row is where the rem-4 contradiction
      lived, so this is the derivation with the run behind it.
    * **the source listing** — ``SOURCE_LISTING_FOR_ACTION``, per expected
      action tool. ADR 0028: the same rule for an action that names a
      category instead of rows. The probe is derived UNFILTERED, which is
      what puts the rows the agent's category will sweep up in the dossier
      next to the ones it will leave — the comparison a category replay is
      reviewed on, and one a hint-filtered page cannot show.
    * **the verify probe** — ``VERIFY_PROBE_FOR_ACTION``, per expected action
      tool. ADR 0025: a verify leg must observe the resource the action
      changed. Reading it BEFORE the run is how the reader sees whether the
      pre-action state is the one the scenario's verify claim expects to move.
    * **every read tool a claim names** — ``expected_evidence_fields[].tools``.
      A claim on a tool's output is a statement that the agent read that tool;
      the field it asserts on has to exist in what the world returns.

    Returns the deduplicated probe list and the notes. Notes are part of the
    dossier, not warnings to swallow: "this probe could not be derived" is
    itself a finding about the scenario.
    """
    pool = _value_pool(scenario)
    probes: list[Probe] = []
    notes: list[str] = []

    dumped = scenario.alert.model_dump()
    for alert_field, argument_field, value in _alert_values(dumped):
        tool = ALERT_SUBJECT_PROBES[alert_field].tool_name
        probes.append(
            _probe(
                tool,
                {argument_field: value},
                f"ALERT_SUBJECT_PROBES: the alert's `{alert_field}` names `{value}`, "
                f"and `{tool}` is the read that observes it. The agent is required to "
                "probe this first (cmd #177).",
            )
        )
    # The unfiltered arm (ADR 0032). Derived separately because its probe is
    # the absence of a filter: the call is the tool with NO narrowing argument,
    # which is also what `_fill` produces for a tool that names no resource, so
    # it merges with the source-listing derivation below into one line rather
    # than adding a call.
    unfiltered_subject = _unfiltered_subject(dumped)
    if unfiltered_subject is not None:
        alert_field, probe = unfiltered_subject
        probes.append(
            _probe(
                probe.tool_name,
                {},
                f"ALERT_SUBJECT_PROBES: the alert's `{alert_field}` names the "
                f"`{dumped.get(alert_field)}` scope, and the UNFILTERED "
                f"`{probe.tool_name}` is the only read that observes it — "
                f"`{probe.argument_field}=null` on that call means 'every category', "
                f"so no filtered page shows a row nothing has classified. The agent is "
                "required to probe this first, and the plan guard then requires the "
                "action to name one of the rows it returns with a null "
                f"`{probe.argument_field}` (ADR 0032).",
            )
        )
    if not _alert_values(dumped) and unfiltered_subject is None:
        notes.append(
            "The alert names no resource in ALERT_SUBJECT_PROBES, so there is no "
            "subject probe to derive. Legitimate and common — a DLQ-depth alert "
            "names a condition, not a resource (see `alert_subject`)."
        )

    action_tools = scenario.expectation.expected_action_tools
    if not action_tools:
        notes.append(
            "The scenario declares no `expected_action_tools`, so no source-row read "
            "and no verify probe are derivable. Read-only and escalate-only scenarios "
            "look like this."
        )

    for tool in action_tools:
        sources = SOURCE_ROW_FOR_ACTION.get(tool)
        if sources is None:
            notes.append(
                f"`{tool}` has no SOURCE_ROW_FOR_ACTION entry at all. That map is "
                "total over Tier-1 by test; if this fires, the map and the registry "
                "have drifted apart."
            )
            continue
        if not sources:
            sibling = (
                " Its coverage requirement is SOURCE_LISTING_FOR_ACTION's instead "
                "(ADR 0028) — see the probe derived from it below."
                if SOURCE_LISTING_FOR_ACTION.get(tool)
                else ""
            )
            notes.append(
                f"`{tool}` is declared INERT in SOURCE_ROW_FOR_ACTION — no listing "
                "classifies the resources it names, so no source row is required and "
                "none is derived. Check the map's own comment for the stated reason "
                f"before reading that as 'nothing to check'.{sibling}"
            )
            continue
        for source in sources:
            probes.append(
                _probe(
                    source.tool_name,
                    {},
                    f"SOURCE_ROW_FOR_ACTION[{tool}]: `{source.tool_name}` is the "
                    f"listing whose rows carry `{source.decision_field}`, the field "
                    f"the {tool} decision depends on (ADR 0027). Read UNFILTERED so "
                    "every row in the world is in the dossier, not only the page the "
                    "precondition asks for.",
                )
            )

    for tool in action_tools:
        for listing in SOURCE_LISTING_FOR_ACTION.get(tool, ()):
            probes.append(
                _probe(
                    listing.tool_name,
                    {},
                    f"SOURCE_LISTING_FOR_ACTION[{tool}]: `{tool}` names a FILTER, "
                    f"not rows — the platform expands it at execution time — so some "
                    f"`{listing.tool_name}` reading has to have COVERED the slice it "
                    f"will expand to before the plan is admitted (ADR 0028). Read it "
                    "UNFILTERED: that is the reading the guard accepts for every "
                    "slice, and it is the only way the dossier shows the rows the "
                    "agent's category is about to sweep up beside the ones it is not.",
                )
            )

    for tool in action_tools:
        for verify in VERIFY_PROBE_FOR_ACTION.get(tool, ()):
            if verify.argument_field is None:
                probes.append(
                    _probe(
                        verify.tool_name,
                        {},
                        f"VERIFY_PROBE_FOR_ACTION[{tool}]: `{verify.tool_name}` "
                        "observes the change without naming a resource (ADR 0025).",
                    )
                )
                continue
            kind = _KIND_BY_FIELD.get(verify.argument_field)
            values = pool.get(kind, []) if kind is not None else []
            if not values:
                notes.append(
                    f"VERIFY_PROBE_FOR_ACTION[{tool}] wants "
                    f"`{verify.tool_name}({verify.argument_field}=…)` and the scenario "
                    f"names no {kind or 'resource'} to put in it — NOT PROBED."
                )
            for value in values:
                probes.append(
                    _probe(
                        verify.tool_name,
                        {verify.argument_field: value},
                        f"VERIFY_PROBE_FOR_ACTION[{tool}]: `{verify.tool_name}` is "
                        f"the read that can observe what {tool} changes on "
                        f"`{value}` (ADR 0025). Its reading here is the PRE-action "
                        "state — the thing the scenario's verify claim expects to "
                        "have moved.",
                    )
                )

    for claim in leaf_claims(scenario.expectation.expected_evidence_fields):
        for tool in claim.tools:
            if not _read_tool(tool):
                continue
            argument_sets, fill_notes = _fill(tool, pool)
            notes.extend(fill_notes)
            for arguments in argument_sets:
                probes.append(
                    _probe(
                        tool,
                        arguments,
                        f"evidence claim: `{tool}.{claim.field}` "
                        f"{claim.describe()} — the claim says the agent read this "
                        "tool, so the field it asserts on has to be in what the "
                        "world returns.",
                    )
                )

    return _merge(probes), notes


# --------------------------------------------------------------------------
# The coherence lint
# --------------------------------------------------------------------------
#
# THE TABLE. Grounded in the platform's own enum, which is the only authority
# on what each hint MEANS (incident-platform, app/models/enums.py):
#
#     REPLAY_SAFE      = transient / poison — replay OK
#     WAIT_AND_REPLAY  = external dep down — retry later
#     HUMAN_REQUIRED   = persistent bug — do NOT replay
#
# A row is coherent when its error text belongs to a family the hint
# sanctions. Three families, matched by substring against the lowercased
# error text, because these are the shapes the platform's own writers emit
# (``scripts/seed_eval_fixtures.py``, ``chaos/seed_dlq_messages.py``,
# ``chaos/poison_message.py``, ``chaos/create_bad_data_job.py``).
#
# Two notes on the shape of the table, both deliberate:
#
# * A **transient** text is coherent with BOTH ``replay_safe`` and
#   ``wait_and_replay``. The brief for this tool put transient/timeout/
#   connection under ``replay_safe`` alone, and that reading would flag two of
#   the four seeded rows — ``wait_and_replay`` +
#   ``ConnectionRefusedError('smtp.mailer.internal:587')`` and
#   ``wait_and_replay`` + ``TimeoutError('stripe.api')`` — which are exactly
#   what the platform's own definition of ``wait_and_replay`` describes: an
#   external dependency that is down, replay once it recovers. The difference
#   between the two hints on a transient error is WHEN to replay, not
#   WHETHER. Flagging both would put two false findings beside the one true
#   one on every single run, and a lint the reader learns to skim is a lint
#   that stops working. Deviation recorded in the PR body.
# * **rate_limit** is coherent with ``wait_and_replay`` ONLY, and
#   **bad_data** with ``human_required`` ONLY. Those two cells are where the
#   asymmetry earns its keep: a rate limit means an immediate replay is
#   actively wrong, and a schema/validation/bad-data text means no replay
#   fixes it. ``replay_safe`` + bad_data is the rem-4 contradiction, and the
#   whole reason this file exists.
#
# A NULL hint sanctions every family: "not categorised" is the platform's
# UNKNOWN and makes no promise about the error text, so there is nothing to
# contradict. The lint says so rather than staying silent, because a null
# hint is itself worth the reader's attention (``list_dlq_messages``: "A null
# hint is UNKNOWN, not replay-safe").
ERROR_FAMILIES: Final[dict[str, tuple[str, ...]]] = {
    "transient": (
        "timeout",
        "timed out",
        "timeouterror",
        "connectionrefused",
        "connection refused",
        "connection reset",
        "connectionreset",
        "connectionerror",
        "temporarily unavailable",
        "service unavailable",
        "503",
        "deadlock",
        "transient",
        "broken pipe",
    ),
    "rate_limit": (
        "rate limit",
        "ratelimit",
        "rate_limit",
        "429",
        "too many requests",
        "throttl",
        "backoff",
        "quota exceeded",
    ),
    "bad_data": (
        "schemavalidationerror",
        "validationerror",
        "missing required field",
        "invalid literal",
        "valueerror",
        "not-a-number",
        "malformed",
        "corrupt",
        "parse error",
        "failed to parse",
        "decode error",
        "unmarshal",
        "constraint violation",
        "integrityerror",
    ),
}

#: hint value → the families of error text it sanctions. ``None`` is the null
#: hint. A hint value absent from this table is itself a finding: the platform
#: has three, and a fourth means the fixture wrote something nothing reads.
HINT_COHERENT_FAMILIES: Final[dict[str | None, frozenset[str]]] = {
    "replay_safe": frozenset({"transient"}),
    "wait_and_replay": frozenset({"transient", "rate_limit"}),
    "human_required": frozenset({"bad_data"}),
    None: frozenset(ERROR_FAMILIES),
}


def error_families(error_message: str) -> frozenset[str]:
    """Every family whose vocabulary appears in this error text."""
    lowered = error_message.lower()
    return frozenset(
        family
        for family, needles in ERROR_FAMILIES.items()
        if any(needle in lowered for needle in needles)
    )


#: The lint's own name for a hint/error contradiction, and the name of the one
#: case where that contradiction is the point. Constants because two readers
#: now compare against them — the renderer's verdict column and the sanctioned
#: rewrite below — and a string literal in three places is how the two would
#: quietly stop agreeing.
INCOHERENT_KIND: Final[str] = "INCOHERENT hint vs error"
SANCTIONED_KIND: Final[str] = "sanctioned incoherent fixture (WO-R2-167)"


@dataclass(frozen=True)
class Finding:
    """One thing the reader should look at. Never a verdict."""

    kind: str
    subject: str
    detail: str


def lint_dlq_row(row: Mapping[str, Any], seen_in: str) -> list[Finding]:
    """Findings for one dead-letter row's (hint, error text) pair.

    Empty means the lint has nothing to say, which is not the same as "this
    row is fine" — an unclassifiable error text also produces a finding
    saying the lint could not classify it, so silence here means the pair was
    positively coherent.
    """
    row_id = str(row.get("id", "<no id>"))
    raw_hint = row.get("remediation_hint")
    hint = raw_hint if isinstance(raw_hint, str) else None
    error = row.get("error_message")
    subject = f"DLQ row `{row_id}` (seen in {seen_in})"

    if hint is not None and hint not in HINT_COHERENT_FAMILIES:
        return [
            Finding(
                "unknown hint",
                subject,
                f"`remediation_hint` is {hint!r}, which is not one of the "
                f"platform's three ({', '.join(sorted(k for k in HINT_COHERENT_FAMILIES if k))}) "
                "or null. Nothing in the agent's routing reads that value, so the "
                "row is effectively uncategorised while looking categorised.",
            )
        ]
    if not isinstance(error, str) or not error.strip():
        return [
            Finding(
                "no error text",
                subject,
                f"hint is {raw_hint!r} and `error_message` is empty, so the pair "
                "cannot be checked. If the agent is expected to reason from the "
                "error, there is nothing here for it to reason from.",
            )
        ]

    families = error_families(error)
    if not families:
        return [
            Finding(
                "unclassified error text",
                subject,
                f"hint {raw_hint!r}; the lint's table matched no family in "
                f"{error.strip()!r}. NO OPINION — read it yourself and ask whether "
                "it supports the hint. If this shape recurs, add it to "
                "`ERROR_FAMILIES`.",
            )
        ]

    sanctioned = HINT_COHERENT_FAMILIES[hint]
    if families & sanctioned:
        return []
    return [
        Finding(
            INCOHERENT_KIND,
            subject,
            f"hint {raw_hint!r} sanctions {sorted(sanctioned)} error texts; this "
            f"row's text reads as {sorted(families)}: {error.strip()!r}. This is the "
            "rem-4 run-A shape (archive efdc3b2a9864): an agent that reads the "
            "error and reasons about it will contradict the hint, and the scenario "
            "grades the hint. Decide which one is wrong BEFORE spending.",
        )
    ]


def dlq_rows_in(payload: object) -> Iterator[Mapping[str, Any]]:
    """Every dead-letter-shaped row anywhere in a read output.

    Structural, not path-based: any object carrying both ``remediation_hint``
    and ``error_message`` is a row this lint has an opinion about, wherever it
    sits. ``list_dlq_messages.items[]`` is where they live today; a tool that
    starts embedding one somewhere else gets linted without this function
    changing, which is the point — a path list would have to be maintained,
    and the maintenance is what rots.
    """
    if isinstance(payload, Mapping):
        if "remediation_hint" in payload and "error_message" in payload:
            yield payload
        for value in payload.values():
            yield from dlq_rows_in(value)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            yield from dlq_rows_in(item)


# --------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Reading:
    """One probe's outcome: the call made, and everything that came back."""

    probe: Probe
    payload: dict[str, Any] | None
    raw: str
    error: str | None

    @property
    def ok(self) -> bool:
        return self.error is None


def _payload_of(result: ToolResult) -> tuple[dict[str, Any] | None, str]:
    """First JSON object in the result, plus the full raw text of every block.

    The raw text is kept even when the JSON parses, because the dossier prints
    it when it does not — and a block that failed to parse is exactly the one
    a reader needs to see verbatim.
    """
    blocks: list[str] = []
    payload: dict[str, Any] | None = None
    for block in result.content:
        text = block.get("text")
        if isinstance(text, str):
            blocks.append(text)
            if payload is None:
                try:
                    parsed = json.loads(text)
                except ValueError:
                    continue
                if isinstance(parsed, dict):
                    payload = parsed
        else:
            blocks.append(json.dumps(block, indent=2, default=str))
    return payload, "\n".join(blocks)


def read(client: MCPClientProtocol, probe: Probe) -> Reading:
    """Make one read call. Never raises — a failed probe is part of the report."""
    try:
        result = client.call_tool(probe.tool, probe.args)
    except MCPError as err:
        return Reading(probe, None, "", f"MCPError: {err}")
    payload, raw = _payload_of(result)
    if result.is_error:
        return Reading(probe, payload, raw, "the tool reported is_error=True")
    if payload is None:
        return Reading(probe, None, raw, "no readable JSON object in the result")
    return Reading(probe, payload, raw, None)


@dataclass(frozen=True)
class PreconditionReading:
    """One precondition probe, checked exactly as ``unmet`` checks it."""

    probe: PreconditionProbe
    reading: Reading
    failures: tuple[str, ...]

    @property
    def met(self) -> bool:
        return self.reading.ok and not self.failures


def _probe_for_precondition(probe: PreconditionProbe) -> Probe:
    """Adapt a scenario's precondition probe into one of this module's calls."""
    return _probe(
        probe.tool,
        probe.arguments,
        f"expected_precondition: {probe.tool} must show "
        + "; ".join(f"{f.path} {f.describe()}" for f in probe.expect),
    )


def check_precondition(client: MCPClientProtocol, probe: PreconditionProbe) -> PreconditionReading:
    """Probe and evaluate one precondition.

    Single-shot on purpose, unlike ``runner._assert_preconditions``, which
    polls for ``probe.attempts``. A dossier is read by a human deciding
    whether to spend, so "not met on the first read" is information they want
    — the polling window exists to absorb a fault that is still landing, and
    hiding a slow seed behind it is the opposite of what this document is for.
    The runner still polls when the paid run happens, and the dossier says the
    declared window so the reader can tell a slow seed from an absent one.
    """
    reading = read(client, _probe_for_precondition(probe))
    if not reading.ok or reading.payload is None:
        return PreconditionReading(probe, reading, ())
    return PreconditionReading(probe, reading, tuple(unmet(probe, reading.payload)))


def action_targets(scenario: Scenario) -> dict[str, list[str]]:
    """Expected action tool → the resource values the scenario says it must name.

    ``expected_action_arguments`` first, because that IS the scenario's
    statement of which resource (cmd #187: "which resource is the second half
    of an exact claim"). A scenario that makes no such statement falls back to
    whatever the alert and the preconditions name for the kinds that tool's
    own arguments accept — and an empty list is itself the finding.
    """
    pool = _value_pool(scenario)
    targets: dict[str, list[str]] = {}
    for tool in scenario.expectation.expected_action_tools:
        values: list[str] = []
        for expectation in scenario.expectation.expected_action_arguments:
            if tool not in expectation.tools or not isinstance(expectation.equals, str):
                continue
            base = expectation.argument.split("[")[0].split(".")[0]
            if _KIND_BY_FIELD.get(base) and expectation.equals not in values:
                values.append(expectation.equals)
        if not values:
            for field in sorted(RESOURCE_ARG_FIELDS.get(tool, frozenset())):
                kind = _KIND_BY_FIELD.get(field)
                for value in pool.get(kind, []) if kind is not None else []:
                    if value not in values:
                        values.append(value)
        targets[tool] = values
    return targets


#: Chaos hooks whose declared product IS an incoherent row. Exactly one, and
#: the platform is where that is decided: `create_mislabeled_dlq_job` writes
#: the lab's ONE sanctioned incoherent pair (hint `replay_safe`, a permanent
#: bad-data text), reachable only through `sanctioned_incoherent_story()`,
#: gated behind an explicit `mislabel: true` with no default, and still flagged
#: by the platform's own coherence screen — plat #199 keeps all three
#: guardrails and a test there asserts the screen keeps reporting it.
#:
#: Keyed on the HOOK rather than on a scenario name or a YAML flag, because a
#: scenario must not be able to declare itself exempt: the exemption below
#: applies to the one row this hook returned during THIS dossier's own seeding,
#: so it can neither be borrowed by another scenario nor stretched to a second
#: row inside this one.
SANCTIONED_INCOHERENT_HOOKS: Final[frozenset[str]] = frozenset({"create_mislabeled_dlq_job"})


def lint_dlq_coherence(
    readings: Sequence[Reading], sanctioned_incoherent: Collection[str] = ()
) -> tuple[list[Finding], list[tuple[str, ...]]]:
    """Findings and a summary table row per dead-letter row seen anywhere.

    ``sanctioned_incoherent`` names row ids whose (hint, error) contradiction is
    the scenario's PREMISE rather than a defect — see
    ``SANCTIONED_INCOHERENT_HOOKS``. Such a row is still linted, still shown,
    and still reported: what changes is which sentence it gets. The rem-4
    finding ("decide which one is wrong BEFORE spending") would be actively
    wrong advice on a row whose whole product is the contradiction, and a
    reader who has learned that INCOHERENT means stop needs to be told plainly
    that this one is deliberate — not left to infer it from silence.

    Silence was the alternative and it is the worse one: dropping the row's
    finding would make the dossier read as if the lab's one deliberately
    mislabelled row were coherent, which is the exact species of untrue-but-
    green this file exists to prevent.
    """
    findings: list[Finding] = []
    rows: list[tuple[str, ...]] = []
    seen_ids: set[str] = set()
    sanctioned = set(sanctioned_incoherent)
    for reading in readings:
        if reading.payload is None:
            continue
        for row in dlq_rows_in(reading.payload):
            row_id = str(row.get("id", "<no id>"))
            if row_id in seen_ids:
                continue
            seen_ids.add(row_id)
            row_findings = lint_dlq_row(row, reading.probe.label)
            incoherent = [f for f in row_findings if f.kind == INCOHERENT_KIND]
            if row_id in sanctioned and incoherent:
                row_findings = [
                    Finding(
                        SANCTIONED_KIND,
                        f.subject,
                        f"{f.detail.split(' This is the ')[0]} **This row is the fixture, not a "
                        "defect.** It was written by a chaos hook whose declared product is the "
                        "contradiction (WO-R2-167), and the scenario grades the agent for "
                        "DISBELIEVING the hint: the error wins, the row is not replayed, and the "
                        "disagreement is reported. Read it and confirm it is the pair you "
                        "expected — a DIFFERENT contradiction on this row would still be a "
                        "defect, and every other row in this world is linted normally.",
                    )
                    if f.kind == INCOHERENT_KIND
                    else f
                    for f in row_findings
                ]
            findings.extend(row_findings)
            raw_error = row.get("error_message")
            error = raw_error if isinstance(raw_error, str) else ""
            verdict = "coherent"
            if row_findings:
                verdict = (
                    "sanctioned incoherent (WO-R2-167)"
                    if row_id in sanctioned and incoherent
                    else "FLAG"
                )
            rows.append(
                (
                    row_id,
                    repr(row.get("remediation_hint")),
                    ", ".join(sorted(error_families(error))) or "—",
                    verdict,
                    error.strip() or "(none)",
                )
            )
    return findings, rows


def lint_action_targets(
    scenario: Scenario,
    readings: Sequence[Reading],
    preconditions: Sequence[PreconditionReading],
) -> tuple[list[Finding], list[tuple[str, ...]]]:
    """Is each action's target actually in the world, in the asserted state?"""
    findings: list[Finding] = []
    rows: list[tuple[str, ...]] = []
    for tool, values in action_targets(scenario).items():
        if not values:
            findings.append(
                Finding(
                    "action target unnamed",
                    f"expected action `{tool}`",
                    "the scenario names no resource for this action, so nothing here "
                    "can check that its target exists. cmd #187: an action claim "
                    "that pins no resource passes on the wrong resource. Note that "
                    "an `expected_action_arguments` claim on a QUANTITY rather than "
                    "a resource — `delay_seconds` on a deferred replay — is not a "
                    "resource pin and does not clear this finding; where the action "
                    "acts on a set the scenario cannot enumerate, the bound is the "
                    "precondition's `total` plus the count claims instead.",
                )
            )
            rows.append((tool, "—", "—", "—"))
            continue
        for value in values:
            # Deduplicated by call, not by reading: the precondition probe and
            # the derived probe are frequently the same call made twice (the
            # runner asserts the premise, the agent then reads it), and listing
            # `get_dag_state(job_id=…)` twice says nothing the first mention
            # did not.
            seen = list(dict.fromkeys(r.probe.label for r in readings if r.ok and value in r.raw))
            touching = [
                p
                for p in preconditions
                if value in json.dumps(dict(p.probe.arguments), default=str)
                or (p.reading.ok and value in p.reading.raw)
            ]
            states = (
                "; ".join(f"{p.probe.tool}: " + ("MET" if p.met else "NOT MET") for p in touching)
                or "no precondition mentions it"
            )
            rows.append((tool, value, ", ".join(seen) or "NOT SEEN", states))
            if not seen:
                findings.append(
                    Finding(
                        "action target absent from every read",
                        f"`{tool}` → `{value}`",
                        "no read probe's output contains this value, so the agent "
                        "cannot reach it by reading — the plan guards would refuse "
                        "the plan (_unsourced_resource_args) even if the agent "
                        "guessed right.",
                    )
                )
            for probe_reading in touching:
                if probe_reading.met:
                    continue
                findings.append(
                    Finding(
                        "precondition not met on the action's target",
                        f"`{tool}` → `{value}`",
                        f"`{probe_reading.probe.tool}` says: "
                        + ("; ".join(probe_reading.failures) or probe_reading.reading.error or "?")
                        + ". The premise the scenario grades against is not there yet.",
                    )
                )
    return findings, rows


def lint_forbidden_furniture(
    scenario: Scenario, readings: Sequence[Reading]
) -> tuple[list[Finding], list[tuple[str, ...]]]:
    """Which forbidden values are sitting in the world, tempting the agent.

    Informational, always. A forbidden value being PRESENT is the normal case
    and often the whole design — ``remediate_runaway_saga_success`` shares its
    world with the four seeded DLQ rows on purpose, so that an agent which
    over-replays is caught. What the reader wants to know is which temptations
    are actually there, and whether the run they are about to buy is a fair
    test of resisting them: a forbidden value that is ABSENT means the
    negative assertion guarding it cannot fire in this world, which is the
    "vacuous negative" shape the grader refuses at load time and that a live
    world can reintroduce by simply not containing the row.
    """
    expectation = scenario.expectation
    buckets: tuple[tuple[str, Sequence[str]], ...] = (
        ("forbidden_replay_job_ids", expectation.forbidden_replay_job_ids),
        ("forbidden_replay_categories", expectation.forbidden_replay_categories),
        ("forbidden_evidence_contains", expectation.forbidden_evidence_contains),
        ("forbidden_action_tools", expectation.forbidden_action_tools),
    )
    findings: list[Finding] = []
    rows: list[tuple[str, ...]] = []
    for name, needles in buckets:
        for needle in needles:
            seen = [r.probe.label for r in readings if r.ok and needle in r.raw]
            rows.append((name, needle, ", ".join(seen) or "not present"))
            if seen:
                findings.append(
                    Finding(
                        "forbidden value present in the world (informational)",
                        f"{name}: `{needle}`",
                        "read back by " + ", ".join(seen) + ". Expected for most "
                        "scenarios — it is the furniture the agent must not touch. "
                        "Confirm the agent has an honest way to tell it apart from "
                        "the thing it IS supposed to act on.",
                    )
                )
            else:
                findings.append(
                    Finding(
                        "forbidden value absent from the world (informational)",
                        f"{name}: `{needle}`",
                        "no read output contains it, so the assertion guarding it "
                        "cannot fire in this world. Not wrong — just not measured "
                        "by the run you are about to buy.",
                    )
                )
    return findings, rows


# --------------------------------------------------------------------------
# Reset, and the baseline re-audit
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BaselineLine:
    name: str
    expected: str
    observed: str
    passed: bool


def _compose_file() -> str:
    return os.environ.get(_COMPOSE_FILE_ENV) or _DEFAULT_COMPOSE_FILE


def chaos_key_count() -> tuple[int | None, str]:
    """Number of ``chaos:*`` keys in the demo stack's redis, or why not.

    Read through ``docker compose exec redis redis-cli``, not through an MCP
    tool, because the platform exposes no read that enumerates its own chaos
    keys — the runbook's baseline table names this check and the coordinator
    has always run it by hand. ``--scan`` rather than ``KEYS`` so a large
    keyspace does not block the server.
    """
    command = [
        "docker",
        "compose",
        "-f",
        _compose_file(),
        "exec",
        "-T",
        _REDIS_SERVICE,
        "redis-cli",
        "--scan",
        "--pattern",
        "chaos:*",
    ]
    try:
        # Fixed argv, never a shell string: nothing here interpolates a
        # scenario name or any other caller-supplied text into a command.
        done = subprocess.run(
            command, cwd=_REPO_ROOT, capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        return None, f"could not run the scan: {type(err).__name__}: {err}"
    if done.returncode != 0:
        return None, f"exit {done.returncode}: {(done.stderr or done.stdout).strip()[:300]}"
    keys = [line for line in done.stdout.splitlines() if line.strip()]
    return len(keys), ("none" if not keys else ", ".join(sorted(keys)))


def audit_baseline(client: MCPClientProtocol) -> list[BaselineLine]:
    """The seeded baseline, re-read after the reset. PASS/FAIL per line."""
    lines: list[BaselineLine] = []
    for tool, expected, label in (
        ("list_dlq_messages", BASELINE_DLQ_TOTAL, "DLQ total"),
        ("list_active_alerts", BASELINE_ACTIVE_ALERTS, "active alerts"),
    ):
        reading = read(client, _probe(tool, {}, "baseline re-audit"))
        if not reading.ok or reading.payload is None:
            lines.append(BaselineLine(label, str(expected), reading.error or "unreadable", False))
            continue
        observed = reading.payload.get("total")
        lines.append(BaselineLine(label, str(expected), str(observed), observed == expected))
    count, detail = chaos_key_count()
    lines.append(
        BaselineLine(
            "redis `chaos:*` keys",
            str(BASELINE_CHAOS_KEYS),
            f"{count} ({detail})" if count is not None else f"unreadable — {detail}",
            count == BASELINE_CHAOS_KEYS,
        )
    )
    return lines


def run_reset() -> tuple[int, str]:
    """``make eval-reset PURGE_IDEMPOTENCY=1`` — the protocol's own reset.

    Shelled out rather than reimplemented: the reset lives inside the platform
    container (``/app/scripts/reset_eval_state.py``) and the Makefile owns the
    compose file, the service name and the purge gate — including the
    ``ifeq ($(PURGE_IDEMPOTENCY),1)`` literal-1 comparison that exists because
    make's ``$(if)`` turned ``PURGE_IDEMPOTENCY=0`` ON. A second caller
    spelling those out again is a second thing to get wrong.

    ``PURGE_IDEMPOTENCY=1`` always: a reused idempotency key answers a replay
    with ``replayed=1, ok=true`` out of the 24-hour cache while nothing runs
    (LESSONS 2026-09-07, demonstrated live at $0), and a dossier that leaves
    the cache warm hands the next paid run that trap.
    """
    command = ["make", "eval-reset", "PURGE_IDEMPOTENCY=1"]
    try:
        # Fixed argv, never a shell string: nothing here interpolates a
        # scenario name or any other caller-supplied text into a command.
        done = subprocess.run(
            command, cwd=_REPO_ROOT, capture_output=True, text=True, timeout=300, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        return 1, f"{' '.join(command)} could not run: {type(err).__name__}: {err}"
    return done.returncode, (done.stdout + done.stderr).strip()


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _json_block(payload: object) -> str:
    """Pretty JSON, in full. Nothing here truncates — that is the whole point."""
    return "```json\n" + json.dumps(payload, indent=2, default=str) + "\n```"


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    if not rows:
        return "_(none)_"
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for row in rows:
        cells = [str(cell).replace("|", "\\|").replace("\n", " ") for cell in row]
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def _findings_block(findings: Sequence[Finding]) -> str:
    if not findings:
        return "_No findings._"
    return "\n".join(f"- **{f.kind}** — {f.subject}\n  {f.detail}" for f in findings)


_HOW_TO_READ: Final[str] = """\
This is a **content** review, not a machinery check. Two readiness sweeps of
`remediate_runaway_saga_success` passed on mechanics and still let a
self-contradictory world through, because nobody read the fault's fields as
the agent would (archive `efdc3b2a9864`, 2026-09-07, ≈$0.15).

So read every field below and ask, of each fact:

1. **Does it support the behaviour the scenario expects, or contradict it?**
   A `replay_safe` hint sitting on a permanent data-bug error text is a
   contradiction. An agent that reasons from the error will escalate; the
   scenario grades the hint; the run is red for the wrong reason.
2. **Could the agent reach this fact by reading?** Every value the expected
   action names must appear in some read output, or the plan guards refuse
   the plan the scenario grades.
3. **Is the verify signal one this world can actually move?** (ADR 0025.)
4. **What is the laziest trajectory that passes?** If it is not the correct
   behaviour, fix the claim before spending (PROTOCOL step 4).

Findings below are **findings, not verdicts**. Nothing here decides whether
to run; a human does, and records the reasoning in the readiness note.
"""


def render(
    *,
    scenario: Scenario,
    generated_at: datetime,
    invocation_id: str,
    settings_url: str,
    head: str,
    seeding: str,
    preconditions: Sequence[PreconditionReading],
    readings: Sequence[Reading],
    notes: Sequence[str],
    dlq_findings: Sequence[Finding],
    dlq_rows: Sequence[Sequence[str]],
    target_findings: Sequence[Finding],
    target_rows: Sequence[Sequence[str]],
    furniture_findings: Sequence[Finding],
    furniture_rows: Sequence[Sequence[str]],
    reset_code: int,
    reset_output: str,
    baseline: Sequence[BaselineLine],
) -> str:
    expectation = scenario.expectation
    out: list[str] = []
    add = out.append

    add(f"# Fault-world dossier — `{scenario.name}`")
    add("")
    add(
        _table(
            ("field", "value"),
            (
                ("generated (UTC)", generated_at.isoformat()),
                ("invocation id", invocation_id),
                ("commander HEAD", head),
                ("platform MCP", settings_url),
                ("read principal", "PLATFORM_SMOKE_TOKEN (read-scoped)"),
                ("chaos principal", "PLATFORM_TOKEN (the runner's own chaos path)"),
                ("LLM calls", "0 — this module constructs no LLM client"),
                ("Tier-1 calls", "0 — chaos hook + `make eval-reset` are the only writes"),
                ("cost", "$0.00"),
            ),
        )
    )
    add("")
    add("## 0. How to read this")
    add("")
    add(_HOW_TO_READ)

    add("## 1. What the scenario claims")
    add("")
    add(f"> {scenario.description.strip()}")
    add("")
    add(
        _table(
            ("claim", "value"),
            (
                ("expected terminal state", str(expectation.expected_terminal_state)),
                ("expected action tools", ", ".join(expectation.expected_action_tools) or "none"),
                ("max tool calls", str(expectation.max_tool_calls)),
                (
                    "chaos hook",
                    scenario.chaos_setup.name if scenario.chaos_setup else "none declared",
                ),
            ),
        )
    )
    add("")
    add("**Evidence claims**")
    add("")
    # `group` is the column that makes an `any_of` readable in a flat table:
    # its members are ALTERNATIVES, and a reviewer who reads them as a
    # conjunction would conclude the scenario demands two verify reads. The
    # `args` and `after` columns exist for the same reason — a claim scoped to
    # one call shape or to the post-action window says something a reviewer
    # cannot infer from tools/field alone, and this table is the pre-run
    # review surface (PROTOCOL step 4) where verify shapes get checked.
    evidence_rows: list[tuple[str, ...]] = []
    for position, claim_entry in enumerate(expectation.expected_evidence_fields, start=1):
        members = (
            claim_entry.any_of if isinstance(claim_entry, AnyOfExpectation) else (claim_entry,)
        )
        for member in members:
            evidence_rows.append(
                (
                    f"any_of #{position}" if len(members) > 1 else "—",
                    ", ".join(member.tools),
                    member.field,
                    member.describe(),
                    f"{member.which}/{member.rows}",
                    (
                        f"{member.where.field} {member.where.describe()}"
                        if member.where is not None
                        else "—"
                    ),
                    (
                        ", ".join(f"{k}={v!r}" for k, v in sorted(member.call_arguments.items()))
                        if member.call_arguments is not None
                        else "—"
                    ),
                    ", ".join(member.before_tools) or "—",
                    ", ".join(member.after_tools) or "—",
                )
            )
    add(
        _table(
            (
                "group",
                "tools",
                "field",
                "comparator",
                "which/rows",
                "where",
                "args",
                "before",
                "after",
            ),
            evidence_rows,
        )
    )
    add("")
    add("**Action-argument claims**")
    add("")
    add(
        _table(
            ("tools", "argument", "comparator"),
            [
                (", ".join(e.tools), e.argument, e.describe())
                for e in expectation.expected_action_arguments
            ],
        )
    )
    add("")
    # The handoff's own claims, and they were missing from this section until
    # 2026-09-08 (WO-R2-164). On an escalate-with-an-action scenario the
    # briefing IS the product — `dlq_human_required_escalates` grades three
    # strings in it, `dlq_mixed_partial` six — and a pre-spend reviewer reading
    # only the two tables above would not have known those claims existed, let
    # alone whether this world can satisfy them. Same species as the gap this
    # module was built to close: the mechanics were checked and the content was
    # not.
    add("**Briefing claims** — substrings the handoff must carry")
    add("")
    add(
        _table(
            ("required substring",),
            [(text,) for text in expectation.expect_briefing_contains],
        )
    )
    add("")

    add("## 2. Chaos seeded")
    add("")
    if scenario.chaos_setup is None:
        add("This scenario declares no `chaos_setup`. Nothing was seeded.")
    else:
        add(f"Hook `{scenario.chaos_setup.name}`, fired through `invoke_chaos_hook` — the")
        add("same function `run_scenario` calls, with the same arguments and principal.")
        add("")
        add("**Arguments**")
        add("")
        add(_json_block(dict(scenario.chaos_setup.arguments)))
        add("")
        add("**Result**")
        add("")
        add(seeding)
    add("")

    add("## 3. Preconditions — the premise the runner will assert")
    add("")
    if not preconditions:
        add("_This scenario declares no `expected_precondition`._")
    for entry in preconditions:
        status = "MET" if entry.met else "NOT MET"
        add(f"### `{entry.probe.tool}` — **{status}**")
        add("")
        add("**Arguments**")
        add("")
        add(_json_block(dict(entry.probe.arguments)))
        add("")
        add(
            _table(
                ("path", "expected", "status"),
                [
                    (
                        f.path,
                        f.describe(),
                        (
                            "met"
                            if not any(f.path in failure for failure in entry.failures)
                            else "NOT MET"
                        ),
                    )
                    for f in entry.probe.expect
                ],
            )
        )
        add("")
        if entry.probe.attempts > 1:
            add(
                f"_The runner polls this probe up to {entry.probe.attempts}× at "
                f"{entry.probe.delay_seconds}s. The dossier reads it once — a "
                "NOT MET here may still be a fault that is landing._"
            )
            add("")
        if entry.failures:
            add("**Failures**")
            add("")
            for failure in entry.failures:
                add(f"- {failure}")
            add("")
        if entry.reading.error is not None:
            add(f"**Probe error:** {entry.reading.error}")
            add("")
        add("**Full output**")
        add("")
        add(
            _json_block(entry.reading.payload)
            if entry.reading.payload is not None
            else "```\n" + (entry.reading.raw or "(no content)") + "\n```"
        )
        add("")

    add("## 4. Every read the agent is expected to make")
    add("")
    add(
        f"{len(readings)} probe(s), derived mechanically from `ALERT_SUBJECT_PROBES`, "
        "`SOURCE_ROW_FOR_ACTION`, `SOURCE_LISTING_FOR_ACTION`, "
        "`VERIFY_PROBE_FOR_ACTION` and the scenario's own evidence claims. "
        "Nothing here is hand-listed."
    )
    add("")
    for reading in readings:
        add(f"### `{reading.probe.label}`")
        add("")
        for origin in reading.probe.origins:
            add(f"- _{origin}_")
        add("")
        if reading.error is not None:
            add(f"**FAILED:** {reading.error}")
            add("")
        add(
            _json_block(reading.payload)
            if reading.payload is not None
            else "```\n" + (reading.raw or "(no content)") + "\n```"
        )
        add("")
    if notes:
        add("### Probes that could not be derived")
        add("")
        add(
            "Each line is a fact about the SCENARIO, not about the world: something "
            "the agent is expected to read that the scenario never names."
        )
        add("")
        for note in notes:
            add(f"- {note}")
        add("")

    add("## 5. Coherence lint — findings, not verdicts")
    add("")
    add("### 5.1 Dead-letter rows: does the hint agree with the error text?")
    add("")
    add(_table(("id", "hint", "error reads as", "lint", "error_message"), dlq_rows))
    add("")
    add(_findings_block(dlq_findings))
    add("")
    add("### 5.2 Is each action's target in the world, in the asserted state?")
    add("")
    add(_table(("action tool", "target", "seen in", "precondition"), target_rows))
    add("")
    add(_findings_block(target_findings))
    add("")
    add("### 5.3 Tempting furniture (informational)")
    add("")
    add(_table(("list", "value", "read back by"), furniture_rows))
    add("")
    add(_findings_block(furniture_findings))
    add("")

    add("## 6. Reset, and the baseline re-audit")
    add("")
    add("`make eval-reset PURGE_IDEMPOTENCY=1` — the protocol's own reset path.")
    add("")
    add(f"Exit code **{reset_code}**.")
    add("")
    add("```\n" + (reset_output or "(no output)") + "\n```")
    add("")
    add(
        _table(
            ("check", "expected", "observed", "result"),
            [
                (line.name, line.expected, line.observed, "PASS" if line.passed else "**FAIL**")
                for line in baseline
            ],
        )
    )
    add("")
    if all(line.passed for line in baseline):
        add("**Baseline re-audit: PASS.** The world is back where it started.")
    else:
        add(
            "**Baseline re-audit: FAIL.** The world is NOT back at the seeded "
            "baseline. Do not start a paid run on it — see the runbook's pre-run "
            "checklist step 4, and note that reset does not clear organic alerts "
            "(WO-R2-131)."
        )
    add("")
    add("## 7. Before you ask for the go")
    add("")
    add(
        "- [ ] Every field in §3 and §4 read, and each one checked against the "
        "behaviour §1 expects.\n"
        "- [ ] Every finding in §5 either explained or fixed.\n"
        "- [ ] The laziest passing trajectory is the correct behaviour "
        "(PROTOCOL step 4).\n"
        "- [ ] Baseline re-audit PASS in §6.\n"
        "- [ ] This dossier pasted into the readiness note, with your answers."
    )
    add("")
    return "\n".join(out)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _head() -> str:
    try:
        # Fixed argv, never a shell string: nothing here interpolates a
        # scenario name or any other caller-supplied text into a command.
        done = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return done.stdout.strip() or "unknown"


def _select(only: Sequence[str], scenarios: Sequence[Scenario]) -> tuple[Scenario | None, int]:
    """The ONLY guard, in the shape ``evals/runner.py`` uses for a live run.

    Three refusals, all exit 2 and all before anything touches the platform:
    no name, more than one name, and a name that is not a scenario's FULL
    name. The last one prints the did-you-mean list the runner prints, because
    the failure it prevents is the same one: a substring silently widens a
    selection, and this selection SEEDS CHAOS INTO A SHARED WORLD.
    """
    if not only:
        print(
            "DOSSIER FAIL: --only <scenario_name> is required. A dossier seeds the "
            "scenario's chaos hook into the shared eval world and then resets it; "
            "there is no meaningful 'all scenarios' form of that."
        )
        print(
            "Name exactly one scenario, e.g. make world-dossier ONLY=remediate_dlq_backlog_success"
        )
        print("nothing was seeded")
        return None, EXIT_SELECTION
    if len(only) > 1:
        print(
            f"DOSSIER FAIL: {len(only)} scenarios named ({', '.join(only)}). One "
            "dossier seeds one fault into one shared world; two would interleave "
            "their faults and neither reading would be about the scenario you ran."
        )
        print("nothing was seeded")
        return None, EXIT_SELECTION
    wanted = only[0]
    known = {s.name: s for s in scenarios}
    if wanted not in known:
        near = sorted(name for name in known if wanted in name)
        print(f"DOSSIER FAIL: {wanted!r} is not a scenario name.")
        print(
            "A dossier selects by full scenario name, exactly as a live run does — "
            "a substring silently widens the selection."
        )
        if near:
            print("Did you mean:")
            for name in near:
                print(f"  ONLY={name}")
        print("nothing was seeded")
        return None, EXIT_SELECTION
    return known[wanted], EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.dossier",
        description="Free, zero-LLM fault-world content review for one scenario.",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="the scenario's FULL name; required, exactly one",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    only: list[str] = [part for value in args.only for part in str(value).split(",") if part]

    scenarios = load_scenarios(_SCENARIOS_DIR)
    scenario, code = _select(only, scenarios)
    if scenario is None:
        return code

    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError as err:
        fields = ", ".join(
            ".".join(str(part) for part in detail["loc"]) or "(settings)" for detail in err.errors()
        )
        print(f"DOSSIER FAIL (env): invalid or missing settings — {fields}")
        print("nothing was seeded")
        return EXIT_PREFLIGHT

    # The read principal is the SMOKE token, deliberately. Everything in
    # sections 3 and 4 is a read, and running them under a token that
    # physically cannot write is what makes "this check is free and safe" a
    # property of the credential rather than of this file staying correct.
    if settings.platform_smoke_token is None or not (
        settings.platform_smoke_token.get_secret_value().strip()
    ):
        print(
            "DOSSIER FAIL (env): PLATFORM_SMOKE_TOKEN is not set in .env. The "
            "dossier reads under the read-scoped principal so a bug in it cannot "
            "mutate the world; there is no fall-back to the write token."
        )
        print("nothing was seeded")
        return EXIT_PREFLIGHT

    read_client = make_client(settings, token=settings.platform_smoke_token.get_secret_value())
    try:
        # Reachability, BEFORE seeding. A stack that cannot answer a listing
        # cannot be left holding a half-seeded fault either.
        probe = read(read_client, _probe("list_dlq_messages", {}, "reachability"))
        if not probe.ok:
            print(f"DOSSIER FAIL (stack): the platform did not answer a read — {probe.error}")
            print("refusing to seed chaos into a stack that cannot be read")
            print("nothing was seeded")
            return EXIT_PREFLIGHT

        generated_at = datetime.now(UTC)
        invocation_id = uuid.uuid4().hex[:12]

        seeding = "_(no chaos_setup)_"
        seeding_failed = False
        if scenario.chaos_setup is not None:
            try:
                result = invoke_chaos_hook(
                    str(settings.platform_mcp_url),
                    settings.platform_token.get_secret_value(),
                    scenario.chaos_setup.name,
                    dict(scenario.chaos_setup.arguments),
                )
                seeding = _json_block(result)
            except ChaosInvocationError as err:
                seeding_failed = True
                seeding = f"**SEEDING FAILED** — {err}"
                print(f"DOSSIER: chaos seeding failed — {err}")

        # The row this scenario's OWN sanctioned hook just wrote, if any. Read
        # off the hook's reply rather than computed or configured, so the §5.1
        # exemption below can only ever apply to a row the platform confirmed
        # it created during this dossier's own seeding.
        sanctioned_incoherent: set[str] = set()
        if (
            scenario.chaos_setup is not None
            and scenario.chaos_setup.name in SANCTIONED_INCOHERENT_HOOKS
            and not seeding_failed
        ):
            seeded_id = result.get("job_id") if isinstance(result, dict) else None
            if isinstance(seeded_id, str) and seeded_id:
                sanctioned_incoherent.add(seeded_id)

        preconditions = [check_precondition(read_client, p) for p in scenario.expected_precondition]
        probes, notes = derive_probes(scenario)
        readings = [read(read_client, p) for p in probes]

        # The lint reads everything that was read, preconditions included: the
        # precondition's filtered DLQ page is a reading of the world too, and
        # the row it returns is the one the scenario is about.
        all_readings = [entry.reading for entry in preconditions] + readings
        dlq_findings, dlq_rows = lint_dlq_coherence(all_readings, sanctioned_incoherent)
        target_findings, target_rows = lint_action_targets(scenario, all_readings, preconditions)
        furniture_findings, furniture_rows = lint_forbidden_furniture(scenario, all_readings)

        reset_code, reset_output = run_reset()
        baseline = audit_baseline(read_client)
    finally:
        close = getattr(read_client, "close", None)
        if callable(close):
            close()

    document = render(
        scenario=scenario,
        generated_at=generated_at,
        invocation_id=invocation_id,
        settings_url=str(settings.platform_mcp_url),
        head=_head(),
        seeding=seeding,
        preconditions=preconditions,
        readings=readings,
        notes=notes,
        dlq_findings=dlq_findings,
        dlq_rows=dlq_rows,
        target_findings=target_findings,
        target_rows=target_rows,
        furniture_findings=furniture_findings,
        furniture_rows=furniture_rows,
        reset_code=reset_code,
        reset_output=reset_output,
        baseline=baseline,
    )
    path = artifacts.write_versioned(
        "dossier",
        scenario.name,
        content=document,
        timestamp=generated_at,
        invocation_id=invocation_id,
    )
    print(document)
    print(f"\ndossier written: {path}")

    if seeding_failed:
        return EXIT_SEEDING
    if reset_code != 0:
        return EXIT_RESET
    if not all(line.passed for line in baseline):
        return EXIT_BASELINE_DIRTY
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    raise SystemExit(main(sys.argv[1:]))

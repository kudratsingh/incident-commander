"""Fault-world content review, mechanised — ``make world-dossier ONLY=<scenario>``.

Two readiness sweeps passed `remediate_runaway_saga_success` on MECHANICS and its run A still
failed on a self-contradictory world (archive ``efdc3b2a9864``) that one free probe would have
shown. So: seed ``Scenario.chaos`` (ADR 0037), check the preconditions, print every derived read
untruncated, lint as FINDINGS NOT VERDICTS, then reset and re-audit. Zero LLM, zero Tier-1.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
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
from evals.world_audit import (
    BASELINE_ACTIVE_ALERTS as BASELINE_ACTIVE_ALERTS,
)
from evals.world_audit import (
    BASELINE_CHAOS_KEYS as BASELINE_CHAOS_KEYS,
)
from evals.world_audit import (
    BASELINE_DLQ_TOTAL as BASELINE_DLQ_TOTAL,
)
from evals.world_audit import (
    BaselineLine as BaselineLine,
)
from evals.world_audit import (
    Probe as Probe,
)
from evals.world_audit import (
    Reading as Reading,
)
from evals.world_audit import (
    _payload_of as _payload_of,
)
from evals.world_audit import (
    _probe as _probe,
)
from evals.world_audit import (
    audit_baseline as audit_baseline,
)
from evals.world_audit import (
    chaos_key_count as chaos_key_count,
)
from evals.world_audit import (
    read as read,
)
from evals.world_audit import (
    read_result as read_result,
)
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
from incident_commander.config import ChaosTokenNotConfigured, Settings
from incident_commander.tools.mcp_client import MCPClientProtocol, make_client
from incident_commander.tools.policies import RESOURCE_ARG_FIELDS, Tier, tier_of
from incident_commander.tools.registry import TOOL_REGISTRY

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
_SCENARIOS_DIR: Final[Path] = _REPO_ROOT / "evals" / "scenarios"

#: Exit codes in ``evals/runner.py``'s vocabulary (2 selection, 3 preflight); the rest
#: are this tool's own and are documented in ``docs/runbook.md``.
EXIT_OK: Final[int] = 0
EXIT_SELECTION: Final[int] = 2
EXIT_PREFLIGHT: Final[int] = 3
EXIT_BASELINE_DIRTY: Final[int] = 4
EXIT_SEEDING: Final[int] = 5
EXIT_RESET: Final[int] = 6


# --------------------------------------------------------------------------
# Probe derivation
# --------------------------------------------------------------------------
#
# Every read is DERIVED, never hand-listed: a written probe list rots the way the SMOKE_ONLY
# list WO-R2-41 deleted did — reading LESS of the world than its reader believes was read.
#
# ``_KIND_BY_FIELD`` is the derivation's one new fact: which argument names mean the same KIND
# of resource, so a value stated in one place can fill an argument elsewhere. Total over
# ``RESOURCE_ARG_FIELDS`` (``TestKindByFieldIsTotal``), so a new one cannot produce a guess.
_KIND_BY_FIELD: Final[dict[str, str]] = {
    "consumer_group": "consumer_group",
    "id": "incident_id",
    "job_id": "job_id",
    "job_ids": "job_id",
    "key": "cache_key",
    "root_job_id": "job_id",
    "trace_id": "trace_id",
}


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

    ``investigation.alert_subject`` returns only the FIRST match; a dossier wants all, over the
    same map and levels. ``SubjectMatch.UNFILTERED`` is skipped: its probe is the ABSENCE of an
    argument, and a scope word must never become an argument value.
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

    Reads ``investigation.alert_subject``'s answer rather than re-walking the map, so
    an unadmitted scope word is inert in both places or in neither.
    """
    subject = alert_subject(alert)
    if subject is None or subject.match is not SubjectMatch.UNFILTERED:
        return None
    return subject.alert_field, ALERT_SUBJECT_PROBES[subject.alert_field]


def _value_pool(scenario: Scenario) -> dict[str, list[str]]:
    """Resource values this scenario states, grouped by kind of resource.

    Three places the scenario has ALREADY written the value down, so nothing is invented: the
    alert payload via ``ALERT_SUBJECT_PROBES``, the ``expected_precondition`` arguments, and
    ``expected_action_arguments``' ``equals`` values.
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
        # ``argument`` is a resolve_path (``job_ids[]`` names a list's elements); the
        # base segment is the argument name.
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

    A tool with no resource-naming argument gets one call with none, like the agent's unfiltered
    read; one with such an argument gets a call per value of that kind. An unnamed kind produces
    a NOTE, never a guess: a probe aimed at a made-up resource reads as evidence and is not.
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

    Numbered below, in the agent's own order, from ``ALERT_SUBJECT_PROBES`` (cmd #177),
    ``SOURCE_ROW_FOR_ACTION`` (ADR 0027, where the rem-4 contradiction lived),
    ``SOURCE_LISTING_FOR_ACTION`` (ADR 0028), ``VERIFY_PROBE_FOR_ACTION`` (ADR 0025) and the
    evidence claims themselves. A note is a finding about the SCENARIO, not about the world.
    """
    pool = _value_pool(scenario)
    probes: list[Probe] = []
    notes: list[str] = []

    # 1. ALERT_SUBJECT_PROBES: the read that observes each resource the alert names.
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
    # 2. Its unfiltered arm (ADR 0032), separate because that probe is the ABSENCE of a filter —
    #    the same call `_fill` makes for a tool naming no resource, so the two merge.
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

    # 3. SOURCE_ROW_FOR_ACTION: the listing whose rows carry the field the action depends on.
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

    # 4. SOURCE_LISTING_FOR_ACTION: coverage of the slice a filter will expand to, UNFILTERED.
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

    # 5. VERIFY_PROBE_FOR_ACTION: the read that observes the change, taken PRE-action here.
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

    # 6. Every read tool an evidence claim names, filled from the value pool.
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
# THE TABLE, grounded in the platform's own enum (app/models/enums.py), the only authority on
# what a hint MEANS: replay_safe = transient/poison, wait_and_replay = external dep down,
# human_required = persistent bug. Matched by substring against what the platform's writers emit.
#
# Two deliberate asymmetries: a transient text is coherent with BOTH replay_safe and
# wait_and_replay (they differ about WHEN to replay, not whether), while rate_limit is
# wait_and_replay ONLY and bad_data human_required ONLY — replay_safe + bad_data is rem-4.
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

#: hint value → the error families it sanctions; ``None`` is the platform's UNKNOWN and
#: sanctions all of them. A hint absent from this table is itself a finding.
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


#: The lint's name for a hint/error contradiction, and for the one case where the contradiction
#: is the point. Constants, because the verdict column and the rewrite below compare on them.
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

    Empty means positively coherent, not merely "nothing to say": an unclassifiable
    error text produces a finding of its own saying the lint could not classify it.
    """
    row_id = str(row.get("id", "<no id>"))
    raw_hint = row.get("remediation_hint")
    hint = raw_hint if isinstance(raw_hint, str) else None
    error = row.get("error_message")
    subject = f"DLQ row `{row_id}` (seen in {seen_in})"

    # 1. A hint the platform does not have: the row is uncategorised while looking categorised.
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
    # 2. No error text, so there is no pair to check.
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

    # 3. Text the table cannot classify — NO OPINION, said out loud rather than passed silently.
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

    # 4. Hint and text disagree: the rem-4 shape, and the reason to decide before spending.
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

    Structural, not path-based: any object with both ``remediation_hint`` and
    ``error_message`` is a row this lint has an opinion about, wherever it sits.
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

    Single-shot, unlike ``runner._assert_preconditions``: a human deciding whether to
    spend wants "not met on the first read", not a slow seed hidden behind the polling
    window. The declared window is printed, so slow reads differently from absent.
    """
    reading = read(client, _probe_for_precondition(probe))
    if not reading.ok or reading.payload is None:
        return PreconditionReading(probe, reading, ())
    return PreconditionReading(probe, reading, tuple(unmet(probe, reading.payload)))


def action_targets(scenario: Scenario) -> dict[str, list[str]]:
    """Expected action tool → the resource values the scenario says it must name.

    ``expected_action_arguments`` first, because that IS the scenario's statement of
    which resource (cmd #187). Without one, fall back to what the alert and the
    preconditions name for that tool's argument kinds; an empty list is the finding.
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


#: Chaos hooks whose declared product IS an incoherent row. Exactly one, decided on the platform
#: (plat #199). Keyed on the HOOK, not a scenario name or a YAML flag, so no scenario can declare
#: itself exempt: the exemption reaches only the row this hook returned during THIS seeding.
SANCTIONED_INCOHERENT_HOOKS: Final[frozenset[str]] = frozenset({"create_mislabeled_dlq_job"})


def lint_dlq_coherence(
    readings: Sequence[Reading], sanctioned_incoherent: Collection[str] = ()
) -> tuple[list[Finding], list[tuple[str, ...]]]:
    """Findings and a summary table row per dead-letter row seen anywhere.

    ``sanctioned_incoherent`` names rows whose contradiction is the scenario's PREMISE
    (``SANCTIONED_INCOHERENT_HOOKS``). Still linted, shown and reported — only the
    sentence changes, because silence would read as if that row were coherent.
    """
    findings: list[Finding] = []
    rows: list[tuple[str, ...]] = []
    seen_ids: set[str] = set()
    sanctioned = set(sanctioned_incoherent)
    for reading in readings:
        if reading.payload is None:
            continue
        # 1. Every dead-letter-shaped row in every reading, each linted once.
        for row in dlq_rows_in(reading.payload):
            row_id = str(row.get("id", "<no id>"))
            if row_id in seen_ids:
                continue
            seen_ids.add(row_id)
            row_findings = lint_dlq_row(row, reading.probe.label)
            incoherent = [f for f in row_findings if f.kind == INCOHERENT_KIND]
            # 2. A sanctioned row's finding is rewritten, never dropped.
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
            # 3. The summary row: id, hint, what the text reads as, and the verdict.
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
        # 1. An action that pins no resource: the finding is that nothing can be checked.
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
            # 2. Can the agent reach this value by reading? Deduplicated by CALL: a precondition
            #    and a derived probe are often the same call made twice.
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
            # 3. And is the premise the scenario grades against already true of it?
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

    Informational: PRESENT is the normal case and often the whole design. An ABSENT
    forbidden value means the negative assertion guarding it cannot fire — the "vacuous
    negative" a live world reintroduces by omission.
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
# Seeding the scenario's fault plan
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Seeding:
    """What this dossier's own seeding did — for the document and for the lint."""

    #: The rendered "Result" block of § 2: one JSON block per hook that fired, or the
    #: failure that stopped the plan.
    markdown: str
    #: True when a hook was refused, so the world is half-seeded and the §5.1 exemption
    #: must not claim a row the platform never wrote.
    failed: bool = False
    #: Row ids a SANCTIONED_INCOHERENT_HOOKS hook reported creating, off its own reply.
    sanctioned_incoherent: frozenset[str] = frozenset()


def seed_chaos(
    scenario: Scenario,
    url: str,
    token: str,
    *,
    invoke: Callable[[str, str, str, dict[str, Any]], Any] = invoke_chaos_hook,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> Seeding:
    """Fire the scenario's whole fault plan, in the order the runner fires it.

    Never the legacy ``chaos_setup`` (``None`` on a plan-declaring scenario, ADR 0037), or this
    would lint an un-faulted world as the scenario's. ``teardown`` is NOT fired here — the reset
    in § 6 undoes more than a compensator can.
    """
    plan = scenario.chaos
    if not plan.setup:
        return Seeding(markdown="_(this scenario declares no chaos)_")

    total = len(plan.setup)
    blocks: list[str] = []
    sanctioned: set[str] = set()
    # 1. Hooks in declared order, stopping at the FIRST failure, as ``run_scenario`` does.
    for position, hook in enumerate(plan.setup, start=1):
        try:
            result = invoke(url, token, hook.name, dict(hook.arguments))
        except ChaosInvocationError as err:
            log(f"DOSSIER: chaos seeding failed — {err}")
            blocks.append(f"**SEEDING FAILED** — hook {position}/{total} `{hook.name}` — {err}")
            return Seeding(
                markdown="\n\n".join(blocks),
                failed=True,
                sanctioned_incoherent=frozenset(sanctioned),
            )
        # 2. The label appears only where there is more than one reply to tell apart.
        label = "" if total == 1 else f"**Hook {position}/{total} — `{hook.name}`**\n\n"
        blocks.append(label + _json_block(result))
        # 3. The row this hook wrote, off its OWN reply, so the §5.1 exemption reaches only a
        #    row the platform confirmed it created.
        if hook.name in SANCTIONED_INCOHERENT_HOOKS:
            seeded_id = result.get("job_id") if isinstance(result, dict) else None
            if isinstance(seeded_id, str) and seeded_id:
                sanctioned.add(seeded_id)

    if plan.settle_seconds:
        sleep(plan.settle_seconds)
    return Seeding(markdown="\n\n".join(blocks), sanctioned_incoherent=frozenset(sanctioned))


# --------------------------------------------------------------------------
# Reset, and the baseline re-audit
# --------------------------------------------------------------------------


def run_reset() -> tuple[int, str]:
    """``make eval-reset PURGE_IDEMPOTENCY=1`` — the protocol's own reset.

    Shelled out, not reimplemented: the Makefile owns the compose file, the service name and the
    purge gate. Always purging, because a warm idempotency cache answers a replay
    ``replayed=1, ok=true`` while nothing runs (LESSONS 2026-09-07).
    """
    command = ["make", "eval-reset", "PURGE_IDEMPOTENCY=1"]
    try:
        # Fixed argv, never a shell string: nothing caller-supplied is interpolated.
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
    """A Markdown table, or a placeholder when there is nothing to show."""
    if not rows:
        return "_(none)_"
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for row in rows:
        cells = [str(cell).replace("|", "\\|").replace("\n", " ") for cell in row]
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def _findings_block(findings: Sequence[Finding]) -> str:
    """The findings as a bullet list, or a line saying there were none."""
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
    """Assemble the whole dossier document from everything that was seeded, read and reset."""
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
                (
                    "chaos principal",
                    "PLATFORM_CHAOS_TOKEN (the runner's own chaos path; the "
                    "agent's PLATFORM_TOKEN cannot seed and cannot read that "
                    "anything was seeded)",
                ),
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
                    # Never ``chaos_setup``: it is ``None`` on a plan-declaring scenario, so
                    # this row would read "none declared" over a two-fault world (ADR 0037).
                    "chaos hooks",
                    ", ".join(f"`{hook.name}`" for hook in scenario.chaos.setup) or "none declared",
                ),
            ),
        )
    )
    add("")
    add("**Evidence claims**")
    add("")
    # `group` makes an `any_of` readable in a flat table: its members are ALTERNATIVES, and read
    # as a conjunction they would look like two demanded verify reads. `args` and `after` too.
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
    # The handoff's own claims (WO-R2-164). On an escalate-with-an-action scenario the briefing
    # IS the product, and the two tables above do not say those claims exist.
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
    # Through ``Scenario.chaos``: ``chaos_setup`` would report a two-fault world as
    # seeding nothing (ADR 0037).
    plan = scenario.chaos
    if not plan.setup:
        add("This scenario declares no chaos. Nothing was seeded.")
    else:
        total = len(plan.setup)
        hooks = "hook" if total == 1 else "hooks"
        add(f"{total} setup {hooks}, fired in declared order through `invoke_chaos_hook` — the")
        add("same function `run_scenario` calls, with the same arguments and the same")
        add("evaluator principal (`PLATFORM_CHAOS_TOKEN`).")
        for position, hook in enumerate(plan.setup, start=1):
            # The position prefix earns its place only where there is an order to read.
            place = "" if total == 1 else f"{position}/{total} — "
            add("")
            add(f"**{place}`{hook.name}` arguments**")
            add("")
            add(_json_block(dict(hook.arguments)))
        if plan.settle_seconds:
            add("")
            add(
                f"_Settle: {plan.settle_seconds}s waited after the last hook and before "
                "anything below was read, exactly as the runner waits._"
            )
        if plan.teardown:
            add("")
            add(
                "_Teardown declared ("
                + ", ".join(f"`{hook.name}`" for hook in plan.teardown)
                + ") and deliberately NOT fired here: this tool restores the world with "
                "`make eval-reset PURGE_IDEMPOTENCY=1` (§ 6), which is authoritative and "
                "undoes more than a compensator can._"
            )
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
    """The commander's short git revision, or ``unknown`` if it cannot be read."""
    try:
        # Fixed argv, never a shell string: nothing caller-supplied is interpolated.
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


def _select(
    only: Sequence[str],
    scenarios: Sequence[Scenario],
    *,
    label: str = "DOSSIER",
    noun: str = "dossier",
    command: str = "world-dossier",
) -> tuple[Scenario | None, int]:
    """The ONLY guard, in the shape ``evals/runner.py`` uses for a live run.

    Three refusals, all exit 2 before anything touches the platform. The vocabulary arguments
    let ``evals/recorder.py`` share the guard (WO-R3-196).
    """
    # 1. No name at all: there is no meaningful "all scenarios" form of seeding one fault.
    if not only:
        print(
            f"{label} FAIL: --only <scenario_name> is required. A {noun} seeds the "
            "scenario's chaos hook into the shared eval world and then resets it; "
            "there is no meaningful 'all scenarios' form of that."
        )
        print(f"Name exactly one scenario, e.g. make {command} ONLY=remediate_dlq_backlog_success")
        print("nothing was seeded")
        return None, EXIT_SELECTION
    # 2. More than one: two faults in one shared world interleave, and neither reading is about
    #    the scenario you ran.
    if len(only) > 1:
        print(
            f"{label} FAIL: {len(only)} scenarios named ({', '.join(only)}). One "
            f"{noun} seeds one fault into one shared world; two would interleave "
            "their faults and neither reading would be about the scenario you ran."
        )
        print("nothing was seeded")
        return None, EXIT_SELECTION
    # 3. Not a FULL scenario name: a substring silently widens a selection that SEEDS CHAOS.
    wanted = only[0]
    known = {s.name: s for s in scenarios}
    if wanted not in known:
        near = sorted(name for name in known if wanted in name)
        print(f"{label} FAIL: {wanted!r} is not a scenario name.")
        print(
            f"A {noun} selects by full scenario name, exactly as a live run does — "
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
    """Seed one scenario's fault, read the world it made, reset it, and write the dossier."""
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

    # 1. Exactly one scenario, by full name.
    scenarios = load_scenarios(_SCENARIOS_DIR)
    scenario, code = _select(only, scenarios)
    if scenario is None:
        return code

    # 2. Settings, and the two credentials this tool needs — both before any hook fires.
    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError as err:
        fields = ", ".join(
            ".".join(str(part) for part in detail["loc"]) or "(settings)" for detail in err.errors()
        )
        print(f"DOSSIER FAIL (env): invalid or missing settings — {fields}")
        print("nothing was seeded")
        return EXIT_PREFLIGHT

    # The SMOKE token deliberately: a credential that cannot write makes "this check is free and
    # safe" a property of the credential, not of this file staying correct.
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

    # A credential this tool cannot work without is a preflight failure, not a half-seeded world.
    try:
        chaos_token = settings.require_chaos_token()
    except ChaosTokenNotConfigured as err:
        print(f"DOSSIER FAIL (env): {err}")
        print("nothing was seeded")
        return EXIT_PREFLIGHT

    read_client = make_client(settings, token=settings.platform_smoke_token.get_secret_value())
    try:
        # 3. Reachability BEFORE seeding: a stack that cannot answer a listing must not be left
        #    holding a half-seeded fault.
        probe = read(read_client, _probe("list_dlq_messages", {}, "reachability"))
        if not probe.ok:
            print(f"DOSSIER FAIL (stack): the platform did not answer a read — {probe.error}")
            print("refusing to seed chaos into a stack that cannot be read")
            print("nothing was seeded")
            return EXIT_PREFLIGHT

        generated_at = datetime.now(UTC)
        invocation_id = uuid.uuid4().hex[:12]

        # 4. The whole plan, fired the way the runner fires it; ``seed_chaos`` also reads the
        #    §5.1 exemption's row ids off each hook's own reply.
        seeded = seed_chaos(
            scenario,
            str(settings.platform_mcp_url),
            chaos_token,
        )
        seeding = seeded.markdown
        sanctioned_incoherent = set(seeded.sanctioned_incoherent)

        # 5. Read the world: the scenario's own premise first, then every derived probe.
        preconditions = [check_precondition(read_client, p) for p in scenario.expected_precondition]
        probes, notes = derive_probes(scenario)
        readings = [read(read_client, p) for p in probes]

        # 6. Lint what came back. Preconditions included: that filtered DLQ page is a reading of
        #    the world too, and its row is usually the one the scenario is about.
        all_readings = [entry.reading for entry in preconditions] + readings
        dlq_findings, dlq_rows = lint_dlq_coherence(all_readings, sanctioned_incoherent)
        target_findings, target_rows = lint_action_targets(scenario, all_readings, preconditions)
        furniture_findings, furniture_rows = lint_forbidden_furniture(scenario, all_readings)

        # 7. Put the world back, then re-audit the baseline it should have returned to.
        reset_code, reset_output = run_reset()
        baseline = audit_baseline(read_client)
    finally:
        close = getattr(read_client, "close", None)
        if callable(close):
            close()

    # 8. Render the document, write it versioned, and exit on the first thing that went wrong.
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

    if seeded.failed:
        return EXIT_SEEDING
    if reset_code != 0:
        return EXIT_RESET
    if not all(line.passed for line in baseline):
        return EXIT_BASELINE_DIRTY
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    raise SystemExit(main(sys.argv[1:]))

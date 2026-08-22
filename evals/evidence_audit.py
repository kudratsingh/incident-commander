"""Cross-tool satisfiability audit for scenario evidence expectations.

``expected_evidence_contains`` is a substring assert over the joined
evidence corpus, and the corpus does not say which tool produced which
entry. A token that can appear in the output of more than one tool
therefore proves nothing about which tool ran: ``failed_traces_scan``
passed a trusted 26/26 live run without ever calling ``search_traces``,
because its one token ``trace`` was satisfied by the ``trace_id`` field
that ``list_dlq_messages`` output happens to carry (live dress-rehearsal
finding, context/INDEX.md 2026-08-16). The prior hygiene rule — assert a
field name, never a value — permits that failure class, because field
names recur across tools.

This module decides, mechanically, which tools could satisfy each token:

* **Field-name skeleton.** Every field name reachable in a tool's output
  model appears verbatim in a ``model_dump_json`` rendering of it, so a
  token that is a substring of any reachable field name of tool X is
  treated as satisfiable by X regardless of values.
* **Canned-value corpus.** Every canned fixture in the suite, rendered
  exactly the way the runtime records evidence
  (``output_model.model_validate(payload).model_dump_json()`` — see
  ``agent/investigation.py::_summarize_probe`` and
  ``agent/remediation.py::_summarize_output``), is a rendering that
  tool's live output is known to be able to take — *whichever scenario
  it came from*. The DLQ fixtures that satisfied ``failed_traces_scan``
  live belong to other scenarios entirely, which is why the corpus is
  suite-wide, never per-scenario.

A token satisfiable by two or more tools is a violation: the scenario
must scope the assertion to its intended tool via
``expected_evidence_fields`` (``EvidenceFieldExpectation``), whose
``tools:`` filter matches ``EvidenceEntry.tool_name`` and cannot be
satisfied by a substring coincidence. Tokens satisfiable by at most one
tool stay legal as substrings: they may be unique to one tool's output
or target bookkeeping prose ("planner stop", "classified as escalated"),
which no tool corpus contains.

``forbidden_evidence_contains`` and ``expect_briefing_contains`` are out
of scope on purpose. A forbidden substring that more tools could produce
is a stronger tripwire, not a weaker one — cross-satisfiability makes a
negative assert fire more readily, never pass wrongly. Briefing asserts
grade the handoff prose, where tool attribution is not the claim.

Enforced continuously by ``tests/unit/test_evidence_audit.py`` over the
committed scenario suite, so the next scenario with a cross-satisfiable
token fails CI instead of passing a live run for the wrong reason.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import get_args

from pydantic import BaseModel, ValidationError

from evals.scenarios.schema import Scenario
from incident_commander.tools.mcp_client import ToolResult
from incident_commander.tools.registry import TOOL_REGISTRY


def _models_in(annotation: object) -> list[type[BaseModel]]:
    """Every ``BaseModel`` subclass reachable in one type annotation."""
    if isinstance(annotation, type):
        return [annotation] if issubclass(annotation, BaseModel) else []
    return [model for arg in get_args(annotation) for model in _models_in(arg)]


def _collect_field_names(model: type[BaseModel], seen: set[type[BaseModel]]) -> set[str]:
    if model in seen:
        return set()
    seen.add(model)
    names: set[str] = set()
    for name, field in model.model_fields.items():
        names.add(field.serialization_alias or name)
        for nested in _models_in(field.annotation):
            names |= _collect_field_names(nested, seen)
    return names


def reachable_field_names(model: type[BaseModel]) -> frozenset[str]:
    """Every field name that can appear in a ``model_dump_json`` of ``model``.

    Walks nested models (including through ``list[...]``, ``| None`` and
    other generic wrappers) because nested field names are serialized
    verbatim too — ``trace_id`` reached ``failed_traces_scan``'s corpus
    through ``ListDlqMessagesOutput.items[].trace_id``, two levels down.
    """
    return frozenset(_collect_field_names(model, set()))


def _render(output_model: type[BaseModel], result: ToolResult) -> str | None:
    """One fixture rendered as the runtime would record it, or ``None``.

    Mirrors ``agent/investigation.py::_parse_output``: first text block
    only, parsed as JSON, validated into the output model. Error results
    and unparsable fixtures return ``None`` — the runtime records those
    as bookkeeping escalations, never as tool evidence, so they cannot
    satisfy an evidence substring under any tool's name.
    """
    if result.is_error:
        return None
    for block in result.content:
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            try:
                payload = json.loads(block["text"])
                return output_model.model_validate(payload).model_dump_json()
            except (ValueError, ValidationError):
                return None
    return None


def rendered_canned_evidence(scenarios: Sequence[Scenario]) -> dict[str, tuple[str, ...]]:
    """Suite-wide canned fixtures per tool, rendered as evidence text.

    Tools absent from ``TOOL_REGISTRY`` are skipped: the runtime cannot
    record evidence under a name it has no output model for.
    """
    corpus: dict[str, list[str]] = {}
    for scenario in scenarios:
        for tool, responses in scenario.canned_tool_responses.items():
            spec = TOOL_REGISTRY.get(tool)
            if spec is None:
                continue
            results = responses if isinstance(responses, tuple) else (responses,)
            for result in results:
                rendering = _render(spec.output_model, result)
                if rendering is not None:
                    corpus.setdefault(tool, []).append(rendering)
    return {tool: tuple(renderings) for tool, renderings in corpus.items()}


def satisfiable_by(
    token: str,
    field_names: Mapping[str, frozenset[str]],
    corpus: Mapping[str, tuple[str, ...]],
) -> frozenset[str]:
    """Tools whose recorded evidence could contain ``token`` as a substring."""
    return frozenset(
        tool
        for tool, names in field_names.items()
        if any(token in name for name in names)
        or any(token in rendering for rendering in corpus.get(tool, ()))
    )


def audit_evidence_scoping(scenarios: Sequence[Scenario]) -> list[str]:
    """One violation line per cross-satisfiable token. Empty list means clean."""
    field_names = {
        name: reachable_field_names(spec.output_model) for name, spec in TOOL_REGISTRY.items()
    }
    corpus = rendered_canned_evidence(scenarios)
    violations: list[str] = []
    for scenario in scenarios:
        for token in scenario.expectation.expected_evidence_contains:
            tools = satisfiable_by(token, field_names, corpus)
            if len(tools) > 1:
                violations.append(
                    f"{scenario.name}: expected_evidence_contains token {token!r} is "
                    f"satisfiable by {sorted(tools)} — it cannot prove which tool ran "
                    "(failed_traces_scan passed a live run without calling "
                    "search_traces exactly this way). Scope the assertion to the "
                    "intended tool with expected_evidence_fields "
                    "(tools: [...], field: ..., one comparator)."
                )
    return violations

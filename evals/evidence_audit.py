"""Cross-tool satisfiability audit for scenario evidence expectations.

``expected_evidence_contains`` is a substring assert over a corpus that does not
say which tool produced an entry — ``failed_traces_scan`` passed a live run
without calling ``search_traces`` because ``trace`` matched ``list_dlq_messages``'
``trace_id``. A token satisfiable by two or more tools (by reachable field name,
or by any canned fixture suite-wide) must use ``expected_evidence_fields``
instead. Forbidden and briefing asserts are out of scope: cross-satisfiability
only makes a negative assert fire more readily. Pinned by test_evidence_audit.py.
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
    """Field names of ``model`` and of every model nested inside it, cycle-safe."""
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

    Walks nested models: ``trace_id`` reached the corpus through
    ``ListDlqMessagesOutput.items[].trace_id``, two levels down.
    """
    return frozenset(_collect_field_names(model, set()))


def _render(output_model: type[BaseModel], result: ToolResult) -> str | None:
    """One fixture rendered as the runtime would record it, or ``None``.

    Mirrors ``agent/investigation.py::_parse_output``. Error and unparsable
    fixtures return ``None``; the runtime never records those as tool evidence.
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

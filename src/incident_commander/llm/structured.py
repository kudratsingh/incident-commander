"""Decoding rules for the agent's OWN structured output (ADR 0035).

Every ``record_output`` model in this repo inherits :class:`StructuredOutput`.
It adds exactly one thing: a ``mode="before"`` model validator that accepts a
nested object or array which arrived as a **JSON string** instead of as JSON,
decodes it, and hands the decoded value to the normal validation path.

Why this exists. Paid run ``779b19a287a7`` (2026-09-08, scenario
``remediate_dlq_backlog_success``) reached the right decision — replay the one
confirmed-safe DLQ row — and then emitted it like this::

    "next_action": "{\\"kind\\": \\"remediate\\", \\"reason\\": \\"…\\"}]"

The nested discriminated union came back serialised as a string with the
enclosing array's ``]`` still attached. ``InvestigationStep`` rejected it, the
run escalated on the first failure, and the scenario graded RED on three
dimensions. Nothing about the agent's *judgement* was wrong; the harness threw
away a correct answer over its wrapping.

What this validator does and does not do:

* It substitutes the decoded value **only** when the decode succeeds *and* the
  decoded type matches the shape the field declares (object for a nested
  model / union / mapping, array for a sequence). Anything else is left
  exactly as it arrived, so the model's own error is the one that fires and
  the diagnosis stays honest.
* It does not touch ``extra="forbid"``, enum members, tier checks, or any
  other constraint. A field that was going to be rejected on its contents is
  still rejected on its contents.
* It applies to the agent's own structured output only. Tool output is
  untrusted data (CLAUDE.md invariant 4) and is parsed elsewhere, unchanged.

The trailing-delimiter tolerance is deliberately narrow. After a strict
``json.loads`` fails, one more attempt decodes the leading JSON value and
accepts it **only when everything after it is whitespace and closing
delimiters** (``]`` / ``}``) — characters that cannot carry content and can
only be a container delimiter that leaked into the string, which is precisely
the live shape. A trailing comma, a second value, or any prose leaves the
string untouched.
"""

from __future__ import annotations

import json
import types
import typing
from collections.abc import Mapping, Sequence, Set
from typing import Any, Final, Literal, get_args, get_origin

from pydantic import BaseModel, model_validator

#: Characters allowed to trail a decoded value. Both are container
#: terminators: neither can begin or continue a JSON value, so their presence
#: after a complete value is a delimiter leak, never truncated content.
TRAILING_DELIMITERS: Final[str] = "]}"

_WHITESPACE: Final[str] = " \t\r\n"

# Sentinel for "this string is not a stringified container" — distinct from
# ``None``, which is a value ``json.loads`` can legitimately return.
_UNDECODED: Final[object] = object()

_MAPPING_ORIGINS: Final[frozenset[Any]] = frozenset({dict, Mapping})
_SEQUENCE_ORIGINS: Final[frozenset[Any]] = frozenset({list, tuple, set, frozenset, Sequence, Set})


def expected_container(annotation: Any) -> type | None:
    """``dict``, ``list``, or ``None`` for "not a container field".

    Derived from the field's own annotation rather than declared per model,
    so a new nested field on any ``record_output`` model is covered the day
    it lands (architecture-principles rule 2: one source of truth, read from
    both places, not two lists to keep in sync).

    A union counts as a container only when every non-``None`` member agrees
    on the same container — ``ProbeAction | StopAction | RemediateAction``
    does; ``str | None`` does not, and a stringified value there is a
    perfectly good ``str``.
    """
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return dict
    origin = get_origin(annotation)
    if origin is Literal:
        return None
    if origin in _MAPPING_ORIGINS:
        return dict
    if origin in _SEQUENCE_ORIGINS:
        return list
    args = [arg for arg in get_args(annotation) if arg is not type(None)]
    if origin is not None and args and _is_union(origin):
        containers = {expected_container(arg) for arg in args}
        if len(containers) == 1:
            return containers.pop()
    return None


def _is_union(origin: Any) -> bool:
    """True for both spellings of a union: ``X | Y`` and ``Union[X, Y]``."""
    return origin in (types.UnionType, typing.Union)


def decode_stringified(value: str, expected: type) -> Any:
    """Decode ``value`` to ``expected`` (``dict`` or ``list``), or ``_UNDECODED``.

    Returns the module sentinel — never a partial or coerced value — when the
    string is not a JSON container of the expected shape. Callers leave the
    original string in place on the sentinel so the model's own error fires.
    """
    text = value.strip()
    if not text:
        return _UNDECODED
    try:
        decoded = json.loads(text)
    except (json.JSONDecodeError, RecursionError):
        decoded = _decode_leading_value(text)
        if decoded is _UNDECODED:
            return _UNDECODED
    if type(decoded) is expected:
        return decoded
    return _UNDECODED


def _decode_leading_value(text: str) -> Any:
    """The one tolerated malformation: a complete value plus stray closers.

    ``json.JSONDecoder().raw_decode`` reads the leading value and reports
    where it stopped. The remainder is accepted only if it is whitespace and
    ``TRAILING_DELIMITERS`` — see the module docstring for why that set and
    no wider one.
    """
    try:
        decoded, end = json.JSONDecoder().raw_decode(text)
    except (ValueError, RecursionError):
        return _UNDECODED
    remainder = text[end:]
    if not remainder or set(remainder) <= set(_WHITESPACE + TRAILING_DELIMITERS):
        return decoded
    return _UNDECODED


class StructuredOutput(BaseModel):
    """Base class for every model the LLM fills in via ``record_output``.

    Carries no configuration of its own — subclasses keep their own
    ``model_config`` (``frozen``, ``extra="forbid"``) untouched. It adds one
    inherited ``mode="before"`` model validator and nothing else.
    """

    @model_validator(mode="before")
    @classmethod
    def _decode_stringified_containers(cls, data: Any) -> Any:
        """Substitute a decoded object/array for a field that arrived as a string.

        Model-level rather than a per-field validator on each nested field:
        a per-field list is a second copy of the schema that drifts the first
        time somebody adds a field (the failure mode architecture-principles
        rule 2 is about). This reads ``cls.model_fields``, so it covers every
        field the model actually declares.
        """
        if not isinstance(data, dict):
            return data
        patched: dict[Any, Any] | None = None
        for name, field in cls.model_fields.items():
            raw = data.get(name)
            if not isinstance(raw, str):
                continue
            expected = expected_container(field.annotation)
            if expected is None:
                continue
            decoded = decode_stringified(raw, expected)
            if decoded is _UNDECODED:
                continue
            if patched is None:
                patched = dict(data)
            patched[name] = decoded
        return data if patched is None else patched

"""Decoding rules for the agent's OWN structured output (ADR 0035).

Every ``record_output`` model inherits :class:`StructuredOutput`, whose ``mode="before"``
validator decodes a nested object or array that arrived as a JSON string (paid run
``779b19a287a7``). It substitutes only on a type match, and never sees tool output.
"""

from __future__ import annotations

import json
import types
import typing
from collections.abc import Mapping, Sequence, Set
from typing import Any, Final, Literal, get_args, get_origin

from pydantic import BaseModel, model_validator

#: Allowed to trail a decoded value: container terminators — a delimiter leak, never content.
TRAILING_DELIMITERS: Final[str] = "]}"

_WHITESPACE: Final[str] = " \t\r\n"

# Sentinel for "not a stringified container"; ``None`` is a value ``json.loads`` returns.
_UNDECODED: Final[object] = object()

_MAPPING_ORIGINS: Final[frozenset[Any]] = frozenset({dict, Mapping})
_SEQUENCE_ORIGINS: Final[frozenset[Any]] = frozenset({list, tuple, set, frozenset, Sequence, Set})


def expected_container(annotation: Any) -> type | None:
    """``dict``, ``list``, or ``None`` for "not a container field".

    Derived from the annotation, so a new nested field is covered the day it lands. A union
    counts only when every non-``None`` member agrees on one container.
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

    On the sentinel the caller leaves the string in place.
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

    The remainder is accepted only if whitespace and ``TRAILING_DELIMITERS``.
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

    Adds one ``mode="before"`` validator, no ``model_config``.
    """

    @classmethod
    def output_refused(cls, error: Exception) -> bool:
        """Whether this failure is the SCHEMA refusing a move, not a payload it cannot read.

        ADR 0074's hook: a narrowed model (``hypothesis.without_probe``) rejects the withdrawn
        move at validation, not the malformation ADR 0035's re-ask exists for. ``False`` by
        default, so a model that narrows nothing keeps ADR 0035 exactly.
        """
        return False

    @model_validator(mode="before")
    @classmethod
    def _decode_stringified_containers(cls, data: Any) -> Any:
        """Substitute a decoded object/array for a field that arrived as a string.

        Model-level, reading ``cls.model_fields``, so no per-field list can drift
        (architecture-principles rule 2).
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

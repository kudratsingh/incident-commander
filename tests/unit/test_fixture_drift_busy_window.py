"""The ``busy-window`` context (WO-R3-326): a row a day of traffic can make look fixed.

``get_slo_status`` reads a rolling 24 h of the jobs table and ``make eval-reset`` does not empty
it, so after a day of rehearsals a warm stack answers ``healthy: false`` and ``-100.0`` too.
"""

from __future__ import annotations

import json
from typing import Final

import pytest

from evals.fixture_drift_ledger import (
    BUSY_WINDOW,
    LEDGER_PATH,
    classify,
    context_of,
    split_for_bless,
)

_BUSY: Final[tuple[tuple[str, str, str, str], ...]] = (
    (
        "api_latency_downstream",
        "get_slo_status",
        "objectives[].budget_remaining_pct[]",
        "not_live_reachable",
    ),
    ("api_latency_downstream", "get_slo_status", "objectives[].healthy[]", "not_live_reachable"),
    (
        "cascading_redis_starves_backpressure",
        "get_slo_status",
        "objectives[].healthy[]",
        "not_live_reachable",
    ),
)


def test_the_three_window_rows_carry_the_word() -> None:
    assert {context_of(key)[0] for key in _BUSY} == {BUSY_WINDOW}


def test_no_other_row_carries_it() -> None:
    rows = json.loads(LEDGER_PATH.read_text())["known_drift"]
    busy = {
        (row["scenario"], row["tool"], row["path"], row["kind"])
        for row in rows
        if row["context"] == BUSY_WINDOW
    }
    assert busy == set(_BUSY)
    assert BUSY_WINDOW in json.loads(LEDGER_PATH.read_text())["_context"]


@pytest.mark.parametrize("stack_context", ["warm", "cold", "unknown"])
def test_a_busy_window_row_is_never_stale(stack_context: str) -> None:
    assert classify([], frozenset(_BUSY), stack_context=stack_context) == ((), ())


def test_a_bless_on_a_warm_stack_carries_it_rather_than_deleting_it() -> None:
    reached = {(key[0], key[1]) for key in _BUSY}
    carried, disproved = split_for_bless((), _BUSY, reached, stack_context="warm")
    assert disproved == ()
    assert set(carried) == set(_BUSY)

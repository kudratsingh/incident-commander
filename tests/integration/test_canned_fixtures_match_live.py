"""Canned fixtures vs the live platform.

``test_contract_snapshot.py`` asks whether the tool SCHEMAS still match; this asks
whether the VALUES the offline suite serves are values the platform can produce.
Nothing asked that before, which is why ``consumer_lag_high`` asserted ``lag: 1200``
against a live ``0`` for months — the fixture stood in for queue traffic nobody had
built, so the suite passed BECAUSE of the hole.

Runs in CI's ``contract`` job, which already boots the pinned platform and seeds it,
and skips cleanly without the live env. READ-SCOPED by construction: under
``PLATFORM_SMOKE_TOKEN`` with no fall back to ``PLATFORM_TOKEN``, because a check
that measures the world must not hold a principal that can change it, and Tier-1
fixtures are never probed at all. That filter is asserted in
``tests/unit/test_fixture_probe_scope.py`` rather than here (WO-R2-122): the
module-level skipif meant the guard against probing a Tier-1 tool only ever ran where
probing one would have done real damage.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from evals.fixture_drift import canned_calls
from evals.fixture_drift_ledger import LEDGER_PATH, classify, load_ledger
from evals.fixture_probe import probe_live
from evals.scenarios.loader import load_scenarios

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCENARIOS_DIR = _REPO_ROOT / "evals" / "scenarios"

_BLESS = "make fixture-drift-bless"


def _live_env_available() -> bool:
    return bool(os.getenv("PLATFORM_MCP_URL") and os.getenv("PLATFORM_SMOKE_TOKEN", "").strip())


pytestmark = pytest.mark.skipif(
    not _live_env_available(),
    reason=(
        "PLATFORM_MCP_URL and PLATFORM_SMOKE_TOKEN required; run `make demo` and "
        "`make bootstrap-token` first. The read-scoped token is required on purpose — "
        "this check never falls back to the write-scoped PLATFORM_TOKEN."
    ),
)


@pytest.fixture(scope="module")
def probe():  # type: ignore[no-untyped-def]
    calls = canned_calls(load_scenarios(_SCENARIOS_DIR))
    assert calls, "no canned fixtures found — the walk lost its subject"
    return probe_live(
        calls,
        mcp_url=os.environ["PLATFORM_MCP_URL"],
        token=os.environ["PLATFORM_SMOKE_TOKEN"],
    )


def test_no_fixture_drift_outside_the_ledger(probe) -> None:  # type: ignore[no-untyped-def]
    new, _ = classify(probe.drifts, load_ledger(), stack_context=probe.stack_context)
    if new:
        listing = "\n".join(f"  {drift.describe()}" for drift in new)
        pytest.fail(
            f"{len(new)} canned fixture value(s) disagree with the pinned platform and are "
            f"not in {LEDGER_PATH.name}:\n{listing}\n\n"
            "A canned response that the platform cannot produce is a mock impersonating a "
            "component, and every scenario grading against it is grading the fixture. Fix "
            f"the fixture, or — if the platform legitimately moved — re-bless with `{_BLESS}` "
            "in a dedicated commit so the diff stays readable."
        )


def test_ledger_holds_no_entry_that_is_already_fixed(probe) -> None:  # type: ignore[no-untyped-def]
    """The half that makes this a ratchet rather than an allowlist.

    A recorded drift that no longer occurs means someone fixed a fixture, and the line
    recording it has to leave in the same PR — a guard whose exception list only grows is
    not a guard.
    """
    _, stale = classify(probe.drifts, load_ledger(), stack_context=probe.stack_context)
    if stale:
        listing = "\n".join(f"  {key}" for key in stale)
        pytest.fail(
            f"{len(stale)} ledger entr(ies) no longer drift — the fixture was fixed. "
            f"Stack context was {probe.stack_context}; delete these lines from {LEDGER_PATH.name} "
            f"(or run `{_BLESS}`):\n{listing}"
        )


def test_every_fixture_was_actually_reachable(probe) -> None:  # type: ignore[no-untyped-def]
    """A fixture that could not be probed is not a fixture that agrees.

    Silence here would read as agreement, which is the exact failure mode
    this whole module exists to remove.
    """
    if probe.errors:
        listing = "\n".join(f"  {e.scenario}:{e.tool} — {e.detail}" for e in probe.errors)
        pytest.fail(f"{len(probe.errors)} canned fixture(s) could not be checked:\n{listing}")


def test_the_walk_is_not_vacuous(probe) -> None:  # type: ignore[no-untyped-def]
    """Floors, so a loader or schema change cannot silently empty the check."""
    assert probe.checked >= 20, f"only {probe.checked} fixtures checked; the walk shrank"
    assert probe.live_calls >= 10, f"only {probe.live_calls} live calls made"

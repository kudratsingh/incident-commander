"""Canned fixtures vs the live platform.

The sibling of ``test_contract_snapshot.py``, one level down. That test asks
whether the tool *schemas* still match; this one asks whether the *values*
the offline suite serves are values the platform can actually produce.

Nothing asked that before, which is why ``consumer_lag_high`` asserted
``lag: 1200`` against a live ``0`` for months — in a repo that also committed
the trace proving it — and the suite stayed green throughout. The fixture was
standing in for queue traffic nobody had built, so the suite passed *because
of* the hole rather than despite it.

Runs in CI in the ``contract`` job, which already boots the pinned platform
by digest and seeds it (``SEED_EVAL_FIXTURES=true``), so this is one extra
step against an existing stack rather than new infrastructure. Skipped
cleanly without the live env, exactly like the contract diff, so
``make test-integration`` in the ``test`` job stays offline.

**Read-scoped by construction.** It runs under ``PLATFORM_SMOKE_TOKEN`` and
deliberately does not fall back to ``PLATFORM_TOKEN``: a check that measures
the world must not hold a principal that can change it. Tier-1 fixtures are
additionally never probed — probing ``replay_dlq_by_category`` to see what it
returns would replay the DLQ. The scope is the boundary; the filter is so a
bug fails loudly instead of at the platform.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from evals.fixture_drift import canned_calls
from evals.fixture_drift_ledger import LEDGER_PATH, classify, load_ledger
from evals.fixture_probe import probe_live, read_tier_calls
from evals.scenarios.loader import load_scenarios
from incident_commander.tools.policies import Tier, tier_of

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
    new, _ = classify(probe.drifts, load_ledger())
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

    A recorded drift that no longer occurs means someone fixed a fixture, and
    the line recording it has to leave in the same PR. Without this the file
    would only ever grow, and a guard whose exception list grows is not a
    guard.
    """
    _, stale = classify(probe.drifts, load_ledger())
    if stale:
        listing = "\n".join(f"  {key}" for key in stale)
        pytest.fail(
            f"{len(stale)} ledger entr(ies) no longer drift — the fixture was fixed. "
            f"Delete these lines from {LEDGER_PATH.name} (or run `{_BLESS}`):\n{listing}"
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


def test_no_tier1_tool_is_ever_probed() -> None:
    """The filter half of the two guards. The scope is the real boundary."""
    calls = canned_calls(load_scenarios(_SCENARIOS_DIR))
    probed = read_tier_calls(calls)
    offenders = sorted({c.tool for c in probed if tier_of(c.tool) is not Tier.READ})
    assert offenders == [], f"drift check would probe non-read tools: {offenders}"
    # And the suite really does carry Tier-1 fixtures, so the filter is doing
    # work rather than trivially passing over an all-read corpus.
    assert any(tier_of(c.tool) is not Tier.READ for c in calls)

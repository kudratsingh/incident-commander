"""The drift walk's Tier-1 filter, checked offline (WO-R2-122).

This is the *filter* half of the fixture-drift probe's two safety guards. The
other half is scope — the walk runs under the read-only
``PLATFORM_SMOKE_TOKEN`` and never falls back to the write-scoped
``PLATFORM_TOKEN`` — and that half can only be observed against a live stack,
so it stays in ``tests/integration/test_canned_fixtures_match_live.py``.

This half needs no platform at all: it reads the committed scenario corpus and
asks whether the walk *would* probe a non-read tool. It lived in that
integration module anyway, under a module-level ``skipif`` for the live
environment, so the one guard that could have run in every CI run was the one
that never ran in any of them — a safety check whose own execution depended on
the thing it was protecting against.

Probing a Tier-1 tool is not a failed assertion, it is a side effect: calling
``replay_dlq_by_category`` to see what it returns replays the DLQ. So the
check belongs where it fails *before* anyone reaches a platform, which is
here, in the offline tier.
"""

from __future__ import annotations

from pathlib import Path

from evals.fixture_drift import canned_calls
from evals.fixture_probe import read_tier_calls
from evals.scenarios.loader import load_scenarios
from incident_commander.tools.policies import Tier, tier_of

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCENARIOS_DIR = _REPO_ROOT / "evals" / "scenarios"


def test_no_tier1_tool_is_ever_probed() -> None:
    """The filter half of the two guards. The scope is the real boundary."""
    calls = canned_calls(load_scenarios(_SCENARIOS_DIR))
    probed = read_tier_calls(calls)
    offenders = sorted({c.tool for c in probed if tier_of(c.tool) is not Tier.READ})
    assert offenders == [], f"drift check would probe non-read tools: {offenders}"
    # And the suite really does carry Tier-1 fixtures, so the filter is doing
    # work rather than trivially passing over an all-read corpus.
    assert any(tier_of(c.tool) is not Tier.READ for c in calls)

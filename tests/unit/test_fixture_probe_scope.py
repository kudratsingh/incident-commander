"""The drift walk's Tier-1 filter, checked offline (WO-R2-122).

The scope half — the walk runs under the read-only ``PLATFORM_SMOKE_TOKEN`` — needs a live
stack and stays in ``tests/integration/``. This half reads the committed corpus and asks
whether the walk WOULD probe a non-read tool. It used to sit behind a live-env ``skipif``,
so the one guard that could run in every CI run never did. Probing Tier-1 is a side effect.
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

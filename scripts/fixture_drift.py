"""Report canned-vs-live fixture drift, and (deliberately) re-bless the ledger.

    uv run python scripts/fixture_drift.py            # report
    uv run python scripts/fixture_drift.py --bless    # rewrite the ledger

Needs a live platform: ``PLATFORM_MCP_URL`` plus a READ-SCOPED token. It
prefers ``PLATFORM_SMOKE_TOKEN`` and refuses to fall back to
``PLATFORM_TOKEN``, which carries write+chaos scope — a drift check has no
business holding a principal that could mutate the world it is measuring.

Exit codes follow the runner's convention: 0 clean, 1 drift outside the
ledger (or stale entries in it), 2 missing prerequisite.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from evals.fixture_drift import canned_calls
from evals.fixture_drift_ledger import classify, dump_ledger, load_ledger
from evals.fixture_probe import probe_live
from evals.scenarios.loader import load_scenarios

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCENARIOS_DIR = _REPO_ROOT / "evals" / "scenarios"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bless",
        action="store_true",
        help="rewrite the known-drift ledger from this run (a deliberate act)",
    )
    args = parser.parse_args(argv)

    mcp_url = os.environ.get("PLATFORM_MCP_URL", "")
    token = os.environ.get("PLATFORM_SMOKE_TOKEN", "")
    if not mcp_url:
        print("ERROR: PLATFORM_MCP_URL is not set. Run `make demo` first.", file=sys.stderr)
        return 2
    if not token.strip():
        print(
            "ERROR: PLATFORM_SMOKE_TOKEN is not set (or is empty). This check reads the "
            "live platform and must do so under the read-scoped principal; it will not "
            "fall back to PLATFORM_TOKEN, which carries write+chaos scope. Run "
            "`make bootstrap-token`.",
            file=sys.stderr,
        )
        return 2

    calls = canned_calls(load_scenarios(_SCENARIOS_DIR))
    result = probe_live(calls, mcp_url=mcp_url, token=token)

    print(
        f"canned fixtures checked: {result.checked} "
        f"({result.skipped_write_tier} Tier-1 fixtures never probed) "
        f"via {result.live_calls} live calls"
    )
    for error in result.errors:
        print(f"  UNCHECKED {error.scenario}:{error.tool} — {error.detail}")

    if args.bless:
        count = dump_ledger(result.drifts)
        print(f"wrote {count} known-drift entries to evals/fixture-drift-ledger.json")
        print("git add + commit the ledger to bless the current fixture state.")
        return 0

    new, stale = classify(result.drifts, load_ledger())
    print(
        f"drift observed: {len(result.drifts)}  new: {len(new)}  stale ledger entries: {len(stale)}"
    )
    for drift in new:
        print(f"  NEW   {drift.describe()}")
    for key in stale:
        print(f"  STALE {key} — no longer drifted; delete this line from the ledger")
    if result.errors:
        print("\nA fixture that could not be probed is not a fixture that agrees.")
    return 1 if (new or stale or result.errors) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

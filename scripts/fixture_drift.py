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
import time
from pathlib import Path

import httpx

from evals.fixture_drift import canned_calls
from evals.fixture_drift_ledger import (
    classify,
    defect_count,
    dump_ledger,
    load_ledger,
    split_for_bless,
)
from evals.fixture_probe import UnseededPlatformError, probe_live
from evals.fixture_shape import check_calls, write_tier_calls
from evals.scenarios.loader import load_scenarios

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCENARIOS_DIR = _REPO_ROOT / "evals" / "scenarios"


def _await_fixtures(calls, mcp_url: str, token: str, budget_seconds: int) -> int:  # type: ignore[no-untyped-def]
    """Poll until every call the drift check makes resolves.

    The platform seeds its fixture pack asynchronously at boot, and CI's
    readiness loop waits for the REST app's ``/healthz`` — not for the data.
    ``test-contract`` never noticed because ``tools/list`` needs no rows; the
    drift check is the first thing in CI that does, and its first run compared
    every fixture against an empty world.

    This waits for exactly the calls the check will make, so it cannot pass
    while a fixture the check probes is still missing. It is a readiness gate,
    not a retry: a failure that survives the budget is reported with the calls
    still unresolved, never swallowed.

    Both ways a not-yet-ready platform answers are caught. It only caught
    ``UnseededPlatformError`` — the "up but empty" case — so the connection
    errors a platform produces while it is *not yet listening* killed the
    loop on attempt one, which is exactly the window this gate exists for.
    ``httpx.HTTPError`` is belt to that braces: since the probe converts
    transport failures into ``ProbeError`` they now arrive as unresolved
    calls, and a raise from anywhere else in the client must not be fatal
    to a poll loop either.
    """
    deadline = time.monotonic() + budget_seconds
    attempt = 0
    while True:
        attempt += 1
        try:
            result = probe_live(calls, mcp_url=mcp_url, token=token)
            if not result.errors:
                print(f"fixture pack ready after {attempt} attempt(s)")
                return 0
            detail = "; ".join(f"{e.scenario}:{e.tool} — {e.detail}" for e in result.errors)
        except (UnseededPlatformError, httpx.HTTPError) as err:
            detail = f"{type(err).__name__}: {err}"
        if time.monotonic() >= deadline:
            print(
                f"ERROR: the platform never became ready within {budget_seconds}s. "
                f"Unresolved: {detail}",
                file=sys.stderr,
            )
            return 2
        print(f"not ready (attempt {attempt}): {detail[:160]}")
        time.sleep(5)


def _parse_pairs(raw: list[str]) -> frozenset[tuple[str, str]]:
    """``SCENARIO:TOOL`` strings to the pair form the probe reports coverage in."""
    pairs: set[tuple[str, str]] = set()
    for item in raw:
        scenario, separator, tool = item.partition(":")
        if not separator or not scenario.strip() or not tool.strip():
            raise SystemExit(f"--not-fresh expects SCENARIO:TOOL, got {item!r}")
        pairs.add((scenario.strip(), tool.strip()))
    return frozenset(pairs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bless",
        action="store_true",
        help="rewrite the known-drift ledger from this run (a deliberate act)",
    )
    parser.add_argument(
        "--await-fixtures",
        type=int,
        metavar="SECONDS",
        help=(
            "poll until every call the check makes resolves, then exit. For CI, "
            "where the platform seeds its fixture pack asynchronously at boot."
        ),
    )
    parser.add_argument(
        "--not-fresh",
        action="append",
        default=[],
        metavar="SCENARIO:TOOL",
        help=(
            "bless only: this run's volume is not a fresh seed for this fixture, "
            "so the run holds no opinion about it — its drift is neither written "
            "nor disproved. Repeatable. For a developer stack whose seeded data "
            "has aged past a fixture's own time window; never for a fixture you "
            "simply do not want reported."
        ),
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

    if args.await_fixtures is not None:
        return _await_fixtures(calls, mcp_url, token, args.await_fixtures)

    try:
        result = probe_live(calls, mcp_url=mcp_url, token=token)
    except UnseededPlatformError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2

    # The Tier-1 half, which needs no platform and so is not gated on one:
    # those fixtures are never probed (probing pause_dag would pause a DAG),
    # and their shape is checked against the committed tool snapshot instead.
    shape_defects = check_calls(write_tier_calls(calls))

    print(
        f"canned fixtures checked: {result.checked} "
        f"({result.skipped_write_tier} Tier-1 fixtures shape-checked offline, never probed) "
        f"via {result.live_calls} live calls"
    )
    for error in result.errors:
        print(f"  UNCHECKED {error.scenario}:{error.tool} — {error.detail}")
    for defect in shape_defects:
        print(f"  SHAPE {defect.describe()}")

    if args.bless:
        if result.errors:
            # The ledger IS the burn-down list. Rewriting it from a run that
            # could not read part of the suite deletes entries nothing
            # disproved, and the deletion is silent — the file just gets
            # shorter, which is what progress looks like.
            print(
                f"ERROR: refusing to bless — {len(result.errors)} fixture(s) could not be "
                "probed (listed above), and a run that did not read them cannot say "
                "whether their ledger entries still hold. Fix the probe failures and "
                "re-run.",
                file=sys.stderr,
            )
            return 2
        # A fixture this run cannot speak to is dropped from BOTH halves of
        # the bless: its drift is not written, and its (scenario, tool) pair
        # is removed from the coverage this run claims, so `split_for_bless`
        # carries any existing entry rather than disproving it. Same posture
        # as the probe-error refusal above — a run that holds no opinion may
        # neither add nor delete — applied to a reading that came back but is
        # about the volume rather than about the fixture.
        #
        # The case it exists for: `failed_traces_scan` probes
        # `search_traces(status="failed", since_hours=1)`, and a developer
        # stack that has been up for more than an hour returns nothing for it
        # while a freshly seeded one (CI's contract job, which is what the
        # ledger is `_blessed_against`) returns the seeded rows. Blessing that
        # reading would write an entry CI never observes, and the ratchet
        # fails on entries no longer observed — so a naive bless from a stale
        # volume turns CI red in the opposite direction.
        not_fresh = _parse_pairs(args.not_fresh)
        unknown = sorted(pair for pair in not_fresh if pair not in set(result.compared))
        if unknown:
            print(
                f"ERROR: --not-fresh names {unknown}, which this run did not compare. "
                "Name a (scenario, tool) pair the check actually read.",
                file=sys.stderr,
            )
            return 2
        drifts = tuple(d for d in result.drifts if (d.scenario, d.tool) not in not_fresh)
        compared = tuple(pair for pair in result.compared if pair not in not_fresh)
        for scenario, tool in sorted(not_fresh):
            print(
                f"  NOT-FRESH {scenario}:{tool} — this run holds no opinion; "
                "nothing written, nothing disproved"
            )
        before = load_ledger()
        carried, disproved = split_for_bless({drift.key for drift in drifts}, before, compared)
        count = dump_ledger(drifts, checked=compared)
        defects = defect_count()
        print(f"wrote {count} known-drift entries to evals/fixture-drift-ledger.json")
        print(f"  {defects} are fixture defects — the burn-down number")
        print(f"  {count - defects} are explained (post-fault / canned-only) and are NOT work")
        print(f"  {len(disproved)} dropped: probed this run and no longer drifting")
        for key in carried:
            print(f"  CARRIED {key} — not probed this run, so nothing disproved it")
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
    return 1 if (new or stale or result.errors or shape_defects) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

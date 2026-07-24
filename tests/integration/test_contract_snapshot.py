"""Live contract diff.

Runs ``tools/list`` against the platform reachable via ``PLATFORM_MCP_URL``,
normalizes the response, and diffs against ``contracts/platform-tools.snapshot.json``.
Skipped cleanly when the env isn't set — CI wires the platform up and provides
the env; local dev runs it via ``make test-contract`` after ``make bootstrap-token``.

When this fails, the fix is one of:

- The platform legitimately shipped a schema change → bump the digest in
  ``demo/compose.yml`` and rerun ``make snapshot`` to bless the new contract.
- The platform accidentally shipped a schema change → open a platform PR.
- The agent's expectations are wrong → align the registry and rerun ``make eval-live``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from incident_commander.tools.contract import compare, normalize

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SNAPSHOT_PATH = _REPO_ROOT / "contracts" / "platform-tools.snapshot.json"


def _live_env_available() -> bool:
    return bool(os.getenv("PLATFORM_MCP_URL") and os.getenv("PLATFORM_TOKEN"))


@pytest.mark.skipif(
    not _live_env_available(),
    reason="PLATFORM_MCP_URL and PLATFORM_TOKEN required; run `make bootstrap-token` first",
)
def test_live_platform_matches_committed_snapshot() -> None:
    mcp_url = os.environ["PLATFORM_MCP_URL"]
    token = os.environ["PLATFORM_TOKEN"]

    r = httpx.post(
        mcp_url,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=15.0,
    )
    r.raise_for_status()
    payload = r.json()
    assert "error" not in payload, f"tools/list failed: {payload.get('error')}"
    live_result = payload["result"]
    live = normalize(live_result)

    committed = json.loads(_SNAPSHOT_PATH.read_text())
    diff = compare(committed, live)

    if not diff.is_empty:
        details = []
        if diff.added:
            details.append(f"added: {list(diff.added)}")
        if diff.removed:
            details.append(f"removed: {list(diff.removed)}")
        if diff.changed:
            details.append(f"changed: {list(diff.changed)}")
        pytest.fail(
            "Live platform contract drifted from committed snapshot. "
            + "; ".join(details)
            + ". Fix: run `make snapshot` to rebless, or bump the platform digest."
        )

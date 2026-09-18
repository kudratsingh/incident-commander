#!/usr/bin/env python3
"""Regenerate ``contracts/platform-tools.snapshot.json`` from a live platform.

One ``tools/list`` call, normalized (sorted, only the diffed fields). The committed snapshot
is the contract at the pinned image digest, so bump that digest in ``demo/compose.yml`` and
rerun this on each platform release.

Usage (``PLATFORM_MCP_URL``, ``PLATFORM_TOKEN`` when the flags are omitted):
    uv run python scripts/snapshot_platform_tools.py [--mcp-url URL] [--token TOKEN]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

from incident_commander.tools.contract import normalize

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SNAPSHOT_PATH = _REPO_ROOT / "contracts" / "platform-tools.snapshot.json"


def fetch_tools(mcp_url: str, token: str) -> dict[str, object]:
    """POST tools/list. Returns the JSON-RPC ``result`` object."""
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
    payload: dict[str, object] = r.json()
    if "error" in payload:
        raise RuntimeError(f"tools/list failed: {payload['error']}")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"unexpected result shape: {type(result).__name__}")
    return result


def main(argv: list[str] | None = None) -> int:
    """Fetch the live tool list, normalize it, and write the snapshot file."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mcp-url",
        default=os.getenv("PLATFORM_MCP_URL"),
        help="Defaults to $PLATFORM_MCP_URL.",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("PLATFORM_TOKEN"),
        help="Defaults to $PLATFORM_TOKEN.",
    )
    parser.add_argument(
        "--out",
        default=str(_SNAPSHOT_PATH),
        help="Output path (defaults to contracts/platform-tools.snapshot.json).",
    )
    args = parser.parse_args(argv)

    if not args.mcp_url or not args.token:
        print(
            "PLATFORM_MCP_URL and PLATFORM_TOKEN must be set (env or flag). "
            "Run `make bootstrap-token` first.",
            file=sys.stderr,
        )
        return 2

    result = fetch_tools(args.mcp_url, args.token)
    # v0.4.8+ emits outputSchema in tools/list (PR #88); registry consistency is
    # tests/unit/test_registry_matches_snapshot.py.
    snapshot = normalize(result)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n")

    tool_count = len(snapshot.get("tools", []))
    # The file is on disk by this line, so this print must not be able to fail:
    # `relative_to` raises for any `--out` outside the repo. Resolve first, then fall back.
    resolved = out_path.resolve()
    try:
        shown: Path = resolved.relative_to(_REPO_ROOT)
    except ValueError:
        shown = resolved
    print(f"wrote {tool_count} tools to {shown}")
    print("git add + commit the snapshot to bless the current contract.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

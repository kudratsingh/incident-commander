"""Probe the live platform for the calls the canned fixtures answer.

Split from ``fixture_drift`` so the comparison stays pure and offline-
testable and only this module touches the network. Read tools only, under
the read-scoped principal — see ``probe_live``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from evals.fixture_drift import CannedCall, Drift, compare
from incident_commander.tools.policies import Tier, tier_of

_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class ProbeError:
    """A live call that could not be made, so its fixture went unchecked."""

    scenario: str
    tool: str
    detail: str


@dataclass(frozen=True)
class ProbeResult:
    drifts: tuple[Drift, ...]
    errors: tuple[ProbeError, ...]
    checked: int
    skipped_write_tier: int
    live_calls: int


def read_tier_calls(calls: Iterable[CannedCall]) -> tuple[CannedCall, ...]:
    """The calls this check may make.

    Tier-1 fixtures are excluded by construction, not by care: probing
    ``replay_dlq_by_category`` to see what it returns would replay the DLQ.
    Their canned payloads stay unvalidated by this check — that is a real
    remaining hole, and the reason the check also runs under the read-scoped
    principal rather than trusting this filter alone.
    """
    return tuple(call for call in calls if tier_of(call.tool) is Tier.READ)


def probe_live(
    calls: Iterable[CannedCall],
    *,
    mcp_url: str,
    token: str,
    client: httpx.Client | None = None,
) -> ProbeResult:
    """Compare every read-tier canned fixture against the live platform.

    ``token`` must be the READ-SCOPED principal. Two independent guards, in
    the order that matters: the caller passes a token the platform refuses
    Tier-1 calls under (``-32002 missing required scope``), and
    ``read_tier_calls`` never asks for one. The scope check is the real
    boundary; the filter is so a bug fails loudly rather than at the
    platform.
    """
    all_calls = tuple(calls)
    probed = read_tier_calls(all_calls)
    owned = client is None
    http = client or httpx.Client(timeout=_TIMEOUT_SECONDS)
    cache: dict[tuple[str, str], tuple[Mapping[str, Any] | None, str | None]] = {}
    drifts: list[Drift] = []
    errors: list[ProbeError] = []
    try:
        for call in probed:
            key = (call.tool, json.dumps(dict(call.arguments), sort_keys=True))
            if key not in cache:
                cache[key] = _call_tool(http, mcp_url, token, call.tool, dict(call.arguments))
            payload, error = cache[key]
            if error is not None or payload is None:
                errors.append(
                    ProbeError(
                        scenario=call.scenario,
                        tool=call.tool,
                        detail=error or "no text content block in result",
                    )
                )
                continue
            drifts.extend(compare(call, payload))
    finally:
        if owned:
            http.close()
    return ProbeResult(
        drifts=tuple(drifts),
        errors=tuple(errors),
        checked=len(probed),
        skipped_write_tier=len(all_calls) - len(probed),
        live_calls=len(cache),
    )


def _call_tool(
    client: httpx.Client,
    mcp_url: str,
    token: str,
    name: str,
    arguments: dict[str, Any],
) -> tuple[Mapping[str, Any] | None, str | None]:
    response = client.post(
        mcp_url,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        return None, f"non-object JSON response: {type(payload).__name__}"
    if "error" in payload:
        error = payload["error"]
        return None, f"MCP error {error.get('code')}: {error.get('message')}"
    result = payload.get("result") or {}
    for block in result.get("content", []):
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parsed = json.loads(block["text"])
            if isinstance(parsed, dict):
                return parsed, None
    return None, "no text content block in result"

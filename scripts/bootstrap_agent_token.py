#!/usr/bin/env python3
"""Bootstrap a working service-account token against a running incident-platform.

Collapses the manual register → promote → login → mint onboarding into one command for
all three eval principals, printing each plaintext token and the ``.env`` lines to set.
Idempotent. ``PLATFORM_REST_URL`` and ``PLATFORM_MCP_URL`` are honoured when exported.

Usage:
    uv run python scripts/bootstrap_agent_token.py
    uv run python scripts/bootstrap_agent_token.py --scope actions:execute
    make bootstrap-token

Three principals (owner decision O-4; platform ADR 0007, ADR 0012):

* ``incident-commander`` — the AGENT, ``PLATFORM_TOKEN``: reads plus ``actions:execute``,
  and **never** ``chaos:invoke``, the scope v0.6.5 keys the ``chaos.%`` audit withholding
  on; an account already holding it is stripped, loudly.
* ``incident-commander-chaos`` — the EVALUATOR, ``PLATFORM_CHAOS_TOKEN``: reads plus
  ``chaos:invoke``, no ``actions:execute``.
* ``incident-commander-smoke`` — the read-only twin, ``PLATFORM_SMOKE_TOKEN``.

``--scope`` WIDENS the agent account rather than replacing the defaults, and refuses
``chaos:invoke``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import httpx

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SNAPSHOT_PATH = _REPO_ROOT / "contracts" / "platform-tools.snapshot.json"
_API_VERSION_SUFFIX = "/api/v1"

DEFAULT_BASE_URL = "http://localhost:8000/api/v1"
DEFAULT_EMAIL = "agent-demo@example.com"
DEFAULT_PASSWORD = "demo-agent-pass-123"  # noqa: S105 - dev-only placeholder
# The DEMO stack's postgres (`make demo`), not the platform's dev stack — that default
# made the protocol's bare `make bootstrap-token` die. Cf. eval-reset's (ADR 0020).
DEFAULT_POSTGRES_CONTAINER = "incident-commander-demo-postgres-1"
DEFAULT_MCP_URL = "http://localhost:8001/mcp"
SERVICE_ACCOUNT_NAME = "incident-commander"
# The agent under test: reads plus actions:execute. chaos:invoke is absent and that is
# load-bearing — v0.6.5 keys `hidden_audit_action_prefixes` on it, so while the agent held
# it `list_audit_events` named the hook that broke it (G3, O-4).
SERVICE_ACCOUNT_SCOPES = [
    "telemetry:read",
    "incidents:read",
    "actions:execute",
]
# The evaluator: chaos:invoke to seed a fault world, reads to verify its own seeding, and
# NOT actions:execute — remediating is the thing being measured.
CHAOS_SERVICE_ACCOUNT_NAME = "incident-commander-chaos"
CHAOS_SERVICE_ACCOUNT_SCOPES = [
    "telemetry:read",
    "incidents:read",
    "chaos:invoke",
]
# The scope the AGENT account must never carry, as one set so the refusal below, the
# strip in `_create_or_get_sa` and the scope list cannot drift apart.
AGENT_FORBIDDEN_SCOPES = frozenset({"chaos:invoke"})
# Read-only twin for the smoke pass: with no actions:execute a Tier-1 attempt 403s and
# grades as an escalation, so "read-only smoke" is structurally true (2026-08-03).
SMOKE_SERVICE_ACCOUNT_NAME = "incident-commander-smoke"
SMOKE_SERVICE_ACCOUNT_SCOPES = [
    "telemetry:read",
    "incidents:read",
]

_SAFE_EMAIL = re.compile(r"^[A-Za-z0-9._+@-]+$")


def known_scopes() -> frozenset[str]:
    """Every scope the pinned platform declares, per the blessed snapshot.

    CI diffs its ``required_scope`` against a live platform (WO-R2-130), which is what makes
    rejecting an unknown ``--scope`` safe. Empty when unreadable.
    """
    try:
        payload = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    tools = payload.get("tools") if isinstance(payload, dict) else None
    if not isinstance(tools, list):
        return frozenset()
    return frozenset(
        str(tool["required_scope"])
        for tool in tools
        if isinstance(tool, dict) and tool.get("required_scope")
    )


def base_url_default() -> str:
    """The REST base, honouring ``PLATFORM_REST_URL`` from the operator's .env.

    The runbook sets it to the host root, so ``/api/v1`` is appended unless already there.
    """
    raw = os.getenv("PLATFORM_REST_URL")
    if not raw:
        return DEFAULT_BASE_URL
    trimmed = raw.rstrip("/")
    return trimmed if _API_VERSION_SUFFIX in trimmed else f"{trimmed}{_API_VERSION_SUFFIX}"


def _register(client: httpx.Client, email: str, password: str) -> None:
    """Create the demo user and its tenant; an existing user is already success."""
    r = client.post(
        "/auth/register",
        json={
            "email": email,
            "password": password,
            "new_tenant_name": "agent-demo",
        },
    )
    if r.status_code in (200, 201):
        print(f"registered {email}")
    elif r.status_code == 409:
        print(f"user {email} exists, skipping register")
    else:
        r.raise_for_status()


def _promote(container: str, email: str) -> None:
    """Direct SQL: elevate to platform admin so the API grants service-account rights.

    The email arrives only by psql binding (``-f -`` on stdin, ``-v email=...``);
    ``_SAFE_EMAIL`` is a backstop, not the control.
    """
    if not _SAFE_EMAIL.match(email):
        raise ValueError(f"refusing to inject unsafe email into SQL: {email!r}")
    cmd = [
        "docker",
        "exec",
        "-i",  # keep stdin open so psql can read the piped statement
        container,
        "psql",
        "-U",
        "postgres",
        "-d",
        "incident_platform",
        "-v",
        f"email={email}",
        "-f",
        "-",
    ]
    subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        input=b"UPDATE users SET is_platform_admin=true, role='admin' WHERE email=:'email';",
    )
    print(f"promoted {email} to platform admin")


def _login(client: httpx.Client, email: str, password: str) -> str:
    """Exchange the demo credentials for the admin JWT the rest of the flow uses."""
    r = client.post("/auth/login", json={"email": email, "password": password})
    r.raise_for_status()
    token: str = r.json()["access_token"]
    print(f"logged in {email}")
    return token


def _create_or_get_sa(
    client: httpx.Client,
    jwt: str,
    name: str,
    scopes: list[str],
    *,
    exact: bool = False,
    forbidden: frozenset[str] = frozenset(),
) -> str:
    """Create the service account, or reuse it, widening its scopes to ``scopes``.

    ``exact=True`` corrects BOTH ways, so the smoke SA cannot keep actions:execute, and
    ``forbidden`` (only the agent's ``chaos:invoke``, which the live account already holds)
    comes off even when widening. Correcting scopes needs PATCH (v0.3.0+).
    """
    headers = {"Authorization": f"Bearer {jwt}"}
    wanted = sorted(set(scopes) - forbidden)
    r = client.post(
        "/admin/service-accounts",
        json={"name": name, "scopes": wanted},
        headers=headers,
    )
    if r.status_code in (200, 201):
        sa_id: str = r.json()["id"]
        print(f"created service account {name} (id={sa_id}) with scopes={wanted}")
        return sa_id
    if r.status_code == 409:
        r2 = client.get("/admin/service-accounts", headers=headers)
        r2.raise_for_status()
        for sa in r2.json()["items"]:
            if sa["name"] == name:
                existing_id: str = sa["id"]
                existing_scopes: list[str] = sa.get("scopes", [])
                current = set(existing_scopes)
                # Replace semantics under `exact`, union otherwise — then the
                # forbidden set comes off either way.
                target = set(wanted) if exact else current | set(wanted)
                stripped = sorted(target & forbidden)
                target -= forbidden
                if stripped:
                    print(
                        f"NOTE: removing scope(s) {stripped} from {name} — this "
                        "principal is the agent under test and must not hold them "
                        "(owner decision O-4; platform v0.6.5 hides the chaos audit "
                        "stream from principals that cannot fire chaos). Tokens "
                        "minted before now keep the scopes they carry: paste the "
                        "PLATFORM_TOKEN printed below and stop using the old one."
                    )
                if current == target:
                    print(
                        f"service account {name} exists (id={existing_id}) "
                        f"with scopes={sorted(existing_scopes)}, reusing"
                    )
                    return existing_id
                print(
                    f"service account {name} exists (id={existing_id}) with "
                    f"scopes={sorted(existing_scopes)}; correcting scopes to "
                    f"{sorted(target)}"
                )
                patch = client.patch(
                    f"/admin/service-accounts/{existing_id}",
                    json={"scopes": sorted(target)},
                    headers=headers,
                )
                if patch.status_code in (200, 204):
                    print(f"scopes on {name} are now {sorted(target)}")
                elif patch.status_code == 404:
                    # Old platform (pre-v0.3.0) — no PATCH route. Warn but
                    # keep the flow going with the existing narrower scopes.
                    extra = sorted(current - target)
                    if extra:
                        print(
                            f"WARNING: platform lacks PATCH — {name} keeps EXTRA "
                            f"scopes {extra}. The principal is NOT the one this "
                            "script claims until the platform is upgraded and this "
                            "is rerun."
                        )
                    else:
                        print(
                            f"WARNING: platform lacks PATCH /admin/service-accounts/{{id}} "
                            f"(pre-v0.3.0). Existing scopes {sorted(existing_scopes)} kept; "
                            f"chaos / actions calls will 403 until platform is upgraded."
                        )
                else:
                    patch.raise_for_status()
                return existing_id
        raise RuntimeError(f"{name} conflicted but not present in listing")
    r.raise_for_status()
    raise RuntimeError("unreachable")


def _mint_token(client: httpx.Client, jwt: str, sa_id: str) -> str:
    """Mint one service account a fresh token and return its plaintext."""
    r = client.post(
        f"/admin/service-accounts/{sa_id}/tokens",
        json={},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    r.raise_for_status()
    plaintext: str = r.json()["plaintext"]
    return plaintext


def main(argv: list[str] | None = None) -> int:
    """Register, promote, and mint a token for each of the three eval principals."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=base_url_default(),
        help="Platform REST base. Defaults to $PLATFORM_REST_URL when exported.",
    )
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument(
        "--postgres-container",
        default=DEFAULT_POSTGRES_CONTAINER,
        help="Container name that runs the platform's postgres",
    )
    parser.add_argument(
        "--mcp-url",
        default=os.getenv("PLATFORM_MCP_URL") or DEFAULT_MCP_URL,
        help=(
            "Echoed in the printed .env snippet. Defaults to $PLATFORM_MCP_URL "
            "when exported, so a port override is never printed back wrong."
        ),
    )
    parser.add_argument(
        "--scope",
        action="append",
        dest="scopes",
        metavar="SCOPE",
        help=(
            "Extra scope for the AGENT service account; repeat for more. Added "
            "to the defaults (" + ", ".join(SERVICE_ACCOUNT_SCOPES) + ") rather "
            "than replacing them. chaos:invoke is refused — it belongs to the "
            + CHAOS_SERVICE_ACCOUNT_NAME
            + " account. The read-only smoke account is never widened."
        ),
    )
    args = parser.parse_args(argv)

    # Deduplicated, order preserved, defaults first: an extra scope WIDENS. Replacing
    # would mint a principal that can act and read no telemetry.
    requested = list(dict.fromkeys(args.scopes or []))
    declared = known_scopes()
    unknown = [scope for scope in requested if scope not in declared] if declared else []
    if unknown:
        # Before any network call: a typo'd scope that reached the platform
        # would mint a plausible-looking token that 403s at the first tool.
        print(
            f"unknown scope(s): {', '.join(unknown)}. The pinned platform "
            f"declares: {', '.join(sorted(declared))}.",
            file=sys.stderr,
        )
        return 2
    # Refused, not quietly dropped: an exit code now beats finding out from a trajectory
    # that names the chaos hook the agent was never meant to see.
    refused = sorted(set(requested) & AGENT_FORBIDDEN_SCOPES)
    if refused:
        print(
            f"refusing --scope {', '.join(refused)} on {SERVICE_ACCOUNT_NAME}: that "
            f"scope belongs to the {CHAOS_SERVICE_ACCOUNT_NAME} account, whose token "
            "is printed as PLATFORM_CHAOS_TOKEN — an agent token carrying it can read "
            "the chaos audit stream, which is the leak the split closes (O-4).",
            file=sys.stderr,
        )
        return 2
    agent_scopes = list(dict.fromkeys([*SERVICE_ACCOUNT_SCOPES, *requested]))

    with httpx.Client(base_url=args.base_url, timeout=10.0) as client:
        _register(client, args.email, args.password)
        _promote(args.postgres_container, args.email)
        jwt = _login(client, args.email, args.password)
        sa_id = _create_or_get_sa(
            client,
            jwt,
            SERVICE_ACCOUNT_NAME,
            agent_scopes,
            forbidden=AGENT_FORBIDDEN_SCOPES,
        )
        token = _mint_token(client, jwt, sa_id)
        # Widening, not exact: a scope deliberately added to the evaluator account should
        # not vanish on the next bootstrap. It must never gain actions:execute.
        chaos_sa_id = _create_or_get_sa(
            client, jwt, CHAOS_SERVICE_ACCOUNT_NAME, CHAOS_SERVICE_ACCOUNT_SCOPES
        )
        chaos_token = _mint_token(client, jwt, chaos_sa_id)
        smoke_sa_id = _create_or_get_sa(
            client,
            jwt,
            SMOKE_SERVICE_ACCOUNT_NAME,
            SMOKE_SERVICE_ACCOUNT_SCOPES,
            exact=True,
        )
        smoke_token = _mint_token(client, jwt, smoke_sa_id)

    print()
    print("=" * 60)
    print("Tokens minted. Copy into .env:")
    print()
    print(f"PLATFORM_MCP_URL={args.mcp_url}")
    # THREE credentials, three principals, and the labels are the point: one value pasted
    # into two variables gives a runner that cannot seed, or an agent that reads the lab.
    print(f"PLATFORM_TOKEN={token}")
    print(f"PLATFORM_CHAOS_TOKEN={chaos_token}")
    print(f"PLATFORM_SMOKE_TOKEN={smoke_token}")
    # Ids, not credentials: they scope the post-stage audit guard to the two accounts just
    # minted, so a shared platform's other principals cannot fail or mask a stage (A-13).
    print(f"PLATFORM_AGENT_PRINCIPAL_ID={sa_id}")
    print(f"PLATFORM_SMOKE_PRINCIPAL_ID={smoke_sa_id}")
    print("=" * 60)
    print()
    print(
        f"Three principals: {SERVICE_ACCOUNT_NAME} = {', '.join(sorted(agent_scopes))} "
        "(the agent under test — no chaos:invoke, so it cannot fire the lab and "
        "cannot read that the lab fired); "
        f"{CHAOS_SERVICE_ACCOUNT_NAME} = {', '.join(sorted(CHAOS_SERVICE_ACCOUNT_SCOPES))} "
        "(the evaluator: seeds, verifies and resets the fault world); "
        f"{SMOKE_SERVICE_ACCOUNT_NAME} = "
        f"{', '.join(sorted(SMOKE_SERVICE_ACCOUNT_SCOPES))} (the read-only stage)."
    )
    print("Paste all three token lines. Never commit any of them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Bootstrap a working service-account token against a running incident-platform.

Collapses the manual four-step onboarding (register → promote → login → mint)
into one command. Prints the plaintext token and the two ``.env`` lines to set.

Assumes the platform is running via its own docker-compose with the standard
container names (``incident-platform-postgres-1``, ``incident-platform-app-1``).
Idempotent — safe to rerun.

Usage:
    uv run python scripts/bootstrap_agent_token.py

Or via Makefile:
    make bootstrap-token
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

import httpx

DEFAULT_BASE_URL = "http://localhost:8000/api/v1"
DEFAULT_EMAIL = "agent-demo@example.com"
DEFAULT_PASSWORD = "demo-agent-pass-123"  # noqa: S105 - dev-only placeholder
DEFAULT_POSTGRES_CONTAINER = "incident-platform-postgres-1"
DEFAULT_MCP_URL = "http://localhost:8001/mcp"
SERVICE_ACCOUNT_NAME = "incident-commander"
# Phase 6+ needs actions:execute (Tier-1 remediation) and chaos:invoke
# (chaos setup for live-eval prep). Keeping the read scopes so the
# investigation path still works.
SERVICE_ACCOUNT_SCOPES = [
    "telemetry:read",
    "incidents:read",
    "actions:execute",
    "chaos:invoke",
]
# Read-only twin for the smoke pass: with no actions:execute scope, a
# Tier-1 attempt 403s at the platform, wraps as MCPError, and grades as
# an escalation — "read-only smoke" becomes structurally true instead of
# a property of the scenario list (2026-08-03 campaign: consumer_lag_high
# fired a real replay during the read-only pass).
SMOKE_SERVICE_ACCOUNT_NAME = "incident-commander-smoke"
SMOKE_SERVICE_ACCOUNT_SCOPES = [
    "telemetry:read",
    "incidents:read",
]

_SAFE_EMAIL = re.compile(r"^[A-Za-z0-9._+@-]+$")


def _register(client: httpx.Client, email: str, password: str) -> None:
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
    """Direct SQL: elevate to platform admin so the API grants service-account rights."""
    if not _SAFE_EMAIL.match(email):
        raise ValueError(f"refusing to inject unsafe email into SQL: {email!r}")
    cmd = [
        "docker",
        "exec",
        container,
        "psql",
        "-U",
        "postgres",
        "-d",
        "incident_platform",
        "-c",
        f"UPDATE users SET is_platform_admin=true, role='admin' WHERE email='{email}'",
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    print(f"promoted {email} to platform admin")


def _login(client: httpx.Client, email: str, password: str) -> str:
    r = client.post("/auth/login", json={"email": email, "password": password})
    r.raise_for_status()
    token: str = r.json()["access_token"]
    print(f"logged in {email}")
    return token


def _create_or_get_sa(
    client: httpx.Client, jwt: str, name: str, scopes: list[str], *, exact: bool = False
) -> str:
    """Create the service account, or reuse the existing one — widening
    its scopes to ``scopes`` if it exists with a narrower set.

    With ``exact=True`` the scopes are corrected in BOTH directions: an
    existing account with extra scopes is narrowed back down. The smoke
    SA uses this — a read-only principal that silently kept
    actions:execute would defeat its whole purpose.

    Scope changes use the platform's ``PATCH /admin/service-accounts/{id}``
    endpoint (v0.3.0+). Older platforms don't have PATCH — the script
    falls back to reusing the existing scopes and prints a warning, so a
    stale platform doesn't crash the whole flow.
    """
    headers = {"Authorization": f"Bearer {jwt}"}
    r = client.post(
        "/admin/service-accounts",
        json={"name": name, "scopes": scopes},
        headers=headers,
    )
    if r.status_code in (200, 201):
        sa_id: str = r.json()["id"]
        print(f"created service account {name} (id={sa_id}) with scopes={scopes}")
        return sa_id
    if r.status_code == 409:
        r2 = client.get("/admin/service-accounts", headers=headers)
        r2.raise_for_status()
        for sa in r2.json()["items"]:
            if sa["name"] == name:
                existing_id: str = sa["id"]
                existing_scopes: list[str] = sa.get("scopes", [])
                acceptable = (
                    set(existing_scopes) == set(scopes)
                    if exact
                    else set(existing_scopes) >= set(scopes)
                )
                if acceptable:
                    print(
                        f"service account {name} exists (id={existing_id}) "
                        f"with scopes={sorted(existing_scopes)}, reusing"
                    )
                    return existing_id
                verb = "correcting scopes to" if exact else "widening to include"
                delta = sorted(set(scopes)) if exact else sorted(set(scopes) - set(existing_scopes))
                print(
                    f"service account {name} exists (id={existing_id}) with "
                    f"scopes={sorted(existing_scopes)}; {verb} {delta}"
                )
                patch = client.patch(
                    f"/admin/service-accounts/{existing_id}",
                    json={"scopes": scopes},
                    headers=headers,
                )
                if patch.status_code in (200, 204):
                    print(f"widened scopes on {name} to {sorted(scopes)}")
                elif patch.status_code == 404:
                    # Old platform (pre-v0.3.0) — no PATCH route. Warn but
                    # keep the flow going with the existing narrower scopes.
                    extra = sorted(set(existing_scopes) - set(scopes))
                    if exact and extra:
                        print(
                            f"WARNING: platform lacks PATCH — {name} keeps EXTRA "
                            f"scopes {extra}. Read-only is NOT structurally true "
                            "until the platform is upgraded and this rerun."
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
    r = client.post(
        f"/admin/service-accounts/{sa_id}/tokens",
        json={},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    r.raise_for_status()
    plaintext: str = r.json()["plaintext"]
    return plaintext


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Platform REST base")
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument(
        "--postgres-container",
        default=DEFAULT_POSTGRES_CONTAINER,
        help="Container name that runs the platform's postgres",
    )
    parser.add_argument(
        "--mcp-url",
        default=DEFAULT_MCP_URL,
        help="Reported back in the printed .env snippet",
    )
    args = parser.parse_args()

    with httpx.Client(base_url=args.base_url, timeout=10.0) as client:
        _register(client, args.email, args.password)
        _promote(args.postgres_container, args.email)
        jwt = _login(client, args.email, args.password)
        sa_id = _create_or_get_sa(client, jwt, SERVICE_ACCOUNT_NAME, SERVICE_ACCOUNT_SCOPES)
        token = _mint_token(client, jwt, sa_id)
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
    print(f"PLATFORM_TOKEN={token}")
    print(f"PLATFORM_SMOKE_TOKEN={smoke_token}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
SERVICE_ACCOUNT_SCOPES = ["telemetry:read", "incidents:read"]

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


def _create_or_get_sa(client: httpx.Client, jwt: str, name: str, scopes: list[str]) -> str:
    headers = {"Authorization": f"Bearer {jwt}"}
    r = client.post(
        "/admin/service-accounts",
        json={"name": name, "scopes": scopes},
        headers=headers,
    )
    if r.status_code in (200, 201):
        sa_id: str = r.json()["id"]
        print(f"created service account {name} (id={sa_id})")
        return sa_id
    if r.status_code == 409:
        r2 = client.get("/admin/service-accounts", headers=headers)
        r2.raise_for_status()
        for sa in r2.json()["items"]:
            if sa["name"] == name:
                existing_id: str = sa["id"]
                print(f"service account {name} exists (id={existing_id}), reusing")
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

    print()
    print("=" * 60)
    print("Token minted. Copy into .env:")
    print()
    print(f"PLATFORM_MCP_URL={args.mcp_url}")
    print(f"PLATFORM_TOKEN={token}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())

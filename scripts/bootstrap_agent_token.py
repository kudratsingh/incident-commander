#!/usr/bin/env python3
"""Bootstrap a working service-account token against a running incident-platform.

Collapses the manual four-step onboarding (register → promote → login → mint)
into one command. Prints the plaintext token and the two ``.env`` lines to set.

Assumes the platform is running via its own docker-compose with the standard
container names (``incident-platform-postgres-1``, ``incident-platform-app-1``).
Idempotent — safe to rerun.

Usage:
    uv run python scripts/bootstrap_agent_token.py
    uv run python scripts/bootstrap_agent_token.py --scope chaos:invoke

Or via Makefile:
    make bootstrap-token

``--scope`` WIDENS the agent service account, repeatably; it never replaces
the defaults, because a token that could seed chaos and read no telemetry
would fail one step into the eval it was minted for. Scope names are checked
against the pinned platform's own contract snapshot.

``PLATFORM_REST_URL`` and ``PLATFORM_MCP_URL`` are honoured when exported, so
the ``.env`` block printed at the end always echoes the stack you are
actually running rather than this file's localhost defaults.
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
# The DEMO stack's postgres — the one this repo owns and the one
# docs/runbook.md tells you to boot with `make demo`. It used to default to
# the platform's own dev-stack container, so the bare `make bootstrap-token`
# in the live-eval protocol died on a CalledProcessError one line after
# `make demo` succeeded. CI never noticed: it passes --postgres-container
# explicitly. Same shape as eval-reset's compose default (ADR 0020).
DEFAULT_POSTGRES_CONTAINER = "incident-commander-demo-postgres-1"
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


def known_scopes() -> frozenset[str]:
    """Every scope the pinned platform declares, per the blessed snapshot.

    Read from ``contracts/platform-tools.snapshot.json`` rather than
    hardcoded here: each tool carries its ``required_scope``, CI's
    ``contract`` job diffs that snapshot against a live platform on every
    PR, and WO-R2-130 put ``required_scope`` itself under that diff. So this
    set cannot drift from the platform without CI saying so — which is what
    makes rejecting an unknown ``--scope`` safe rather than merely
    opinionated.

    Returns an empty set if the snapshot is missing or unreadable; the
    caller then skips validation rather than blocking a bootstrap on it.
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

    The runbook's ``.env`` sets ``PLATFORM_REST_URL`` to the host root
    (``http://localhost:8000``) while this script talks to the versioned API
    beneath it, so the ``/api/v1`` suffix is appended unless the operator
    already wrote one. Reading it at all is the point: hardcoding
    ``localhost:8000`` meant an operator on a non-default port watched this
    script report success against a stack they were not running.
    """
    raw = os.getenv("PLATFORM_REST_URL")
    if not raw:
        return DEFAULT_BASE_URL
    trimmed = raw.rstrip("/")
    return trimmed if _API_VERSION_SUFFIX in trimmed else f"{trimmed}{_API_VERSION_SUFFIX}"


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
    """Direct SQL: elevate to platform admin so the API grants service-account rights.

    The email reaches the statement only through psql variable binding: the
    constant SQL is piped on stdin (``-f -``, where ``:'email'`` interpolation
    happens — ``-c`` never interpolates variables) and the value rides
    ``-v email=...``. Binding is the primary injection control; the
    ``_SAFE_EMAIL`` allowlist below stays as a defense-in-depth backstop.
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


def main(argv: list[str] | None = None) -> int:
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
            "Extra scope for the agent service account; repeat for more. Added "
            "to the defaults (" + ", ".join(SERVICE_ACCOUNT_SCOPES) + ") rather "
            "than replacing them. The read-only smoke account is never widened."
        ),
    )
    args = parser.parse_args(argv)

    # Deduplicated, order preserved, defaults first: `--scope chaos:invoke`
    # is documented as the remedy for a chaos refusal, so it has to WIDEN.
    # Replacing would mint a principal that can seed chaos and read no
    # telemetry — failing one step later for a reason nobody would trace
    # back to this command.
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
    agent_scopes = list(dict.fromkeys([*SERVICE_ACCOUNT_SCOPES, *requested]))

    with httpx.Client(base_url=args.base_url, timeout=10.0) as client:
        _register(client, args.email, args.password)
        _promote(args.postgres_container, args.email)
        jwt = _login(client, args.email, args.password)
        sa_id = _create_or_get_sa(client, jwt, SERVICE_ACCOUNT_NAME, agent_scopes)
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
    # Ids, not credentials — they scope the post-stage audit guard to the
    # two service accounts this script just minted, so a shared platform's
    # other principals cannot fail (or mask) a smoke stage (A-13).
    print(f"PLATFORM_AGENT_PRINCIPAL_ID={sa_id}")
    print(f"PLATFORM_SMOKE_PRINCIPAL_ID={smoke_sa_id}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Bootstrap a working service-account token against a running incident-platform.

Collapses the manual four-step onboarding (register → promote → login → mint)
into one command, for all three eval principals. Prints each plaintext token
and the ``.env`` lines to set.

Assumes the platform is running via its own docker-compose with the standard
container names (``incident-platform-postgres-1``, ``incident-platform-app-1``).
Idempotent — safe to rerun.

Usage:
    uv run python scripts/bootstrap_agent_token.py
    uv run python scripts/bootstrap_agent_token.py --scope actions:execute

Or via Makefile:
    make bootstrap-token

**Three principals, two of them credentials the eval uses on every live run**
(owner decision O-4, 2026-09-15; platform ADR 0007 § two principals, ADR 0012
§ "Why two principals"):

* ``incident-commander`` — the AGENT under test. ``telemetry:read``,
  ``incidents:read``, ``actions:execute``, and **never** ``chaos:invoke``:
  platform v0.6.5 withholds the ``chaos.%`` audit rows from principals that
  cannot fire chaos, so a token holding that scope lets the agent read which
  fault was injected seconds before its own alert. Printed as
  ``PLATFORM_TOKEN``. If the account already holds ``chaos:invoke`` this
  script strips it and says so — the same one narrowing the platform's own
  ``scripts/seed_incident_commander.py`` performs, for the same reason.
* ``incident-commander-chaos`` — the EVALUATOR. ``telemetry:read``,
  ``incidents:read``, ``chaos:invoke``: it seeds a fault world, verifies what
  it seeded, and tears it down. No ``actions:execute`` — remediation is the
  agent's job, and this principal must never be able to do it. Printed as
  ``PLATFORM_CHAOS_TOKEN``.
* ``incident-commander-smoke`` — the read-only twin for ``make eval-smoke``.

``--scope`` WIDENS the agent service account, repeatably; it never replaces
the defaults, because a token that could act and read no telemetry would fail
one step into the eval it was minted for. Scope names are checked against the
pinned platform's own contract snapshot, and asking for ``chaos:invoke`` there
is refused outright: that scope belongs to the chaos account now, and granting
it to the agent would undo the split rather than widen a principal.

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
# The agent under test. Phase 6+ needs actions:execute (Tier-1 remediation)
# on top of the two read scopes the investigation path uses.
#
# chaos:invoke is deliberately absent, and its absence is load-bearing rather
# than tidy. Platform v0.6.5 hides every `chaos.%` audit row from principals
# that cannot fire chaos (`hidden_audit_action_prefixes`, keyed on exactly
# this scope), so while the agent held it `list_audit_events` answered "who
# broke this?" with the hook name and its arguments — a hidden-label leak that
# invalidated the diagnosis claim of every live remediation run (divergence
# G3, owner decision O-4). The filter and the split ship together because
# either one alone does nothing.
SERVICE_ACCOUNT_SCOPES = [
    "telemetry:read",
    "incidents:read",
    "actions:execute",
]
# The evaluator / runner. Holds chaos:invoke so it can seed a fault world and
# then read the rows it seeded; holds the two read scopes for the same reason
# (verifying its own seeding), and NOT actions:execute — remediating is the
# thing being measured, so the principal that stages the world must not be
# able to do it.
CHAOS_SERVICE_ACCOUNT_NAME = "incident-commander-chaos"
CHAOS_SERVICE_ACCOUNT_SCOPES = [
    "telemetry:read",
    "incidents:read",
    "chaos:invoke",
]
# The scope the AGENT account must never carry. Kept as a set beside the
# table above so the refusal below, the strip in `_create_or_get_sa` and the
# scope list cannot drift apart — this is the whole content of the split.
AGENT_FORBIDDEN_SCOPES = frozenset({"chaos:invoke"})
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
    """Create the service account, or reuse the existing one — widening
    its scopes to ``scopes`` if it exists with a narrower set.

    With ``exact=True`` the scopes are corrected in BOTH directions: an
    existing account with extra scopes is narrowed back down. The smoke
    SA uses this — a read-only principal that silently kept
    actions:execute would defeat its whole purpose.

    ``forbidden`` is the one narrowing that happens even on the widening
    path, and the agent account is the only caller that passes any:
    ``chaos:invoke``. Widening alone could never reach the state O-4 asks
    for, because the live ``incident-commander`` account already HOLDS that
    grant — so a union-only bootstrap would report success and leave the
    leak open. The removal is announced rather than silent (the platform
    seeder's D-01 rule), and tokens minted before now keep the scopes they
    carry, which is why the banner tells the operator to re-paste.

    Scope changes use the platform's ``PATCH /admin/service-accounts/{id}``
    endpoint (v0.3.0+). Older platforms don't have PATCH — the script
    falls back to reusing the existing scopes and prints a warning, so a
    stale platform doesn't crash the whole flow.
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

    # Deduplicated, order preserved, defaults first: an extra scope WIDENS.
    # Replacing would mint a principal that can act and read no telemetry —
    # failing one step later for a reason nobody would trace back to this
    # command.
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
    # Refused, not quietly dropped. An operator asking for this wants a
    # principal the split says cannot exist, and finding out later — from a
    # trajectory that names the chaos hook the agent was never meant to see —
    # is worse than an exit code now.
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
        # Widening, not exact: the chaos account is the evaluator's, and an
        # operator who has deliberately added a scope to it (a future reset
        # leg needing more than reads) should not have it silently removed by
        # the next bootstrap. What it must never gain is actions:execute, and
        # nothing here grants that.
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
    # THREE credentials, three principals, and the labels are the point: an
    # operator who pasted one value into two variables would have a runner
    # that cannot seed, or an agent that can read the lab, and neither
    # failure names itself.
    print(f"PLATFORM_TOKEN={token}")
    print(f"PLATFORM_CHAOS_TOKEN={chaos_token}")
    print(f"PLATFORM_SMOKE_TOKEN={smoke_token}")
    # Ids, not credentials — they scope the post-stage audit guard to the
    # two service accounts this script just minted, so a shared platform's
    # other principals cannot fail (or mask) a smoke stage (A-13).
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

"""Regression: the promote statement must never carry the email in its SQL text.

Finding C-14: `_promote`'s privilege-granting UPDATE used to f-string the email in,
guarded only by _SAFE_EMAIL. WO-C6-01: the email reaches psql only as `-v email=...`,
the statement is a constant on stdin with `:'email'`, and the regex is a backstop.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from scripts import bootstrap_agent_token
from scripts.bootstrap_agent_token import (
    AGENT_FORBIDDEN_SCOPES,
    CHAOS_SERVICE_ACCOUNT_NAME,
    CHAOS_SERVICE_ACCOUNT_SCOPES,
    DEFAULT_POSTGRES_CONTAINER,
    SERVICE_ACCOUNT_NAME,
    SERVICE_ACCOUNT_SCOPES,
    SMOKE_SERVICE_ACCOUNT_NAME,
    SMOKE_SERVICE_ACCOUNT_SCOPES,
    _promote,
    base_url_default,
    known_scopes,
    main,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONTAINER = "incident-platform-postgres-1"
_EMAIL = "agent-demo@example.com"


class _RunRecorder:
    """Stands in for subprocess.run: records argv + stdin, executes nothing."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bytes | None]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        stdin = kwargs.get("input")
        assert stdin is None or isinstance(stdin, bytes), "_promote must pass stdin as bytes"
        self.calls.append((list(args), stdin))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=b"", stderr=b"")


def _install_recorder(monkeypatch: pytest.MonkeyPatch) -> _RunRecorder:
    recorder = _RunRecorder()
    monkeypatch.setattr(subprocess, "run", recorder)
    return recorder


def test_email_reaches_the_statement_only_via_psql_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _install_recorder(monkeypatch)

    _promote(_CONTAINER, _EMAIL)

    assert len(recorder.calls) == 1
    argv, stdin = recorder.calls[0]
    carrying = [arg for arg in argv if _EMAIL in arg]
    assert carrying == [f"email={_EMAIL}"], (
        f"raw email must appear only in the -v binding element, found in: {carrying!r}"
    )
    # The assertion that would have caught the f-string version of C-14.
    assert _EMAIL.encode() not in (stdin or b"")


def test_statement_is_constant_with_bound_placeholder_on_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _install_recorder(monkeypatch)

    _promote(_CONTAINER, _EMAIL)

    argv, stdin = recorder.calls[0]
    assert argv[:3] == ["docker", "exec", "-i"], "stdin piping needs docker exec -i"
    assert "-f" in argv, "psql must read the statement from stdin (-f -)"
    assert argv[argv.index("-f") + 1] == "-"
    assert stdin is not None
    assert b"WHERE email=:'email'" in stdin


def test_unsafe_email_is_rejected_before_subprocess_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _install_recorder(monkeypatch)

    with pytest.raises(ValueError, match="refusing to inject unsafe email"):
        _promote(_CONTAINER, "a'b")

    assert recorder.calls == []


class TestTheDefaultTargetsTheStackTheDocsTellYouToBoot:
    """`make bootstrap-token` with no arguments must hit the demo stack.

    The default named `incident-platform-postgres-1`, a container `make demo` never starts,
    so the documented protocol died one line after `make demo` reported success. CI passed
    --postgres-container explicitly. These derive the name from demo/compose.yml.
    """

    @staticmethod
    def _compose() -> dict[str, Any]:
        loaded: dict[str, Any] = yaml.safe_load((_REPO_ROOT / "demo" / "compose.yml").read_text())
        return loaded

    def test_the_default_container_is_the_one_make_demo_starts(self) -> None:
        compose = self._compose()
        project = compose["name"]
        # Compose names containers <project>-<service>-<index>.
        expected = f"{project}-postgres-1"
        assert expected == DEFAULT_POSTGRES_CONTAINER, (
            f"bootstrap defaults to {DEFAULT_POSTGRES_CONTAINER!r}, but `make demo` "
            f"starts {expected!r}. The documented two-command protocol is broken."
        )

    def test_the_service_it_names_actually_exists_in_the_demo_stack(self) -> None:
        # Guards the other half: a container name can match the project and
        # still name a service the demo stack does not define.
        services = self._compose()["services"]
        assert "postgres" in services, (
            "demo/compose.yml no longer defines a `postgres` service — the "
            "bootstrap default names a container that will never exist"
        )


class _FakePlatform:
    """A whole platform in a MockTransport: register, login, SAs, tokens.

    Nothing touches a running stack — the demo stack is the coordinator's rehearsal
    fixture. ``existing`` pre-loads accounts the script finds already there (the live
    ``incident-commander`` HOLDS ``chaos:invoke``); ``patched`` records what each PATCH asked.
    """

    def __init__(self, existing: dict[str, list[str]] | None = None) -> None:
        self.created: dict[str, list[str]] = {}
        self.existing: dict[str, list[str]] = dict(existing or {})
        self.patched: dict[str, list[str]] = {}
        self.hosts: list[str] = []

    def _id_for(self, name: str) -> str:
        return f"sa-existing-{name}"

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.hosts.append(f"{request.url.scheme}://{request.url.netloc.decode()}")
        path = request.url.path
        if path.endswith("/auth/register"):
            return httpx.Response(201, json={"id": "user-1"})
        if path.endswith("/auth/login"):
            return httpx.Response(200, json={"access_token": "jwt-1"})
        if path.endswith("/admin/service-accounts") and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": self._id_for(name), "name": name, "scopes": scopes}
                        for name, scopes in self.existing.items()
                    ]
                },
            )
        if path.endswith("/admin/service-accounts"):
            body = json.loads(request.content)
            name = str(body["name"])
            if name in self.existing:
                return httpx.Response(409, json={"detail": f"{name} exists"})
            self.created[name] = list(body["scopes"])
            return httpx.Response(201, json={"id": f"sa-{len(self.created)}"})
        if path.endswith("/tokens"):
            return httpx.Response(200, json={"plaintext": "sa_plaintext"})
        if request.method == "PATCH" and "/admin/service-accounts/" in path:
            sa_id = path.rsplit("/", 1)[-1]
            body = json.loads(request.content)
            for name in self.existing:
                if self._id_for(name) == sa_id:
                    self.patched[name] = list(body["scopes"])
                    self.existing[name] = list(body["scopes"])
                    return httpx.Response(200, json={"id": sa_id})
            return httpx.Response(404, json={"detail": f"no such sa {sa_id}"})
        return httpx.Response(404, json={"detail": f"unrouted {path}"})


def _install_fake(monkeypatch: pytest.MonkeyPatch, platform: _FakePlatform) -> _FakePlatform:
    real_client = httpx.Client

    def _factory(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(platform.handler)
        return real_client(*args, **kwargs)

    # The script does `import httpx`, so patching the module attribute here
    # is what its `httpx.Client(...)` call resolves through.
    monkeypatch.setattr(httpx, "Client", _factory)
    monkeypatch.setattr(bootstrap_agent_token, "_promote", lambda _container, _email: None)
    for var in ("PLATFORM_MCP_URL", "PLATFORM_REST_URL"):
        monkeypatch.delenv(var, raising=False)
    return platform


def _fake_platform(monkeypatch: pytest.MonkeyPatch) -> _FakePlatform:
    return _install_fake(monkeypatch, _FakePlatform())


class TestTheScopeFlagThreePlacesDocument:
    """``--scope`` is printed by three files; it must exist (WO-R2-100).

    Three documents named one interface and it did not exist: an operator pasted the fix
    and got ``unrecognized arguments: --scope``. Since v0.6.5 the remedy is pasting
    ``PLATFORM_CHAOS_TOKEN``, and the flag now refuses the one scope that undoes the split.
    """

    def test_the_flag_repeats(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        platform = _fake_platform(monkeypatch)
        assert main(["--scope", "actions:execute", "--scope", "telemetry:read"]) == 0
        capsys.readouterr()
        granted = platform.created[SERVICE_ACCOUNT_NAME]
        assert {"actions:execute", "telemetry:read"} <= set(granted)
        assert len(granted) == len(set(granted)), "scopes must not duplicate"

    def test_scope_widens_rather_than_replaces(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The footgun this flag must not become.

        If ``--scope`` REPLACED the defaults, the eval would fail a step later on a read.
        """
        platform = _fake_platform(monkeypatch)
        assert main(["--scope", "incidents:read"]) == 0
        capsys.readouterr()
        assert set(SERVICE_ACCOUNT_SCOPES) <= set(platform.created[SERVICE_ACCOUNT_NAME])

    def test_the_smoke_account_stays_read_only(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--scope widens the AGENT principal only; the smoke twin is read-only by construction."""
        platform = _fake_platform(monkeypatch)
        assert main(["--scope", "actions:execute"]) == 0
        capsys.readouterr()
        assert platform.created[SMOKE_SERVICE_ACCOUNT_NAME] == sorted(SMOKE_SERVICE_ACCOUNT_SCOPES)

    def test_an_unknown_scope_is_refused_before_anything_is_created(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A typo must not mint a token that 403s at the first tool call."""
        platform = _fake_platform(monkeypatch)
        assert main(["--scope", "chaos:invoak"]) == 2
        err = capsys.readouterr().err
        assert "chaos:invoak" in err
        assert "chaos:invoke" in err, "say which scopes the platform does declare"
        assert platform.created == {}, "refuse before touching the platform"

    def test_the_known_scope_set_comes_from_the_blessed_snapshot(self) -> None:
        """Not a hardcoded list: the contract job diffs `required_scope` live (WO-R2-130)."""
        assert known_scopes() == {
            "actions:execute",
            # The 5th, on the v0.6.13 pin: the commander's own run telemetry.
            "agent_runs:write",
            "chaos:invoke",
            "incidents:read",
            "telemetry:read",
        }

    def test_no_doc_tells_you_to_pass_a_scope_the_command_refuses(self) -> None:
        """The other half of the three-documents problem, updated for the split.

        Docs naming a scope the command now refuses is the same failure in reverse. Both halves:
        every documented value must be a declared scope AND one the agent may hold.
        """
        documented: set[str] = set()
        for rel in (
            "scripts/chaos_setup.py",
            "docs/runbook.md",
            "evals/guards.py",
            "scripts/bootstrap_agent_token.py",
        ):
            text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
            documented.update(re.findall(r"--scope\s+([A-Za-z_]+:[A-Za-z_]+)", text))
        assert documented, "layout canary: no --scope example found in the docs"
        assert documented <= known_scopes(), (
            f"docs tell operators to pass scope(s) the platform does not declare: "
            f"{sorted(documented - known_scopes())}"
        )
        refused = sorted(documented & AGENT_FORBIDDEN_SCOPES)
        assert refused == [], (
            f"docs tell operators to pass scope(s) this command refuses: {refused}. "
            "The remedy for a chaos refusal is PLATFORM_CHAOS_TOKEN, not a wider "
            "agent account."
        )


class TestItNeverPrintsAnOverrideBackWrong:
    """The .env snippet must echo what the operator actually set (WO-R2-100).

    The script hardcoded localhost:8000/8001 under "Copy into .env", so a non-default
    port got pasted over.
    """

    def test_an_exported_mcp_url_is_echoed_not_overwritten(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _fake_platform(monkeypatch)
        monkeypatch.setenv("PLATFORM_MCP_URL", "http://localhost:9001/mcp")
        assert main([]) == 0
        out = capsys.readouterr().out
        assert "PLATFORM_MCP_URL=http://localhost:9001/mcp" in out
        assert "8001" not in out, "the hardcoded default must not reappear"

    def test_an_explicit_flag_still_wins_over_the_environment(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _fake_platform(monkeypatch)
        monkeypatch.setenv("PLATFORM_MCP_URL", "http://localhost:9001/mcp")
        assert main(["--mcp-url", "http://elsewhere:7000/mcp"]) == 0
        assert "PLATFORM_MCP_URL=http://elsewhere:7000/mcp" in capsys.readouterr().out

    def test_an_exported_rest_url_is_the_one_it_talks_to(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Not just echoed — the REST override has to change where the
        register/login/mint calls actually go, or the script reports success
        against a stack the operator is not running."""
        platform = _fake_platform(monkeypatch)
        monkeypatch.setenv("PLATFORM_REST_URL", "http://localhost:9000")
        assert main([]) == 0
        capsys.readouterr()
        assert platform.hosts, "no REST call was made"
        assert set(platform.hosts) == {"http://localhost:9000"}

    def test_a_rest_url_that_already_names_the_api_version_is_not_doubled(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _fake_platform(monkeypatch)
        monkeypatch.setenv("PLATFORM_REST_URL", "http://localhost:9000/api/v1")
        assert main([]) == 0
        capsys.readouterr()
        assert base_url_default() == "http://localhost:9000/api/v1"


class TestTwoPrincipalsNotOne:
    """The token split (owner decision O-4, platform v0.6.5).

    The bootstrap must mint the same two accounts with the same scope tables as the
    platform's own seeder, because either may have seeded the eval world.
    """

    def test_the_agent_account_has_no_chaos_scope(self) -> None:
        # The platform hides `chaos.%` audit rows from principals without this scope.
        assert "chaos:invoke" not in SERVICE_ACCOUNT_SCOPES
        assert set(SERVICE_ACCOUNT_SCOPES) == {
            "telemetry:read",
            "incidents:read",
            "actions:execute",
            # The mirror of the platform's seeder (v0.6.13, ADR 0035). A WRITE-only
            # scope: it adds nothing the agent can read, which is what keeps ADR
            # 0012 intact while the console watches the run.
            "agent_runs:write",
        }

    def test_the_reporting_scope_buys_no_reading(self) -> None:
        """The agent may report its run and may not read one back.

        The scope exists for two write tools; if the pinned platform ever grows a read
        tool needing it, this account would silently gain the ability to read what it
        reported, which is the leak ADR 0035's design exists to prevent.
        """
        payload = json.loads(
            (_REPO_ROOT / "contracts" / "platform-tools.snapshot.json").read_text(encoding="utf-8")
        )
        scoped = {
            t["name"] for t in payload["tools"] if t.get("required_scope") == "agent_runs:write"
        }
        assert scoped == {"report_agent_run", "report_agent_briefing"}

    def test_the_chaos_account_can_seed_and_verify_but_not_act(self) -> None:
        # Reads included so the runner can verify what it seeded; actions:execute excluded.
        assert set(CHAOS_SERVICE_ACCOUNT_SCOPES) == {
            "telemetry:read",
            "incidents:read",
            "chaos:invoke",
        }
        assert "actions:execute" not in CHAOS_SERVICE_ACCOUNT_SCOPES

    def test_the_forbidden_set_is_exactly_the_scope_the_filter_keys_on(self) -> None:
        # One member, matching the platform's `hidden_audit_action_prefixes`.
        assert set(AGENT_FORBIDDEN_SCOPES) == {"chaos:invoke"}

    def test_every_declared_scope_is_one_the_platform_declares(self) -> None:
        # Same protection the --scope flag gets: a typo in a table would mint
        # a plausible-looking principal that 403s at its first call.
        for table in (
            SERVICE_ACCOUNT_SCOPES,
            CHAOS_SERVICE_ACCOUNT_SCOPES,
            SMOKE_SERVICE_ACCOUNT_SCOPES,
        ):
            assert set(table) <= known_scopes(), sorted(set(table) - known_scopes())

    def test_all_three_accounts_are_provisioned(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        platform = _fake_platform(monkeypatch)
        assert main([]) == 0
        capsys.readouterr()
        assert set(platform.created) == {
            SERVICE_ACCOUNT_NAME,
            CHAOS_SERVICE_ACCOUNT_NAME,
            SMOKE_SERVICE_ACCOUNT_NAME,
        }
        assert platform.created[CHAOS_SERVICE_ACCOUNT_NAME] == sorted(CHAOS_SERVICE_ACCOUNT_SCOPES)
        assert "chaos:invoke" not in platform.created[SERVICE_ACCOUNT_NAME]

    def test_both_env_lines_are_printed_under_their_own_labels(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # D-11 on the commander side: one credential pasted into two variables gives a runner
        # that cannot seed.
        _fake_platform(monkeypatch)
        assert main([]) == 0
        out = capsys.readouterr().out
        assert "PLATFORM_TOKEN=sa_plaintext" in out
        assert "PLATFORM_CHAOS_TOKEN=sa_plaintext" in out
        assert "PLATFORM_SMOKE_TOKEN=sa_plaintext" in out

    def test_chaos_invoke_on_the_agent_is_refused_before_anything_is_created(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Refused, not quietly dropped, and refused before the first call.

        Silent narrowing would hand back a token that looks right; minting it reopens the leak.
        """
        platform = _fake_platform(monkeypatch)
        assert main(["--scope", "chaos:invoke"]) == 2
        err = capsys.readouterr().err
        assert "chaos:invoke" in err
        assert CHAOS_SERVICE_ACCOUNT_NAME in err
        assert "PLATFORM_CHAOS_TOKEN" in err
        assert platform.created == {}, "refuse before touching the platform"

    def test_an_existing_agent_account_has_chaos_invoke_stripped(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The case a widening-only bootstrap could never reach.

        The live `incident-commander` account HOLDS chaos:invoke, so "union the defaults in"
        leaves the leak and reports success.
        """
        platform = _FakePlatform()
        platform.existing = {
            SERVICE_ACCOUNT_NAME: [
                "telemetry:read",
                "incidents:read",
                "actions:execute",
                "chaos:invoke",
            ]
        }
        _install_fake(monkeypatch, platform)
        assert main([]) == 0
        out = capsys.readouterr().out
        # The strip and the widening in one PATCH: chaos:invoke off, and the
        # v0.6.13 reporting scope on, because an account predating the pin has
        # neither the right scopes nor a usable token.
        assert platform.patched[SERVICE_ACCOUNT_NAME] == [
            "actions:execute",
            "agent_runs:write",
            "incidents:read",
            "telemetry:read",
        ]
        assert "chaos:invoke" in out, "the removal must be announced, not silent"
        assert "re-paste" in out or "paste the" in out

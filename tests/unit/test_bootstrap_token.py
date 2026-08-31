"""Regression: the promote statement must never carry the email in its SQL text.

`_promote` in scripts/bootstrap_agent_token.py issues a privilege-granting
UPDATE (is_platform_admin=true) via docker exec + psql. Finding C-14: the
email used to be f-string-interpolated into that statement, guarded only by
the _SAFE_EMAIL allowlist — one "helpful" regex widening away from injection.
These tests pin the replacement contract (WO-C6-01): the email reaches psql
solely as a `-v email=...` variable, the statement itself is a constant piped
on stdin with the `:'email'` placeholder, and the regex stays live as a
defense-in-depth backstop that fires before any subprocess is spawned.
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

    The runbook's live-eval protocol is `make demo` then `make bootstrap-token`,
    and for the whole life of that protocol the second command could not
    work: the default named `incident-platform-postgres-1`, a container from
    the *platform's* dev compose, which `make demo` never starts. The bare
    invocation died on a CalledProcessError one line after `make demo`
    reported success.

    CI never caught it because the workflow passes --postgres-container
    explicitly, so the default was exercised by exactly nobody except a human
    following the documented path — the one case that matters for a protocol
    whose whole job is to be followed under time pressure before a paid run.

    Asserting against a literal would just re-pin today's string. These derive
    the name from demo/compose.yml the way Compose itself does, so renaming
    the project or the service reds the test instead of silently re-breaking
    the default.
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

    Nothing here touches a running stack. The demo compose stack is a live
    rehearsal fixture for the coordinator, and a test that registered users
    or widened a service account against it would be editing someone else's
    world — so the entire flow is answered in-process.
    """

    def __init__(self) -> None:
        self.created: dict[str, list[str]] = {}
        self.hosts: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.hosts.append(f"{request.url.scheme}://{request.url.netloc.decode()}")
        path = request.url.path
        if path.endswith("/auth/register"):
            return httpx.Response(201, json={"id": "user-1"})
        if path.endswith("/auth/login"):
            return httpx.Response(200, json={"access_token": "jwt-1"})
        if path.endswith("/admin/service-accounts"):
            body = json.loads(request.content)
            name = str(body["name"])
            self.created[name] = list(body["scopes"])
            return httpx.Response(201, json={"id": f"sa-{len(self.created)}"})
        if path.endswith("/tokens"):
            return httpx.Response(200, json={"plaintext": "sa_plaintext"})
        return httpx.Response(404, json={"detail": f"unrouted {path}"})


def _fake_platform(monkeypatch: pytest.MonkeyPatch) -> _FakePlatform:
    platform = _FakePlatform()
    real_client = httpx.Client

    def _factory(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(platform.handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(bootstrap_agent_token.httpx, "Client", _factory)
    monkeypatch.setattr(bootstrap_agent_token, "_promote", lambda _container, _email: None)
    for var in ("PLATFORM_MCP_URL", "PLATFORM_REST_URL"):
        monkeypatch.delenv(var, raising=False)
    return platform


class TestTheScopeFlagThreePlacesDocument:
    """`--scope chaos:invoke` is printed by three files; it must exist (WO-R2-100).

    ``scripts/chaos_setup.py`` says it twice — module docstring and the
    missing-credential error — ``docs/runbook.md`` prints it as the
    copy-pasteable fix, and ``evals/guards.py`` raises pointing at it. An
    operator whose chaos run had just refused for lack of scope followed
    that instruction under time pressure and got ``unrecognized arguments:
    --scope``. Three documents named one interface and the interface did
    not exist; the cheap correct fix is to build it.
    """

    def test_the_exact_documented_command_succeeds(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        platform = _fake_platform(monkeypatch)
        assert main(["--scope", "chaos:invoke"]) == 0
        capsys.readouterr()
        assert "chaos:invoke" in platform.created[SERVICE_ACCOUNT_NAME]

    def test_the_flag_repeats(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        platform = _fake_platform(monkeypatch)
        assert main(["--scope", "chaos:invoke", "--scope", "actions:execute"]) == 0
        capsys.readouterr()
        granted = platform.created[SERVICE_ACCOUNT_NAME]
        assert {"chaos:invoke", "actions:execute"} <= set(granted)
        assert len(granted) == len(set(granted)), "scopes must not duplicate"

    def test_scope_widens_rather_than_replaces(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The footgun this flag must not become.

        If ``--scope chaos:invoke`` REPLACED the defaults, the documented
        remedy for "chaos refused me" would mint a token that can seed
        chaos and read nothing — and the eval it was minted for would fail
        one step later, on telemetry, for a reason nobody would connect
        back to this command.
        """
        platform = _fake_platform(monkeypatch)
        assert main(["--scope", "chaos:invoke"]) == 0
        capsys.readouterr()
        assert set(SERVICE_ACCOUNT_SCOPES) <= set(platform.created[SERVICE_ACCOUNT_NAME])

    def test_the_smoke_account_stays_read_only(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--scope widens the AGENT principal only. The smoke twin is
        read-only by construction (ADR: "read-only smoke" is structurally
        true, not a property of the scenario list) — a --scope that leaked
        into it would quietly delete that guarantee."""
        platform = _fake_platform(monkeypatch)
        assert main(["--scope", "chaos:invoke"]) == 0
        capsys.readouterr()
        assert platform.created[SMOKE_SERVICE_ACCOUNT_NAME] == SMOKE_SERVICE_ACCOUNT_SCOPES

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
        """Not a hardcoded list: the contract job diffs this snapshot against
        a live platform on every PR, and WO-R2-130 put `required_scope`
        itself under that diff. That is what makes rejecting an unknown
        scope safe rather than merely opinionated."""
        assert known_scopes() == {
            "actions:execute",
            "chaos:invoke",
            "incidents:read",
            "telemetry:read",
        }

    def test_every_scope_the_docs_tell_you_to_pass_is_real(self) -> None:
        """The other half of the three-documents problem: the flag existing
        is not enough if the docs name a scope the platform never declares."""
        documented: set[str] = set()
        for rel in ("scripts/chaos_setup.py", "docs/runbook.md", "evals/guards.py"):
            text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
            documented.update(re.findall(r"--scope\s+([A-Za-z_]+:[A-Za-z_]+)", text))
        assert documented, "layout canary: no --scope example found in the docs"
        assert documented <= known_scopes(), (
            f"docs tell operators to pass scope(s) the platform does not declare: "
            f"{sorted(documented - known_scopes())}"
        )


class TestItNeverPrintsAnOverrideBackWrong:
    """The .env snippet must echo what the operator actually set (WO-R2-100).

    The script hardcoded localhost:8000/8001 and printed PLATFORM_MCP_URL
    in a block headed "Copy into .env". An operator running the platform on
    a non-default port — two stacks side by side, or 8001 already taken —
    pasted that block over their correct value and broke a working config
    by following the instructions.
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

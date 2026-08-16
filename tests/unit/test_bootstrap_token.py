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

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.bootstrap_agent_token import DEFAULT_POSTGRES_CONTAINER, _promote

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

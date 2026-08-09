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
from typing import Any

import pytest

from scripts.bootstrap_agent_token import _promote

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

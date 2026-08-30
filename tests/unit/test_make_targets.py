"""The Makefile targets an operator types, exercised rather than read.

Three defects, one file (WO-R2-89):

* ``eval-reset``'s ``--purge-idempotency`` flag was gated with make's
  ``$(if ...)``, which tests a value for non-emptiness rather than for truth.
  ``PURGE_IDEMPOTENCY=0`` therefore DELETED the idempotency rows, on the one
  flag in this repo whose whole purpose is to destroy data.
* ``chaos-*`` and ``traffic`` never put PLATFORM_MCP_URL/PLATFORM_TOKEN/
  PLATFORM_SMOKE_TOKEN into the child environment, so the seeding step the
  live-eval runbook depends on aborted every time for anyone who kept their
  credentials in ``.env`` — which is what the runbook tells them to do.
* ``eval-smoke`` skipped the trace render when the run failed, which is the
  exact bug that was fixed in ``eval-live`` and never carried across.

These run the real Makefile, not a paraphrase of it. Two mechanisms:

``make -n`` prints the commands a target would run without running them,
which is enough for the flag gate — the flag either appears in the printed
command line or it does not.

For the recipes whose defect is about the *environment* (which ``-n`` cannot
show) and about *ordering under failure*, the Makefile is copied into a tmp
directory next to a fake ``.env`` and a fake ``uv`` on PATH that records what
it was asked to run. Nothing real is invoked: no platform, no docker, no
network, no tokens. The subject is still the shipped Makefile text.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_MAKEFILE: Final[Path] = _REPO_ROOT / "Makefile"

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="make not available")

# Values a test can look for unambiguously in the fake uv's log.
_MCP_URL: Final[str] = "http://make-test.invalid:8001/mcp"
_WRITE_TOKEN: Final[str] = "sa_write_scoped_for_chaos"
_SMOKE_TOKEN: Final[str] = "sa_read_scoped_for_smoke"

# A `uv` that runs nothing: it appends its arguments and the platform
# variables it can see to a log, and reports whatever exit code the test
# asked for on the eval runner line.
_FAKE_UV = """#!/bin/sh
{
  echo "ARGS: $@"
  echo "PLATFORM_MCP_URL=${PLATFORM_MCP_URL-<unset>}"
  echo "PLATFORM_TOKEN=${PLATFORM_TOKEN-<unset>}"
  echo "PLATFORM_SMOKE_TOKEN=${PLATFORM_SMOKE_TOKEN-<unset>}"
} >> "$FAKE_UV_LOG"
case "$@" in
  *evals.runner*) exit "${FAKE_RUNNER_EXIT:-0}" ;;
esac
exit 0
"""


def _scrubbed_env() -> dict[str, str]:
    """The parent environment with every platform credential removed.

    Without this the test could pass on the developer's exported variables
    rather than on the recipe handing them over, which is the whole question.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("PLATFORM_")}
    env.pop("PURGE_IDEMPOTENCY", None)
    return env


def _make_dry_run(*args: str) -> str:
    """``make -n`` against the real Makefile in the repo, as printed."""
    result = subprocess.run(
        ["make", "-n", "-C", str(_REPO_ROOT), *args],
        capture_output=True,
        text=True,
        env=_scrubbed_env(),
        check=False,
    )
    assert result.returncode == 0, f"make -n {args} failed: {result.stderr}"
    return result.stdout


@pytest.fixture
def make_sandbox(tmp_path: Path) -> Path:
    """The shipped Makefile, a fake .env, and a fake ``uv`` on PATH."""
    shutil.copy(_MAKEFILE, tmp_path / "Makefile")
    (tmp_path / ".env").write_text(
        f"PLATFORM_MCP_URL={_MCP_URL}\n"
        f"PLATFORM_TOKEN={_WRITE_TOKEN}\n"
        f"PLATFORM_SMOKE_TOKEN={_SMOKE_TOKEN}\n"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(_FAKE_UV)
    fake_uv.chmod(0o755)
    return tmp_path


def _run_in_sandbox(
    sandbox: Path, target: str, *, runner_exit: str = "0"
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run ``target`` in the sandbox; return the process and the fake uv log."""
    log = sandbox / "uv.log"
    env = _scrubbed_env()
    env["PATH"] = f"{sandbox / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_UV_LOG"] = str(log)
    env["FAKE_RUNNER_EXIT"] = runner_exit
    result = subprocess.run(
        ["make", "-C", str(sandbox), target],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return result, log.read_text() if log.exists() else ""


class TestPurgeIdempotencyGate:
    """Only the literal 1 may delete rows."""

    @pytest.mark.parametrize("value", ["0", "no", "false", "off", "", "2"])
    def test_a_non_one_value_does_not_purge(self, value: str) -> None:
        printed = _make_dry_run("eval-reset", f"PURGE_IDEMPOTENCY={value}")
        assert "--purge-idempotency" not in printed, (
            f"PURGE_IDEMPOTENCY={value!r} still passes --purge-idempotency. make's "
            "$(if ...) tests for a non-empty string, not for the value 1, so every "
            "'off'-looking spelling turned the DB row deletion ON."
        )

    def test_unset_does_not_purge(self) -> None:
        assert "--purge-idempotency" not in _make_dry_run("eval-reset")

    def test_one_does_purge(self) -> None:
        # The other direction, so the fix cannot be "never pass the flag".
        assert "--purge-idempotency" in _make_dry_run("eval-reset", "PURGE_IDEMPOTENCY=1")

    def test_the_gate_is_not_a_non_emptiness_test(self) -> None:
        # The specific construct, named so it cannot come back by accident.
        # Executable lines only: the Makefile quotes the forbidden pattern in
        # a comment, on purpose, so the reason survives next to the fix.
        executable = "\n".join(
            line for line in _MAKEFILE.read_text().splitlines() if not line.strip().startswith("#")
        )
        assert "$(if $(PURGE_IDEMPOTENCY)" not in executable, (
            "the destructive flag is gated on non-emptiness again; compare it to "
            "the literal 1 with ifeq"
        )


class TestPlatformCredentialsReachTheScripts:
    """`make chaos-*` and `make traffic` in a shell with only .env present."""

    def test_chaos_targets_receive_the_write_scoped_credentials(self, make_sandbox: Path) -> None:
        result, log = _run_in_sandbox(make_sandbox, "chaos-kill-consumer")
        assert result.returncode == 0, result.stderr
        assert "chaos_setup.py" in log, "the chaos script was never invoked"
        assert f"PLATFORM_MCP_URL={_MCP_URL}" in log, (
            "chaos_setup.py reads PLATFORM_MCP_URL from os.environ and make's "
            "`-include .env` only sets a MAKE variable, so the documented seeding "
            "step aborts before it does anything"
        )
        assert f"PLATFORM_TOKEN={_WRITE_TOKEN}" in log

    def test_traffic_receives_the_read_scoped_credentials(self, make_sandbox: Path) -> None:
        result, log = _run_in_sandbox(make_sandbox, "traffic")
        assert result.returncode == 0, result.stderr
        assert "traffic_loop.py" in log
        assert f"PLATFORM_MCP_URL={_MCP_URL}" in log
        assert f"PLATFORM_SMOKE_TOKEN={_SMOKE_TOKEN}" in log, (
            "traffic_loop.py refuses --until-lag without PLATFORM_SMOKE_TOKEN, so "
            "`make traffic UNTIL_LAG=N` exits 2 with the recipe as written"
        )

    def test_traffic_never_sees_the_write_scoped_token(self, make_sandbox: Path) -> None:
        # traffic_loop.py is read-scoped by construction. Handing it the full
        # token would undo that for the sake of a convenience.
        _result, log = _run_in_sandbox(make_sandbox, "traffic")
        assert _WRITE_TOKEN not in log

    def test_the_credentials_are_not_exported_to_every_target(self, make_sandbox: Path) -> None:
        # The header's reason for having no blanket `export` still holds: the
        # handover is per-target, not repo-wide.
        _result, log = _run_in_sandbox(make_sandbox, "trace-report")
        assert "format_traces.py" in log
        assert "PLATFORM_TOKEN=<unset>" in log


class TestSmokeRendersTracesWhenTheRunFails:
    """The failing run is the one whose trajectories are worth having."""

    def test_a_failing_smoke_run_still_renders_the_traces(self, make_sandbox: Path) -> None:
        result, log = _run_in_sandbox(make_sandbox, "eval-smoke", runner_exit="1")
        assert "evals.runner" in log, "the smoke runner was never invoked"
        assert "format_traces.py" in log, (
            "a failed smoke run skipped the trace render: make aborts a recipe on "
            "the first non-zero line, and eval-smoke never got eval-live's fix"
        )
        assert result.returncode != 0, "the recipe must still fail with the runner's code"

    def test_a_passing_smoke_run_still_renders_the_traces(self, make_sandbox: Path) -> None:
        result, log = _run_in_sandbox(make_sandbox, "eval-smoke", runner_exit="0")
        assert "format_traces.py" in log
        assert result.returncode == 0

    def test_eval_live_and_eval_smoke_agree(self) -> None:
        # They were the same recipe with one fixed and one not. Neither may
        # lose the render again.
        text = _MAKEFILE.read_text()
        for target in ("eval-live:", "eval-smoke:"):
            recipe = text.split(target, 1)[1].split("\n\n", 1)[0]
            assert "format_traces.py" in recipe
            assert "exit $$code" in recipe, (
                f"{target} does not carry the runner's exit code past the trace "
                "render, so a failing run is either reported as a pass or renders "
                "nothing"
            )

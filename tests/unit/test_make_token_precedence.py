"""Regression: make variable precedence must never decide the principal.

Run 001 stage 1 ran with write scope because `-include .env` (PR #62)
overrode the PLATFORM_TOKEN that `eval-smoke` exported (PR #69), and make
re-exported the FILE's value to the recipe. These tests pin both the
mechanism (so nobody "fixes" .env inclusion into the token path again)
and the structural remedy (the runner selects the principal from config).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import AnyHttpUrl, PostgresDsn, SecretStr

from evals import runner as runner_module
from incident_commander.config import Settings

_REPO = Path(__file__).resolve().parents[2]

# The hermetic PATH handed to the make subprocess below, and — since
# WO-R2-102 — the same one the skip guard searches. They disagreed: the
# guard asked the AMBIENT PATH whether make existed while the child got
# these three directories, so on any machine with make in /opt/homebrew/bin
# or ~/.local/bin the guard passed and the subprocess then raised
# FileNotFoundError. The test errored where it had promised to skip.
_SUBPROCESS_PATH: Final[str] = "/usr/bin:/bin:/usr/local/bin"


@pytest.mark.skipif(
    shutil.which("make", path=_SUBPROCESS_PATH) is None,
    reason=f"make is not on the PATH the subprocess gets ({_SUBPROCESS_PATH})",
)
def test_included_file_beats_recipe_exported_env(tmp_path: Path) -> None:
    # The mechanism itself, reproduced in miniature.
    (tmp_path / ".env").write_text("TOKEN=from_file\n")
    (tmp_path / "Makefile").write_text("-include .env\nprobe:\n\t@echo $$TOKEN\n")
    out = subprocess.run(
        ["make", "probe"],
        cwd=tmp_path,
        env={"PATH": _SUBPROCESS_PATH, "TOKEN": "from_env"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert out == "from_file", (
        "make's -include no longer overrides an environment variable; the "
        "eval-smoke plumbing comment can be revisited if so"
    )


def test_eval_smoke_does_not_pass_the_token_through_make() -> None:
    makefile = (_REPO / "Makefile").read_text()
    recipe = makefile.split("eval-smoke:", 1)[1].split("\n\n", 1)[0]
    # Executable lines only — the recipe carries a comment that names the
    # forbidden pattern precisely so nobody reintroduces it.
    executable = "\n".join(line for line in recipe.splitlines() if not line.strip().startswith("#"))
    assert "PLATFORM_TOKEN=" not in executable, (
        "eval-smoke must not thread the token through make — `-include .env` "
        "overrides it (Run 001 stage-1 bug). Pass --smoke and let the runner "
        "select the principal from Settings."
    )
    assert "--smoke" in executable


def test_runner_smoke_flag_selects_the_smoke_principal() -> None:
    runner = (_REPO / "evals" / "runner.py").read_text()
    assert "platform_smoke_token" in runner
    assert "assert_read_only_principal" in runner
    assert "assert_no_tier1_successes" in runner


def _smoke_settings(smoke_token: SecretStr | None) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        anthropic_api_key=SecretStr("sk-ant-test-not-a-real-key"),
        judge_model="claude-haiku-4-5",
        platform_mcp_url=AnyHttpUrl("http://real.host:8001/mcp"),
        platform_rest_url=AnyHttpUrl("http://real.host:8000"),
        platform_token=SecretStr("sa_full_scope"),
        platform_webhook_secret=SecretStr("whsec_test"),
        database_url=PostgresDsn("postgresql://eval:eval@localhost:5432/eval"),
        platform_smoke_token=smoke_token,
    )


def test_empty_smoke_secret_is_unset_and_refuses_the_stage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """S-04: ``SecretStr("")`` is not a principal — it is a missing one.

    ``if settings.platform_smoke_token is None`` passes for an empty secret,
    so ``mcp_token`` became ``""`` and ``make_client``'s ``token or ...``
    then selected the FULL write principal for every client in the stage,
    the guard client included. The empty string must take the same exit as
    an absent token, not the privileged default.
    """

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the smoke stage must not proceed on an empty token")

    # Every collaborator past the token check is a tripwire: reaching any of
    # them means the empty secret was accepted as a principal. preflight_auth
    # in particular would otherwise be a real network call.
    monkeypatch.setattr(runner_module, "run_all", _boom)
    monkeypatch.setattr(runner_module, "make_client", _boom)
    monkeypatch.setattr(runner_module, "preflight_auth", _boom)
    monkeypatch.setattr(runner_module, "load_scenarios", _boom)
    monkeypatch.setattr(runner_module, "_settings_for_mode", lambda _live: _smoke_settings(None))
    monkeypatch.setattr(sys, "argv", ["evals.runner", "--live", "--smoke"])
    assert runner_module.main() == 3, "sanity: an absent token already exits 3"

    monkeypatch.setattr(
        runner_module, "_settings_for_mode", lambda _live: _smoke_settings(SecretStr(""))
    )
    assert runner_module.main() == 3
    assert "PLATFORM_SMOKE_TOKEN is not set" in capsys.readouterr().out


def test_only_guard_refuses_gate_and_bless_at_parse_time() -> None:
    """A-03: `make eval-reg ONLY=x` / `make baseline ONLY=x` must refuse
    BEFORE the `eval` prerequisite could overwrite latest.json with a
    filtered report. That forces a parse-time conditional that swaps in a
    prerequisite-free $(error) rule — a recipe-line check would fire only
    after the filtered eval already ran (the study/runs.jsonl artifact-loss
    pattern, dressed up as a fix).

    Pinned on the Makefile TEXT so a red implementation can never launch an
    eval run from inside pytest (ADR 0011 freeze). The manual freeze-safe
    probe is `make -n <target> ONLY=x`: guard present → dies at parse time,
    exit 2, no recipe output; guard absent → exit 0 (-n executes nothing
    either way).
    """
    makefile = (_REPO / "Makefile").read_text()
    for target in ("eval-reg", "baseline"):
        assert f"ifdef ONLY\n{target}:\n\t$(error " in makefile, (
            f"the {target} target must be wrapped in a parse-time `ifdef ONLY` "
            "guard whose ONLY-branch rule has NO prerequisites and a $(error) "
            "recipe — a recipe-line echo/exit would run after the eval "
            "prerequisite already overwrote latest.json with a filtered report"
        )
        assert f"else\n{target}: eval" in makefile, (
            f"the unfiltered {target} rule must keep depending on eval in the "
            "else-branch of the ONLY guard"
        )

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
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(shutil.which("make") is None, reason="make not available")
def test_included_file_beats_recipe_exported_env(tmp_path: Path) -> None:
    # The mechanism itself, reproduced in miniature.
    (tmp_path / ".env").write_text("TOKEN=from_file\n")
    (tmp_path / "Makefile").write_text("-include .env\nprobe:\n\t@echo $$TOKEN\n")
    out = subprocess.run(
        ["make", "probe"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "TOKEN": "from_env"},
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

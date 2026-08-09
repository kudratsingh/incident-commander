"""Regression: dependency pinning must actually exist (C-03, ADR 0012).

CLAUDE.md promises "uv-managed, pinned dependencies", but nothing enforced
the mechanism: uv.lock was gitignored and untracked, pyproject.toml carries
only >= floors, and every CI job installed with an unfrozen `uv sync` — so
each fresh clone or CI run resolved the newest allowed versions. These
tests pin the mechanism itself: the lockfile is tracked (not gitignored)
and every CI install refuses to run from anything but the committed lock.
"""

from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]


def test_uv_lock_not_gitignored() -> None:
    gitignore_lines = {line.strip() for line in (_REPO / ".gitignore").read_text().splitlines()}
    assert "uv.lock" not in gitignore_lines, (
        ".gitignore must not ignore uv.lock — the committed lockfile is the "
        "dependency-pinning mechanism (ADR 0012)"
    )


def test_uv_lock_present() -> None:
    assert (_REPO / "uv.lock").is_file(), (
        "uv.lock must exist at the repo root; regenerate with `uv lock` only "
        "as a deliberate upgrade commit (ADR 0012)"
    )


def test_ci_installs_are_locked() -> None:
    workflows = sorted((_REPO / ".github" / "workflows").glob("*.yml"))
    assert workflows, "expected at least one workflow under .github/workflows"
    offenders = [
        f"{workflow.name}:{lineno}: {line.strip()}"
        for workflow in workflows
        for lineno, line in enumerate(workflow.read_text().splitlines(), start=1)
        if "uv sync" in line and "--locked" not in line
    ]
    assert not offenders, (
        "every CI `uv sync` must install from the committed lockfile with "
        "--locked (ADR 0012); unfrozen installs: " + "; ".join(offenders)
    )

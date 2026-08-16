"""A Makefile recipe that runs a script importing ``evals`` must set PYTHONPATH.

``python scripts/x.py`` puts ``scripts/`` on ``sys.path[0]``, not the repo root,
so ``import evals`` raises ``ModuleNotFoundError`` before the script's first
line runs. Every ``make chaos-*`` target was broken this way — all seven, since
whenever ``chaos_setup.py`` grew its ``evals.chaos_hooks`` import — and the
failure is total and immediate:

    $ make chaos-help
    ModuleNotFoundError: No module named 'evals'

pytest does not have the problem (``pyproject.toml`` sets ``pythonpath``), which
is why every test of that script passes while the target a human types does not.
That gap is the reason this is a lint over the Makefile rather than a test of
the script.

Static on purpose: no subprocess, no ``uv run``, no platform. It reads the two
files and compares them.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_MAKEFILE: Final[Path] = _REPO_ROOT / "Makefile"
_SCRIPTS_DIR: Final[Path] = _REPO_ROOT / "scripts"

# `from evals...` / `import evals...` at the start of a line.
_IMPORTS_EVALS: Final[re.Pattern[str]] = re.compile(r"^(?:from|import)\s+evals\b", re.MULTILINE)
# A recipe line invoking a script under scripts/, capturing what precedes it.
_INVOCATION: Final[re.Pattern[str]] = re.compile(
    r"^\t(?P<prefix>.*?)python\s+(?P<script>scripts/\S+\.py)"
)


def _scripts_importing_evals() -> set[str]:
    return {
        f"scripts/{path.name}"
        for path in sorted(_SCRIPTS_DIR.glob("*.py"))
        if _IMPORTS_EVALS.search(path.read_text())
    }


def _recipe_invocations() -> list[tuple[int, str, str]]:
    """``(line number, script path, everything before `python`)`` per recipe line."""
    found: list[tuple[int, str, str]] = []
    for number, line in enumerate(_MAKEFILE.read_text().splitlines(), start=1):
        match = _INVOCATION.match(line)
        if match:
            found.append((number, match.group("script"), match.group("prefix")))
    return found


class TestMakeRecipesCanImportEvals:
    def test_every_recipe_running_an_evals_importing_script_sets_pythonpath(self) -> None:
        needs_path = _scripts_importing_evals()
        offenders = [
            f"Makefile:{number} runs {script} without PYTHONPATH"
            for number, script, prefix in _recipe_invocations()
            if script in needs_path and "PYTHONPATH" not in prefix
        ]
        assert offenders == [], (
            "these targets die on `ModuleNotFoundError: No module named 'evals'` before "
            f"running a line: {offenders}. `python scripts/x.py` puts scripts/ on "
            "sys.path[0], not the repo root. Prefix the recipe with `PYTHONPATH=.` "
            "(pytest does not need it, which is why the tests pass and the target does not)."
        )

    def test_the_lint_has_a_subject(self) -> None:
        # If no script imports evals any more, this file is dead weight and
        # should be deleted rather than left passing vacuously.
        assert _scripts_importing_evals(), "no script imports evals — remove this lint"

    def test_the_chaos_targets_are_covered(self) -> None:
        # The specific targets that were broken. Named so a future edit that
        # drops PYTHONPATH from them fails here with the reason attached.
        chaos_lines = [
            (number, prefix)
            for number, script, prefix in _recipe_invocations()
            if script == "scripts/chaos_setup.py"
        ]
        assert len(chaos_lines) >= 7, f"expected the make chaos-* family, found {len(chaos_lines)}"
        assert all("PYTHONPATH" in prefix for _, prefix in chaos_lines)

    def test_recipes_for_scripts_that_do_not_import_evals_are_left_alone(self) -> None:
        # The rule is targeted, not blanket: a script with no evals import does
        # not need the repo root on its path, and requiring it everywhere would
        # make the lint noise rather than signal.
        needs_path = _scripts_importing_evals()
        others = {script for _, script, _ in _recipe_invocations()} - needs_path
        assert others, "every invoked script imports evals — the exclusion is untested"

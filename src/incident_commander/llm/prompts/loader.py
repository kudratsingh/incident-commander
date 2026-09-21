"""Read prompts from ``src/incident_commander/llm/prompts/<name>.md``.

A file may write ``{{rule:<key>}}`` for a rule from ``shared_rules.py``; expanding it here
means no caller opts in or out (ADR 0054, INC-002). ``test_prompts_snapshot.py`` gates drift.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from incident_commander.llm.prompts.shared_rules import render

_PROMPTS_DIR = Path(__file__).resolve().parent


class PromptNotFoundError(RuntimeError):
    pass


def raw_prompt(name: str) -> str:
    """``prompts/<name>.md`` exactly as authored, shared rules unexpanded.

    For tests that assert a prompt file *delegates* the shared rule; not for a role.
    """
    path = _PROMPTS_DIR / f"{name}.md"
    if not path.is_file():
        raise PromptNotFoundError(f"prompt file not found: {path}")
    return path.read_text().rstrip() + "\n"


@lru_cache(maxsize=32)
def load_prompt(name: str) -> str:
    """Read ``prompts/<name>.md``, expand its shared rules. Cached per name."""
    return render(raw_prompt(name))


def available_prompts() -> tuple[str, ...]:
    """Every name ``load_prompt`` can serve: file *stems*, sorted, uncached so it cannot stale.

    Lets ``tests/unit/test_prompts_snapshot.py`` walk the directory, not a hand-kept dict.
    """
    return tuple(sorted(path.stem for path in _PROMPTS_DIR.glob("*.md")))

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
    """``prompts/<name>.md`` exactly as authored, with ``{{rule:...}}`` left in place.

    For tests that check a prompt file delegates to a shared rule. Never use it to build a
    real prompt: the placeholder would reach the model as literal text.
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
    """Every name ``load_prompt`` can serve: the ``.md`` file names without the extension, sorted.

    Read from disk on every call, never cached, so a prompt file added during a test run shows
    up: ``tests/unit/test_prompts_snapshot.py`` walks this instead of a hand-kept list.
    """
    return tuple(sorted(path.stem for path in _PROMPTS_DIR.glob("*.md")))

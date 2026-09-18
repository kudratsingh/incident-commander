"""Read prompts from ``src/incident_commander/llm/prompts/<name>.md``.

Returns the file content with every shared rule expanded; ``tests/unit/test_prompts_snapshot.py``
gates drift. A file may write ``{{rule:<key>}}`` for a rule from ``shared_rules.py``, and
expanding it here means no caller opts in or out (ADR 0054, INC-002). ``raw_prompt`` is the
unexpanded file, for tests that check a ``*.md`` carries the placeholder, not a copy.
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

    Not for a role. It lets a test assert a prompt file *delegates* the shared
    rule instead of restating it.
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
    """Every name ``load_prompt`` can serve, sorted — the directory, enumerated.

    So ``tests/unit/test_prompts_snapshot.py`` walks the directory instead of its own
    hand-maintained dict. Returns file *stems*, and is uncached so it cannot go stale.
    """
    return tuple(sorted(path.stem for path in _PROMPTS_DIR.glob("*.md")))

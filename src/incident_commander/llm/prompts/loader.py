"""Read prompts from ``src/incident_commander/llm/prompts/<name>.md``.

Prompts are markdown files. Loader returns raw file content; snapshot tests
under ``tests/unit/test_prompts_snapshot.py`` gate any accidental drift.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent


class PromptNotFoundError(RuntimeError):
    pass


@lru_cache(maxsize=32)
def load_prompt(name: str) -> str:
    """Read ``prompts/<name>.md`` and return its content. Cached per name."""
    path = _PROMPTS_DIR / f"{name}.md"
    if not path.is_file():
        raise PromptNotFoundError(f"prompt file not found: {path}")
    return path.read_text().rstrip() + "\n"

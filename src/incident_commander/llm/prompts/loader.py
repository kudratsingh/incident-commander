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


def available_prompts() -> tuple[str, ...]:
    """Every name ``load_prompt`` can serve, sorted — the directory, enumerated.

    This exists so the snapshot suite can walk the prompt directory instead
    of its own table of expectations. ``tests/unit/test_prompts_snapshot.py``
    pinned a sha256 per prompt in a hand-maintained dict and then
    parametrized over *that dict*, so it checked exactly the prompts someone
    had already remembered to add: a new ``prompts/*.md`` shipped with no
    snapshot, no structural invariant test, and nothing anywhere to notice
    the gap. A load-bearing string could then change with no hash moving in
    any PR diff, which is the entire protection the snapshot suite claims to
    provide (CLAUDE.md: prompts live in versioned files with snapshot tests).

    Returns file *stems* — the exact strings ``load_prompt`` accepts — rather
    than paths, so no caller has to reconstruct the ``<name>.md`` convention
    for itself. Deliberately uncached, unlike ``load_prompt``: the answer is
    a directory listing, and a stale one would restore the very blind spot
    this function removes.
    """
    return tuple(sorted(path.stem for path in _PROMPTS_DIR.glob("*.md")))

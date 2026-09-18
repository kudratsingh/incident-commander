"""Read prompts from ``src/incident_commander/llm/prompts/<name>.md``.

Prompts are markdown files. Loader returns the file content with every shared
rule expanded; snapshot tests under ``tests/unit/test_prompts_snapshot.py``
gate any accidental drift.

**One rule, several readers.** A file may write ``{{rule:<key>}}`` where a rule
from ``shared_rules.py`` belongs, and ``load_prompt`` expands it while serving
the file. That is the whole mechanism behind "the planner, the routing table
and the judge are given the identical sentence in the same change" (ADR 0054,
INC-002): because the expansion happens here, no caller opts in and none can
opt out — ``investigation.py``, ``remediation.py``, the best-of-N strategies
and ``evals/graders/llm_judge.py`` each keep the one ``load_prompt`` call they
already had. ``raw_prompt`` is the unexpanded file, for the tests that have to
check a ``*.md`` carries the placeholder rather than a hand-typed copy of the
rule.
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

    Uncached and not what a role should be given: the placeholder is not a
    rule. It exists so a test can assert that a prompt file *delegates* the
    shared rule instead of restating it, which is the only way to catch the
    failure this indirection exists to prevent — a hand-typed copy that reads
    the same today and drifts tomorrow.
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

"""Doc-drift tripwires: documented env vars (B-03) and documented paths (B-13).

docs/safety-model.md once documented an ``AGENT_ENABLED`` kill switch that no
code read — ``Settings`` has ``extra="ignore"``, so an operator following the
runbook set a no-op variable during a live incident. This test makes that
class of drift fail CI: every env-var-shaped token the operator docs mention
must either be a real ``Settings`` field or be explicitly allowlisted as a
known non-Settings variable.

The same class of drift bit the path vocabulary. CLAUDE.md's repository layout
described ``src/agent/``, ``src/tools/`` and ``src/agent/prompts/`` — a tree
that never shipped (the package is ``src/incident_commander/``). That was not
cosmetic: ``.github/workflows/evals.yml``'s path filter was written against the
documented vocabulary, so the regression gate never fired on a prompt change
(A-08). ``test_claude_md_layout_paths_exist`` and
``test_claude_md_names_no_pre_package_src_paths`` make that drift fail CI too.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

from incident_commander.config import Settings

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_CLAUDE_MD: Final[Path] = _REPO_ROOT / "CLAUDE.md"
_DOCS: Final[tuple[Path, ...]] = (
    _REPO_ROOT / "docs" / "safety-model.md",
    _REPO_ROOT / "docs" / "runbook.md",
)

# SCREAMING_SNAKE with at least one underscore: matches `AGENT_ENABLED` and
# the token inside `AGENT_ENABLED=false`, not single words like `READ`.
_SCREAMING_SNAKE: Final[re.Pattern[str]] = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_FENCED_BLOCK: Final[re.Pattern[str]] = re.compile(r"```.*?```", re.DOTALL)
_INLINE_SPAN: Final[re.Pattern[str]] = re.compile(r"`([^`\n]+)`")

# Exact names only — no substring matching. Every entry is a token the docs
# legitimately mention that is NOT (and must not become) a Settings field.
_NON_SETTINGS_TOKENS: Final[frozenset[str]] = frozenset(
    {
        # Code symbols named in the docs, not environment variables.
        "ALLOWED_TRANSITIONS",  # transition graph, agent/orchestrator.py
        "RESOURCE_ARG_FIELDS",  # per-tool resource-naming fields, tools/policies.py
        "ALERT_SUBJECT_PROBES",  # alert field -> subject probe, agent/investigation.py
        "TIER_1",  # Tier enum values, tools/policies.py
        "TIER_2",
        # Environment variables / make flags consumed outside Settings.
        "CHAOS_ENABLED",  # platform-side chaos gate (demo/compose.yml)
        "EVAL_TRACE_DIR",  # eval runner trace destination (evals/runner.py)
        "PLATFORM_COMPOSE",  # read by `make eval-reset`, not by the agent
        "PLATFORM_SERVICE",  # the compose service `make eval-reset` execs into
        "PURGE_IDEMPOTENCY",  # `make eval-reset` opt-in purge flag
        "SMOKE_ONLY",  # `make eval-smoke` scenario-list override
        "UNTIL_LAG",  # `make traffic` stop-at-this-backlog flag (scripts/traffic_loop.py)
        # Settings on the PLATFORM, quoted in docs/safety-model.md "Rate
        # limits" (platform #169). Named here because the ceilings the agent
        # has to live under are the platform's, and a doc that states them
        # without naming the knob cannot be checked against the platform.
        "MCP_RATE_LIMIT_PER_PRINCIPAL",
        "MCP_RATE_LIMIT_WINDOW_SECONDS",
        "ADMIN_NL_QUERY_RATE_LIMIT",
        "ADMIN_DIGEST_RATE_LIMIT",
    }
)


def _documented_tokens(doc: Path) -> frozenset[str]:
    """Env-var-shaped tokens in ``doc``'s fenced blocks and inline code spans."""
    text = doc.read_text(encoding="utf-8")
    spans: list[str] = _FENCED_BLOCK.findall(text)
    spans.extend(_INLINE_SPAN.findall(_FENCED_BLOCK.sub("", text)))
    return frozenset(token for span in spans for token in _SCREAMING_SNAKE.findall(span))


@pytest.mark.parametrize("doc", _DOCS, ids=lambda doc: str(doc.name))
def test_documented_env_vars_exist_in_settings(doc: Path) -> None:
    unknown = sorted(
        token
        for token in _documented_tokens(doc)
        if token not in _NON_SETTINGS_TOKENS and token.lower() not in Settings.model_fields
    )
    assert unknown == [], (
        f"{doc.name} documents env-var-shaped tokens with no Settings field: {unknown}. "
        "Either add the field to src/incident_commander/config.py or add the token to "
        "_NON_SETTINGS_TOKENS with a comment saying what actually consumes it."
    )


def test_allowlist_stays_minimal() -> None:
    """Allowlist hygiene: entries must not shadow real fields or go stale."""
    shadowing = sorted(t for t in _NON_SETTINGS_TOKENS if t.lower() in Settings.model_fields)
    assert shadowing == [], f"allowlisted tokens are now Settings fields — remove: {shadowing}"
    documented = {token for doc in _DOCS for token in _documented_tokens(doc)}
    stale = sorted(_NON_SETTINGS_TOKENS - documented)
    assert stale == [], f"allowlisted tokens no longer appear in any scanned doc: {stale}"


# --- CLAUDE.md path drift (B-13; the vocabulary that produced A-08) ----------

_LAYOUT_HEADING: Final[str] = "## Repository layout"
_LAYOUT_FENCE: Final[re.Pattern[str]] = re.compile(r"```text\n(.*?)^```", re.DOTALL | re.MULTILINE)
# One tree row: 4-char indent units, a connector, then the entry text.
_TREE_ROW: Final[re.Pattern[str]] = re.compile(r"^((?:│   |    )*)(?:├── |└── )(.+)$")
_PLANNED: Final[str] = "(planned"
# Pre-package layout vocabulary: `src/<anything but the real package>/`.
_STALE_SRC_PATH: Final[re.Pattern[str]] = re.compile(
    r"src/(?!incident_commander/)[A-Za-z_][A-Za-z0-9_]*/"
)


def _layout_rows() -> list[str]:
    """The fenced tree under CLAUDE.md's '## Repository layout' heading."""
    text = _CLAUDE_MD.read_text(encoding="utf-8")
    heading = text.split(_LAYOUT_HEADING, 1)
    assert len(heading) == 2, f"CLAUDE.md has no '{_LAYOUT_HEADING}' heading"
    fence = _LAYOUT_FENCE.search(heading[1])
    assert fence is not None, "the Repository layout section has no ```text fence"
    return fence.group(1).splitlines()


def _declared_paths() -> tuple[list[str], list[str]]:
    """Repo-relative paths in the layout tree, split into (real, planned)."""
    parents: list[str] = []
    real: list[str] = []
    planned: list[str] = []
    for row in _layout_rows():
        match = _TREE_ROW.match(row)
        if match is None:
            continue  # the root line, blank lines, wrapped comments
        depth = len(match.group(1)) // 4
        entry = match.group(2)
        names = [name.strip() for name in entry.split("#", 1)[0].split(",") if name.strip()]
        if not names:
            continue
        if len(names) == 1 and names[0].endswith("/"):
            parents = [*parents[:depth], names[0].rstrip("/")]
        prefix = "/".join(parents[:depth])
        bucket = planned if _PLANNED in entry else real
        for name in names:
            leaf = name.rstrip("/")
            bucket.append(f"{prefix}/{leaf}" if prefix else leaf)
    return real, planned


def test_claude_md_layout_paths_exist() -> None:
    """Every unannotated path in the layout tree is a path that exists."""
    real, planned = _declared_paths()
    assert len(real) >= 30, f"layout parser found only {len(real)} paths — the tree format changed"
    missing = sorted(path for path in real if not (_REPO_ROOT / path).exists())
    assert missing == [], (
        f"CLAUDE.md's repository layout names paths that do not exist: {missing}. "
        f"Fix the tree, or annotate the entry '{_PLANNED} — Phase N)' if it is roadmap."
    )
    shipped = sorted(path for path in planned if (_REPO_ROOT / path).exists())
    assert shipped == [], (
        f"CLAUDE.md's repository layout still marks shipped paths as planned: {shipped}. "
        "Drop the annotation and describe what actually landed."
    )


def test_claude_md_names_no_pre_package_src_paths() -> None:
    """No prose or table row may resurrect the never-shipped src/<top-level>/ tree."""
    text = _CLAUDE_MD.read_text(encoding="utf-8")
    stale = sorted({line.strip() for line in text.splitlines() if _STALE_SRC_PATH.search(line)})
    assert stale == [], (
        "CLAUDE.md names pre-package source paths; the tree is src/incident_commander/<pkg>/. "
        "This vocabulary is what evals.yml's path filter was written against (A-08).\n"
        + "\n".join(stale)
    )

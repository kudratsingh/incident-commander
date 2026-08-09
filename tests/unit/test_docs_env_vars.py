"""Doc-drift tripwire for documented environment variables (finding B-03).

docs/safety-model.md once documented an ``AGENT_ENABLED`` kill switch that no
code read — ``Settings`` has ``extra="ignore"``, so an operator following the
runbook set a no-op variable during a live incident. This test makes that
class of drift fail CI: every env-var-shaped token the operator docs mention
must either be a real ``Settings`` field or be explicitly allowlisted as a
known non-Settings variable.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

from incident_commander.config import Settings

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
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
        "TIER_1",  # Tier enum values, tools/policies.py
        "TIER_2",
        # Environment variables / make flags consumed outside Settings.
        "CHAOS_ENABLED",  # platform-side chaos gate (demo/compose.yml)
        "EVAL_TRACE_DIR",  # eval runner trace destination (evals/runner.py)
        "PLATFORM_COMPOSE",  # read by `make eval-reset`, not by the agent
        "PURGE_IDEMPOTENCY",  # `make eval-reset` opt-in purge flag
        "SMOKE_ONLY",  # `make eval-smoke` scenario-list override
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

"""Regression: every GitHub Action must be pinned by full commit SHA (F2-14).

Both workflows pinned upstream actions by mutable major-version tag
(actions/checkout@v4, astral-sh/setup-uv@v3). A mutable tag can be retargeted
by a compromised upstream to point at malicious code, poisoning every CI job
that runs it — including the required checks branch protection trusts. These
tests parse every workflow and require each ``uses:`` reference to name a
full 40-hex-char commit SHA (the human-readable version stays in a trailing
comment in the file), so a tag-pinned action cannot be reintroduced silently.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[2]
_WORKFLOWS_DIR = _REPO / ".github" / "workflows"
_SHA_PINNED = re.compile(r"^[^@]+@[0-9a-f]{40}(\s*#.*)?$")


def _workflow_files() -> list[Path]:
    return sorted(p for ext in ("*.yml", "*.yaml") for p in _WORKFLOWS_DIR.glob(ext))


def _collect_uses(node: object) -> list[str]:
    """Every ``uses:`` value in a parsed workflow document, in document order."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "uses" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_collect_uses(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_collect_uses(item))
    return found


def test_collector_sees_the_current_actions() -> None:
    # Canary against a vacuous pass: both workflows install actions today,
    # so an empty collection means the walker (or the layout) broke.
    workflows = _workflow_files()
    assert workflows, f"layout canary: no workflows under {_WORKFLOWS_DIR}"
    collected: list[str] = []
    for workflow in workflows:
        document: object = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        collected.extend(_collect_uses(document))
    assert collected, "collector canary: no `uses:` references found in any workflow"


def test_every_action_is_sha_pinned() -> None:
    offenders: list[str] = []
    for workflow in _workflow_files():
        document: object = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        offenders.extend(
            f"{workflow.name}: uses: {ref}"
            for ref in _collect_uses(document)
            if _SHA_PINNED.fullmatch(ref) is None
        )
    assert not offenders, (
        "every GitHub Action must be pinned by full 40-char commit SHA, not "
        "a mutable tag (F2-14): " + "; ".join(offenders)
    )

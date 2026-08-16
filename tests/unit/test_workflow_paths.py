"""Regression: the eval gate's path filter must cover the real layout (A-08, S-16).

evals.yml's pull_request path filter was written against CLAUDE.md's
documented-but-never-shipped layout (src/agent/prompts/): it listed
src/incident_commander/agent/** and tools/** but not
src/incident_commander/llm/** — where all five prompts actually live — nor
src/incident_commander/config.py (agent_model/judge_model pins), nor
contracts/platform-tools.snapshot.json (planner-facing tool descriptions,
loaded at import by the registry). Prompt, model-pin, and snapshot-rebless
PRs therefore merged without ever triggering the eval regression gate.

These tests parse the workflow and mechanically prove the filter covers the
prompt loader's directory and the other planner-behavior surfaces, so the
filter cannot silently drift from the layout again. Deriving the prompt list
from the real prompts directory means a future prompt move breaks the test —
which is the point.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO / ".github" / "workflows" / "evals.yml"
_PROMPTS_DIR = _REPO / "src" / "incident_commander" / "llm" / "prompts"
_CI = _REPO / ".github" / "workflows" / "ci.yml"


def _github_match(pattern: str, path: str) -> bool:
    """True if a GitHub Actions path-filter pattern matches a repo-relative path.

    GitHub semantics: ``**`` matches any characters including ``/``, while
    ``*`` matches any characters except ``/``. fnmatch is wrong here — its
    ``*`` crosses ``/``, so an fnmatch-based check would pass vacuously.
    """
    escaped = re.escape(pattern)
    regex = escaped.replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
    return re.fullmatch(regex, path) is not None


def _gate_paths() -> list[str]:
    """The pull_request path filter of evals.yml, as a list of patterns."""
    workflow: object = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    # YAML 1.1 parses the workflow's unquoted `on:` key as boolean True, so
    # the trigger table normally lives under True; accept both spellings.
    triggers: object = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict), "evals.yml has no `on:` trigger table"
    pull_request: object = triggers.get("pull_request")
    assert isinstance(pull_request, dict), "evals.yml has no pull_request trigger"
    raw_paths: object = pull_request.get("paths")
    assert isinstance(raw_paths, list), "evals.yml pull_request has no paths filter"
    paths: list[str] = []
    for entry in raw_paths:
        assert isinstance(entry, str)
        paths.append(entry)
    return paths


def _covered(path: str) -> bool:
    return any(_github_match(pattern, path) for pattern in _gate_paths())


def test_matcher_mirrors_github_semantics() -> None:
    # Self-check of the matcher: with fnmatch semantics (`*` crossing `/`)
    # the coverage assertions below could pass vacuously.
    assert _github_match("src/incident_commander/llm/**", "src/incident_commander/llm/prompts/x.md")
    assert not _github_match("src/*", "src/incident_commander/config.py")
    assert not _github_match("evals/runner.py", "evals/runner_py")


def test_gate_covers_every_prompt_file() -> None:
    prompts = sorted(_PROMPTS_DIR.glob("*.md"))
    assert prompts, f"layout canary: no prompt files under {_PROMPTS_DIR}"
    for prompt in prompts:
        rel = prompt.relative_to(_REPO).as_posix()
        assert _covered(rel), (
            f"evals.yml path filter does not cover prompt {rel} — behavior-"
            "changing prompt edits would merge without the eval gate (A-08)"
        )


def test_gate_covers_model_config() -> None:
    assert _covered("src/incident_commander/config.py"), (
        "evals.yml path filter does not cover config.py, where the "
        "agent_model/judge_model pins live (A-08)"
    )


def test_gate_covers_contract_snapshot() -> None:
    assert _covered("contracts/platform-tools.snapshot.json"), (
        "evals.yml path filter does not cover the tool-description snapshot "
        "the planner reads at import (S-16)"
    )


def test_gate_covers_runner() -> None:
    assert _covered("evals/runner.py"), (
        "evals.yml path filter no longer covers the eval harness itself"
    )


class TestContractJobRetriesItsFlakySteps:
    """The contract job gates both platform checks, so its flakes lie.

    It boots a stack and mints a token before running either the schema diff
    or the fixture-drift check. Two of its steps failed for unrelated
    environmental reasons on 2026-08-16 — a registry 502 pulling postgres,
    and a 404 minting a service-account token that passed unchanged on
    re-run — and both presented as a platform-contract failure. A gate that
    cries wolf gets ignored exactly when it is right.
    """

    @staticmethod
    def _contract_steps() -> list[dict[str, object]]:
        workflow = yaml.safe_load(_CI.read_text())
        steps = workflow["jobs"]["contract"]["steps"]
        assert isinstance(steps, list)
        return steps

    @staticmethod
    def _step(name_fragment: str) -> str:
        for step in TestContractJobRetriesItsFlakySteps._contract_steps():
            if name_fragment in str(step.get("name", "")):
                return str(step.get("run", ""))
        raise AssertionError(f"no contract step named like {name_fragment!r}")

    def test_the_image_pull_retries(self) -> None:
        run = self._step("Boot the pinned demo stack")
        assert "pull" in run, (
            "the pull is implicit in `up`, so a registry blip reads as a boot failure"
        )
        assert "attempt" in run, "no retry around the image pull"

    def test_the_token_bootstrap_retries(self) -> None:
        run = self._step("Mint a platform service-account token")
        assert "attempt" in run, "no retry around the five-step bootstrap"

    def test_both_still_fail_hard_after_their_retries(self) -> None:
        # Retrying must not become swallowing: a genuine failure has to end
        # the job, or the drift check runs against a stack that never booted.
        for fragment in ("Boot the pinned demo stack", "Mint a platform service-account token"):
            assert "exit 1" in self._step(fragment), (
                f"{fragment}: retries with no terminal failure would hide a real break"
            )

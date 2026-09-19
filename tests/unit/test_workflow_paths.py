"""Regression: the eval gate's path filter must cover the real layout (A-08, S-16).

evals.yml's filter was written against a documented-but-never-shipped layout: it listed
``agent/**`` and ``tools/**`` but not ``llm/**`` (where all five prompts live), nor
``config.py`` (model pins), nor the contract snapshot — so prompt, model-pin and rebless
PRs merged without triggering the gate. The prompt list is derived from the real directory.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO / ".github" / "workflows" / "evals.yml"
_PROMPTS_DIR = _REPO / "src" / "incident_commander" / "llm" / "prompts"
_CI = _REPO / ".github" / "workflows" / "ci.yml"


def _translate(pattern: str) -> str:
    """Compile one GitHub filter pattern to a regex, per GitHub's cheat sheet.

    ``**`` crosses ``/``, ``*`` does not, ``?`` and ``+`` are QUANTIFIERS, ``[]`` is a class.
    The old version made ``!`` literal, so an exclusion failed OPEN.
    """
    out: list[str] = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**", i):
            out.append("(?:.*)")
            i += 2
        elif char == "*":
            out.append("(?:[^/]*)")
            i += 1
        elif char in "?+":
            # A quantifier with nothing to quantify is not valid GitHub
            # syntax; treat it as a literal rather than emitting bad regex.
            out.append(char if out else re.escape(char))
            i += 1
        elif char == "[":
            close = pattern.find("]", i + 1)
            if close == -1:
                out.append(re.escape(char))
                i += 1
            else:
                out.append(f"[{pattern[i + 1 : close]}]")
                i = close + 1
        else:
            out.append(re.escape(char))
            i += 1
    return "".join(out)


def _github_match(pattern: str, path: str) -> bool:
    """True if a GitHub Actions path-filter pattern matches a repo-relative path.

    Positive patterns only: a leading ``!`` is the caller's business.
    """
    assert not pattern.startswith("!"), (
        f"{pattern!r} is an exclusion; polarity belongs to _covered, not the matcher"
    )
    return re.fullmatch(_translate(pattern), path) is not None


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


def _covered(path: str, patterns: list[str] | None = None) -> bool:
    """Whether the eval gate fires for ``path``, honouring exclusions.

    GitHub evaluates in order and the LAST match wins, so a literal ``!`` left the earlier
    positive standing and reported an excluded path as gated.
    """
    covered = False
    for pattern in patterns if patterns is not None else _gate_paths():
        if pattern.startswith("!"):
            if _github_match(pattern[1:], path):
                covered = False
        elif _github_match(pattern, path):
            covered = True
    return covered


def test_matcher_mirrors_github_semantics() -> None:
    # Self-check of the matcher: with fnmatch semantics (`*` crossing `/`)
    # the coverage assertions below could pass vacuously.
    assert _github_match("src/incident_commander/llm/**", "src/incident_commander/llm/prompts/x.md")
    assert not _github_match("src/*", "src/incident_commander/config.py")
    assert not _github_match("evals/runner.py", "evals/runner_py")


class TestTheMatcherUnderstandsTheWholeFilterSyntax:
    """The coverage tests are only as honest as the matcher (WO-R2-101).

    ``_covered`` knew two of GitHub's six filter constructs and read the other four as
    literal text. Exclusions are the dangerous direction: they fail OPEN.
    """

    def test_an_exclusion_removes_a_path_an_earlier_positive_matched(self) -> None:
        patterns = ["evals/**", "!evals/reports/**"]
        assert _covered("evals/runner.py", patterns)
        assert not _covered("evals/reports/baseline.json", patterns), (
            "an exclusion in evals.yml was ignored — _covered called an "
            "EXCLUDED path gated, which is how an ungated PR merges"
        )

    def test_a_later_positive_re_adds_an_excluded_path(self) -> None:
        """GitHub resolves the list in order, last match wins."""
        patterns = ["evals/**", "!evals/reports/**", "evals/reports/baseline.json"]
        assert _covered("evals/reports/baseline.json", patterns)

    def test_question_mark_and_plus_are_quantifiers_not_wildcards(self) -> None:
        # GitHub's cheat sheet: `?` is "zero or one of the preceding
        # character" — fnmatch's single-char wildcard reading is wrong.
        assert _github_match("evals/runner.pyc?", "evals/runner.py")
        assert _github_match("evals/runner.pyc?", "evals/runner.pyc")
        assert not _github_match("evals/runner.pyc?", "evals/runner.pyX")
        assert _github_match("evals/re+ports/x.json", "evals/reeports/x.json")

    def test_character_classes_are_classes(self) -> None:
        assert _github_match("evals/scenario[0-9].yaml", "evals/scenario3.yaml")
        assert not _github_match("evals/scenario[0-9].yaml", "evals/scenarioX.yaml")

    def test_a_dot_is_still_literal(self) -> None:
        # The regression the old escape-everything approach got right, and
        # which the new translation must not lose.
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

    It boots a stack and mints a token first. A registry 502 and a 404 minting a token
    (2026-08-16) both presented as a platform-contract failure.
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

    @staticmethod
    def _commands(name_fragment: str) -> str:
        """The step's run block with its comment lines stripped.

        Every step carries a long comment naming what it does, so ``assert "pull" in run`` was
        satisfied by "# Pull first, with retries" (WO-R2-101).
        """
        run = TestContractJobRetriesItsFlakySteps._step(name_fragment)
        return "\n".join(line for line in run.splitlines() if not line.lstrip().startswith("#"))

    def test_the_image_pull_retries(self) -> None:
        commands = self._commands("Boot the pinned demo stack")
        assert re.search(r"docker\s+compose\b[^\n]*\bpull\b", commands), (
            "the pull is implicit in `up`, so a registry blip reads as a boot failure"
        )
        assert "attempt" in commands, "no retry around the image pull"

    def test_the_token_bootstrap_retries(self) -> None:
        commands = self._commands("Mint a platform service-account token")
        assert "attempt" in commands, "no retry around the five-step bootstrap"

    def test_both_still_fail_hard_after_their_retries(self) -> None:
        # Retrying must not become swallowing: a genuine failure has to end
        # the job, or the drift check runs against a stack that never booted.
        for fragment in ("Boot the pinned demo stack", "Mint a platform service-account token"):
            assert "exit 1" in self._commands(fragment), (
                f"{fragment}: retries with no terminal failure would hide a real break"
            )


class TestEveryJobIsBoundedAndLeastPrivileged:
    """Both workflows must cap their runtime and their token (WO-R2-101).

    The `contract` job waits on healthchecks, a seeder and a REST app; with no
    `timeout-minutes` a hang runs to GitHub's six-hour default, holding a concurrency slot.
    Neither workflow declared `permissions`, and nothing here writes to the repo.
    """

    _WORKFLOWS = ("ci.yml", "evals.yml")

    @staticmethod
    def _load(name: str) -> dict[str, Any]:
        loaded: dict[str, Any] = yaml.safe_load(
            (_REPO / ".github" / "workflows" / name).read_text(encoding="utf-8")
        )
        return loaded

    @pytest.mark.parametrize("workflow", _WORKFLOWS)
    def test_every_job_has_a_timeout(self, workflow: str) -> None:
        jobs = self._load(workflow)["jobs"]
        unbounded = [name for name, job in jobs.items() if "timeout-minutes" not in job]
        assert unbounded == [], (
            f"{workflow}: {unbounded} have no timeout-minutes, so a hang burns "
            "GitHub's 6-hour default while holding the concurrency slot"
        )

    @pytest.mark.parametrize("workflow", _WORKFLOWS)
    def test_the_timeouts_are_actually_tight(self, workflow: str) -> None:
        """A timeout is only a timeout if it is shorter than the default."""
        jobs = self._load(workflow)["jobs"]
        for name, job in jobs.items():
            minutes = job["timeout-minutes"]
            assert isinstance(minutes, int)
            assert 0 < minutes <= 60, f"{workflow}:{name} timeout-minutes={minutes} is not a bound"

    @pytest.mark.parametrize("workflow", _WORKFLOWS)
    def test_the_workflow_declares_least_privilege(self, workflow: str) -> None:
        declared = self._load(workflow).get("permissions")
        assert declared is not None, (
            f"{workflow} declares no top-level permissions, so every job runs "
            "with the repository's default GITHUB_TOKEN scope"
        )
        assert declared == {"contents": "read"}, (
            f"{workflow} grants {declared!r}; nothing in it writes to the repo"
        )

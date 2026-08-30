"""The operator docs must describe commands that exist (WO-R2-90).

``docs/runbook.md`` and ``.env.example`` are what an operator reads immediately
before a live run, and they had drifted away from the ``Makefile`` and from the
runner's actual refusals in six places at once — three of them commands that
cannot succeed as written, one of them a default that sends the reader at the
wrong stack:

* ``make eval-reset``'s documented defaults were the sibling platform checkout
  and its ``app`` container. The Makefile defaults to this repo's demo stack and
  the ``api`` service. Following the doc resets a different Postgres and a
  different Redis, and reports success — the eval then runs against state
  nobody prepared.
* the image-bump procedure told the reader to run an unscoped
  ``docker compose up -d --wait``, which this repo has already established
  always fails when the one-shot services are recreated, which is exactly what
  a digest bump does. ``make demo`` exists precisely to scope that wait.
* the step-2 live command was a bare ``make eval-live``. An unfiltered live
  selection is refused by the runner before any spend — twice over, in fact.
* the DLQ seeding note named ``replay_dlq_messages`` as "the fix". That tool was
  demoted to legacy by the v0.4.0 categorization tools and is no longer what any
  scenario expects.
* the cost/scope line described a 33-scenario suite.

The generalisable half is that each of these is checkable against something in
the repo that cannot itself go stale — the Makefile's own rules and ``?=``
defaults, the scenario tree, the runner's refusals — which is what this file
does, following the pattern ``tests/unit/test_demo_docs.py`` already uses for
the demo docs. Nothing here hand-lists a forbidden string.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

from evals.runner import _SCENARIOS_DIR
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_MAKEFILE: Final[Path] = _REPO_ROOT / "Makefile"
_RUNBOOK: Final[Path] = _REPO_ROOT / "docs" / "runbook.md"
_ENV_EXAMPLE: Final[Path] = _REPO_ROOT / ".env.example"
_OPERATOR_DOCS: Final[tuple[Path, ...]] = (_RUNBOOK, _ENV_EXAMPLE)

# A make rule: a target name at column 0, followed by `:` and not `=`
# (which would be `VAR := value`).
_RULE: Final[re.Pattern[str]] = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)\s*:(?!=)", re.MULTILINE)
# `NAME ?= value` — the overridable defaults an operator can be told about.
_DEFAULT: Final[re.Pattern[str]] = re.compile(r"^([A-Z][A-Z0-9_]*)\s*\?=\s*(.*)$", re.MULTILINE)
# A fenced block plus its info string, so shell blocks can be told from yaml.
# Leading whitespace is allowed and matters: the image-bump procedure — the
# one carrying the unscoped compose wait — is a numbered list whose fences are
# indented under it, and an anchored `^```` skips exactly those blocks.
_FENCE: Final[re.Pattern[str]] = re.compile(
    r"^[ \t]*```([a-z]*)\n(.*?)^[ \t]*```", re.DOTALL | re.MULTILINE
)
# `make target` / `make target VAR=x`, as typed.
_MAKE_CALL: Final[re.Pattern[str]] = re.compile(r"\bmake\s+([a-z][a-z0-9-]*)")
_SHELL_INFO: Final[frozenset[str]] = frozenset({"bash", "sh", "shell", "console", ""})


def _makefile_text() -> str:
    return _MAKEFILE.read_text(encoding="utf-8")


def _make_targets() -> frozenset[str]:
    """Every target the Makefile declares a rule for."""
    return frozenset(_RULE.findall(_makefile_text()))


def _make_defaults() -> dict[str, str]:
    """Every `VAR ?= value` default, as make would expand it."""
    return {name: value.strip() for name, value in _DEFAULT.findall(_makefile_text())}


def _shell_blocks(doc: Path) -> list[str]:
    """The contents of ``doc``'s shell-ish fenced blocks."""
    return [
        body
        for info, body in _FENCE.findall(doc.read_text(encoding="utf-8"))
        if info in _SHELL_INFO
    ]


def _uncommented(block: str) -> list[str]:
    """Command lines of a shell block, with `#` comment lines dropped."""
    return [
        line for line in block.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]


def _claim_units(doc: Path) -> list[str]:
    """``doc`` as blank-line-separated paragraphs, one claim per unit.

    A paragraph rather than a line, because a claim is not reliably confined
    to one: ``.env.example`` states ``PLATFORM_COMPOSE``'s default across two
    wrapped comment lines, and a line-wise scan reads the variable name and
    the word "defaults" as unrelated. Leading ``#`` is stripped so the same
    unit shape covers the markdown docs and the env template.
    """
    units: list[str] = []
    current: list[str] = []
    for raw in doc.read_text(encoding="utf-8").splitlines():
        line = raw.lstrip().removeprefix("#").strip()
        if line:
            current.append(line)
            continue
        if current:
            units.append(" ".join(current))
            current = []
    if current:
        units.append(" ".join(current))
    return units


@pytest.fixture(scope="module")
def scenarios() -> list[Scenario]:
    return list(load_scenarios(_SCENARIOS_DIR))


def test_parser_canary() -> None:
    """Every assertion below is "no offenders found"; prove we can find any.

    A regex that stops matching turns each of these into a silent pass, which
    is the exact failure mode the file exists to prevent elsewhere.
    """
    targets = _make_targets()
    assert {"eval", "eval-live", "eval-smoke", "eval-reset", "demo"} <= targets, (
        f"the Makefile rule parser found {len(targets)} targets and is missing "
        "ones that certainly exist — the rule format changed"
    )
    assert "PLATFORM_COMPOSE" in _make_defaults(), "the `?=` default parser matched nothing"
    assert len(_shell_blocks(_RUNBOOK)) > 5, "the runbook fence parser found almost no blocks"


# --- commands that exist ----------------------------------------------------


def test_every_documented_make_target_exists() -> None:
    """A documented target that is not a rule is a command that cannot run.

    ``eval-live-remediation`` was deleted for being unsafe; a doc that still
    named it would send an operator at a target make answers with "No rule to
    make target", which reads as a broken checkout rather than a stale doc.
    """
    targets = _make_targets()
    missing = sorted(
        {
            name
            for doc in _OPERATOR_DOCS
            for block in _shell_blocks(doc)
            for line in _uncommented(block)
            for name in _MAKE_CALL.findall(line)
            if name not in targets
        }
    )
    assert missing == [], (
        f"the operator docs invoke make target(s) that the Makefile does not "
        f"declare: {missing}. Fix the doc, or add the rule."
    )


def test_documented_defaults_match_the_makefile() -> None:
    """A stated default must be the real one.

    Scoped to sentences that name the variable *and* claim a default, because
    that is the claim being checked — prose that merely mentions
    ``PLATFORM_COMPOSE`` while telling you to override it is not asserting
    anything about its value. This is what caught ``PLATFORM_COMPOSE``:
    both docs described the sibling platform checkout long after the Makefile
    moved to ``demo/compose.yml``, and following that resets a stack nobody is
    testing while reporting success.
    """
    wrong: list[str] = []
    for name, default in _make_defaults().items():
        for doc in _OPERATOR_DOCS:
            for unit in _claim_units(doc):
                if name not in unit or "efault" not in unit:
                    continue
                if default not in unit:
                    wrong.append(f"{doc.name}: {name} defaults to {default!r} — {unit}")
    assert wrong == [], (
        "the operator docs state a default that the Makefile contradicts. The "
        "Makefile's `?=` value is the only one that can be right:\n" + "\n".join(wrong)
    )


def test_eval_reset_names_the_service_the_makefile_shells_into() -> None:
    """The container name is half the reset target, and it drifted with the file.

    ``PLATFORM_COMPOSE`` and ``PLATFORM_SERVICE`` moved together and the docs
    followed neither, describing a ``docker compose exec`` into the platform
    dev stack's ``app``. The service name is not a default anyone states as a
    default, so the check is keyed on the sentence that describes the reset.
    """
    service = _make_defaults()["PLATFORM_SERVICE"]
    wrong = [
        f"{doc.name}: {unit}"
        for doc in _OPERATOR_DOCS
        for unit in _claim_units(doc)
        if "eval-reset" in unit and "container" in unit and service not in unit
    ]
    assert wrong == [], (
        f"the operator docs describe `make eval-reset` shelling into a container "
        f"other than the Makefile's PLATFORM_SERVICE default ({service!r}):\n" + "\n".join(wrong)
    )


# --- commands the runner would refuse ---------------------------------------


def test_an_unfiltered_live_run_really_is_refused(scenarios: list[Scenario]) -> None:
    """The premise of the rule below, derived rather than asserted.

    An unfiltered ``--live`` selection is the whole suite, and the whole suite
    trips two pre-spend refusals: canned-only scenarios cannot run live (exit
    8) and more than one state-mutating scenario cannot share an invocation
    (exit 7). If the tree ever stops making that true, this fails and the doc
    rule below should be revisited rather than silently enforced for nothing.
    """
    assert [s for s in scenarios if s.canned_only], "no canned-only scenario — exit 8 unreachable"
    mutating = [s for s in scenarios if s.expectation.expected_action_tools or s.chaos_setup]
    assert len(mutating) > 1, "at most one mutating scenario — exit 7 unreachable"


def test_documented_eval_live_invocations_are_filtered() -> None:
    """So the documented way to run a live eval must not be the refused one.

    ``make eval-live`` unfiltered was the runbook's step 2 for the entire life
    of the two refusals above: the documented happy path always failed. Every
    invocation has to carry ``ONLY=``, which is also the one-fault-one-scenario
    protocol the rest of the runbook insists on.
    """
    unfiltered = [
        line.strip()
        for doc in _OPERATOR_DOCS
        for block in _shell_blocks(doc)
        for line in _uncommented(block)
        if re.search(r"\bmake\s+eval-live\b", line) and "ONLY=" not in line
    ]
    assert unfiltered == [], (
        "the operator docs show an unfiltered `make eval-live`, which the runner "
        "refuses before any spend (exit 8 for canned-only scenarios, exit 7 for a "
        "multi-mutating selection). Show the filtered form:\n" + "\n".join(unfiltered)
    )


def test_documented_compose_waits_are_scoped_to_services() -> None:
    """`docker compose up --wait` unscoped fails whenever a one-shot re-runs.

    The Makefile's own ``demo`` target carries the finding and the fix: compose
    fails the wait when a one-shot (migrate, redpanda-init) exits during the
    watch window, so the wait is scoped to the five long-running services. The
    image-bump procedure told the reader to run the unscoped form — during a
    digest bump, which recreates every one-shot there is.
    """
    offenders = [
        line.strip()
        for doc in _OPERATOR_DOCS
        for block in _shell_blocks(doc)
        for line in _uncommented(block)
        if "docker compose" in line
        and re.search(r"\bup\b", line)
        and "--wait" in line
        and not re.search(r"--wait\s+\S", line)
    ]
    assert offenders == [], (
        "the operator docs run `docker compose up --wait` without naming "
        "services. Compose fails the wait when a one-shot exits during the watch "
        "window, so this always fails on a re-up. Use `make demo`, or name the "
        "long-running services as that target does:\n" + "\n".join(offenders)
    )


# --- claims about the suite -------------------------------------------------


def test_documented_suite_size_matches_the_scenario_tree(scenarios: list[Scenario]) -> None:
    """A cost estimate is only useful if its scenario count is the real one.

    The runbook advertised a 33-scenario suite and "~29 live" while the tree
    had grown past both, which understates what a live campaign costs and what
    it covers.
    """
    total, live = len(scenarios), len([s for s in scenarios if not s.canned_only])
    claims = [
        (line, int(match.group(1)))
        for doc in _OPERATOR_DOCS
        for line in _claim_units(doc)
        for match in [re.search(r"suite of (\d+)", line)]
        if match
    ]
    assert claims, "no documented suite size found — the cost/scope line went missing"
    wrong = [line for line, claimed in claims if claimed != total]
    assert wrong == [], f"the operator docs claim a suite size that is not {total}:\n" + "\n".join(
        wrong
    )
    live_claims = [
        (line, int(match.group(1)))
        for doc in _OPERATOR_DOCS
        for line in _claim_units(doc)
        for match in [re.search(r"~(\d+) live", line)]
        if match
    ]
    wrong_live = [line for line, claimed in live_claims if claimed != live]
    assert wrong_live == [], (
        f"the operator docs claim a live-scenario count that is not {live}:\n"
        + "\n".join(wrong_live)
    )


def test_tools_named_as_the_fix_are_ones_a_scenario_expects(scenarios: list[Scenario]) -> None:
    """ "X is the fix" must name a tool the agent is actually graded on using.

    ``replay_dlq_messages`` survived in the seeding instructions long after the
    v0.4.0 categorization tools replaced it. It is still in the registry, so an
    "is it a known tool" check would have passed it — the honest question is
    whether any scenario expects it, and none does. Seeding chaos and then
    watching for the wrong tool is a run misread as a failure.
    """
    routed = {tool for s in scenarios for tool in s.expectation.expected_action_tools}
    assert routed, "no scenario expects any action tool — the derivation is broken"
    stale = sorted(
        {
            name
            for doc in _OPERATOR_DOCS
            for name in re.findall(r"`?([a-z_]+)`? is the fix", doc.read_text(encoding="utf-8"))
            if name not in routed
        }
    )
    assert stale == [], (
        f"the operator docs name {stale} as 'the fix', but no scenario expects "
        f"that tool. The routed fixes are {sorted(routed)} — a demoted tool named "
        "here sends the reader watching for a call the agent will never make."
    )


# --- variables that exist ---------------------------------------------------

# `NAME=` at the start of a line, commented-out or live. A commented example
# still documents the variable, which is the thing being checked here.
_ASSIGNMENT: Final[re.Pattern[str]] = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=", re.MULTILINE)


def _env_example_names() -> frozenset[str]:
    return frozenset(_ASSIGNMENT.findall(_ENV_EXAMPLE.read_text(encoding="utf-8")))


def test_env_example_documents_every_settings_field() -> None:
    """The file an operator copies must offer every knob the agent reads.

    ``Settings`` has ``extra="ignore"``, so an undocumented field is not a
    crash — it is a default nobody knew they could change, discovered during
    an incident if at all. #166 added nine fields at once; this is what keeps
    the next batch from landing unmentioned.
    """
    from incident_commander.config import Settings

    missing = sorted({name.upper() for name in Settings.model_fields} - _env_example_names())
    assert missing == [], (
        f".env.example does not mention Settings field(s): {missing}. Add each "
        "with a comment saying what it does and whether it is optional — "
        "commented-out is fine for one that should keep its default."
    )


def test_env_example_documents_every_overridable_make_default() -> None:
    """`VAR ?= x` in the Makefile is an operator knob, and `.env` is where it goes.

    ``-include .env`` at the top of the Makefile means every one of these can be
    set once in ``.env`` instead of remembered on each invocation, which the
    runbook tells the reader to do. ``PLATFORM_SERVICE`` was such a knob and
    appeared in no operator-facing file at all, so the half of ``eval-reset``
    that names the container was untunable-by-documentation and silently wrong.
    """
    missing = sorted(set(_make_defaults()) - _env_example_names())
    assert missing == [], (
        f"the Makefile declares overridable default(s) {missing} that .env.example "
        "never mentions. `-include .env` makes .env the place an operator sets "
        "them, so an undocumented one is a knob nobody can find."
    )


def test_env_example_assigns_nothing_the_repo_does_not_read() -> None:
    """The mirror: a variable in the template that nothing consumes is a lie.

    This is the ``AGENT_ENABLED``-that-no-code-read defect
    (``tests/unit/test_docs_env_vars.py``) aimed at the file operators
    actually copy. A name here is either a ``Settings`` field or a make
    variable with a rule behind it; there is no third kind.
    """
    from incident_commander.config import Settings

    known = {name.upper() for name in Settings.model_fields} | set(_make_defaults())
    # Read by demo/compose.yml rather than by the agent or by make.
    compose_read = {
        name
        for name in _env_example_names()
        if name in (_REPO_ROOT / "demo" / "compose.yml").read_text(encoding="utf-8")
    }
    unread = sorted(_env_example_names() - known - compose_read)
    assert unread == [], (
        f".env.example assigns {unread}, which is not a Settings field, not a "
        "Makefile `?=` default, and not referenced by demo/compose.yml. Setting "
        "it does nothing; either wire it up or drop it."
    )

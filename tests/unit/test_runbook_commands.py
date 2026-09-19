"""The operator docs must describe commands that exist (WO-R2-90).

``docs/runbook.md`` and ``.env.example`` had drifted from the ``Makefile`` and the runner's
refusals in six places: ``eval-reset`` defaults naming the sibling checkout, an unscoped
``compose up --wait``, a bare ``make eval-live`` (refused), ``replay_dlq_messages`` named
as the fix, and a 33-scenario cost line. Each is checkable against the repo itself.
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
# A fenced block plus its info string, so shell blocks differ from yaml. Leading
# whitespace matters: the image-bump fences are indented under a list.
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

    A paragraph, not a line: ``.env.example`` states ``PLATFORM_COMPOSE``'s default across
    two wrapped comment lines. Leading ``#`` is stripped.
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

    ``eval-live-remediation`` was deleted; a doc naming it reads as a broken checkout.
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

    Scoped to sentences that name the variable *and* claim a default. This caught
    ``PLATFORM_COMPOSE``, which named the sibling checkout after the move to ``demo/``.
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

    ``PLATFORM_SERVICE`` is not stated as a default, so the check is keyed on the
    sentence describing the reset.
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
    """The premise of the rule below — now asserted structurally, not derived.

    The missing filter is refused on its own terms by ``make eval-live``'s ``ifndef ONLY``
    guard and the runner's exit-2 backstop. Kept as a tripwire on exits 8 and 7.
    """
    assert [s for s in scenarios if s.canned_only], "no canned-only scenario — exit 8 unreachable"
    mutating = [s for s in scenarios if s.expectation.expected_action_tools or s.chaos_setup]
    assert len(mutating) > 1, "at most one mutating scenario — exit 7 unreachable"


def test_documented_eval_live_invocations_are_filtered() -> None:
    """So the documented way to run a live eval must not be the refused one.

    ``make eval-live`` unfiltered was the runbook's step 2 for the whole life of the
    refusals above. Every invocation has to carry ``ONLY=``.
    """
    unfiltered = [
        line.strip()
        for doc in _OPERATOR_DOCS
        for block in _shell_blocks(doc)
        for line in _uncommented(block)
        if re.search(r"\bmake\s+eval-live\b", line) and "ONLY=" not in line
    ]
    assert unfiltered == [], (
        "the operator docs show an unfiltered `make eval-live`, which make refuses "
        "at parse time (exit 2, `ifndef ONLY`) and the runner refuses again before "
        "the settings load. Show the filtered form:\n" + "\n".join(unfiltered)
    )


def test_documented_compose_waits_are_scoped_to_services() -> None:
    """`docker compose up --wait` unscoped fails whenever a one-shot re-runs.

    The Makefile's ``demo`` target scopes the wait to the five long-running services; the
    image-bump procedure told the reader to run the unscoped form.
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

    The runbook advertised 33 long after the tree grew.
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

    ``replay_dlq_messages`` is still in the registry, so "is it known" would pass it;
    no scenario expects it.
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

    ``Settings`` has ``extra="ignore"``, so an undocumented field is a default nobody
    knew about.
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

    ``-include .env`` means each can be set once instead of remembered per invocation.
    ``PLATFORM_SERVICE`` appeared in no operator-facing file at all.
    """
    missing = sorted(set(_make_defaults()) - _env_example_names())
    assert missing == [], (
        f"the Makefile declares overridable default(s) {missing} that .env.example "
        "never mentions. `-include .env` makes .env the place an operator sets "
        "them, so an undocumented one is a knob nobody can find."
    )


def test_env_example_assigns_nothing_the_repo_does_not_read() -> None:
    """The mirror: a variable in the template that nothing consumes is a lie.

    A name here is a ``Settings`` field or a make variable with a rule behind it.
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


# ADR 0018's pattern: a code is defined in one place and every document agrees.
_EXIT_ADR: Final[Path] = (
    _REPO_ROOT / "docs" / "ADR" / "0037-a-scenarios-fault-is-a-plan-and-the-plan-is-put-back.md"
)
# A table row whose first cell is a bare integer, bold or not: `| 7 |`,
# `| **10** |`. Nothing else in either document is shaped like that.
_EXIT_ROW: Final[re.Pattern[str]] = re.compile(r"^\|\s*\*{0,2}(\d+)\*{0,2}\s*\|", re.MULTILINE)


def _exit_codes(text: str) -> set[int]:
    return {int(code) for code in _EXIT_ROW.findall(text)}


def _runbook_exit_section() -> str:
    """The runbook's own exit-code section, sliced at its heading."""
    text = _RUNBOOK.read_text(encoding="utf-8")
    marker = "### Runner exit codes"
    assert marker in text, "docs/runbook.md lost its exit-code section heading"
    body = text.split(marker, 1)[1]
    return body.split("\n## ", 1)[0]


def test_the_runbook_exit_table_carries_every_code_the_adr_defines() -> None:
    """The operator's table and the contract's table are the same table.

    ADR 0037 restated the contract whole as 0–10 while the runbook's table stopped at 8,
    so exits 9 and 10 were reachable and unmentioned. Pinned against the ADR.
    """
    documented = _exit_codes(_runbook_exit_section())
    contracted = _exit_codes(_EXIT_ADR.read_text(encoding="utf-8"))
    assert contracted == set(range(11)), (
        "ADR 0037 no longer enumerates 0-10; a new code extends that ADR the "
        "way 0018 and 0020 were extended, and this test moves with it"
    )
    assert documented == contracted, (
        f"docs/runbook.md's exit table and ADR 0037 disagree: "
        f"only in the runbook={sorted(documented - contracted)}, "
        f"only in the ADR={sorted(contracted - documented)}"
    )


def test_the_runbook_says_how_to_clear_the_teardown_latch() -> None:
    """Exit 10 is the one refusal that survives the invocation that caused it.

    A blocked operator who cannot find the way out of it in the runbook is an
    operator who will reach for `rm`.
    """
    section = _runbook_exit_section()
    assert "--clear-chaos-block" in section
    assert "make eval-reset PURGE_IDEMPOTENCY=1" in section
    assert ".chaos-teardown-block.json" in section

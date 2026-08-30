"""Doc-drift tripwire: eval-methodology.md's grader contract vs the grader.

A-16. ``docs/eval-methodology.md`` is the doc a scenario author writes a new
YAML from, and it had drifted away from ``evals/graders/deterministic.py`` in
two ways that cost the author real time:

* it said the grader "scores four dimensions" and its table listed four, while
  ``GradeDimension`` has had five members since the Phase-6 DLQ-categorization
  work added ``SAFETY``. A dimension nobody documents is a dimension nobody
  writes expectations for;
* it documented the action expectation as the singular ``expected_action_tool``
  while the field is ``expected_action_tools``, a tuple. ``ScenarioExpectation``
  is ``extra="forbid"``, so copying the documented name is not a silently
  ungraded dimension — it is a confusing scenario-load failure.

The finding's own ``why_tests_missed`` was "docs aren't linted against the
schema". These tests are that lint: the dimension table and every
expectation-field-shaped token in the doc are checked against the code, so the
next dimension or field rename fails CI here instead of on a scenario author.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

from evals.graders.deterministic import GradeDimension, ScenarioExpectation
from evals.scenarios.schema import Scenario

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_DOC: Final[Path] = _REPO_ROOT / "docs" / "eval-methodology.md"

_HEADING: Final[str] = "## Grading dimensions"
# A markdown table row whose first cell is a single inline-code token.
_TABLE_ROW: Final[re.Pattern[str]] = re.compile(r"^\|\s*`([A-Za-z_]+)`\s*\|")
# "scores five dimensions with pure logic" — the prose count that drifted.
_COUNT_PHRASE: Final[re.Pattern[str]] = re.compile(r"scores\s+([a-z]+)\s+dimensions")
# Tokens shaped like a ScenarioExpectation field, anywhere in the doc.
# ``expect_`` is here as well as ``expected_``: ``expect_briefing_contains``
# is a real field, and a prefix list that missed it would leave the newest
# expectation outside the only lint that keeps this page honest.
_FIELD_SHAPED: Final[re.Pattern[str]] = re.compile(
    r"\b(?:expected|expect|forbidden|max)_[a-z0-9_]+\b"
)

_NUMBER_WORDS: Final[dict[int, str]] = {
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
}

# Fields that carry no grading assertion and so need no row in the doc.
# Stated by name with a reason, rather than left to fall out of whichever
# direction the lint happens not to check: ``name`` is the scenario's
# identity, matched against the report, never something an author writes
# as an expectation.
_UNDOCUMENTED_BY_DESIGN: Final[frozenset[str]] = frozenset({"name"})


def _doc_text() -> str:
    return _DOC.read_text(encoding="utf-8")


def _documented_field_tokens() -> set[str]:
    """Every expectation-field-shaped token appearing anywhere in the doc."""
    return set(_FIELD_SHAPED.findall(_doc_text()))


def _real_fields() -> set[str]:
    return set(ScenarioExpectation.model_fields) | set(Scenario.model_fields)


def _grading_section() -> str:
    """The '## Grading dimensions' section body, up to the next H2."""
    parts = _doc_text().split(_HEADING, 1)
    assert len(parts) == 2, f"{_DOC.name} has no '{_HEADING}' heading"
    return parts[1].split("\n## ", 1)[0]


def test_grading_section_canary() -> None:
    """Guard against a vacuous pass if the section is renamed or reshaped."""
    rows = [_TABLE_ROW.match(line) for line in _grading_section().splitlines()]
    assert [m for m in rows if m], (
        f"layout canary: no `dimension` table rows found under '{_HEADING}' in {_DOC}"
    )


def test_documented_dimensions_match_the_grader() -> None:
    """Every GradeDimension is a table row, and no row invents one."""
    documented = {
        match.group(1).lower()
        for line in _grading_section().splitlines()
        if (match := _TABLE_ROW.match(line))
    }
    actual = {dimension.value for dimension in GradeDimension}
    assert documented == actual, (
        f"{_DOC.name}'s grading table documents {sorted(documented)} but "
        f"evals/graders/deterministic.py grades {sorted(actual)}. Undocumented "
        f"dimensions: {sorted(actual - documented)}; invented: "
        f"{sorted(documented - actual)}."
    )


def test_documented_dimension_count_matches_the_grader() -> None:
    """The prose count is a second copy of the same fact — keep it honest."""
    match = _COUNT_PHRASE.search(_grading_section())
    assert match is not None, (
        f"{_DOC.name} no longer states how many dimensions the grader scores "
        "('scores <word> dimensions'); that sentence is the one A-16 caught stale."
    )
    count = len(GradeDimension)
    assert count in _NUMBER_WORDS, (
        f"GradeDimension has {count} members, which _NUMBER_WORDS in "
        f"{Path(__file__).name} has no word for — extend the map. (This was a "
        "KeyError, which reads as a broken test rather than as the doc-drift "
        "signal it is.)"
    )
    expected = _NUMBER_WORDS[count]
    assert match.group(1) == expected, (
        f"{_DOC.name} says the grader 'scores {match.group(1)} dimensions' but "
        f"GradeDimension has {len(GradeDimension)} members ({expected})."
    )


def test_documented_expectation_fields_exist_on_the_model() -> None:
    """No stale field name may survive in the doc — extra='forbid' has teeth.

    ``expected_action_tool`` (singular) is the specific regression: a scenario
    copying it fails to load. Any other ``expected_``/``expect_``/
    ``forbidden_``/``max_`` token that is not a real field is the same bug
    with a different name.

    Both models are checked, because the field-shaped names are split across
    them: the graded assertions live on ``ScenarioExpectation`` and
    ``expected_precondition`` — a gate, not a grade — lives on ``Scenario``.
    Checking only one made a real field look like a typo.
    """
    fields = _real_fields()
    unknown = sorted(_documented_field_tokens() - fields)
    assert unknown == [], (
        f"{_DOC.name} names expectation fields that neither ScenarioExpectation "
        f"nor Scenario defines: {unknown}. Both are extra='forbid', so a scenario "
        f"author copying one of these gets a load failure. Real fields: "
        f"{sorted(fields)}."
    )


def test_every_expectation_field_is_documented() -> None:
    """The other direction — a new field must not ship undocumented.

    This lint used to run one way only: it failed when the doc named a
    field the models lacked, and never when a model gained a field the
    doc omitted. That is the same drift shape A-16 recorded, pointing the
    other way, and the *more* likely one — fields get added to
    ``ScenarioExpectation`` far more often than the doc invents names.
    The sibling dimension test above has always been bidirectional (it
    asserts set equality); this one now matches it.

    An undocumented expectation is not a load failure like a misspelled
    one, which is what makes it worse: the author simply never learns the
    assertion exists, so the dimension it feeds is graded vacuously across
    every scenario written from this page.
    """
    documented = _documented_field_tokens()
    graded = set(ScenarioExpectation.model_fields) - _UNDOCUMENTED_BY_DESIGN
    undocumented = sorted(graded - documented)
    assert undocumented == [], (
        f"ScenarioExpectation defines {undocumented}, which {_DOC.name} never "
        f"names. Add a row for each to the expectation-field documentation so "
        f"scenario authors can find it — or, if the field genuinely carries no "
        f"grading assertion, add it to _UNDOCUMENTED_BY_DESIGN in "
        f"{Path(__file__).name} with the reason. Documented today: "
        f"{sorted(documented)}."
    )


def test_field_shaped_pattern_still_matches_every_real_field() -> None:
    """The prefix list in ``_FIELD_SHAPED`` is itself hand-maintained.

    Both directions of the lint above see only tokens this regex matches,
    so a field added under a prefix it does not know — ``require_``,
    ``min_``, ``at_least_`` — is invisible to the lint that is supposed to
    guard it, and both tests keep passing while the coverage quietly
    shrinks. Checking the pattern against the real field names turns that
    silent gap into this failure.
    """
    graded = set(ScenarioExpectation.model_fields) - _UNDOCUMENTED_BY_DESIGN
    unmatched = sorted(f for f in graded if not _FIELD_SHAPED.fullmatch(f))
    assert unmatched == [], (
        f"_FIELD_SHAPED does not match {unmatched}, so those fields are "
        f"outside every check in {Path(__file__).name}. Widen the prefix "
        f"alternation in the pattern to cover them; a field the pattern "
        f"cannot see is a field the doc lint cannot guard."
    )

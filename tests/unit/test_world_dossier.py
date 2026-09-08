"""`make world-dossier` — the free, zero-LLM reading of a scenario's fault world.

The run this file exists because of: `remediate_runaway_saga_success` run A,
2026-09-07, archive `efdc3b2a9864`, ≈$0.15. The stuck chain's dead-lettered
root was seeded ``remediation_hint: replay_safe`` and carried the error text
``SchemaValidationError: payload missing required field 'user_id' …``. The
agent read the row (which ADR 0027 requires), reasoned that a missing required
field is a permanent data bug no replay can fix, and escalated naming the
contradiction. Two readiness sweeps had passed on that scenario; both checked
mechanics and neither read the fault's own fields.

So the properties pinned here are the ones that make the tool worth running:

* the probes are DERIVED from the guard maps, not listed — a hand-list rots
  in the direction of reading less of the world than the reader believes;
* the coherence lint is red on exactly that (hint, error) pair and green on a
  transient one, so the finding it exists to make is the finding it makes;
* the ONLY guard refuses before anything is seeded, in the shape `eval-live`
  refuses;
* the dossier write is create-only, like every other eval artifact
  (invariant 9).

Every test here is hermetic: a fake MCP client, ``tmp_path`` for writes, and
the suite-wide outbound-socket block in ``conftest.py``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from evals import artifacts, dossier
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario, chaos_tool_names
from incident_commander.tools.mcp_client import MCPError, ToolResult
from incident_commander.tools.policies import RESOURCE_ARG_FIELDS, Tier, tier_of
from incident_commander.tools.registry import TOOL_REGISTRY

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_SCENARIOS_DIR: Final[Path] = _REPO_ROOT / "evals" / "scenarios"

#: The chain root of `remediate_runaway_saga_success`, computed offline from a
#: migration constant (see the scenario YAML's own comment) and therefore the
#: same id on every stack.
_SAGA_ROOT: Final[str] = "a2412a54-65f0-5258-95ab-5c168a15df64"


class FakeClient:
    """Structural ``MCPClientProtocol`` fake keyed by tool name.

    Deliberately not ``evals.fakes.CannedMCPClient``: that one is a queue per
    tool, built for a trajectory that reads the same tool twice and expects
    different answers. A dossier reads each derived probe once and wants to
    assert on WHICH calls were made, so this records them.
    """

    def __init__(self, payloads: Mapping[str, Any], errors: Sequence[str] = ()) -> None:
        self._payloads = dict(payloads)
        self._errors = set(errors)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        if name in self._errors:
            raise MCPError(-32603, f"fake failure for {name}")
        if name not in self._payloads:
            raise MCPError(-32601, f"no fake payload for {name}")
        return ToolResult(
            content=[{"type": "text", "text": json.dumps(self._payloads[name])}],
            is_error=False,
        )


@pytest.fixture(scope="module")
def scenarios() -> dict[str, Scenario]:
    return {s.name: s for s in load_scenarios(_SCENARIOS_DIR)}


class TestProbeDerivation:
    """The probes come out of the maps, and out of nowhere else."""

    def test_the_dag_scenario_derives_its_three_readings(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        """Alert subject → get_dag_state; source row and verify → the rest.

        This is the derivation the rem-4 review needed and did not have. Each
        of the three is a different map answering a different question, and
        the union is "everything the agent is expected to read":

        * ``ALERT_SUBJECT_PROBES`` — what is the alert about? (cmd #177)
        * ``SOURCE_ROW_FOR_ACTION`` — is the thing we are about to change
          safe to change? (ADR 0027)
        * ``VERIFY_PROBE_FOR_ACTION`` — can we observe what we changed?
          (ADR 0025)
        """
        probes, notes = dossier.derive_probes(scenarios["remediate_runaway_saga_success"])
        by_label = {probe.label: probe for probe in probes}

        assert f"get_dag_state(job_id='{_SAGA_ROOT}')" in by_label
        assert "list_dlq_messages()" in by_label
        # Nothing else: two calls read this whole world, which is why the
        # check is cheap enough to be unskippable.
        assert len(probes) == 2, [p.label for p in probes]
        assert notes == []

        dag_origins = " ".join(by_label[f"get_dag_state(job_id='{_SAGA_ROOT}')"].origins)
        assert "ALERT_SUBJECT_PROBES" in dag_origins
        assert "VERIFY_PROBE_FOR_ACTION[replay_dlq_by_ids]" in dag_origins

        dlq_origins = " ".join(by_label["list_dlq_messages()"].origins)
        assert "SOURCE_ROW_FOR_ACTION[replay_dlq_by_ids]" in dlq_origins

    def test_a_category_scenario_derives_the_listing_from_the_coverage_map(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        """ADR 0028's map is the fourth derivation, and on a category-replay
        scenario it is the one that earns the probe.

        `SOURCE_ROW_FOR_ACTION` is inert for `replay_dlq_by_category` — a
        category names no row — so before this map existed the dossier's
        reason for reading the DLQ on `dlq_replay_safe_success` came only
        from the scenario's own evidence claim. That is a weaker footing
        than it looks: a scenario that dropped the claim would have dropped
        the probe with it, and the review would stop showing the rows the
        agent is about to sweep.

        The derived probe is UNFILTERED, which is the point of it — the
        review compares the rows the category will take against the ones it
        will leave, and a hint-filtered page shows only the first half.
        """
        probes, notes = dossier.derive_probes(scenarios["dlq_replay_safe_success"])
        by_label = {probe.label: probe for probe in probes}

        assert "list_dlq_messages()" in by_label
        origins = " ".join(by_label["list_dlq_messages()"].origins)
        assert "SOURCE_LISTING_FOR_ACTION[replay_dlq_by_category]" in origins
        assert "names a FILTER" in origins

        # And the INERT note for the by-id map points at its sibling, so a
        # reader does not take "no source row is required" for "nothing to
        # check about what this replays".
        inert = [n for n in notes if "INERT in SOURCE_ROW_FOR_ACTION" in n]
        assert inert, notes
        assert any("SOURCE_LISTING_FOR_ACTION" in n for n in inert)

    def test_one_call_derived_twice_is_probed_once_and_says_why_twice(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        """Dedup by call, union the reasons — a probe is not less important
        for being derivable two ways, and the reader wants both reasons."""
        probes, _ = dossier.derive_probes(scenarios["remediate_runaway_saga_success"])
        labels = [probe.label for probe in probes]
        assert len(labels) == len(set(labels))
        dag = next(p for p in probes if p.tool == "get_dag_state")
        assert len(dag.origins) > 1

    def test_the_source_row_read_is_unfiltered(self, scenarios: dict[str, Scenario]) -> None:
        """The precondition asks the platform for the `replay_safe` page; the
        dossier asks for the whole listing.

        Both readings are in the document on purpose. The filtered one is the
        scenario's premise; the UNFILTERED one is what the coherence lint
        needs, because a contradiction on a row the filter excluded is still
        a contradiction the agent will read.
        """
        probes, _ = dossier.derive_probes(scenarios["remediate_runaway_saga_success"])
        dlq = next(p for p in probes if p.tool == "list_dlq_messages")
        assert dlq.args == {}

    def test_a_read_only_scenario_derives_the_alert_subject_and_says_what_it_cannot(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        """No action tools means no source row and no verify probe, and the
        dossier SAYS so rather than quietly deriving one probe."""
        probes, notes = dossier.derive_probes(scenarios["consumer_lag_high"])
        assert [p.label for p in probes] == ["get_consumer_lag(consumer_group='worker-dispatcher')"]
        assert any("expected_action_tools" in note for note in notes)

    def test_no_scenario_in_the_tree_raises(self, scenarios: dict[str, Scenario]) -> None:
        """Derivation is total over the corpus. A scenario that made it crash
        would be a scenario nobody could review before spending on it."""
        for scenario in scenarios.values():
            probes, _notes = dossier.derive_probes(scenario)
            for probe in probes:
                assert probe.tool in TOOL_REGISTRY

    def test_a_derived_probe_is_never_a_write(self, scenarios: dict[str, Scenario]) -> None:
        """Zero Tier-1 calls, structurally. The claim on the tin."""
        for scenario in scenarios.values():
            probes, _ = dossier.derive_probes(scenario)
            for probe in probes:
                assert tier_of(probe.tool) is Tier.READ, probe.label


class TestKindByFieldIsTotal:
    """A resource-naming argument with no kind produces a guessed probe."""

    def test_every_resource_arg_field_has_a_kind(self) -> None:
        """``RESOURCE_ARG_FIELDS`` is the platform's list of arguments that
        NAME something; ``_KIND_BY_FIELD`` says which ones name the same
        something. A field missing here does not fail loudly — it silently
        drops a probe (``_fill`` writes a note and moves on), so the whole
        dossier reads less of the world with nothing to say it did."""
        fields = {field for fields in RESOURCE_ARG_FIELDS.values() for field in fields}
        missing = sorted(fields - set(dossier._KIND_BY_FIELD))
        assert not missing, (
            f"{missing} name a platform resource and have no entry in "
            "_KIND_BY_FIELD, so no dossier probe can be filled with one. Add "
            "the field with the kind of resource it names."
        )


class TestCoherenceLint:
    """(hint, error text) — findings, not verdicts."""

    def test_replay_safe_on_a_schema_error_is_flagged(self) -> None:
        """The rem-4 run-A row, verbatim from `seed_eval_fixtures.py`."""
        findings = dossier.lint_dlq_row(
            {
                "id": _SAGA_ROOT,
                "remediation_hint": "replay_safe",
                "error_message": (
                    "SchemaValidationError: payload missing required field "
                    "'user_id' (received keys: ['tenant_id', 'action', 'ts'])"
                ),
            },
            "list_dlq_messages()",
        )
        assert [f.kind for f in findings] == ["INCOHERENT hint vs error"]
        assert "bad_data" in findings[0].detail
        assert "efdc3b2a9864" in findings[0].detail

    def test_replay_safe_on_a_timeout_is_clean(self) -> None:
        """A transient error is what `replay_safe` means (platform enums.py:
        "transient / poison — replay OK"). No finding, and silence here means
        POSITIVELY coherent — an unclassifiable text produces a finding of
        its own, so it cannot be mistaken for this."""
        assert (
            dossier.lint_dlq_row(
                {
                    "id": "9f2b",
                    "remediation_hint": "replay_safe",
                    "error_message": "upstream call timed out after 30s: TimeoutError('stripe')",
                },
                "list_dlq_messages()",
            )
            == []
        )

    def test_wait_and_replay_admits_a_transient_error(self) -> None:
        """The one cell added to the brief's table, and the reason.

        `wait_and_replay` is the platform's "external dep down — retry later".
        A connection refusal from an SMTP relay IS that, and two of the four
        seeded rows carry exactly it. Reading transient errors as
        `replay_safe`-only would put two false findings beside the one true
        one on every run, and a lint the reader learns to skim is a lint that
        has stopped working. The difference between the two hints on a
        transient error is WHEN to replay, not WHETHER.
        """
        assert (
            dossier.lint_dlq_row(
                {
                    "id": "1a2b",
                    "remediation_hint": "wait_and_replay",
                    "error_message": (
                        "send_email downstream call failed: "
                        "ConnectionRefusedError('smtp.mailer.internal:587')"
                    ),
                },
                "list_dlq_messages()",
            )
            == []
        )

    def test_replay_safe_on_a_rate_limit_is_flagged(self) -> None:
        """The asymmetry that earns the table its keep: a rate limit means an
        immediate replay is actively wrong, so it sanctions `wait_and_replay`
        and not `replay_safe`."""
        findings = dossier.lint_dlq_row(
            {
                "id": "3c4d",
                "remediation_hint": "replay_safe",
                "error_message": "429 Too Many Requests — rate limit exceeded, backoff 60s",
            },
            "list_dlq_messages()",
        )
        assert [f.kind for f in findings] == ["INCOHERENT hint vs error"]

    def test_a_null_hint_contradicts_nothing_and_still_gets_read(self) -> None:
        """ "Not categorised" is the platform's UNKNOWN. It cannot disagree
        with an error text, so there is no incoherence finding — but a null
        hint is worth the reader's attention on its own ("A null hint is
        UNKNOWN, not replay-safe" — `list_dlq_messages`), and the row still
        appears in §5.1's table."""
        assert (
            dossier.lint_dlq_row(
                {
                    "id": "5e6f",
                    "remediation_hint": None,
                    "error_message": "ValueError: invalid literal for int()",
                },
                "list_dlq_messages()",
            )
            == []
        )

    def test_an_unreadable_error_text_says_it_has_no_opinion(self) -> None:
        """Unknown ≠ coherent. The lint says which it is."""
        findings = dossier.lint_dlq_row(
            {"id": "7a", "remediation_hint": "replay_safe", "error_message": "widget exploded"},
            "list_dlq_messages()",
        )
        assert [f.kind for f in findings] == ["unclassified error text"]
        assert "NO OPINION" in findings[0].detail

    def test_a_hint_outside_the_platforms_three_is_a_finding(self) -> None:
        findings = dossier.lint_dlq_row(
            {"id": "8b", "remediation_hint": "maybe_later", "error_message": "timeout"},
            "list_dlq_messages()",
        )
        assert [f.kind for f in findings] == ["unknown hint"]

    def test_rows_are_found_wherever_they_sit(self) -> None:
        """Structural, not path-based: a tool that starts embedding a
        dead-letter row somewhere new gets linted without this code moving."""
        found = list(
            dossier.dlq_rows_in(
                {
                    "items": [{"id": "a", "remediation_hint": None, "error_message": "x"}],
                    "nested": {
                        "deep": [{"id": "b", "remediation_hint": "x", "error_message": "y"}]
                    },
                    "not_a_row": {"id": "c", "status": "ok"},
                }
            )
        )
        assert sorted(str(row["id"]) for row in found) == ["a", "b"]


class TestTheSanctionedIncoherentFixture:
    """The lab's ONE deliberately mislabelled row reads as the premise, not red.

    `dlq_mislabeled_replay_safe` (WO-R2-167) seeds a row whose hint says
    `replay_safe` and whose error is a permanent CSV data fault. The lint is
    right to see a contradiction — that IS the fixture — and the rem-4 sentence
    it would otherwise print ("decide which one is wrong BEFORE spending") is
    actively wrong advice on a row whose whole product is the contradiction.

    What must NOT happen is the row going quiet: a dossier that showed it as
    coherent would be the same species of untrue-but-green the file exists to
    prevent. So the row keeps a finding and keeps its own verdict word.
    """

    _MISLABELLED = "be64a675-212b-5379-8349-816d17a8107a"
    _CSV_ERROR = "ValueError: invalid literal for int() with base 10: 'not-a-number' at row 15,382"

    def _readings(self) -> list[dossier.Reading]:
        client = FakeClient(
            {
                "list_dlq_messages": {
                    "total": 2,
                    "items": [
                        {
                            "id": self._MISLABELLED,
                            "remediation_hint": "replay_safe",
                            "error_message": self._CSV_ERROR,
                        },
                        {
                            "id": "fc8d2a03-23b3-5371-9acb-46443c73baa5",
                            "remediation_hint": "replay_safe",
                            "error_message": "UpstreamTimeout: partner-api timed out after 30s",
                        },
                    ],
                }
            }
        )
        return [dossier.read(client, dossier._probe("list_dlq_messages", {}, "test"))]

    def test_without_the_exemption_it_is_the_rem_4_finding(self) -> None:
        """Red-before: this is what every other incoherent row still gets."""
        findings, rows = dossier.lint_dlq_coherence(self._readings())
        assert [f.kind for f in findings] == [dossier.INCOHERENT_KIND]
        assert "efdc3b2a9864" in findings[0].detail
        assert {row[3] for row in rows} == {"FLAG", "coherent"}

    def test_with_the_exemption_it_is_named_as_the_fixture(self) -> None:
        findings, rows = dossier.lint_dlq_coherence(self._readings(), {self._MISLABELLED})
        assert [f.kind for f in findings] == [dossier.SANCTIONED_KIND]
        assert "WO-R2-167" in findings[0].kind
        assert "the fixture, not a defect" in findings[0].detail
        assert "DISBELIEVING the hint" in findings[0].detail
        verdicts = {row[0]: row[3] for row in rows}
        assert verdicts[self._MISLABELLED] == "sanctioned incoherent (WO-R2-167)"

    def test_the_row_is_still_reported_rather_than_silenced(self) -> None:
        """The important half. Silence would read as "this row is fine"."""
        findings, rows = dossier.lint_dlq_coherence(self._readings(), {self._MISLABELLED})
        assert findings, "the sanctioned row produced no finding at all"
        assert all(row[3] != "coherent" or row[0] != self._MISLABELLED for row in rows)
        # And the pair itself is still printed, so a reader can check it is the
        # contradiction they expected rather than a different one.
        assert any(row[0] == self._MISLABELLED and "bad_data" in row[2] for row in rows)

    def test_the_exemption_does_not_reach_the_row_beside_it(self) -> None:
        """Naming one row exempts one row. The neighbour is linted normally."""
        client = FakeClient(
            {
                "list_dlq_messages": {
                    "total": 2,
                    "items": [
                        {
                            "id": self._MISLABELLED,
                            "remediation_hint": "replay_safe",
                            "error_message": self._CSV_ERROR,
                        },
                        {
                            "id": "someone-else",
                            "remediation_hint": "replay_safe",
                            "error_message": self._CSV_ERROR,
                        },
                    ],
                }
            }
        )
        readings = [dossier.read(client, dossier._probe("list_dlq_messages", {}, "test"))]
        findings, rows = dossier.lint_dlq_coherence(readings, {self._MISLABELLED})
        kinds = {f.subject.split("`")[1]: f.kind for f in findings}
        assert kinds[self._MISLABELLED] == dossier.SANCTIONED_KIND
        assert kinds["someone-else"] == dossier.INCOHERENT_KIND
        verdicts = {row[0]: row[3] for row in rows}
        assert verdicts["someone-else"] == "FLAG"

    def test_a_coherent_row_named_as_sanctioned_is_unaffected(self) -> None:
        """The exemption rewrites an existing finding; it never invents one.

        So a sanctioned id whose row turned out coherent — the hook writing
        something else — reads coherent, which is itself a signal worth seeing.
        """
        client = FakeClient(
            {
                "list_dlq_messages": {
                    "total": 1,
                    "items": [
                        {
                            "id": self._MISLABELLED,
                            "remediation_hint": "human_required",
                            "error_message": self._CSV_ERROR,
                        }
                    ],
                }
            }
        )
        readings = [dossier.read(client, dossier._probe("list_dlq_messages", {}, "test"))]
        findings, rows = dossier.lint_dlq_coherence(readings, {self._MISLABELLED})
        assert findings == []
        assert rows[0][3] == "coherent"

    def test_the_sanctioned_hook_set_is_the_platforms_one_exception(self) -> None:
        """Keyed on the hook, so no scenario can declare itself exempt."""
        hooks = dossier.SANCTIONED_INCOHERENT_HOOKS
        assert hooks == frozenset({"create_mislabeled_dlq_job"})
        # And it is a hook the platform actually registers, so the exemption
        # cannot outlive the tool that earns it.
        assert not hooks - chaos_tool_names()


class TestSelectionGuard:
    """Nothing is seeded until exactly one scenario has been named."""

    def test_no_only_refuses(
        self, capsys: pytest.CaptureFixture[str], scenarios: dict[str, Scenario]
    ) -> None:
        selected, code = dossier._select([], list(scenarios.values()))
        assert selected is None
        assert code == dossier.EXIT_SELECTION
        out = capsys.readouterr().out
        assert "DOSSIER FAIL" in out
        assert "nothing was seeded" in out

    def test_main_without_only_refuses_before_touching_settings(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The runner backstop, in the shape `eval-live` has one.

        The Makefile refuses at parse time, but ``python -m evals.dossier``
        never comes through make — and this one SEEDS CHAOS, so the backstop
        is not a nicety. ``Settings`` is poisoned to prove the refusal happens
        first: no env is read, so a checkout with no `.env` still refuses for
        the right reason.
        """

        def _explode() -> None:
            raise AssertionError("settings must not be constructed before the ONLY guard")

        monkeypatch.setattr(dossier, "Settings", _explode)
        assert dossier.main([]) == dossier.EXIT_SELECTION
        assert "nothing was seeded" in capsys.readouterr().out

    def test_two_scenarios_refuse(
        self, capsys: pytest.CaptureFixture[str], scenarios: dict[str, Scenario]
    ) -> None:
        """Two faults in one shared world make both readings meaningless."""
        selected, code = dossier._select(
            ["remediate_runaway_saga_success", "saga_stuck"], list(scenarios.values())
        )
        assert selected is None
        assert code == dossier.EXIT_SELECTION
        assert "2 scenarios named" in capsys.readouterr().out

    def test_a_substring_refuses_and_says_what_it_meant(
        self, capsys: pytest.CaptureFixture[str], scenarios: dict[str, Scenario]
    ) -> None:
        """Full names only, exactly as a live run selects — a substring
        silently widens a selection, and `ONLY=dlq_backlog` taking
        `remediate_dlq_backlog_success` with it is how a read-only stage
        smuggled a mutating scenario past the ADR 0020 gate."""
        selected, code = dossier._select(["runaway_saga"], list(scenarios.values()))
        assert selected is None
        assert code == dossier.EXIT_SELECTION
        out = capsys.readouterr().out
        assert "Did you mean:" in out
        assert "ONLY=remediate_runaway_saga_success" in out

    def test_a_name_that_is_also_a_prefix_stays_runnable(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        """Exact match FIRST, the runner's rule. `dlq_backlog` is a real
        scenario AND a substring of `remediate_dlq_backlog_success`; refusing
        it would make it impossible to review before spending on it."""
        selected, code = dossier._select(["dlq_backlog"], list(scenarios.values()))
        assert selected is not None and selected.name == "dlq_backlog"
        assert code == dossier.EXIT_OK


class TestDossierWriteIsCreateOnly:
    """Invariant 9 applies to the dossier like every other eval artifact."""

    def test_a_second_write_to_the_same_path_raises(self, tmp_path: Path) -> None:
        """A dossier is the evidence that somebody looked at the world before
        the money was released. Overwriting one erases that."""
        stamp = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
        first = artifacts.write_versioned(
            "dossier",
            "remediate_runaway_saga_success",
            content="first reading",
            timestamp=stamp,
            invocation_id="aaaabbbbcccc",
            directory=tmp_path,
        )
        assert first.read_text() == "first reading"
        with pytest.raises(FileExistsError):
            artifacts.write_versioned(
                "dossier",
                "remediate_runaway_saga_success",
                content="second reading",
                timestamp=stamp,
                invocation_id="aaaabbbbcccc",
                directory=tmp_path,
            )
        assert first.read_text() == "first reading"

    def test_the_name_carries_scenario_stamp_and_invocation(self, tmp_path: Path) -> None:
        path = artifacts.write_versioned(
            "dossier",
            "remediate_runaway_saga_success",
            content="x",
            timestamp=datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC),
            invocation_id="aaaabbbbcccc",
            directory=tmp_path,
        )
        assert path.name == "remediate_runaway_saga_success.20260907T120000Z.aaaabbbbcccc.md"

    def test_newest_resolves_a_second_reading(self, tmp_path: Path) -> None:
        """Two readings of the same scenario are two versions, and the
        resolver returns the later one — by the filename's stamp, never by
        mtime."""
        for hour, invocation in ((12, "aaaabbbbcccc"), (13, "ddddeeeeffff")):
            artifacts.write_versioned(
                "dossier",
                "saga_stuck",
                content=f"reading at {hour}",
                timestamp=datetime(2026, 9, 7, hour, 0, 0, tzinfo=UTC),
                invocation_id=invocation,
                directory=tmp_path,
            )
        assert artifacts.newest("dossier", "saga_stuck", directory=tmp_path).read_text() == (
            "reading at 13"
        )

    def test_the_dossier_directory_is_under_reports(self) -> None:
        assert artifacts.directory_for("dossier").parts[-3:] == (
            "evals",
            "reports",
            "dossiers",
        )


class TestReadingTheWorld:
    """The probe/lint pipeline over a fake platform."""

    def _payloads(self, hint: str, error: str) -> dict[str, Any]:
        return {
            "get_dag_state": {
                "seed_id": _SAGA_ROOT,
                "paused": False,
                "nodes": [
                    {"id": _SAGA_ROOT, "status": "dead_letter", "retry_count": 3},
                    {"id": "child-1", "status": "waiting", "retry_count": 0},
                ],
            },
            "list_dlq_messages": {
                "total": 5,
                "items": [
                    {"id": _SAGA_ROOT, "remediation_hint": hint, "error_message": error},
                    {
                        "id": "f030f975-974e-5ce3-aa6b-444136507d86",
                        "remediation_hint": "human_required",
                        "error_message": "ValueError: invalid literal for int()",
                    },
                ],
            },
            "list_active_alerts": {"total": 3, "alerts": []},
        }

    def test_the_contradiction_surfaces_from_a_real_scenarios_probes(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        """End to end over the fake: derive, read, lint, find it."""
        client = FakeClient(
            self._payloads(
                "replay_safe",
                "SchemaValidationError: payload missing required field 'user_id'",
            )
        )
        probes, _ = dossier.derive_probes(scenarios["remediate_runaway_saga_success"])
        readings = [dossier.read(client, probe) for probe in probes]
        assert all(reading.ok for reading in readings)
        findings, rows = dossier.lint_dlq_coherence(readings)
        assert [f.kind for f in findings] == ["INCOHERENT hint vs error"]
        assert findings[0].subject.startswith(f"DLQ row `{_SAGA_ROOT}`")
        assert {row[3] for row in rows} == {"FLAG", "coherent"}

    def test_a_coherent_world_produces_no_dlq_finding(self, scenarios: dict[str, Scenario]) -> None:
        client = FakeClient(
            self._payloads("replay_safe", "worker connection reset by peer, retrying")
        )
        probes, _ = dossier.derive_probes(scenarios["remediate_runaway_saga_success"])
        readings = [dossier.read(client, probe) for probe in probes]
        assert dossier.lint_dlq_coherence(readings)[0] == []

    def test_the_action_target_lint_finds_an_absent_resource(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        """If no read output carries the id the action must name, the agent
        cannot reach it by reading — and the plan guards would refuse the
        plan the scenario grades."""
        payloads = self._payloads("replay_safe", "timeout")
        payloads["get_dag_state"] = {"seed_id": "someone-else", "paused": False, "nodes": []}
        payloads["list_dlq_messages"] = {"total": 4, "items": []}
        client = FakeClient(payloads)
        scenario = scenarios["remediate_runaway_saga_success"]
        probes, _ = dossier.derive_probes(scenario)
        readings = [dossier.read(client, probe) for probe in probes]
        findings, _rows = dossier.lint_action_targets(scenario, readings, [])
        assert [f.kind for f in findings] == ["action target absent from every read"]

    def test_a_failing_probe_is_reported_not_raised(self, scenarios: dict[str, Scenario]) -> None:
        """A dead probe is part of the report. Raising would lose every
        reading taken before it — and the world would still be holding the
        seeded fault."""
        client = FakeClient(self._payloads("replay_safe", "timeout"), errors=["get_dag_state"])
        probes, _ = dossier.derive_probes(scenarios["remediate_runaway_saga_success"])
        readings = [dossier.read(client, probe) for probe in probes]
        failed = [r for r in readings if not r.ok]
        assert len(failed) == 1
        assert "MCPError" in (failed[0].error or "")

    def test_forbidden_furniture_is_reported_both_ways(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        """Present is the normal case. ABSENT is the one worth noticing: the
        negative assertion guarding it cannot fire in a world that does not
        contain it."""
        client = FakeClient(self._payloads("replay_safe", "timeout"))
        scenario = scenarios["remediate_runaway_saga_success"]
        probes, _ = dossier.derive_probes(scenario)
        readings = [dossier.read(client, probe) for probe in probes]
        findings, rows = dossier.lint_forbidden_furniture(scenario, readings)
        kinds = {f.kind for f in findings}
        assert "forbidden value present in the world (informational)" in kinds
        assert "forbidden value absent from the world (informational)" in kinds
        assert any(row[1] == "f030f975-974e-5ce3-aa6b-444136507d86" for row in rows)

    def test_the_document_prints_every_output_in_full(self, scenarios: dict[str, Scenario]) -> None:
        """Truncation is how the contradiction stayed invisible through two
        sweeps. The rendered dossier carries the whole payload."""
        error = "SchemaValidationError: payload missing required field 'user_id'"
        client = FakeClient(self._payloads("replay_safe", error))
        scenario = scenarios["remediate_runaway_saga_success"]
        probes, notes = dossier.derive_probes(scenario)
        readings = [dossier.read(client, probe) for probe in probes]
        findings, rows = dossier.lint_dlq_coherence(readings)
        document = dossier.render(
            scenario=scenario,
            generated_at=datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC),
            invocation_id="aaaabbbbcccc",
            settings_url="http://localhost:8001/mcp",
            head="deadbee",
            seeding="{}",
            preconditions=[],
            readings=readings,
            notes=notes,
            dlq_findings=findings,
            dlq_rows=rows,
            target_findings=[],
            target_rows=[],
            furniture_findings=[],
            furniture_rows=[],
            reset_code=0,
            reset_output="ok",
            baseline=[dossier.BaselineLine("DLQ total", "4", "4", True)],
        )
        assert error in document
        assert "INCOHERENT hint vs error" in document
        assert "Baseline re-audit: PASS" in document
        assert "$0.00" in document
        # Whole payloads, not summaries: every field of every reading is in
        # the document, because the field that mattered last time was one
        # nobody would have chosen to summarise.
        for reading in readings:
            assert reading.payload is not None
            assert json.dumps(reading.payload, indent=2) in document
        assert "[truncated]" not in document

    def test_a_dirty_baseline_says_so_loudly(self, scenarios: dict[str, Scenario]) -> None:
        document = dossier.render(
            scenario=scenarios["dlq_backlog"],
            generated_at=datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC),
            invocation_id="aaaabbbbcccc",
            settings_url="http://localhost:8001/mcp",
            head="deadbee",
            seeding="{}",
            preconditions=[],
            readings=[],
            notes=[],
            dlq_findings=[],
            dlq_rows=[],
            target_findings=[],
            target_rows=[],
            furniture_findings=[],
            furniture_rows=[],
            reset_code=0,
            reset_output="ok",
            baseline=[dossier.BaselineLine("DLQ total", "4", "7", False)],
        )
        assert "Baseline re-audit: FAIL" in document
        assert "WO-R2-131" in document


class TestBaselineMatchesTheRunbook:
    """One copy of the seeded baseline, pinned to the document that states it.

    LESSONS 2026-09-07: "five places for the same truth means five stale
    copies". The dossier re-audits the same numbers the runbook's pre-run
    checklist tells a human to check, so the two must agree by test rather
    than by discipline.
    """

    def test_the_numbers_agree(self) -> None:
        runbook = (_REPO_ROOT / "docs" / "runbook.md").read_text()
        expected = {
            r"\|\s*DLQ total\s*\|\s*\*\*(\d+)\*\*\s*\|": dossier.BASELINE_DLQ_TOTAL,
            r"\|\s*Active alerts\s*\|\s*\*\*(\d+)\*\*\s*\|": dossier.BASELINE_ACTIVE_ALERTS,
            r"\|\s*Redis `chaos:\*` keys\s*\|\s*\*\*(\d+)\*\*\s*\|": dossier.BASELINE_CHAOS_KEYS,
        }
        for pattern, value in expected.items():
            match = re.search(pattern, runbook)
            assert match is not None, (
                f"docs/runbook.md no longer carries a pre-run baseline row matching "
                f"{pattern!r}. The dossier re-audits that table; if the table moved, "
                "move this check with it rather than deleting it."
            )
            assert int(match.group(1)) == value

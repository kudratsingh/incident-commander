"""``scripts/fixture_drift.py``'s two decisions: when to wait, and when to bless.

Both are exercised with ``probe_live`` replaced, because both are about what
the script does with a result rather than about how the result was obtained
— that half lives in ``test_fixture_drift.py``. Nothing here touches the
network and nothing writes under ``evals/`` (ADR 0011 freeze): the bless
tests assert the committed ledger is byte-identical afterwards.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from evals.fixture_drift import Drift
from evals.fixture_drift_ledger import LEDGER_PATH
from evals.fixture_probe import ProbeError, ProbeResult
from scripts import fixture_drift as cli


@pytest.fixture
def platform_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLATFORM_MCP_URL", "http://x/mcp")
    monkeypatch.setenv("PLATFORM_SMOKE_TOKEN", "read-scoped")


def _result(
    *,
    drifts: tuple[Drift, ...] = (),
    errors: tuple[ProbeError, ...] = (),
    compared: tuple[tuple[str, str], ...] = (),
) -> ProbeResult:
    return ProbeResult(
        drifts=drifts,
        errors=errors,
        checked=len(compared),
        skipped_write_tier=0,
        live_calls=len(compared),
        compared=compared,
    )


def _scripted(monkeypatch: pytest.MonkeyPatch, outcomes: list[Any]) -> list[Any]:
    """Replace ``probe_live`` with a queue of results-or-raises."""
    remaining = list(outcomes)

    def fake_probe(calls: Any, **kwargs: Any) -> ProbeResult:  # noqa: ARG001
        outcome = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, ProbeResult)
        return outcome

    monkeypatch.setattr(cli, "probe_live", fake_probe)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    return remaining


class TestReadinessGate:
    """``--await-fixtures`` exists to survive a platform that is still booting.

    It only ever caught ``UnseededPlatformError`` — the "up but empty" case —
    so the connection errors a platform produces while it is *not yet up*
    killed the poll loop on attempt one, which is precisely the window the
    gate was added for.
    """

    def test_survives_a_platform_that_has_not_opened_its_port(
        self, monkeypatch: pytest.MonkeyPatch, platform_env: None
    ) -> None:
        remaining = _scripted(
            monkeypatch,
            [
                httpx.ConnectError("[Errno 61] Connection refused"),
                httpx.ReadTimeout("timed out"),
                _result(compared=(("s", "get_consumer_lag"),)),
            ],
        )
        assert cli.main(["--await-fixtures", "60"]) == 0
        assert len(remaining) == 1, "the gate gave up before the platform came up"

    def test_a_failure_that_outlives_the_budget_is_still_reported(
        self,
        monkeypatch: pytest.MonkeyPatch,
        platform_env: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # A readiness gate, not a retry: an unresolved call at the deadline is
        # reported with the reason, never swallowed into a pass.
        _scripted(monkeypatch, [httpx.ConnectError("[Errno 61] Connection refused")])
        assert cli.main(["--await-fixtures", "0"]) == 2
        assert "Connection refused" in capsys.readouterr().err


class TestBlessRefusesOnAnUnprobedFixture:
    """The ledger may only shrink on evidence, and an error is not evidence.

    ``--bless`` rewrote the whole ledger from the drift observed in one run,
    including runs where ``result.errors`` said some fixtures were never
    reached. Every ledger entry for an unreached fixture then vanished — a
    silent deletion of work nobody had disproved, in the file that IS the
    burn-down list.
    """

    def test_bless_refuses_and_leaves_the_ledger_untouched(
        self,
        monkeypatch: pytest.MonkeyPatch,
        platform_env: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        before = LEDGER_PATH.read_bytes()
        written: list[Any] = []
        monkeypatch.setattr(cli, "dump_ledger", lambda *a, **k: written.append((a, k)))
        _scripted(
            monkeypatch,
            [
                _result(
                    errors=(ProbeError(scenario="s", tool="get_consumer_lag", detail="HTTP 502"),),
                    compared=(("other", "get_redis_health"),),
                )
            ],
        )

        assert cli.main(["--bless"]) == 2
        assert written == [], "the ledger was rewritten from an incomplete run"
        assert LEDGER_PATH.read_bytes() == before
        assert "refusing to bless" in capsys.readouterr().err

    def test_bless_proceeds_when_every_fixture_was_probed(
        self, monkeypatch: pytest.MonkeyPatch, platform_env: None
    ) -> None:
        before = LEDGER_PATH.read_bytes()
        written: list[Any] = []

        def fake_dump(drifts: Any, path: Any = None, **kwargs: Any) -> int:
            written.append((tuple(drifts), kwargs))
            return len(tuple(drifts))

        monkeypatch.setattr(cli, "dump_ledger", fake_dump)
        drift = Drift(scenario="s", tool="get_consumer_lag", path="lag", kind="value")
        _scripted(monkeypatch, [_result(drifts=(drift,), compared=(("s", "get_consumer_lag"),))])

        assert cli.main(["--bless"]) == 0
        assert len(written) == 1
        # The coverage the run actually established is what licenses a
        # deletion, so it has to reach the writer.
        assert written[0][1]["checked"] == (("s", "get_consumer_lag"),)
        assert LEDGER_PATH.read_bytes() == before


class TestNotFreshHoldsNoOpinion:
    """``--not-fresh`` is the stale-volume twin of the probe-error refusal.

    The refusal above covers a fixture the run could not read. This covers
    one it read against a world that is not a fresh seed, where the reading
    is a true statement about the developer's volume and a false one about
    the fixture — the case the ledger's own ``_blessed_against`` note
    describes and nothing enforced.

    The concrete instance: `failed_traces_scan` probes
    ``search_traces(status="failed", since_hours=1)``. A stack up for more
    than an hour returns nothing; CI's freshly seeded contract job returns
    the seeded rows. Blessing that reading writes an entry CI never
    observes, and the ratchet fails on entries no longer observed — so a
    naive bless from a stale volume turns CI red in the *opposite*
    direction, which is the failure mode this flag exists to prevent.
    """

    @staticmethod
    def _dump_spy(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
        written: list[Any] = []

        def fake_dump(drifts: Any, path: Any = None, **kwargs: Any) -> int:
            written.append((tuple(drifts), kwargs))
            return len(tuple(drifts))

        monkeypatch.setattr(cli, "dump_ledger", fake_dump)
        return written

    def test_a_not_fresh_fixture_is_neither_written_nor_disproved(
        self, monkeypatch: pytest.MonkeyPatch, platform_env: None
    ) -> None:
        written = self._dump_spy(monkeypatch)
        keep = Drift(scenario="keep", tool="list_dlq_messages", path="total", kind="value")
        stale = Drift(scenario="aged", tool="search_traces", path="matches[]", kind="no_live_rows")
        _scripted(
            monkeypatch,
            [
                _result(
                    drifts=(keep, stale),
                    compared=(("keep", "list_dlq_messages"), ("aged", "search_traces")),
                )
            ],
        )

        assert cli.main(["--bless", "--not-fresh", "aged:search_traces"]) == 0
        ((drifts, kwargs),) = written
        # Its drift is not written...
        assert drifts == (keep,)
        # ...and its coverage is withdrawn, so `split_for_bless` carries any
        # existing entry for it rather than deleting one this run cannot
        # speak to. Withdrawing only the drift would have been worse than
        # doing nothing: the entry would be silently disproved.
        assert kwargs["checked"] == (("keep", "list_dlq_messages"),)

    def test_naming_a_fixture_the_run_never_compared_is_refused(
        self,
        monkeypatch: pytest.MonkeyPatch,
        platform_env: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # A typo would otherwise silently bless the very row it was meant to
        # hold back — the flag would look applied and do nothing.
        written = self._dump_spy(monkeypatch)
        drift = Drift(scenario="aged", tool="search_traces", path="matches[]", kind="no_live_rows")
        _scripted(monkeypatch, [_result(drifts=(drift,), compared=(("aged", "search_traces"),))])

        assert cli.main(["--bless", "--not-fresh", "aged:serch_traces"]) == 2
        assert written == []
        assert "did not compare" in capsys.readouterr().err

    def test_a_malformed_pair_is_rejected(self) -> None:
        with pytest.raises(SystemExit, match="SCENARIO:TOOL"):
            cli._parse_pairs(["no-colon-here"])

    def test_the_flag_does_nothing_outside_bless(
        self, monkeypatch: pytest.MonkeyPatch, platform_env: None
    ) -> None:
        """Reporting is not blessing: the check must still SEE the row.

        Silencing it in the report as well would hide a genuine drift on a
        fresh stack from anyone who happened to pass the flag.
        """
        drift = Drift(scenario="aged", tool="search_traces", path="matches[]", kind="no_live_rows")
        _scripted(monkeypatch, [_result(drifts=(drift,), compared=(("aged", "search_traces"),))])

        assert cli.main(["--not-fresh", "aged:search_traces"]) == 1

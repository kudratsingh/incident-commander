"""A probe the lab makes on a service account's token says so (platform v0.6.17).

Demo finding F4: the eval runner's principal-guard probes and the world audit's reads
deliberately wear a service account's token, so the platform recorded them as
``agent.tool_invoked`` and the ``/demo`` page read seven of them, after the reset
boundary, as a run nobody made. Platform ADR 0038 answers it with ``_lab_probe``
beside ``arguments`` plus ``X-Lab-Principal``; this file pins the commander's half
(WO-R3-335): the field is sent where the platform reads it, only with the lab's own
credential, never on the agent's own calls, and a refusal is loud.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

import httpx
import pytest
from pydantic import SecretStr

from evals import runner
from evals import world_audit as audit
from evals.guards import (
    _CHAOS_PROBE_TOOL,
    _PROBE_TOOL,
    AuditWindowScan,
    PrincipalGuardError,
    assert_chaos_blind_principal,
    assert_chaos_capable_principal,
    assert_read_only_principal,
    assert_write_capable_principal,
)
from evals.preconditions import probe_label
from evals.runner import _eval_defaults, _lab_probe_credential
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario
from incident_commander.tools.mcp_client import (
    LAB_PRINCIPAL_HEADER,
    LAB_PROBE_PARAM,
    LAB_PROBE_REASON_MAX_CHARS,
    LabProbeClient,
    LabProbeRefused,
    MCPClient,
    MCPClientProtocol,
    MCPError,
    ToolResult,
)

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_SRC: Final[Path] = _REPO_ROOT / "src" / "incident_commander"

#: The lab credential the fake platform will honour, and one it will not.
_LAB_TOKEN: Final[str] = "chaos-or-smoke-token"
_AGENT_TOKEN: Final[str] = "agent-token"

_WORLD: Final[dict[str, Any]] = {
    "list_dlq_messages": {
        "total": 4,
        "items": [
            {"id": f"row-{i}", "remediation_hint": "replay_safe", "fenced_at": None}
            for i in range(4)
        ],
    },
    "list_active_alerts": {"total": 3},
    "get_consumer_lag": {"lag": 0, "lag_known": True},
    "get_cache_key_info": {"exists": True, "size": 120},
    "get_dag_state": {"paused": False, "nodes": [{"status": "completed"}]},
    "search_traces": {
        "matches": [
            {"trace_id": "0e24ca29-1d47-57e9-b898-4d79bb6da981"},
            {"trace_id": "edeeb994-56d2-53e6-88fd-8af47e695dbc"},
        ]
    },
    "list_audit_events": {"total": 0, "events": []},
}


class _FakePlatform:
    """The smallest platform that honours ``_lab_probe`` the way v0.6.17 does.

    It is a transport rather than a client fake on purpose: what F4 needs proved is the
    REQUEST — which envelope key the reason travels under and which header carries the
    credential — and only the real client's own bytes can show that.
    """

    def __init__(
        self,
        *,
        payloads: Mapping[str, Any] = _WORLD,
        refusals: Mapping[str, tuple[int, str]] | None = None,
        lab_credentials: frozenset[str] = frozenset({_LAB_TOKEN}),
    ) -> None:
        self._payloads = payloads
        self._refusals = dict(refusals or {})
        self._lab_credentials = lab_credentials
        #: One entry per call the platform SERVED or refused — the audit log it writes.
        self.rows: list[dict[str, Any]] = []
        #: The raw request as it arrived: params, arguments, headers.
        self.requests: list[dict[str, Any]] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def actions(self) -> list[str]:
        return [row["action"] for row in self.rows]

    def reasons(self) -> list[str | None]:
        return [row.get("lab_probe_reason") for row in self.rows]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        params = dict(body["params"])
        name = str(params.get("name", ""))
        arguments = dict(params.get("arguments", {}))
        reason = params.get(LAB_PROBE_PARAM)
        self.requests.append(
            {
                "params": params,
                "arguments": arguments,
                "lab_principal_header": request.headers.get(LAB_PRINCIPAL_HEADER),
                "authorization": request.headers.get("Authorization"),
            }
        )
        if reason is not None:
            refusal = self._refuse_label(reason, request.headers.get(LAB_PRINCIPAL_HEADER))
            if refusal is not None:
                # The call does not run and nothing is recorded against the agent.
                return self._error(body["id"], refusal)
            self.rows.append({"action": "lab.probe", "tool": name, "lab_probe_reason": reason})
        else:
            self.rows.append({"action": "agent.tool_invoked", "tool": name})
        if name in self._refusals:
            code, message = self._refusals[name]
            return self._error(body["id"], {"code": code, "message": message})
        payload = self._payloads.get(name, {})
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"content": [{"type": "text", "text": json.dumps(payload)}]},
            },
        )

    def _refuse_label(self, reason: object, header: str | None) -> dict[str, Any] | None:
        """The platform's own rules, in its own order (platform ADR 0038)."""
        if header is None:
            return self._lab_refusal("credential_missing")
        if not header.startswith("Bearer "):
            return self._lab_refusal("credential_invalid")
        token = header.removeprefix("Bearer ")
        if token not in self._lab_credentials:
            return self._lab_refusal("credential_not_authorised")
        if not isinstance(reason, str) or not 1 <= len(reason) <= LAB_PROBE_REASON_MAX_CHARS:
            return self._lab_refusal("reason_invalid")
        return None

    @staticmethod
    def _lab_refusal(reason_code: str) -> dict[str, Any]:
        return {
            "code": -32602,
            "message": "lab probe refused: the label needs the lab's own credential",
            "data": {"error_code": "lab_probe_refused", "reason_code": reason_code},
        }

    @staticmethod
    def _error(call_id: object, error: Mapping[str, Any]) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": call_id, "error": dict(error)})


def _client(platform: _FakePlatform, token: str = _AGENT_TOKEN) -> MCPClient:
    return MCPClient(
        base_url="http://platform.test/mcp",
        token=token,
        transport=platform.transport,
        sleep=lambda _seconds: None,
    )


def _now() -> datetime:
    return datetime.now(UTC)


class _Recorder:
    """A client fake that records the lab label of every call it is asked to make."""

    def __init__(
        self, behavior: ToolResult | Exception | dict[str, ToolResult | Exception]
    ) -> None:
        self._behavior = behavior
        self.calls: list[str] = []
        self.labels: list[tuple[str | None, str | None]] = []

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
        lab_probe: str | None = None,
        lab_principal_token: str | None = None,
    ) -> ToolResult:
        self.calls.append(name)
        self.labels.append((lab_probe, lab_principal_token))
        behavior = self._behavior[name] if isinstance(self._behavior, dict) else self._behavior
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


class TestTheWorldAuditsReadsAreTheLabs:
    """The red-before test of WO-R3-335, against a platform that honours the field."""

    @pytest.fixture
    def platform(self, monkeypatch: pytest.MonkeyPatch) -> _FakePlatform:
        # The guard's Tier-1 probe must be refused ON SCOPE for the read-only guard to
        # pass — the same answer the real platform gives the smoke token.
        platform = _FakePlatform(
            refusals={_PROBE_TOOL: (-32002, "missing required scope: actions:execute")},
            lab_credentials=frozenset({"smoke-token"}),
        )
        settings = _eval_defaults().model_copy(
            update={"platform_smoke_token": SecretStr("smoke-token")}
        )
        monkeypatch.setattr(audit, "Settings", lambda: settings)
        monkeypatch.setattr(
            audit,
            "make_client",
            lambda _settings, *, token: _client(platform, token=token),
        )
        monkeypatch.setattr(audit, "chaos_key_count", lambda: (0, "none"))
        monkeypatch.setattr(audit, "process_count", lambda: (0, "none"))
        return platform

    def test_every_read_lands_as_lab_probe_and_none_as_the_agents(
        self, platform: _FakePlatform, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert audit.main(["--roots", "root-a"]) == 0
        assert "WORLD AUDIT: PASS" in capsys.readouterr().out
        # Every call the audit made, the guard's own probe included.
        assert platform.rows, "the audit made no calls at all"
        assert set(platform.actions()) == {"lab.probe"}
        assert "agent.tool_invoked" not in platform.actions()
        assert audit.LAB_PROBE_REASON in platform.reasons()
        # The guard's probe carries its own reason, naming what it proves.
        guard_rows = [row for row in platform.rows if row["tool"] == _PROBE_TOOL]
        assert len(guard_rows) == 1
        assert guard_rows[0]["lab_probe_reason"].startswith("principal guard:")

    def test_the_reason_is_a_sibling_of_arguments_never_inside_it(
        self, platform: _FakePlatform
    ) -> None:
        assert audit.main(["--roots", "root-a"]) == 0
        for request in platform.requests:
            assert LAB_PROBE_PARAM in request["params"]
            # Inside `arguments` it would reach a tool's input model and the agent's
            # prompt, which is the whole reason the platform put it on the envelope.
            assert LAB_PROBE_PARAM not in request["arguments"]

    def test_the_credential_travels_in_its_own_header_and_the_call_stays_the_smokes(
        self, platform: _FakePlatform
    ) -> None:
        assert audit.main(["--roots", "root-a"]) == 0
        for request in platform.requests:
            assert request["lab_principal_header"] == "Bearer smoke-token"
            # The Authorization header is untouched: the call IS the smoke account's,
            # and the label only says who asked for it.
            assert request["authorization"] == "Bearer smoke-token"


class TestARefusedLabelIsLoud:
    def test_a_refusal_raises_its_own_type_carrying_the_reason_code(self) -> None:
        platform = _FakePlatform(lab_credentials=frozenset())
        client = _client(platform)
        with pytest.raises(LabProbeRefused) as refused:
            client.call_tool(
                "get_consumer_lag",
                {"consumer_group": "worker-dispatcher"},
                lab_probe="world audit read",
                lab_principal_token=_AGENT_TOKEN,
            )
        assert refused.value.code == -32602
        assert refused.value.reason_code == "credential_not_authorised"
        # Its own type BECAUSE the code is -32602: the guards read that code as "the
        # scope check passed", so an undistinguished refusal would invert their verdict.
        assert isinstance(refused.value, MCPError)

    def test_a_refusal_is_never_retried(self) -> None:
        platform = _FakePlatform(lab_credentials=frozenset())
        client = _client(platform)
        with pytest.raises(LabProbeRefused):
            client.call_tool(
                "get_consumer_lag",
                {},
                lab_probe="world audit read",
                lab_principal_token=_AGENT_TOKEN,
            )
        assert len(platform.requests) == 1

    def test_an_argument_refusal_is_still_a_plain_mcp_error(self) -> None:
        platform = _FakePlatform(
            refusals={"get_consumer_lag": (-32602, "Invalid params: consumer_group")}
        )
        client = _client(platform)
        with pytest.raises(MCPError) as err:
            client.call_tool("get_consumer_lag", {})
        assert not isinstance(err.value, LabProbeRefused)

    @pytest.mark.parametrize(
        "guard",
        [assert_read_only_principal, assert_write_capable_principal, assert_chaos_blind_principal],
    )
    def test_a_guard_reports_the_refusal_rather_than_a_scope_verdict(self, guard: Any) -> None:
        # The dangerous misread: -32602 means "refused on arguments" to every guard, so
        # a refused label would report the OPPOSITE of the truth about the scope.
        platform = _FakePlatform(lab_credentials=frozenset())
        client = _client(platform)
        with pytest.raises(LabProbeRefused):
            guard(client, lab_principal_token=_LAB_TOKEN)

    def test_the_audit_window_scan_lets_the_refusal_through(self) -> None:
        platform = _FakePlatform(lab_credentials=frozenset())
        scan = AuditWindowScan(_now(), lab_principal_token=_LAB_TOKEN)
        with pytest.raises(LabProbeRefused):
            scan.checkpoint(_client(platform))


class TestTheRequestIsValidatedBeforeTheWire:
    @pytest.mark.parametrize(
        ("reason", "token"),
        [
            ("world audit read", None),
            (None, _LAB_TOKEN),
        ],
    )
    def test_one_without_the_other_is_a_request_bug(
        self, reason: str | None, token: str | None
    ) -> None:
        platform = _FakePlatform()
        client = _client(platform)
        with pytest.raises(ValueError, match="both lab_probe and lab_principal_token"):
            client.call_tool("get_consumer_lag", {}, lab_probe=reason, lab_principal_token=token)
        assert platform.requests == []

    def test_an_over_long_reason_is_refused_here_rather_than_there(self) -> None:
        platform = _FakePlatform()
        client = _client(platform)
        with pytest.raises(ValueError, match="1 to 200"):
            client.call_tool(
                "get_consumer_lag",
                {},
                lab_probe="x" * (LAB_PROBE_REASON_MAX_CHARS + 1),
                lab_principal_token=_LAB_TOKEN,
            )
        assert platform.requests == []

    def test_an_empty_reason_is_refused(self) -> None:
        client = _client(_FakePlatform())
        with pytest.raises(ValueError, match="1 to 200"):
            client.call_tool("get_consumer_lag", {}, lab_probe="", lab_principal_token=_LAB_TOKEN)

    def test_a_blank_credential_never_becomes_an_unlabelled_call(self) -> None:
        platform = _FakePlatform()
        client = _client(platform)
        with pytest.raises(ValueError, match="credential is empty"):
            client.call_tool(
                "get_consumer_lag", {}, lab_probe="world audit read", lab_principal_token="  "
            )
        assert platform.requests == []

    def test_the_wrapper_validates_its_pair_at_construction(self) -> None:
        with pytest.raises(ValueError, match="1 to 200"):
            LabProbeClient(_client(_FakePlatform()), reason="", principal_token=_LAB_TOKEN)


class TestTheAgentsOwnPathNeverLabels:
    def test_the_seam_the_orchestrator_holds_has_no_lab_parameter(self) -> None:
        parameters = inspect.signature(MCPClientProtocol.call_tool).parameters
        assert "lab_probe" not in parameters
        assert "lab_principal_token" not in parameters

    def test_an_ordinary_call_is_recorded_as_the_agents(self) -> None:
        platform = _FakePlatform()
        _client(platform).call_tool("get_consumer_lag", {"consumer_group": "worker-dispatcher"})
        assert platform.actions() == ["agent.tool_invoked"]
        assert platform.requests[0]["lab_principal_header"] is None
        assert LAB_PROBE_PARAM not in platform.requests[0]["params"]

    def test_a_label_does_not_stick_to_the_next_call_from_the_same_client(self) -> None:
        platform = _FakePlatform()
        client = _client(platform)
        client.call_tool(
            "get_consumer_lag", {}, lab_probe="world audit read", lab_principal_token=_LAB_TOKEN
        )
        client.call_tool("get_consumer_lag", {})
        assert platform.actions() == ["lab.probe", "agent.tool_invoked"]
        assert platform.requests[1]["lab_principal_header"] is None

    def test_no_module_the_agent_runs_through_mentions_the_label(self) -> None:
        # The label is the evaluator's vocabulary. `mcp_client.py` offers it; nothing
        # else under src/ may pass it, or the agent's own reads could relabel themselves.
        offenders = sorted(
            path.relative_to(_SRC).as_posix()
            for path in _SRC.rglob("*.py")
            if "lab_probe" in path.read_text() and path.name != "mcp_client.py"
        )
        assert offenders == []


class TestTheGuardsSayWhatTheyProve:
    """Each probe's reason is the sentence an operator reads in the audit stream."""

    @staticmethod
    def _fake(behavior: ToolResult | Exception | dict[str, ToolResult | Exception]) -> _Recorder:
        return _Recorder(behavior)

    def test_the_read_only_guard_labels_its_probe_with_the_lab_credential(self) -> None:
        client = self._fake(MCPError(-32002, "missing required scope: actions:execute"))
        assert_read_only_principal(client, lab_principal_token=_LAB_TOKEN)
        (reason, token) = client.labels[0]
        assert token == _LAB_TOKEN
        assert reason == "principal guard: proves this token cannot execute a Tier-1 action"

    def test_the_write_guard_labels_both_of_its_probes(self) -> None:
        client = self._fake(
            {
                _PROBE_TOOL: MCPError(-32602, "invalid tool arguments"),
                _CHAOS_PROBE_TOOL: MCPError(-32002, "missing required scope: chaos:invoke"),
            }
        )
        assert_write_capable_principal(client, lab_principal_token=_LAB_TOKEN)
        assert [token for _reason, token in client.labels] == [_LAB_TOKEN, _LAB_TOKEN]
        assert [reason for reason, _token in client.labels] == [
            "principal guard: proves the agent token can execute a Tier-1 action",
            "principal guard: proves the agent token cannot seed chaos",
        ]

    def test_the_chaos_guard_labels_the_evaluators_own_probe(self) -> None:
        client = self._fake(MCPError(-32602, "invalid tool arguments"))
        assert_chaos_capable_principal(client, lab_principal_token=_LAB_TOKEN)
        assert client.labels == [
            ("principal guard: proves the evaluator token can seed chaos", _LAB_TOKEN)
        ]

    def test_the_audit_window_scan_labels_its_page_read(self) -> None:
        page = ToolResult(
            content=[{"type": "text", "text": json.dumps(_WORLD["list_audit_events"])}]
        )
        client = _Recorder(page)
        AuditWindowScan(_now(), lab_principal_token=_LAB_TOKEN).checkpoint(client)
        assert client.labels == [
            ("principal guard: reads the audit window for Tier-1 successes", _LAB_TOKEN)
        ]

    def test_without_a_credential_nothing_is_labelled(self) -> None:
        # Not a fallback from a refusal — a caller that has no lab credential at all
        # (an offline fake, or a platform older than v0.6.17). The pair is all-or-nothing.
        client = self._fake(MCPError(-32002, "missing required scope: actions:execute"))
        assert_read_only_principal(client)
        assert client.labels == [(None, None)]

    def test_every_guard_reason_fits_the_platforms_bound(self) -> None:
        from evals import guards

        reasons = [
            guards._READ_ONLY_PROBE_REASON,
            guards._WRITE_PROBE_REASON,
            guards._CHAOS_BLIND_PROBE_REASON,
            guards._CHAOS_CAPABLE_PROBE_REASON,
            guards._AUDIT_SCAN_REASON,
        ]
        for reason in reasons:
            assert 1 <= len(reason) <= LAB_PROBE_REASON_MAX_CHARS
            assert reason.startswith("principal guard: ")

    def test_a_scope_verdict_still_fails_as_a_principal_guard_error(self) -> None:
        # The label changes nothing about what the guards decide.
        client = self._fake(MCPError(-32602, "Invalid params: job_id is not a valid UUID"))
        with pytest.raises(PrincipalGuardError, match="actions:execute"):
            assert_read_only_principal(client, lab_principal_token=_LAB_TOKEN)


class TestTheRunnersCredential:
    def test_the_evaluators_token_is_the_lab_credential(self) -> None:
        settings = _eval_defaults().model_copy(
            update={"platform_chaos_token": SecretStr("chaos-token")}
        )
        assert _lab_probe_credential(settings) == "chaos-token"

    @pytest.mark.parametrize("value", [None, SecretStr(""), SecretStr("   ")])
    def test_an_unset_or_blank_token_means_no_label(self, value: SecretStr | None) -> None:
        settings = _eval_defaults().model_copy(update={"platform_chaos_token": value})
        assert _lab_probe_credential(settings) is None


class TestThePremiseReadsAreTheLabs:
    """ADR 0075 (the fourth take's false warning): the precondition probes are the lab's.

    They go out on the AGENT's token on purpose — the premise has to be true of the world the
    agent will see — so without a label the platform writes them as ``agent.tool_invoked``. The
    console counted one such row against the four steps the run reported and printed "4 steps
    reported · 5 calls the platform recorded — the two do not agree". The fifth call was this.
    """

    @staticmethod
    def _scenario() -> Scenario:
        return next(
            s
            for s in load_scenarios(_REPO_ROOT / "evals" / "scenarios")
            if s.name == "remediate_consumer_lag_success"
        )

    @staticmethod
    def _lag(lag: int) -> ToolResult:
        return ToolResult(
            content=[
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "consumer_group": "worker-dispatcher",
                            "lag": lag,
                            "lag_known": True,
                            "source": "live",
                            "age_seconds": 3,
                        }
                    ),
                }
            ]
        )

    def test_every_premise_read_carries_the_labs_reason_and_credential(self) -> None:
        scenario = self._scenario()
        client = _Recorder(self._lag(15_000))

        runner._assert_preconditions(scenario, client, None, lab_principal_token=_LAB_TOKEN)

        assert client.labels, "no premise read was made"
        for reason, token in client.labels:
            assert token == _LAB_TOKEN
            assert reason is not None
            assert reason.startswith("precondition: ")
            assert 1 <= len(reason) <= LAB_PROBE_REASON_MAX_CHARS

    def test_the_reason_names_what_the_probe_proves(self) -> None:
        probe = next(
            p for p in self._scenario().expected_precondition if p.tool == "get_consumer_lag"
        )
        reason = probe_label(probe)
        assert reason.startswith("precondition: get_consumer_lag proves ")
        # From the probe's own expectations, never a written description that could drift.
        assert probe.expect[0].path in reason

    def test_a_reason_longer_than_the_platform_takes_is_cut_not_refused(self) -> None:
        probe = self._scenario().expected_precondition[0]
        wide = probe.model_copy(
            update={
                "expect": tuple(
                    field.model_copy(update={"path": field.path + "x" * 400})
                    for field in probe.expect
                )
            }
        )
        reason = probe_label(wide)
        assert len(reason) == LAB_PROBE_REASON_MAX_CHARS
        assert reason.endswith("…")

    def test_without_the_credential_the_reads_go_out_unlabelled_as_before(self) -> None:
        client = _Recorder(self._lag(15_000))

        runner._assert_preconditions(self._scenario(), client, None)

        assert client.labels == [(None, None)] * len(client.calls)

    def test_a_label_changes_nothing_about_whether_the_premise_holds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A healthy world still fails the premise, labelled or not. The shipped probe polls
        # ten times fifteen seconds apart, so the wait is stubbed the way
        # ``test_polling_window`` stubs it — the claim here is about the verdict.
        monkeypatch.setattr(runner, "time", SimpleNamespace(sleep=lambda _seconds: None))
        client = _Recorder(self._lag(0))
        with pytest.raises(runner.PreconditionNotMet):
            runner._assert_preconditions(
                self._scenario(), client, None, lab_principal_token=_LAB_TOKEN
            )

"""FastAPI ingress. Receives HMAC-signed alerts, spawns runs in a background task.

Wiring shape: create_app(settings) builds the engine, checkpointer, and a
per-request ``MCPClient``. The state machine's INVESTIGATING transition is
wired via ``make_investigate(client)`` per run so live tool calls happen with
a fresh HTTP client and the module-level TRANSITIONS registry stays untouched.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, status
from sqlalchemy import create_engine

from incident_commander.agent.briefing import render_briefing
from incident_commander.agent.factory import start_run
from incident_commander.agent.investigation import make_investigate
from incident_commander.agent.loop import run_to_completion
from incident_commander.agent.orchestrator import TRANSITIONS, Checkpointer, Transition
from incident_commander.agent.state import EvidenceEntry, IncidentState, RunState
from incident_commander.api.hmac_verify import verify
from incident_commander.api.schemas import AlertPayload, HealthResponse, IngestResponse
from incident_commander.config import Settings, get_settings
from incident_commander.persistence.postgres import PostgresCheckpointer
from incident_commander.tools.mcp_client import make_client

RunTask = Callable[[RunState, Settings, Checkpointer], None]

_log = logging.getLogger(__name__)

# Replay suppression (ADR 0014): remember the signature of each accepted
# delivery so an identical redelivery within the skew window returns 202
# without spawning a second investigation run. Process-local by design and
# lost on restart — durable single-flight/dedupe is the ADR-0002 lease work
# (finding B-05), not this cache.
_REPLAY_CACHE_MAX_ENTRIES: Final[int] = 1024
_replay_cache: dict[str, float] = {}


def _is_replay(signature_hex: str, now: float, window_seconds: float) -> bool:
    """True iff ``signature_hex`` was already accepted within the window.

    First sight records the signature and returns False. Expired entries are
    pruned on every call, and the cache stays capacity-bounded by evicting
    the oldest entry.
    """
    for key in [k for k, seen in _replay_cache.items() if now - seen > window_seconds]:
        del _replay_cache[key]
    if signature_hex in _replay_cache:
        return True
    if len(_replay_cache) >= _REPLAY_CACHE_MAX_ENTRIES:
        del _replay_cache[min(_replay_cache, key=_replay_cache.__getitem__)]
    _replay_cache[signature_hex] = now
    return False


def create_app(
    settings: Settings | None = None,
    checkpointer: Checkpointer | None = None,
    run_task: RunTask | None = None,
) -> FastAPI:
    """Build the FastAPI app. Tests inject ``checkpointer`` and ``run_task``."""
    resolved_settings = settings or get_settings()
    resolved_checkpointer = checkpointer or PostgresCheckpointer(
        create_engine(str(resolved_settings.database_url))
    )
    task: RunTask = run_task or _run_investigation

    app = FastAPI(title="Incident Commander", version="0.1.0")
    app.state.settings = resolved_settings
    app.state.checkpointer = resolved_checkpointer

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.post(
        "/alerts",
        response_model=IngestResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def ingest_alert(
        request: Request,
        background_tasks: BackgroundTasks,
    ) -> IngestResponse:
        body = await request.body()
        # The platform emitter signs into X-Alert-Signature (alerts.py); the
        # legacy X-Signature-256 name is kept as a fallback for pre-fix tools.
        signature = request.headers.get("X-Alert-Signature") or request.headers.get(
            "X-Signature-256", ""
        )
        if not verify(
            body,
            signature,
            resolved_settings.platform_webhook_secret.get_secret_value(),
        ):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing signature")

        # X-Alert-Timestamp is epoch MILLISECONDS platform-side. It is not
        # part of the v1 signed material, so this skew check only bounds the
        # replay window — it does not close it (ADR 0014).
        timestamp_header = request.headers.get("X-Alert-Timestamp")
        now = time.time()
        if timestamp_header is not None:
            try:
                timestamp_ms = int(timestamp_header)
            except ValueError as err:
                raise HTTPException(
                    status.HTTP_401_UNAUTHORIZED, "non-numeric X-Alert-Timestamp"
                ) from err
            if abs(now - timestamp_ms / 1000) > resolved_settings.webhook_max_skew_seconds:
                raise HTTPException(
                    status.HTTP_401_UNAUTHORIZED,
                    "X-Alert-Timestamp outside the accepted skew window",
                )

        signature_hex = signature.removeprefix("sha256=")
        if _is_replay(signature_hex, now, float(resolved_settings.webhook_max_skew_seconds)):
            # 202, not 4xx: the platform emitter treats any >=400 as delivery
            # failure and would log/retry, so legitimate at-least-once
            # redelivery must look accepted. No run is spawned; the id is
            # synthetic (the emitter only reads the status code).
            _log.warning(
                "suppressed replayed webhook delivery (signature %s...); no run spawned",
                signature_hex[:12],
            )
            return IngestResponse(incident_id=uuid4())

        try:
            payload = AlertPayload.model_validate_json(body)
        except ValueError as err:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"malformed alert payload: {err}"
            ) from err

        run = start_run(payload.model_dump(), resolved_settings, datetime.now(UTC))

        # Durability before acknowledgement (finding B-04): the TRIAGE row
        # exists before the 202, so a process death between here and the
        # background task still leaves a record of the alert. Enabled or
        # killed, the write is the same — the kill switch below only skips
        # spawning the run. ``run_to_completion`` writes its own TRIAGE
        # snapshot on entry, so the append-only log normally carries two
        # identical TRIAGE rows (version+1); that is benign and cheaper than
        # plumbing a skip flag through the loop.
        #
        # Fail-open: a checkpoint failure is logged, never surfaced as a 5xx.
        # The run store being down must not turn webhook ingestion into
        # delivery failures during exactly the kind of incident that took it
        # down, and paging runs through the platform's oncall route
        # regardless of this endpoint.
        try:
            resolved_checkpointer.write(run)
        except Exception:
            _log.exception(
                "checkpoint write failed for incident %s (AGENT_ENABLED=%s); "
                "acknowledging delivery without a durable record",
                run.incident_id,
                str(resolved_settings.agent_enabled).lower(),
            )

        if not resolved_settings.agent_enabled:
            # Kill switch (docs/safety-model.md#kill-switch): the alert is
            # recorded above as a TRIAGE-state run so nothing is lost, but no
            # investigation is spawned — the state machine never advances.
            _log.warning(
                "kill switch active (AGENT_ENABLED=false): recorded incident %s "
                "in TRIAGE; no investigation run spawned",
                run.incident_id,
            )
            return IngestResponse(incident_id=run.incident_id)

        background_tasks.add_task(task, run, resolved_settings, resolved_checkpointer)
        return IngestResponse(incident_id=run.incident_id)

    return app


def _run_investigation(
    run: RunState,
    settings: Settings,
    checkpointer: Checkpointer,
) -> None:
    """Background task: wire a per-run MCP client, run the state machine.

    Failure rail (finding B-04): without the ``except`` below, any exception
    here — a checkpointer failure with Postgres down is the realistic vector —
    kills the task silently, leaving the incident stranded at whatever
    non-terminal checkpoint it reached with nothing recording why. Every exit
    path now leaves a terminal snapshot behind.
    """
    try:
        with make_client(settings) as client:
            transitions: dict[IncidentState, Transition] = dict(TRANSITIONS)
            transitions[IncidentState.INVESTIGATING] = make_investigate(client)
            final = run_to_completion(
                run,
                clock=lambda: datetime.now(UTC),
                checkpointer=checkpointer,
                transitions=transitions,
            )
    except Exception as exc:
        _record_run_failure(run, checkpointer, exc)
        # Re-raise the ORIGINAL exception: starlette runs background tasks
        # after the response, so this surfaces in the server log with its
        # stack intact. Swallowing it would hide the crash class this rail
        # exists to record.
        raise
    else:
        _log_briefing(final)


def _record_run_failure(run: RunState, checkpointer: Checkpointer, exc: Exception) -> None:
    """Best-effort terminal FAILED checkpoint for a crashed run.

    Written directly via ``checkpointer.write``, not ``dispatch``: this is a
    crash rail, not a state-machine transition (``ALLOWED_TRANSITIONS`` already
    lists FAILED as a successor of every non-terminal state).

    The crashed run's latest state is not the local ``run`` — the loop
    checkpoints progress internally and returns nothing on raise — so recover
    it via ``load`` and fall back to ``run``. The realistic crash vector IS the
    checkpointer, so the whole rail is best-effort: if it cannot write, it logs
    and lets the original exception carry the story. Recovery/resume of a
    stranded run is separate, structural work (ADR 0002 lease); this only
    guarantees a terminal record exists.
    """
    try:
        latest = checkpointer.load(run.incident_id) or run
        if latest.state.is_terminal:
            # The run already reached its outcome; a crash afterwards must not
            # overwrite a real RESOLVED/ESCALATED record with FAILED.
            return
        at = datetime.now(UTC)
        checkpointer.write(
            latest.model_copy(
                update={
                    "state": IncidentState.FAILED,
                    "updated_at": at,
                    "evidence": (
                        *latest.evidence,
                        EvidenceEntry(
                            tool_name="_run_failure",
                            arguments={"error_type": type(exc).__name__},
                            result_summary=str(exc)[:500],
                            timestamp=at,
                        ),
                    ),
                }
            )
        )
    except Exception:
        _log.exception(
            "failure rail could not record a terminal FAILED checkpoint for incident %s",
            run.incident_id,
        )


def _log_briefing(final: RunState) -> None:
    """Emit the invariant-7 handoff artifact on the service path.

    ``render_briefing`` had exactly one caller (``evals/runner.py``), so a real
    incident never produced the artifact a human is supposed to be handed.
    Logging it is the minimal producer; durable briefing storage (a table, an
    API surface) is future design. Rendering must never break the rail — the
    run already reached its terminal state, so a briefing failure is logged,
    not propagated.
    """
    try:
        _log.info(
            "incident terminal",
            extra={
                "incident_id": str(final.incident_id),
                "final_state": final.state.value,
                "briefing": render_briefing(final).model_dump_json(),
            },
        )
    except Exception:
        _log.exception(
            "could not render the escalation briefing for incident %s", final.incident_id
        )

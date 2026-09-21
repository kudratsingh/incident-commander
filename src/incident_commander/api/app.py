"""FastAPI ingress. Receives HMAC-signed alerts, spawns runs in a background task.

``make_investigate(client)`` wires INVESTIGATING per run; the module-level
TRANSITIONS registry is untouched.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Final, Literal
from uuid import UUID, uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response, status
from sqlalchemy import Engine
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from incident_commander.agent.briefing import render_briefing
from incident_commander.agent.factory import derive_incident_id, start_run
from incident_commander.agent.investigation import make_investigate
from incident_commander.agent.loop import run_to_completion
from incident_commander.agent.orchestrator import TRANSITIONS, Checkpointer, Transition
from incident_commander.agent.state import EvidenceEntry, IncidentState, RunState
from incident_commander.api.hmac_verify import verify, verify_delivery
from incident_commander.api.schemas import AlertPayload, HealthResponse, IngestResponse
from incident_commander.config import Settings, get_settings
from incident_commander.persistence.lease import incident_lease
from incident_commander.persistence.pool import RunSlots, create_pooled_engine
from incident_commander.persistence.postgres import PostgresCheckpointer
from incident_commander.tools.mcp_client import make_client

RunTask = Callable[[RunState, Settings, Checkpointer], None]

# Named rather than a bool: duplicate delivery and a full agent are different refusals.
Admission = Literal["admitted", "at_capacity", "lease_lost"]

_log = logging.getLogger(__name__)

# Replay suppression (ADR 0014, ADR 0023). A repeated legacy signature may be honest
# redelivery (suppressed quietly); a repeated nonce is a replay (refused). Process-local.
_REPLAY_CACHE_MAX_ENTRIES: Final[int] = 1024
_replay_cache: dict[str, float] = {}
_nonce_cache: dict[str, float] = {}


def _is_replay(cache: dict[str, float], key: str, now: float, window_seconds: float) -> bool:
    """True iff ``key`` was seen in ``cache`` within the window; first sight records it."""
    for stale in [k for k, seen in cache.items() if now - seen > window_seconds]:
        del cache[stale]
    if key in cache:
        return True
    if len(cache) >= _REPLAY_CACHE_MAX_ENTRIES:
        del cache[min(cache, key=cache.__getitem__)]
    cache[key] = now
    return False


class _BodyTooLargeError(Exception):
    """Raised inside the receive wrapper when a streamed body passes the cap."""


class BodySizeLimitMiddleware:
    """Refuse an over-length request body ahead of every route (WO-R2-86).

    ``/alerts`` must buffer before it can authenticate, so the cap belongs ahead of the
    route. Two gates: a declared ``Content-Length``, and a streamed body counted as it comes.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = _declared_content_length(scope)
        if declared is not None and declared > self.max_bytes:
            await self._refuse(scope, receive, send, declared)
            return

        received = 0
        responding = False

        async def counted_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLargeError
            return message

        async def watched_send(message: Message) -> None:
            nonlocal responding
            if message["type"] == "http.response.start":
                responding = True
            await send(message)

        try:
            await self.app(scope, counted_receive, watched_send)
        except _BodyTooLargeError:
            if responding:
                # A status line is already on the wire; a 413 is no longer honest.
                raise
            await self._refuse(scope, receive, send, received)

    async def _refuse(self, scope: Scope, receive: Receive, send: Send, size: int) -> None:
        _log.warning(
            "refused a %d-byte request body to %s (cap %d bytes)",
            size,
            scope.get("path", "?"),
            self.max_bytes,
        )
        response = JSONResponse(
            {"detail": f"request body exceeds the {self.max_bytes}-byte cap"},
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        )
        await response(scope, receive, send)


def _declared_content_length(scope: Scope) -> int | None:
    """The request's declared body size, or ``None`` if absent or unparseable.

    ``None`` only means this gate abstains; the streaming count below still holds the line.
    """
    for name, value in scope.get("headers", ()):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


# An id no run will ever carry, so the probe does not depend on the store's contents.
_HEALTH_PROBE_INCIDENT_ID: Final[UUID] = UUID("00000000-0000-0000-0000-000000000000")


async def _probe_datastore(checkpointer: Checkpointer, timeout_seconds: float) -> str | None:
    """``None`` if the run store answered; a short reason if it did not.

    Bounded — ``DB_POOL_TIMEOUT_SECONDS`` is not a wait a health check inherits.
    An exception TYPE, never its message: connection errors carry DSN credentials.
    """
    try:
        await asyncio.wait_for(
            run_in_threadpool(checkpointer.load, _HEALTH_PROBE_INCIDENT_ID),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        _log.warning("health probe of the run store did not answer in %ss", timeout_seconds)
        return f"no answer within {timeout_seconds}s"
    except Exception as err:  # noqa: BLE001 - any failure to read is a failure to serve
        _log.warning("health probe of the run store failed: %s", type(err).__name__)
        return f"probe failed: {type(err).__name__}"
    return None


def create_app(
    settings: Settings | None = None,
    checkpointer: Checkpointer | None = None,
    run_task: RunTask | None = None,
) -> FastAPI:
    """Build the FastAPI app. Tests inject ``checkpointer`` and ``run_task``."""
    resolved_settings = settings or get_settings()
    # Kept because the single-flight lease (ADR 0016) needs a real connection.
    engine: Engine | None = None
    slots: RunSlots | None = None
    if checkpointer is None:
        engine = create_pooled_engine(resolved_settings)
        resolved_checkpointer: Checkpointer = PostgresCheckpointer(engine)
        # Admission bound (ADR 0022): more live runs than the pool can serve is a deadlock.
        slots = RunSlots(resolved_settings.max_concurrent_runs)
    else:
        resolved_checkpointer = checkpointer

    def investigate(run: RunState, run_settings: Settings, run_checkpointer: Checkpointer) -> None:
        """Run one incident to completion on this app's own engine and admission slots."""
        _run_investigation(run, run_settings, run_checkpointer, engine=engine, slots=slots)

    task: RunTask = run_task or investigate

    app = FastAPI(title="Incident Commander", version="0.1.0")
    app.state.settings = resolved_settings
    app.state.checkpointer = resolved_checkpointer
    app.state.engine = engine
    app.state.run_slots = slots
    # Outermost middleware, so the cap is enforced before any per-request machinery runs.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=resolved_settings.webhook_max_body_bytes)

    @app.get("/health", response_model=HealthResponse)
    async def health(response: Response) -> HealthResponse:
        """Liveness AND datastore readiness.

        An agent whose run store is unreachable accepts alerts and loses them.
        Degraded is a 503, not a 200 with a sad field: probes read status codes.
        """
        reason = await _probe_datastore(
            resolved_checkpointer, resolved_settings.health_probe_timeout_seconds
        )
        if reason is None:
            return HealthResponse(status="ok", details={"datastore": "ok"})
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(status="degraded", details={"datastore": reason})

    @app.post(
        "/alerts",
        response_model=IngestResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def ingest_alert(
        request: Request,
        background_tasks: BackgroundTasks,
    ) -> IngestResponse:
        """Accept a signed alert from the platform and start a run for it.

        Signature first; replays dropped; recorded before the 202.
        """
        body = await request.body()
        # The platform emitter signs into X-Alert-Signature (alerts.py); the
        # legacy X-Signature-256 name is kept as a fallback for pre-fix tools.
        signature = request.headers.get("X-Alert-Signature") or request.headers.get(
            "X-Signature-256", ""
        )
        # X-Alert-Timestamp is epoch MILLISECONDS platform-side.
        timestamp_header = request.headers.get("X-Alert-Timestamp")
        # The scheme selector (ADR 0023): the prefix is `sha256=` in both schemes.
        nonce_header = request.headers.get("X-Alert-Nonce")
        secret = resolved_settings.platform_webhook_secret.get_secret_value()
        skew_seconds = resolved_settings.webhook_max_skew_seconds
        now = time.time()

        if nonce_header is not None:
            # Nonce-bound scheme: the MAC covers {timestamp}.{nonce}.{body}.
            if timestamp_header is None:
                # The timestamp is inside the MAC here: nothing to verify without it.
                raise HTTPException(
                    status.HTTP_401_UNAUTHORIZED,
                    "X-Alert-Nonce present without X-Alert-Timestamp",
                )
            if not verify_delivery(body, timestamp_header, nonce_header, signature, secret):
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing signature")
            # Signed timestamp, so this genuinely bounds replay, not just the honest case.
            _reject_unless_within_skew(timestamp_header, now, skew_seconds)
            # Twice the skew window: acceptance spans [stamp - skew, stamp + skew]. After the
            # MAC, so an unauthenticated caller cannot poison the cache.
            if _is_replay(_nonce_cache, nonce_header, now, float(skew_seconds) * 2):
                _log.warning(
                    "refused replayed webhook delivery (nonce %s); no run spawned",
                    nonce_header[:16],
                )
                # 401, unlike the legacy path: a nonce is per delivery, so a repeat is a replay.
                raise HTTPException(
                    status.HTTP_401_UNAUTHORIZED, "replayed delivery: nonce already seen"
                )
        else:
            # Legacy body-only scheme: the pinned platform image, plus pre-fix tooling.
            if not verify(body, signature, secret):
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing signature")

            # Not part of the legacy signed material, so this skew check only
            # bounds the replay window — it does not close it (ADR 0014).
            if timestamp_header is not None:
                _reject_unless_within_skew(timestamp_header, now, skew_seconds)

            signature_hex = signature.removeprefix("sha256=")
            if _is_replay(_replay_cache, signature_hex, now, float(skew_seconds)):
                # 202, not 4xx: the emitter retries anything >=400, so honest
                # redelivery must look accepted. No run; the id is synthetic.
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

        alert = payload.model_dump()
        # Durable identity (ADR 0016): a redelivery of the same (source, fingerprint) joins
        # the live run; fail-open to a fresh id. In the threadpool because the derivation does
        # up to 64 blocking loads, which on the event loop would stop /health (ADR 0022).
        try:
            incident_id = await run_in_threadpool(derive_incident_id, alert, resolved_checkpointer)
        except Exception:
            _log.exception(
                "incident identity derivation failed; falling back to a fresh id "
                "(redeliveries of this alert will not dedupe)"
            )
            incident_id = uuid4()

        run = start_run(alert, resolved_settings, datetime.now(UTC), incident_id=incident_id)

        # Durability before acknowledgement (B-04): the TRIAGE row exists before the 202.
        # Conditional on there being no snapshot yet — a fresh TRIAGE row on top of an
        # in-flight run would hand the resume path a state stripped of evidence (ADR 0016).
        try:
            await run_in_threadpool(_write_ingress_checkpoint, resolved_checkpointer, run)
        except Exception:
            _log.exception(
                "checkpoint write failed for incident %s (AGENT_ENABLED=%s); "
                "acknowledging delivery without a durable record",
                run.incident_id,
                str(resolved_settings.agent_enabled).lower(),
            )

        if not resolved_settings.agent_enabled:
            # Kill switch (docs/safety-model.md#kill-switch): recorded at TRIAGE, not run.
            _log.warning(
                "kill switch active (AGENT_ENABLED=false): recorded incident %s "
                "in TRIAGE; no investigation run spawned",
                run.incident_id,
            )
            return IngestResponse(incident_id=run.incident_id)

        # Every accepted delivery spawns a task; the lease, not the ingress, decides who runs.
        background_tasks.add_task(task, run, resolved_settings, resolved_checkpointer)
        return IngestResponse(incident_id=run.incident_id)

    return app


def _reject_unless_within_skew(header: str, now: float, max_skew_seconds: int) -> None:
    """401 unless ``header`` (epoch MILLISECONDS) is inside the skew window.

    Integer milliseconds, and the guard spans the parse AND the compare: ``10**400`` raises
    ``OverflowError``, which outside the guard was a 500 from one header.
    """
    try:
        timestamp_ms = int(header)
        outside_window = abs(int(now * 1000) - timestamp_ms) > max_skew_seconds * 1000
    except (ValueError, OverflowError) as err:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unusable X-Alert-Timestamp") from err
    if outside_window:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "X-Alert-Timestamp outside the accepted skew window",
        )


def _write_ingress_checkpoint(checkpointer: Checkpointer, run: RunState) -> None:
    """ADR 0016's conditional ingress write, as one threadpool-callable unit."""
    if checkpointer.load(run.incident_id) is None:
        checkpointer.write(run)


@contextmanager
def _admission(
    slots: RunSlots | None, engine: Engine | None, incident_id: UUID
) -> Iterator[Admission]:
    """Take a run slot, then the single-flight lease. Hold both for the body.

    Order is the point (ADR 0022): the lease pins a pooled connection, so a
    capacity refusal must happen BEFORE one is held. Both are non-blocking.
    """
    with _run_admission(slots) as admitted:
        if not admitted:
            yield "at_capacity"
            return
        with _single_flight(engine, incident_id) as acquired:
            yield "admitted" if acquired else "lease_lost"


@contextmanager
def _run_admission(slots: RunSlots | None) -> Iterator[bool]:
    """``RunSlots.acquire``, or unbounded admission when no pool is wired.

    Same reason as ``_single_flight``: no pool to protect.
    """
    if slots is None:
        yield True
        return
    with slots.acquire() as admitted:
        yield admitted


@contextmanager
def _single_flight(engine: Engine | None, incident_id: UUID) -> Iterator[bool]:
    """``incident_lease``, or an always-granted lease when no engine is wired.

    Only the injected-checkpointer wiring (single-process) has no engine.
    """
    if engine is None:
        yield True
        return
    with incident_lease(engine, incident_id) as acquired:
        yield acquired


def _run_investigation(
    run: RunState,
    settings: Settings,
    checkpointer: Checkpointer,
    *,
    engine: Engine | None = None,
    slots: RunSlots | None = None,
) -> None:
    """Background task: take a run slot and the lease, resume or start, run.

    Single-flight + resume (ADR 0016); admission sheds above the bound (ADR 0022).
    The crash rail (finding B-04) writes FAILED only under ``held_lease``.
    """
    held_lease = False
    try:
        with _admission(slots, engine, run.incident_id) as verdict:
            if verdict == "at_capacity":
                _shed_at_capacity(run, slots)
                return
            if verdict == "lease_lost":
                _log.info(
                    "another worker holds the single-flight lease for incident %s; "
                    "not starting a second run",
                    run.incident_id,
                )
                return

            # Past this line the lease is ours — a crash here is ours to record.
            held_lease = True
            latest = checkpointer.load(run.incident_id)
            if latest is None:
                resuming = run
            elif latest.state.is_terminal:
                # Terminal is done. Resuming FAILED would arm a retry loop on
                # redelivery; a recurrence opens a new incident instead.
                _log.info(
                    "incident %s already reached terminal state %s; not resuming",
                    run.incident_id,
                    latest.state.value,
                )
                return
            elif latest.state is IncidentState.AWAITING_APPROVAL:
                # Tier-2 resume is out of scope (ADR 0016): the stub transition
                # would raise and the rail would turn a waiting incident FAILED.
                _log.info(
                    "incident %s is awaiting_approval; approval-bound resume is not "
                    "implemented, leaving it untouched",
                    run.incident_id,
                )
                return
            else:
                resuming = latest
                if latest.state is not IncidentState.TRIAGE:
                    # Not TRIAGE, so this is a real crash-resume worth announcing.
                    _log.info(
                        "resuming incident %s from its %s checkpoint",
                        run.incident_id,
                        latest.state.value,
                    )

            with make_client(settings) as client:
                transitions: dict[IncidentState, Transition] = dict(TRANSITIONS)
                transitions[IncidentState.INVESTIGATING] = make_investigate(client)
                final = run_to_completion(
                    resuming,
                    clock=lambda: datetime.now(UTC),
                    checkpointer=checkpointer,
                    transitions=transitions,
                )
    except Exception as exc:
        _record_run_failure(run, checkpointer, exc, held_lease=held_lease)
        # Re-raise the ORIGINAL exception so it reaches the server log with its stack.
        raise
    else:
        _log_briefing(final)


def _shed_at_capacity(run: RunState, slots: RunSlots | None) -> None:
    """Log an alert the agent is too busy to investigate. Do not queue it, do not fail it.

    Invariant 5: a refusal would storm a full agent (the emitter retries anything >= 400) and
    the platform pages a human regardless. No checkpoint write — that needs a connection.
    """
    _log.warning(
        "at capacity (%s concurrent runs): incident %s is recorded in TRIAGE but will "
        "not be investigated. Alerts still page humans through the platform "
        "(invariant 5). Raise DB_POOL_SIZE/DB_MAX_OVERFLOW to lift the bound, or add "
        "replicas.",
        "unbounded" if slots is None else slots.ceiling,
        run.incident_id,
    )


def _record_run_failure(
    run: RunState, checkpointer: Checkpointer, exc: Exception, *, held_lease: bool
) -> None:
    """Best-effort terminal FAILED checkpoint for a crashed run.

    ``held_lease`` is the safety argument: FAILED is non-resumable (ADR 0016), so
    writing it unleased abandons another worker's live run. A crash rail, not a
    transition, so it writes via ``checkpointer.write`` rather than ``dispatch``.
    """
    if not held_lease:
        _log.warning(
            "run for incident %s crashed (%s) without ever holding the single-flight "
            "lease; not recording FAILED — another worker may own this incident",
            run.incident_id,
            type(exc).__name__,
        )
        return
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

    A failure here is logged, never propagated.
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

"""The agent's HTTP front door: it accepts signed alerts and investigates them in the background.

An accepted alert is recorded, acknowledged, and then run by a background task that first has to win
a slot and the incident's lease. Each run gets its own INVESTIGATING transition from
``make_investigate(client)``, so the shared TRANSITIONS table is never modified.
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

# The three answers to "may this worker run this incident?". Three names rather than a true/false,
# because "we are already full" and "someone else owns this incident" need different handling.
Admission = Literal["admitted", "at_capacity", "lease_lost"]

_log = logging.getLogger(__name__)

# What has already been delivered, remembered in this process only. A repeated legacy signature may
# be the platform honestly retrying, so it is accepted and dropped; a repeated nonce cannot be, so
# it is refused (ADR 0014, ADR 0023). Per-process means several replicas each keep their own.
_REPLAY_CACHE_MAX_ENTRIES: Final[int] = 1024
_replay_cache: dict[str, float] = {}
_nonce_cache: dict[str, float] = {}


def _is_replay(cache: dict[str, float], key: str, now: float, window_seconds: float) -> bool:
    """True when ``key`` was already seen inside the window; a first sighting is recorded and False.

    Entries older than the window are dropped on the way in, and once the cache is full the oldest
    entry is evicted, so a flood of deliveries cannot grow this dict without bound.
    """
    for stale in [k for k, seen in cache.items() if now - seen > window_seconds]:
        del cache[stale]
    if key in cache:
        return True
    if len(cache) >= _REPLAY_CACHE_MAX_ENTRIES:
        del cache[min(cache, key=cache.__getitem__)]
    cache[key] = now
    return False


class _BodyTooLargeError(Exception):
    """Raised while reading a streamed body once it passes the cap, to stop reading immediately."""


class BodySizeLimitMiddleware:
    """Refuse an over-long request body before any route sees it.

    ``/alerts`` has to hold the whole body in memory before it can check the signature, so without
    this an unauthenticated caller would decide how much memory this process uses. Two checks: a
    declared ``Content-Length``, and the bytes actually counted as they stream in.
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
                # A response has already started going out, so a 413 cannot be sent any more: let
                # the error surface as a broken response rather than claim a status we cannot set.
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
    """The body size the request claims in ``Content-Length``, or ``None`` if absent or unreadable.

    ``None`` only means this cheap check has no opinion; the streamed byte count still enforces
    the cap, so a caller cannot get past it by omitting or corrupting the header.
    """
    for name, value in scope.get("headers", ()):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


# The id the health check looks up. No real run will ever have it, so the check asks the same
# question whatever the store contains, and finding nothing is the expected answer.
_HEALTH_PROBE_INCIDENT_ID: Final[UUID] = UUID("00000000-0000-0000-0000-000000000000")


async def _probe_datastore(checkpointer: Checkpointer, timeout_seconds: float) -> str | None:
    """``None`` when the run store answered, or a short reason when it did not.

    It gives up after ``timeout_seconds`` rather than inheriting the pool's own wait, because a
    check that hangs as long as the fault it is reporting has stopped answering too. The reason
    names the exception TYPE and never its message, which for a connection error holds the password.
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
    # 1. Decide where runs are stored. A caller that passed its own checkpointer gets no engine and
    #    no run slots; the real wiring builds both, because the lease needs a connection to hold.
    resolved_settings = settings or get_settings()
    engine: Engine | None = None
    slots: RunSlots | None = None
    if checkpointer is None:
        engine = create_pooled_engine(resolved_settings)
        resolved_checkpointer: Checkpointer = PostgresCheckpointer(engine)
        slots = RunSlots(resolved_settings.max_concurrent_runs)
    else:
        resolved_checkpointer = checkpointer

    # 2. Decide what a delivery actually runs. The default closes over this app's engine and slots,
    #    so a background task cannot reach a different database than the one ingress wrote to.
    def investigate(run: RunState, run_settings: Settings, run_checkpointer: Checkpointer) -> None:
        """Run one incident to completion on this app's own engine and admission slots."""
        _run_investigation(run, run_settings, run_checkpointer, engine=engine, slots=slots)

    task: RunTask = run_task or investigate

    # 3. Build the app and hang that wiring off it. The body-size cap goes on first, which makes it
    #    the OUTERMOST layer: an over-long body is refused before any route or parser touches it.
    app = FastAPI(title="Incident Commander", version="0.1.0")
    app.state.settings = resolved_settings
    app.state.checkpointer = resolved_checkpointer
    app.state.engine = engine
    app.state.run_slots = slots
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=resolved_settings.webhook_max_body_bytes)

    # 4. The health endpoint an orchestrator polls to decide whether this process should serve.
    @app.get("/health", response_model=HealthResponse)
    async def health(response: Response) -> HealthResponse:
        """Report both that this process is alive and that it can still reach the run store.

        An agent whose store is unreachable accepts alerts and then loses them, so that counts as
        unhealthy. It answers 503 rather than a 200 with a sad field, because probes read the code.
        """
        reason = await _probe_datastore(
            resolved_checkpointer, resolved_settings.health_probe_timeout_seconds
        )
        if reason is None:
            return HealthResponse(status="ok", details={"datastore": "ok"})
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(status="degraded", details={"datastore": reason})

    # 5. The one endpoint that starts work: the platform posts a signed alert here. Its own numbered
    #    steps below run in order, from checking the signature to spawning the background run.
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

        Nothing is parsed or acted on before the signature is checked, replayed deliveries are
        dropped, and the incident is written down before this returns 202.
        """
        # 1. Read the raw bytes. The signature covers exactly these, so the body must not be parsed
        #    or normalised before it has been checked.
        body = await request.body()
        # 2. Collect the headers the check needs. The platform's emitter signs into
        #    X-Alert-Signature; X-Signature-256 is the older name, still accepted for older tools.
        signature = request.headers.get("X-Alert-Signature") or request.headers.get(
            "X-Signature-256", ""
        )
        # X-Alert-Timestamp is epoch MILLISECONDS on the platform side, not seconds.
        timestamp_header = request.headers.get("X-Alert-Timestamp")
        # Whether this header is present is what chooses between the two signing schemes below
        # (ADR 0023) — the signature itself cannot tell them apart, as both read `sha256=<hex>`.
        nonce_header = request.headers.get("X-Alert-Nonce")
        secret = resolved_settings.platform_webhook_secret.get_secret_value()
        skew_seconds = resolved_settings.webhook_max_skew_seconds
        now = time.time()

        # 3. The newer scheme, chosen by the nonce header being present: check the signature over
        #    timestamp, nonce and body, then refuse one that is too old or a nonce seen before.
        if nonce_header is not None:
            if timestamp_header is None:
                # The timestamp is part of the signed material here, so without it there is nothing
                # to verify the signature against — refuse rather than guess at one.
                raise HTTPException(
                    status.HTTP_401_UNAUTHORIZED,
                    "X-Alert-Nonce present without X-Alert-Timestamp",
                )
            if not verify_delivery(body, timestamp_header, nonce_header, signature, secret):
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing signature")
            # The timestamp is signed in this scheme, so nobody can change it: checking it against
            # our clock genuinely limits how old a replayed delivery may be.
            _reject_unless_within_skew(timestamp_header, now, skew_seconds)
            # Nonces are remembered for twice the skew window, because a delivery is accepted from
            # skew seconds early to skew seconds late. Checked AFTER the signature, so an
            # unauthenticated caller cannot fill this cache with nonces of its choosing.
            if _is_replay(_nonce_cache, nonce_header, now, float(skew_seconds) * 2):
                _log.warning(
                    "refused replayed webhook delivery (nonce %s); no run spawned",
                    nonce_header[:16],
                )
                # A 401 here, unlike the legacy branch below: a nonce is issued once per delivery,
                # so seeing one twice is a replay attempt and not an honest retry.
                raise HTTPException(
                    status.HTTP_401_UNAUTHORIZED, "replayed delivery: nonce already seen"
                )
        # 4. No nonce, so the legacy scheme the pinned platform image still uses: check the
        #    signature over the body alone, and treat a repeat as an honest retry, not an attack.
        else:
            if not verify(body, signature, secret):
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing signature")

            # The timestamp is not signed in this scheme, so anyone replaying the body can set it to
            # now. The check still narrows the window an old capture is accepted in; it cannot close
            # it, which is why the nonce scheme above exists at all (ADR 0014).
            if timestamp_header is not None:
                _reject_unless_within_skew(timestamp_header, now, skew_seconds)

            signature_hex = signature.removeprefix("sha256=")
            if _is_replay(_replay_cache, signature_hex, now, float(skew_seconds)):
                # Answer 202 rather than an error: the platform's emitter retries anything from 400
                # up, so an honest redelivery has to look accepted or it will keep coming back. No
                # run is started and the id returned is a fresh one that belongs to nothing.
                _log.warning(
                    "suppressed replayed webhook delivery (signature %s...); no run spawned",
                    signature_hex[:12],
                )
                return IngestResponse(incident_id=uuid4())

        # 5. Only now that the delivery is authenticated, read the JSON. Unparseable content is the
        #    sender's mistake, so it gets a 422 rather than being logged and forgotten.
        try:
            payload = AlertPayload.model_validate_json(body)
        except ValueError as err:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"malformed alert payload: {err}"
            ) from err

        alert = payload.model_dump()
        # 6. Work out which incident this alert belongs to, so a second delivery joins the run
        #    already investigating it (ADR 0016). In a thread: it does up to 64 blocking reads.
        try:
            incident_id = await run_in_threadpool(derive_incident_id, alert, resolved_checkpointer)
        # A failure here must not lose the alert, so fall back to a brand-new id: the alert is
        # investigated, and only the joining-an-existing-run behaviour is given up.
        except Exception:
            _log.exception(
                "incident identity derivation failed; falling back to a fresh id "
                "(redeliveries of this alert will not dedupe)"
            )
            incident_id = uuid4()

        run = start_run(alert, resolved_settings, datetime.now(UTC), incident_id=incident_id)

        # 7. Write the incident down BEFORE answering 202, so an agent that dies right after
        #    acknowledging has still recorded the alert (finding B-04) — and only if nothing is yet.
        try:
            await run_in_threadpool(_write_ingress_checkpoint, resolved_checkpointer, run)
        except Exception:
            _log.exception(
                "checkpoint write failed for incident %s (AGENT_ENABLED=%s); "
                "acknowledging delivery without a durable record",
                run.incident_id,
                str(resolved_settings.agent_enabled).lower(),
            )

        # 8. The kill switch: with AGENT_ENABLED false the alert is still accepted and recorded and
        #    no run starts. The platform pages a human anyway; refusing would just lose the record.
        if not resolved_settings.agent_enabled:
            _log.warning(
                "kill switch active (AGENT_ENABLED=false): recorded incident %s "
                "in TRIAGE; no investigation run spawned",
                run.incident_id,
            )
            return IngestResponse(incident_id=run.incident_id)

        # 9. Hand the run to a background task and answer 202 at once. Every accepted delivery
        #    starts a task; whether it investigates is settled later, by the lease, not here.
        background_tasks.add_task(task, run, resolved_settings, resolved_checkpointer)
        return IngestResponse(incident_id=run.incident_id)

    return app


def _reject_unless_within_skew(header: str, now: float, max_skew_seconds: int) -> None:
    """Answer 401 unless ``header``, epoch MILLISECONDS, is close enough to our own clock.

    The try block covers the comparison as well as the parse on purpose: a header like ``10**400``
    parses fine and then raises ``OverflowError`` on the arithmetic, which used to turn one
    malformed header into a 500 from the ingress endpoint.
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
    """Write the first snapshot for an incident, but only if nothing is stored for it yet.

    Both the read and the write are here so they can run in one worker thread, and so the decision
    not to overwrite an in-flight run's state lives in a single place (ADR 0016).
    """
    if checkpointer.load(run.incident_id) is None:
        checkpointer.write(run)


@contextmanager
def _admission(
    slots: RunSlots | None, engine: Engine | None, incident_id: UUID
) -> Iterator[Admission]:
    """Take a run slot, then the incident's lease, and hold both for as long as the body runs.

    The order is the whole point (ADR 0022): taking the lease pins a pooled connection for the rest
    of the run, so the "we are full" decision has to happen before one is held. Neither waits.
    """
    with _run_admission(slots) as admitted:
        if not admitted:
            yield "at_capacity"
            return
        with _single_flight(engine, incident_id) as acquired:
            yield "admitted" if acquired else "lease_lost"


@contextmanager
def _run_admission(slots: RunSlots | None) -> Iterator[bool]:
    """``RunSlots.acquire``, or always-yes when no slots were wired.

    No slots means no connection pool to protect — the single-process test wiring — so there is
    nothing for a bound to prevent. Same reasoning as ``_single_flight`` below.
    """
    if slots is None:
        yield True
        return
    with slots.acquire() as admitted:
        yield admitted


@contextmanager
def _single_flight(engine: Engine | None, incident_id: UUID) -> Iterator[bool]:
    """``incident_lease``, or an always-granted lease when no engine is wired.

    Only the wiring that had a checkpointer handed to it has no engine, and that is one process
    with one store, where no second worker exists to race with.
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
    """Investigate one incident in a background task: get permission, resume or start, then run.

    Only one worker may run an incident at a time (ADR 0016), and a worker that is already at its
    run limit sheds the work instead of queueing it (ADR 0022). If this crashes, a FAILED record is
    written — but only by a worker that actually held the lease, which ``held_lease`` tracks.
    """
    held_lease = False
    try:
        # 1. Ask for a run slot and then the incident's lease. Both are refusals, not exceptions.
        with _admission(slots, engine, run.incident_id) as verdict:
            # 2. This worker is already running as many incidents as its pool can serve: log the
            #    alert as unattended and stop. Humans are paged by the platform regardless.
            if verdict == "at_capacity":
                _shed_at_capacity(run, slots)
                return
            # 3. Another worker holds this incident's lease, so it is already being investigated.
            #    Stop quietly: a second run would duplicate every tool call and every action.
            if verdict == "lease_lost":
                _log.info(
                    "another worker holds the single-flight lease for incident %s; "
                    "not starting a second run",
                    run.incident_id,
                )
                return

            # 4. Record that this worker holds the lease: that is the condition the crash rail
            #    checks, so from here on a crash in this run is ours to write down as FAILED.
            held_lease = True
            # 5. Decide where to start from: the alert as it arrived, or the newest stored snapshot
            #    of an earlier attempt at the same incident.
            latest = checkpointer.load(run.incident_id)
            if latest is None:
                resuming = run
            # 6. The incident already finished — resolved, escalated or failed. Leave it alone: a
            #    problem that comes back opens a new incident, it does not retry this one.
            elif latest.state.is_terminal:
                _log.info(
                    "incident %s already reached terminal state %s; not resuming",
                    run.incident_id,
                    latest.state.value,
                )
                return
            # 7. The incident is waiting for a human to approve an action. Resuming that is not
            #    built yet (ADR 0016): it would raise, and the crash rail would mark it FAILED.
            elif latest.state is IncidentState.AWAITING_APPROVAL:
                _log.info(
                    "incident %s is awaiting_approval; approval-bound resume is not "
                    "implemented, leaving it untouched",
                    run.incident_id,
                )
                return
            # 8. An unfinished run: carry on from its stored state. A snapshot still in TRIAGE is
            #    just the row ingress wrote, so only a later state means a real crash is resuming.
            else:
                resuming = latest
                if latest.state is not IncidentState.TRIAGE:
                    _log.info(
                        "resuming incident %s from its %s checkpoint",
                        run.incident_id,
                        latest.state.value,
                    )

            # 9. Open one platform connection and drive the state machine to a terminal state. Only
            #    the INVESTIGATING transition is swapped, on a copy: it is the one needing a client.
            with make_client(settings) as client:
                transitions: dict[IncidentState, Transition] = dict(TRANSITIONS)
                transitions[IncidentState.INVESTIGATING] = make_investigate(client)
                final = run_to_completion(
                    resuming,
                    clock=lambda: datetime.now(UTC),
                    checkpointer=checkpointer,
                    transitions=transitions,
                )
    # 10. The run crashed: try to leave a terminal FAILED record, then re-raise the ORIGINAL
    #     exception unchanged so it reaches the server log with its own stack trace.
    except Exception as exc:
        _record_run_failure(run, checkpointer, exc, held_lease=held_lease)
        raise
    # 11. The run finished on its own terms: log the briefing a human would read.
    else:
        _log_briefing(final)


def _shed_at_capacity(run: RunState, slots: RunSlots | None) -> None:
    """Log that the agent was too busy to investigate this alert. Do not queue it, do not fail it.

    Refusing the delivery would make the platform's emitter retry it, which storms an agent that is
    already full, and the platform pages a human for the alert either way (invariant 5). Nothing is
    written to the store here, because writing needs the connection this refusal is protecting.
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
    """Try to record that a crashed run ended, without ever making things worse.

    ``held_lease`` is the safety condition: a FAILED record can never be resumed (ADR 0016), so
    writing one for an incident this worker did not own would abandon another worker's live run.
    This is a crash rail, so it only writes a record — it never starts or retries anything.
    """
    # 1. This worker never owned the incident, so it must not declare it dead. Log and stop.
    if not held_lease:
        _log.warning(
            "run for incident %s crashed (%s) without ever holding the single-flight "
            "lease; not recording FAILED — another worker may own this incident",
            run.incident_id,
            type(exc).__name__,
        )
        return
    try:
        # 2. Read the newest snapshot. If the run already reached an outcome, a crash after that
        #    must not replace a real RESOLVED or ESCALATED record with FAILED.
        latest = checkpointer.load(run.incident_id) or run
        if latest.state.is_terminal:
            return
        # 3. Append a FAILED snapshot that keeps everything the run had learned, plus one evidence
        #    entry naming the exception type and the first 500 characters of its message.
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
    # 4. Even this failed — the database is probably the reason the run crashed. Log it and give up:
    #    raising here would replace the original crash with a less informative one.
    except Exception:
        _log.exception(
            "failure rail could not record a terminal FAILED checkpoint for incident %s",
            run.incident_id,
        )


def _log_briefing(final: RunState) -> None:
    """Log the briefing that hands a finished incident to a human (invariant 7).

    Writing the briefing must never be what breaks a run that has already finished, so a failure
    here is logged and swallowed.
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

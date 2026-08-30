"""FastAPI ingress. Receives HMAC-signed alerts, spawns runs in a background task.

Wiring shape: create_app(settings) builds the engine, checkpointer, and a
per-request ``MCPClient``. The state machine's INVESTIGATING transition is
wired via ``make_investigate(client)`` per run so live tool calls happen with
a fresh HTTP client and the module-level TRANSITIONS registry stays untouched.
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
from incident_commander.api.hmac_verify import verify
from incident_commander.api.schemas import AlertPayload, HealthResponse, IngestResponse
from incident_commander.config import Settings, get_settings
from incident_commander.persistence.lease import incident_lease
from incident_commander.persistence.pool import RunSlots, create_pooled_engine
from incident_commander.persistence.postgres import PostgresCheckpointer
from incident_commander.tools.mcp_client import make_client

RunTask = Callable[[RunState, Settings, Checkpointer], None]

# Why a run did or did not get to execute. Three outcomes, named rather than
# collapsed into a bool, because the two refusals are different events with
# different operator meanings: "somebody else already owns this incident"
# (routine, expected on every duplicate delivery) versus "this agent is full"
# (a capacity signal that wants attention).
Admission = Literal["admitted", "at_capacity", "lease_lost"]

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


class _BodyTooLargeError(Exception):
    """Raised inside the receive wrapper when a streamed body passes the cap."""


class BodySizeLimitMiddleware:
    """Refuse an over-length request body ahead of every route (WO-R2-86).

    ``/alerts`` cannot authenticate before it buffers: the HMAC covers the
    body, so ``await request.body()`` necessarily runs before ``verify``. That
    ordering is not a bug to fix in the route — it is the contract — which
    means the only place that can stop an unauthenticated caller from choosing
    this process's memory footprint is ahead of the route. Hence a middleware,
    which also covers any endpoint added later for free.

    Two gates, because the client picks which one applies:

    * a declared ``Content-Length`` over the cap is refused without reading a
      single body byte;
    * an undeclared or chunked body is counted as it streams and refused the
      moment the running total passes the cap — so lying about the length, or
      omitting it, buys no more memory than declaring it honestly.

    The refusal is a 413. The platform emitter reads any status >= 400 as a
    delivery failure and will retry, which is the correct outcome for a
    payload this endpoint will never accept in its current shape.
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
                # A status line is already on the wire; there is no honest way
                # to turn it into a 413 from here. Surface it as the server
                # error it is rather than sending a second response.
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

    ``None`` is the safe answer for a malformed header: it only means this
    gate abstains, and the streaming count below still holds the line.
    """
    for name, value in scope.get("headers", ()):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


# An id no run will ever carry, so the probe reads and finds nothing rather
# than depending on the contents of the store.
_HEALTH_PROBE_INCIDENT_ID: Final[UUID] = UUID("00000000-0000-0000-0000-000000000000")


async def _probe_datastore(checkpointer: Checkpointer, timeout_seconds: float) -> str | None:
    """``None`` if the run store answered; a short reason if it did not.

    The probe is a real ``load`` of an id that never exists. For the Postgres
    checkpointer that is a pooled checkout plus one indexed SELECT — the same
    work every ingress path does first, and exactly what a dead database or an
    exhausted pool fails. A cheaper probe (or none, which is what /health did)
    reports an agent healthy when it cannot record a single alert.

    Bounded, because ``DB_POOL_TIMEOUT_SECONDS`` is not a wait a health check
    may inherit. ``run_in_threadpool`` cannot be cancelled, so a timed-out
    probe leaves its thread to finish in the background; that is bounded by
    the pool timeout and is preferable to an endpoint that stops answering.

    The reason string carries an exception TYPE, never its message: a DSN with
    credentials in it routinely appears in the text of a connection error, and
    this endpoint is unauthenticated.
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
    # The engine is kept, not discarded: the single-flight lease (ADR 0016)
    # needs a real connection to take its advisory lock on. An injected
    # checkpointer means there is no engine — see ``_single_flight``.
    engine: Engine | None = None
    slots: RunSlots | None = None
    if checkpointer is None:
        engine = create_pooled_engine(resolved_settings)
        resolved_checkpointer: Checkpointer = PostgresCheckpointer(engine)
        # Admission bound (ADR 0022), sized from the pool rather than chosen:
        # a run holds a lease connection for its whole life, so allowing more
        # live runs than the pool can serve is a deadlock, not merely slow.
        # One instance per app, shared by every background task it spawns.
        # Paired with the engine because it exists to protect that engine's
        # pool — the injected-checkpointer wiring has no pool to protect.
        slots = RunSlots(resolved_settings.max_concurrent_runs)
    else:
        resolved_checkpointer = checkpointer

    def investigate(run: RunState, run_settings: Settings, run_checkpointer: Checkpointer) -> None:
        _run_investigation(run, run_settings, run_checkpointer, engine=engine, slots=slots)

    task: RunTask = run_task or investigate

    app = FastAPI(title="Incident Commander", version="0.1.0")
    app.state.settings = resolved_settings
    app.state.checkpointer = resolved_checkpointer
    app.state.engine = engine
    app.state.run_slots = slots
    # Outermost of the app's own middleware, so the cap is enforced before any
    # per-request machinery runs on a body this process has decided not to read.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=resolved_settings.webhook_max_body_bytes)

    @app.get("/health", response_model=HealthResponse)
    async def health(response: Response) -> HealthResponse:
        """Liveness AND datastore readiness.

        This used to answer ``ok`` unconditionally, which made it a check that
        the process was running — something the caller already knew, since it
        got a reply. An agent whose run store is unreachable can neither
        derive an incident id, nor record a TRIAGE row, nor take a lease: it
        accepts alerts and loses them. That agent must not report healthy,
        because /health is what the availability work is judged by.

        Degraded is reported as 503, not as a 200 with a sad field: an
        orchestrator's probe reads the status code, and a body nobody parses
        is not a signal.
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
            _reject_unless_within_skew(
                timestamp_header, now, resolved_settings.webhook_max_skew_seconds
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

        alert = payload.model_dump()
        # Durable identity (ADR 0016): an at-least-once redelivery of the same
        # (source, fingerprint) resolves to the same incident id, so it joins
        # the live run instead of starting a second investigation against one
        # fault. Fail-open like the write below — a store that is down
        # degrades to today's fresh id rather than failing the delivery.
        #
        # ``run_in_threadpool``, not a direct call: ``derive_incident_id``
        # walks the generation chain with one synchronous indexed ``load`` per
        # closed generation (up to 64), and each of those can now sit for
        # ``DB_POOL_TIMEOUT_SECONDS`` waiting for a free connection. Called
        # directly from this ``async def`` it blocks the event loop, and a
        # blocked event loop means /health stops answering — so a database
        # slow enough to be worth alerting on would also make the agent look
        # dead to whatever watches it (ADR 0022).
        try:
            incident_id = await run_in_threadpool(derive_incident_id, alert, resolved_checkpointer)
        except Exception:
            _log.exception(
                "incident identity derivation failed; falling back to a fresh id "
                "(redeliveries of this alert will not dedupe)"
            )
            incident_id = uuid4()

        run = start_run(alert, resolved_settings, datetime.now(UTC), incident_id=incident_id)

        # Durability before acknowledgement (finding B-04): the TRIAGE row
        # exists before the 202, so a process death between here and the
        # background task still leaves a record of the alert. Enabled or
        # killed, the write is the same — the kill switch below only skips
        # spawning the run.
        #
        # Conditional on there being no snapshot yet, and that is correctness,
        # not thrift (ADR 0016): ``run_snapshots`` is append-only and ``load``
        # returns the highest version, so appending a fresh TRIAGE row on top
        # of an in-flight INVESTIGATING run would make ``load`` return TRIAGE
        # and hand the resume path a state stripped of its evidence. The first
        # delivery still always writes, so the guarantee above is intact. On
        # the fresh path ``run_to_completion`` writes its own TRIAGE snapshot
        # on entry, so the log still carries two identical TRIAGE rows; that
        # stays benign and cheaper than plumbing a skip flag through the loop.
        #
        # Fail-open: a checkpoint failure is logged, never surfaced as a 5xx —
        # and a failed LOOKUP is treated exactly like a failed write. The run
        # store being down must not turn webhook ingestion into delivery
        # failures during exactly the kind of incident that took it down, and
        # paging runs through the platform's oncall route regardless of this
        # endpoint.
        #
        # Off the event loop for the same reason as the derivation above, and
        # as ONE hop rather than two so the load and its conditional write
        # stay adjacent.
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
            # Kill switch (docs/safety-model.md#kill-switch): the alert is
            # recorded above as a TRIAGE-state run so nothing is lost, but no
            # investigation is spawned — the state machine never advances.
            _log.warning(
                "kill switch active (AGENT_ENABLED=false): recorded incident %s "
                "in TRIAGE; no investigation run spawned",
                run.incident_id,
            )
            return IngestResponse(incident_id=run.incident_id)

        # Every accepted delivery still spawns a task. The lease, not the
        # ingress, decides who actually runs: a duplicate arriving mid-run
        # loses it and exits immediately, while one arriving after the owning
        # process died finds the lock released and resumes. Deciding "is
        # anything live?" here would need exactly the liveness knowledge only
        # the lock has.
        background_tasks.add_task(task, run, resolved_settings, resolved_checkpointer)
        return IngestResponse(incident_id=run.incident_id)

    return app


def _reject_unless_within_skew(header: str, now: float, max_skew_seconds: int) -> None:
    """401 unless ``header`` (epoch MILLISECONDS) is inside the skew window.

    Compared in integer milliseconds. The comparison used to be
    ``abs(now - timestamp_ms / 1000)``, and that division is a conversion to
    float — ``int`` is unbounded, so a header of ``10**400`` parsed fine and
    then raised ``OverflowError`` from the arithmetic, outside the
    ``except ValueError`` that guarded only the parse. The result was an
    unauthenticated caller turning the ingress into a 500 with one header.
    Integer arithmetic has no conversion left to overflow, and a magnitude
    that large is by definition outside the window, so it is refused by the
    ordinary stale-timestamp path.

    ``OverflowError`` is caught beside ``ValueError`` all the same, and the
    whole parse-and-compare sits inside the guard rather than the parse alone:
    the defect was not that this arm was missing, it was that the guard did
    not cover the line that failed.
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

    The order is the whole point (ADR 0022). Taking the lease is what pins a
    pooled connection for the length of the run, so a run that is going to be
    refused for capacity has to be refused BEFORE it holds one — a bound
    applied after the lease would be a bound on nothing.

    Both are non-blocking tries, and both are held for the entire ``with``
    body: the slot is what the pool arithmetic counts, so releasing it early
    would let a second run in against connections the first has not given
    back.
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

    Same shape and the same reason as ``_single_flight``: the bound exists to
    protect a connection pool, and the injected-checkpointer wiring that tests
    use has no pool to protect.
    """
    if slots is None:
        yield True
        return
    with slots.acquire() as admitted:
        yield admitted


@contextmanager
def _single_flight(engine: Engine | None, incident_id: UUID) -> Iterator[bool]:
    """``incident_lease``, or an always-granted lease when no engine is wired.

    ``create_app`` only builds an engine when it also builds the checkpointer,
    so the injected-checkpointer wiring — tests today — has nothing to lock
    against. That wiring is single-process and non-durable by construction, so
    running unleased is sound there and nowhere else. The served app always
    has an engine.
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

    Single-flight + resume (ADR 0016, implementing ADR 0002's promise): the
    lease is held across the whole run, so one incident has at most one live
    writer no matter how many times the alert is delivered. Inside it, the
    latest checkpoint decides what to run — a crashed run continues from where
    it died instead of re-spending its budget to rebuild evidence it already
    recorded.

    Admission (ADR 0022) is the outer gate: above the configured concurrency
    bound the run is shed rather than queued, because the pool cannot serve it
    and waiting half an hour for a slot is not a better answer than saying so.

    Failure rail (finding B-04): without the ``except`` below, any exception
    here — a checkpointer failure with Postgres down is the realistic vector —
    kills the task silently, leaving the incident stranded at whatever
    non-terminal checkpoint it reached with nothing recording why. Every exit
    path now leaves a terminal snapshot behind.

    The rail spans admission too, so it also catches crashes from BEFORE this
    process owns anything — pool exhaustion inside ``incident_lease`` above
    all. ``held_lease`` is what separates the two: only a run that got past
    the ``admitted`` verdict may write FAILED, because that write is terminal
    and would otherwise close another worker's live incident (see
    ``_record_run_failure``).
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

            # Past this line the lease is ours and stays ours for the rest of
            # the ``with`` body, which is exactly the window in which a crash
            # is this process's to record.
            held_lease = True
            latest = checkpointer.load(run.incident_id)
            if latest is None:
                resuming = run
            elif latest.state.is_terminal:
                # RESOLVED/ESCALATED/FAILED are done. FAILED especially: it is
                # the crash rail's record that a run died unhandled, and
                # resuming it would arm a redelivery-driven retry loop with no
                # operator signal. A recurrence opens a new incident instead.
                _log.info(
                    "incident %s already reached terminal state %s; not resuming",
                    run.incident_id,
                    latest.state.value,
                )
                return
            elif latest.state is IncidentState.AWAITING_APPROVAL:
                # Tier-2 resume is deliberately out of scope (ADR 0016): the
                # transition is still a stub, so resuming would raise
                # NotImplementedError and the rail below would turn a merely-
                # waiting incident into a FAILED one. First thing the Tier-2
                # phase deletes.
                _log.info(
                    "incident %s is awaiting_approval; approval-bound resume is not "
                    "implemented, leaving it untouched",
                    run.incident_id,
                )
                return
            else:
                resuming = latest
                if latest.state is not IncidentState.TRIAGE:
                    # A TRIAGE checkpoint is just the ingress row this task was
                    # spawned for — nothing to announce. Anything further along
                    # means a real crash-resume, which an operator wants to see.
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
        # Re-raise the ORIGINAL exception: starlette runs background tasks
        # after the response, so this surfaces in the server log with its
        # stack intact. Swallowing it would hide the crash class this rail
        # exists to record.
        raise
    else:
        _log_briefing(final)


def _shed_at_capacity(run: RunState, slots: RunSlots | None) -> None:
    """Log an alert the agent is too busy to investigate. Do not queue it, do not fail it.

    The choice here is forced by CLAUDE.md invariant 5. The agent augments the
    incident response path and never gates it, so the one thing overload must
    not do is make the delivery look failed: the platform emitter treats
    anything >= 400 as a failed delivery and retries, and a retry storm aimed
    at an agent that is already at capacity is how a busy agent becomes a down
    one. The delivery was acknowledged 202 and the alert is already durably
    recorded at TRIAGE by the ingress write, so nothing is lost — the platform
    pages a human off that same alert whether or not this agent ever looks at
    it, which is exactly what invariant 5 buys.

    Nor does the alert vanish quietly. It leaves two marks: this WARNING, and
    a TRIAGE-state run that never advances — the same visible residue the kill
    switch leaves, and found the same way (incidents sitting at TRIAGE with no
    investigation).

    Deliberately no extra checkpoint write. Recording "shed" durably would
    mean asking for a connection at the moment connections are the scarce
    resource — adding load to the overload path in order to describe it. The
    log line carries it instead.
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

    ``held_lease`` is required, not defaulted, and it is the whole safety
    argument: writing FAILED is a claim about a run THIS process was
    conducting. A process that crashed on its way to the lease — the pool is
    exhausted, Postgres blinked — knows nothing about the incident except that
    someone may be investigating it right now, and per ADR 0016 the FAILED it
    would write is non-resumable: the holder's next checkpoint gate sees a
    terminal state and abandons a live investigation, and the closed
    generation makes ``derive_incident_id`` fork the next redelivery into a
    second incident for one fault. So a non-holder logs and steps aside. The
    keyword is the guard against the ordering regressing silently — a future
    caller cannot reach the terminal write without stating, at the call site,
    that it owns the lease.

    Written directly via ``checkpointer.write``, not ``dispatch``: this is a
    crash rail, not a state-machine transition (``ALLOWED_TRANSITIONS`` already
    lists FAILED as a successor of every non-terminal state).

    The crashed run's latest state is not the local ``run`` — the loop
    checkpoints progress internally and returns nothing on raise — so recover
    it via ``load`` and fall back to ``run``. The realistic crash vector IS the
    checkpointer, so the whole rail is best-effort: if it cannot write, it logs
    and lets the original exception carry the story. The FAILED record it
    leaves is terminal on purpose: the resume gate in ``_run_investigation``
    refuses to re-run it (ADR 0016), so a deterministically-crashing run cannot
    be pushed into a redelivery-driven retry loop.
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

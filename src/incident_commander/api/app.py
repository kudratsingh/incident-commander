"""FastAPI ingress. Receives HMAC-signed alerts, spawns runs in a background task.

Wiring shape: create_app(settings) builds the engine, checkpointer, and a
per-request ``MCPClient``. The state machine's INVESTIGATING transition is
wired via ``make_investigate(client)`` per run so live tool calls happen with
a fresh HTTP client and the module-level TRANSITIONS registry stays untouched.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, status
from sqlalchemy import create_engine

from incident_commander.agent.factory import start_run
from incident_commander.agent.investigation import make_investigate
from incident_commander.agent.loop import run_to_completion
from incident_commander.agent.orchestrator import TRANSITIONS, Checkpointer, Transition
from incident_commander.agent.state import IncidentState, RunState
from incident_commander.api.hmac_verify import verify
from incident_commander.api.schemas import AlertPayload, HealthResponse, IngestResponse
from incident_commander.config import Settings, get_settings
from incident_commander.persistence.postgres import PostgresCheckpointer
from incident_commander.tools.mcp_client import make_client

RunTask = Callable[[RunState, Settings, Checkpointer], None]


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
        signature = request.headers.get("X-Signature-256", "")
        if not verify(
            body,
            signature,
            resolved_settings.platform_webhook_secret.get_secret_value(),
        ):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing signature")
        try:
            payload = AlertPayload.model_validate_json(body)
        except ValueError as err:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"malformed alert payload: {err}"
            ) from err

        run = start_run(payload.model_dump(), resolved_settings, datetime.now(UTC))
        background_tasks.add_task(task, run, resolved_settings, resolved_checkpointer)
        return IngestResponse(incident_id=run.incident_id)

    return app


def _run_investigation(
    run: RunState,
    settings: Settings,
    checkpointer: Checkpointer,
) -> None:
    """Background task: wire a per-run MCP client, run the state machine."""
    with make_client(settings) as client:
        transitions: dict[IncidentState, Transition] = dict(TRANSITIONS)
        transitions[IncidentState.INVESTIGATING] = make_investigate(client)
        run_to_completion(
            run,
            clock=lambda: datetime.now(UTC),
            checkpointer=checkpointer,
            transitions=transitions,
        )

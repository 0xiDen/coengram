"""Typed HTTP Adapter over the Memory Module Interface."""

import asyncio
import time
from typing import Annotated

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict

from agent_memory_service.admin_http import mount_admin_routes
from agent_memory_service.agents import AgentInvocation, AgentRuntimeModule, AgentRunView
from agent_memory_service.auth import AuthenticationError, TokenService
from agent_memory_service.body_limits import RequestBodyLimitMiddleware
from agent_memory_service.control import ControlModule
from agent_memory_service.governance import (
    KnowledgeCandidateView,
    ProposeKnowledge,
    ReviewDecision,
    ReviewKnowledge,
)
from agent_memory_service.lifecycle import (
    CorrectMemory,
    ErasureDecision,
    ErasureRequestView,
    RequestErasure,
    ReviewErasure,
)
from agent_memory_service.memory import MemoryModule
from agent_memory_service.models import (
    MemoryMutationReceipt,
    PrivateMemoryInspection,
    RecallQuery,
    RecallResult,
    RetainMemory,
    TenantSession,
)
from agent_memory_service.stores.memory import TenantMemoryUnavailable
from agent_memory_service.telegram import TelegramAdapter
from agent_memory_service.telemetry import SafeTelemetry


class KnowledgeReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: ReviewDecision
    rationale: str
    idempotency_key: str


class CorrectionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    replacement_content: str
    kind: str = "explicit"
    confidence: float = 1.0
    reason: str
    idempotency_key: str


class ErasureRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: str
    reason: str
    idempotency_key: str


class ErasureReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: ErasureDecision
    rationale: str
    idempotency_key: str


def create_http_app(
    memory: MemoryModule,
    tokens: TokenService,
    *,
    telegram: TelegramAdapter | None = None,
    agents: AgentRuntimeModule | None = None,
    telemetry: SafeTelemetry | None = None,
    execute_agent_runs: bool = True,
    control: ControlModule | None = None,
) -> FastAPI:
    """Create an application whose routes share one authentication and memory seam."""

    app = FastAPI(title="CoEngram", version="0.1.0")
    app.add_middleware(RequestBodyLimitMiddleware)
    bearer = HTTPBearer(auto_error=False)

    @app.exception_handler(TenantMemoryUnavailable)
    async def memory_unavailable_handler(_request: object, _exc: Exception) -> Response:
        if telemetry is not None:
            telemetry.observe_route("unavailable")
            telemetry.set_dependency_health("neo4j", False)
        return Response(
            content='{"code":"memory_unavailable","message":"Tenant Memory is unavailable"}',
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            media_type="application/json",
        )

    @app.exception_handler(PermissionError)
    async def forbidden_handler(_request: object, _exc: Exception) -> Response:
        return Response(
            content='{"code":"forbidden","message":"Operation is not permitted"}',
            status_code=status.HTTP_403_FORBIDDEN,
            media_type="application/json",
        )

    @app.exception_handler(LookupError)
    async def not_found_handler(_request: object, _exc: Exception) -> Response:
        return Response(
            content='{"code":"not_found","message":"Resource was not found"}',
            status_code=status.HTTP_404_NOT_FOUND,
            media_type="application/json",
        )

    @app.exception_handler(ValueError)
    async def invalid_request_handler(_request: object, _exc: Exception) -> Response:
        return Response(
            content='{"code":"invalid_request","message":"Request data is invalid"}',
            status_code=status.HTTP_400_BAD_REQUEST,
            media_type="application/json",
        )

    def authenticated_session(
        credential: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> TenantSession:
        started = time.perf_counter()
        if credential is None or credential.scheme.casefold() != "bearer":
            if telemetry is not None:
                telemetry.observe_authentication("missing", time.perf_counter() - started)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
        try:
            session = tokens.authenticate(credential.credentials)
        except AuthenticationError as exc:
            if telemetry is not None:
                telemetry.observe_authentication("invalid", time.perf_counter() - started)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized",
            ) from exc
        if telemetry is not None:
            telemetry.observe_authentication("success", time.perf_counter() - started)
            telemetry.observe_authenticated_request(session)
        return session

    @app.get("/healthz", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    if telemetry is not None:

        @app.get("/metrics", include_in_schema=False)
        async def metrics() -> Response:
            return Response(
                content=telemetry.render_metrics(),
                media_type="text/plain; version=0.0.4",
            )

    if control is not None:
        mount_admin_routes(app, control)

    @app.post(
        "/api/v1/memories",
        response_model=MemoryMutationReceipt,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def retain(
        command: RetainMemory,
        session: Annotated[TenantSession, Depends(authenticated_session)],
    ) -> MemoryMutationReceipt:
        if telemetry is None:
            return await memory.retain_result(session, command)
        async with telemetry.operation("embedding_search", session):
            receipt = await memory.retain_result(session, command)
        telemetry.observe_route("resolved")
        telemetry.set_dependency_health("tenant_postgres", True)
        return receipt

    @app.get("/api/v1/memories", response_model=tuple[PrivateMemoryInspection, ...])
    async def inspect_private(
        session: Annotated[TenantSession, Depends(authenticated_session)],
    ) -> tuple[PrivateMemoryInspection, ...]:
        items = await memory.inspect_private(session)
        if telemetry is not None:
            telemetry.observe_route("resolved")
            telemetry.set_dependency_health("neo4j", True)
        return items

    @app.post("/api/v1/memories/recall", response_model=RecallResult)
    async def recall(
        query: RecallQuery,
        session: Annotated[TenantSession, Depends(authenticated_session)],
    ) -> RecallResult:
        if telemetry is None:
            return await memory.recall(session, query)
        async with telemetry.operation("embedding_search", session):
            result = await memory.recall(session, query)
        telemetry.observe_route("resolved")
        telemetry.set_dependency_health("neo4j", True)
        return result

    @app.post(
        "/api/v1/knowledge/candidates",
        response_model=KnowledgeCandidateView,
        status_code=status.HTTP_201_CREATED,
    )
    async def propose_knowledge(
        command: ProposeKnowledge,
        session: Annotated[TenantSession, Depends(authenticated_session)],
    ) -> KnowledgeCandidateView:
        return await memory.propose_knowledge(session, command)

    @app.post(
        "/api/v1/knowledge/candidates/{candidate_id}/reviews",
        response_model=KnowledgeCandidateView,
    )
    async def review_knowledge(
        candidate_id: str,
        body: KnowledgeReviewBody,
        session: Annotated[TenantSession, Depends(authenticated_session)],
    ) -> KnowledgeCandidateView:
        return await memory.review_knowledge(
            session,
            ReviewKnowledge(candidate_id=candidate_id, **body.model_dump()),
        )

    @app.get(
        "/api/v1/knowledge/candidates",
        response_model=tuple[KnowledgeCandidateView, ...],
    )
    async def list_knowledge_candidates(
        session: Annotated[TenantSession, Depends(authenticated_session)],
    ) -> tuple[KnowledgeCandidateView, ...]:
        return await memory.list_knowledge_candidates(session)

    @app.post(
        "/api/v1/memories/{memory_id}/corrections",
        response_model=MemoryMutationReceipt,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def correct_memory(
        memory_id: str,
        body: CorrectionBody,
        session: Annotated[TenantSession, Depends(authenticated_session)],
    ) -> MemoryMutationReceipt:
        return await memory.correct_result(
            session,
            CorrectMemory(memory_id=memory_id, **body.model_dump()),
        )

    @app.post(
        "/api/v1/erasure-requests",
        response_model=ErasureRequestView,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def request_erasure(
        body: ErasureRequestBody,
        session: Annotated[TenantSession, Depends(authenticated_session)],
    ) -> ErasureRequestView:
        return await memory.request_erasure(session, RequestErasure(**body.model_dump()))

    @app.post(
        "/api/v1/erasure-requests/{request_id}/reviews",
        response_model=ErasureRequestView,
    )
    async def review_erasure(
        request_id: str,
        body: ErasureReviewBody,
        session: Annotated[TenantSession, Depends(authenticated_session)],
    ) -> ErasureRequestView:
        return await memory.review_erasure(
            session,
            ReviewErasure(request_id=request_id, **body.model_dump()),
        )

    @app.get("/api/v1/memory-archives/private")
    async def export_private(
        session: Annotated[TenantSession, Depends(authenticated_session)],
    ) -> Response:
        return Response(
            content=await memory.export_private(session),
            media_type="application/x-ndjson",
        )

    @app.post("/api/v1/memory-archives/private/import")
    async def import_private(
        session: Annotated[TenantSession, Depends(authenticated_session)],
        archive: Annotated[bytes, Body(media_type="application/x-ndjson")],
    ) -> dict[str, object]:
        return (await memory.import_private(session, archive)).model_dump(mode="json")

    @app.get("/api/v1/memory-archives/tenant")
    async def export_tenant(
        session: Annotated[TenantSession, Depends(authenticated_session)],
    ) -> Response:
        return Response(
            content=await memory.export_tenant(session),
            media_type="application/x-ndjson",
        )

    @app.post("/api/v1/memory-archives/tenant/import")
    async def import_tenant(
        session: Annotated[TenantSession, Depends(authenticated_session)],
        archive: Annotated[bytes, Body(media_type="application/x-ndjson")],
    ) -> dict[str, object]:
        return (await memory.import_tenant(session, archive)).model_dump(mode="json")

    if telegram is not None:

        @app.post(
            "/api/v1/telegram/webhook",
            status_code=status.HTTP_204_NO_CONTENT,
            include_in_schema=False,
        )
        async def telegram_webhook(
            payload: dict[str, object],
            secret: Annotated[
                str,
                Header(alias="X-Telegram-Bot-Api-Secret-Token"),
            ] = "",
        ) -> Response:
            await telegram.handle_update(secret, payload)
            return Response(status_code=status.HTTP_204_NO_CONTENT)

    if agents is not None:
        background_runs: set[asyncio.Task[AgentRunView]] = set()

        @app.post(
            "/api/v1/agent-runs",
            response_model=AgentRunView,
            status_code=status.HTTP_202_ACCEPTED,
        )
        async def start_agent_run(
            invocation: AgentInvocation,
            session: Annotated[TenantSession, Depends(authenticated_session)],
        ) -> AgentRunView:
            run = await agents.start_agent_run(session, invocation)
            if telemetry is not None:
                telemetry.observe_agent_run(run.id, run.state.value)
            if execute_agent_runs:

                async def execute() -> AgentRunView:
                    if telemetry is None:
                        return await agents.run_until_terminal(session, run.id)
                    async with telemetry.operation("synthesis", session):
                        return await agents.run_until_terminal(session, run.id)

                task = asyncio.create_task(execute())
                background_runs.add(task)
                task.add_done_callback(background_runs.discard)
            return run

        @app.get("/api/v1/agent-runs/{run_id}", response_model=AgentRunView)
        async def get_agent_run(
            run_id: str,
            session: Annotated[TenantSession, Depends(authenticated_session)],
        ) -> AgentRunView:
            return agents.status(session, run_id)

        @app.delete("/api/v1/agent-runs/{run_id}", response_model=AgentRunView)
        async def cancel_agent_run(
            run_id: str,
            session: Annotated[TenantSession, Depends(authenticated_session)],
        ) -> AgentRunView:
            return agents.cancel(session, run_id)

    return app

"""Authenticated, intent-oriented MCP Adapter for the Memory Module."""

from __future__ import annotations

import time
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from pydantic import AnyHttpUrl
from starlette.applications import Starlette

from agent_memory_service.agents import AgentInvocation, AgentRuntimeModule
from agent_memory_service.auth import AuthenticationError, TokenService
from agent_memory_service.body_limits import RequestBodyLimitMiddleware
from agent_memory_service.governance import (
    ProposeKnowledge,
    ReviewDecision,
    ReviewKnowledge,
)
from agent_memory_service.lifecycle import CorrectMemory, RequestErasure
from agent_memory_service.memory import MemoryModule
from agent_memory_service.models import (
    MemoryKind,
    PrincipalKind,
    RecallQuery,
    RetainMemory,
    TenantSession,
)
from agent_memory_service.telemetry import SafeTelemetry


class OpaqueTokenVerifier(TokenVerifier):
    """Adapt tenant-scoped Access Tokens to MCP's resource-server middleware."""

    def __init__(self, tokens: TokenService, telemetry: SafeTelemetry | None = None) -> None:
        self._tokens = tokens
        self._telemetry = telemetry

    async def verify_token(self, token: str) -> AccessToken | None:
        started = time.perf_counter()
        try:
            session = self._tokens.authenticate(token)
        except AuthenticationError:
            if self._telemetry is not None:
                self._telemetry.observe_authentication("invalid", time.perf_counter() - started)
            return None
        if self._telemetry is not None:
            self._telemetry.observe_authentication("success", time.perf_counter() - started)
            self._telemetry.observe_authenticated_request(session)
        return AccessToken(
            token=token,
            client_id=session.actor_id,
            scopes=["memory", *sorted(session.roles)],
            subject=session.actor_id,
            claims={
                "tenant_id": session.tenant_id,
                "actor_id": session.actor_id,
                "actor_kind": session.actor_kind.value,
                "roles": sorted(session.roles),
                "subject_user_id": session.subject_user_id,
                "delegation_id": session.delegation_id,
                "token_id": session.token_id,
            },
        )


class BodyLimitedFastMCP(FastMCP[None]):
    """Keep the 2 MiB MCP ceiling when this Adapter is served standalone."""

    def streamable_http_app(self) -> Starlette:
        app = super().streamable_http_app()
        app.add_middleware(RequestBodyLimitMiddleware)
        return app


def create_memory_mcp_server(
    memory: MemoryModule,
    tokens: TokenService,
    *,
    agents: AgentRuntimeModule | None = None,
    public_url: str = "http://127.0.0.1:8080",
    telemetry: SafeTelemetry | None = None,
) -> FastMCP[None]:
    """Expose only the platform's authorized memory intentions over MCP."""

    base_url = AnyHttpUrl(public_url.rstrip("/") + "/")
    mcp: FastMCP[None] = BodyLimitedFastMCP(
        "CoEngram",
        instructions=(
            "Private Memory is scoped by the supplied credential. Tenant Knowledge "
            "requires human review. Never infer that an empty recall means memory is available."
        ),
        token_verifier=OpaqueTokenVerifier(tokens, telemetry),
        auth=AuthSettings(
            issuer_url=base_url,
            resource_server_url=base_url,
            required_scopes=["memory"],
        ),
        host="0.0.0.0",
        port=8080,
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
    )

    @mcp.tool(name="memory_recall", structured_output=True)
    async def memory_recall(
        query: str,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Recall the authenticated Principal's Private Memory and Tenant Knowledge."""

        result = await memory.recall(_tenant_session(), RecallQuery(query=query, limit=limit))
        return result.model_dump(mode="json")

    @mcp.tool(name="memory_retain", structured_output=True)
    async def memory_retain(
        content: str,
        idempotency_key: str,
        kind: MemoryKind = MemoryKind.EXPLICIT,
        confidence: float = 1.0,
    ) -> dict[str, Any]:
        """Retain an explicit item in the authenticated Principal's Private Memory."""

        result = await memory.retain_result(
            _tenant_session(),
            RetainMemory(
                content=content,
                kind=kind,
                confidence=confidence,
                idempotency_key=idempotency_key,
            ),
        )
        return result.model_dump(mode="json")

    @mcp.tool(name="memory_inspect_private", structured_output=True)
    async def memory_inspect_private() -> dict[str, Any]:
        """Inspect authoritative Private Memory lifecycle state, including erasures."""

        items = await memory.inspect_private(_tenant_session())
        return {"version": 1, "items": [item.model_dump(mode="json") for item in items]}

    @mcp.tool(name="knowledge_propose", structured_output=True)
    async def knowledge_propose(
        claim: str,
        source_memory_ids: list[str],
        idempotency_key: str,
        confidence: float = 1.0,
        duplicate_memory_ids: list[str] | None = None,
        conflicting_memory_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Submit a distilled Knowledge Candidate without changing source visibility."""

        candidate = await memory.propose_knowledge(
            _tenant_session(),
            ProposeKnowledge(
                claim=claim,
                source_memory_ids=tuple(source_memory_ids),
                duplicate_memory_ids=tuple(duplicate_memory_ids or ()),
                conflicting_memory_ids=tuple(conflicting_memory_ids or ()),
                confidence=confidence,
                idempotency_key=idempotency_key,
            ),
        )
        return candidate.model_dump(mode="json")

    @mcp.tool(name="knowledge_review", structured_output=True)
    async def knowledge_review(
        candidate_id: str,
        decision: ReviewDecision,
        rationale: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Approve or reject a Knowledge Candidate as an authorized human Curator."""

        candidate = await memory.review_knowledge(
            _tenant_session(),
            ReviewKnowledge(
                candidate_id=candidate_id,
                decision=decision,
                rationale=rationale,
                idempotency_key=idempotency_key,
            ),
        )
        return candidate.model_dump(mode="json")

    @mcp.tool(name="memory_correct", structured_output=True)
    async def memory_correct(
        memory_id: str,
        replacement_content: str,
        reason: str,
        idempotency_key: str,
        kind: MemoryKind = MemoryKind.EXPLICIT,
        confidence: float = 1.0,
    ) -> dict[str, Any]:
        """Supersede an owned Private Memory Item while preserving its history."""

        result = await memory.correct_result(
            _tenant_session(),
            CorrectMemory(
                memory_id=memory_id,
                replacement_content=replacement_content,
                kind=kind,
                confidence=confidence,
                reason=reason,
                idempotency_key=idempotency_key,
            ),
        )
        return result.model_dump(mode="json")

    @mcp.tool(name="memory_request_erasure", structured_output=True)
    async def memory_request_erasure(
        memory_id: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Request higher-approval erasure of an owned Private Memory Item."""

        request = await memory.request_erasure(
            _tenant_session(),
            RequestErasure(
                memory_id=memory_id,
                reason=reason,
                idempotency_key=idempotency_key,
            ),
        )
        return request.model_dump(mode="json")

    if agents is not None:

        @mcp.tool(name="knowledge_synthesize", structured_output=True)
        async def knowledge_synthesize(
            request: str,
            source_memory_ids: list[str],
            idempotency_key: str,
        ) -> dict[str, Any]:
            """Queue reviewed-knowledge synthesis from explicitly selected memories."""

            run = await agents.start_agent_run(
                _tenant_session(),
                AgentInvocation(
                    capability="knowledge_synthesis",
                    input={
                        "request": request,
                        "source_memory_ids": source_memory_ids,
                    },
                    idempotency_key=idempotency_key,
                ),
            )
            return run.model_dump(mode="json")

        @mcp.tool(name="agent_run_status", structured_output=True)
        async def agent_run_status(run_id: str) -> dict[str, Any]:
            """Inspect one Agent Run in the authenticated Tenant."""

            return agents.status(_tenant_session(), run_id).model_dump(mode="json")

        @mcp.tool(name="agent_run_cancel", structured_output=True)
        async def agent_run_cancel(run_id: str) -> dict[str, Any]:
            """Request cooperative cancellation of one Agent Run."""

            return agents.cancel(_tenant_session(), run_id).model_dump(mode="json")

    return mcp


def _tenant_session() -> TenantSession:
    access_token = get_access_token()
    if access_token is None or access_token.claims is None:
        raise PermissionError("Authenticated Tenant Session is unavailable")
    claims = access_token.claims
    roles = claims.get("roles")
    if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
        raise PermissionError("Authenticated Tenant Session is invalid")
    return TenantSession(
        tenant_id=str(claims["tenant_id"]),
        actor_id=str(claims["actor_id"]),
        actor_kind=PrincipalKind(str(claims["actor_kind"])),
        roles=frozenset(roles),
        subject_user_id=(
            None if claims.get("subject_user_id") is None else str(claims["subject_user_id"])
        ),
        delegation_id=(
            None if claims.get("delegation_id") is None else str(claims["delegation_id"])
        ),
        token_id=None if claims.get("token_id") is None else str(claims["token_id"]),
    )

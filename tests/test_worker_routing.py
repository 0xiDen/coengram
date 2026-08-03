from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict

from agent_memory_service.agents import (
    AgentCapability,
    AgentInvocation,
    AgentRunContext,
    AgentRunState,
    AgentRuntimeModule,
    InMemoryAgentRunRepository,
    RecordedProvider,
)
from agent_memory_service.auth import TokenService
from agent_memory_service.control import ControlModule, InMemoryControlStore
from agent_memory_service.governance import CandidateStatus, KnowledgeCandidate
from agent_memory_service.stores.memory import InMemoryTenantMemoryRouter
from agent_memory_service.worker import MemoryGraphProjector, reauthorize_agent_claim


class _NeverInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str


class _NeverCapability(AgentCapability[_NeverInput]):
    name = "never"
    input_model = _NeverInput

    async def execute(
        self,
        context: AgentRunContext,
        command: _NeverInput,
    ) -> dict[str, str]:
        del context, command
        raise AssertionError("revoked Agent Run executed application work")


@pytest.mark.asyncio
async def test_graph_projector_routes_candidate_to_exact_tenant_idempotently() -> None:
    router = InMemoryTenantMemoryRouter(["tenant-a", "tenant-b"])
    candidate = KnowledgeCandidate(
        id="candidate-1",
        tenant_id="tenant-a",
        claim="Use bounded retries.",
        confidence=0.9,
        proposer_id="user-alice",
        source_memory_ids=("private-1",),
        status=CandidateStatus.PUBLISHING,
        created_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    projector = MemoryGraphProjector(router)

    await projector.project(candidate)
    await projector.project(candidate)

    assert len(await router.for_tenant("tenant-a").list_tenant_knowledge()) == 1
    assert await router.for_tenant("tenant-b").list_tenant_knowledge() == ()


@pytest.mark.asyncio
async def test_worker_cancels_queued_delegated_run_after_authority_is_revoked() -> None:
    store = InMemoryControlStore()
    control = ControlModule(store, TokenService(store))
    control.create_tenant("tenant-a", "Product A")
    control.create_principal("user-alice", "Alice", "user")
    control.create_principal("agent-helper", "Helper", "agent")
    control.grant_membership("tenant-a", "user-alice", "tenant_member")
    control.grant_membership("tenant-a", "agent-helper", "tenant_member")
    delegation = control.create_delegation(
        "delegation-alice-helper",
        tenant_id="tenant-a",
        agent_id="agent-helper",
        subject_user_id="user-alice",
    )
    session = control.authenticate(
        control.issue_delegated_access_token(delegation.delegation_id).access_token
    )
    repository = InMemoryAgentRunRepository()
    runtime = AgentRuntimeModule(
        capabilities=(_NeverCapability(),),
        provider=RecordedProvider([]),
        repository=repository,
    )
    started = await runtime.start_agent_run(
        session,
        AgentInvocation(
            capability="never",
            input={"text": "must not leave the queue"},
            idempotency_key="revoked-before-worker",
        ),
    )
    claim = repository.claim_runnable("tenant-a", lease_seconds=60)
    assert claim is not None

    control.revoke_delegation(delegation.delegation_id)
    reauthorized = reauthorize_agent_claim(control, repository, claim)
    cancelled = await runtime.run_claimed(reauthorized)

    assert cancelled.id == started.id
    assert cancelled.state is AgentRunState.CANCELLED
    persisted = repository.get("tenant-a", started.id)
    assert persisted is not None
    assert persisted.state is AgentRunState.CANCELLED

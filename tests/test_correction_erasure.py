from __future__ import annotations

import pytest

from agent_memory_service.lifecycle import (
    CorrectMemory,
    ErasureDecision,
    ErasureStatus,
    InMemoryErasureStore,
    RequestErasure,
    ReviewErasure,
)
from agent_memory_service.memory import MemoryModule
from agent_memory_service.models import (
    MemoryKind,
    PrincipalKind,
    RecallQuery,
    RetainMemory,
    TenantSession,
)
from agent_memory_service.stores.memory import InMemoryTenantMemoryRouter


def _user(principal_id: str, *roles: str) -> TenantSession:
    return TenantSession(
        tenant_id="tenant-a",
        actor_id=principal_id,
        actor_kind=PrincipalKind.USER,
        roles=frozenset(roles or ("tenant_member",)),
    )


@pytest.mark.asyncio
async def test_correction_supersedes_owned_memory_without_rewriting_history() -> None:
    router = InMemoryTenantMemoryRouter(["tenant-a"])
    memory = MemoryModule(router, erasures=InMemoryErasureStore())
    alice = _user("user-alice")
    original = await memory.retain(
        alice,
        RetainMemory(
            content="Deployments happen on Thursday.",
            kind=MemoryKind.OUTCOME,
            idempotency_key="deployment-day",
        ),
    )

    corrected = await memory.correct(
        alice,
        CorrectMemory(
            memory_id=original.id,
            replacement_content="Deployments happen on Tuesday.",
            kind=MemoryKind.OUTCOME,
            reason="The release calendar changed.",
            idempotency_key="correct-deployment-day",
        ),
    )

    assert corrected.supersedes_id == original.id
    assert [
        item.content
        for item in (await memory.recall(alice, RecallQuery(query="deployments"))).items
    ] == ["Deployments happen on Tuesday."]


@pytest.mark.asyncio
async def test_erasure_requires_separate_admin_approval_and_worker_completion() -> None:
    router = InMemoryTenantMemoryRouter(["tenant-a"])
    erasures = InMemoryErasureStore()
    memory = MemoryModule(router, erasures=erasures)
    alice = _user("user-alice")
    admin = _user("user-admin", "tenant_member", "tenant_administrator")
    item = await memory.retain(
        alice,
        RetainMemory(
            content="Alice's temporary incident bridge code is 1234.",
            idempotency_key="temporary-bridge-code",
        ),
    )
    request = await memory.request_erasure(
        alice,
        RequestErasure(
            memory_id=item.id,
            reason="The temporary secret must be removed.",
            idempotency_key="erase-bridge-code",
        ),
    )

    assert request.status is ErasureStatus.REQUESTED
    assert not hasattr(request, "content")
    assert (await memory.recall(alice, RecallQuery(query="bridge code"))).items

    approved = await memory.review_erasure(
        admin,
        ReviewErasure(
            request_id=request.id,
            decision=ErasureDecision.APPROVE,
            rationale="Ownership and scope confirmed.",
            idempotency_key="approve-bridge-erasure",
        ),
    )
    assert approved.status is ErasureStatus.APPROVED
    assert (await memory.recall(alice, RecallQuery(query="bridge code"))).items

    completed = await memory.erase_next("tenant-a")
    assert completed is not None
    assert completed.status is ErasureStatus.COMPLETED
    assert completed.review_rationale is None
    completed_record = await erasures.get_request("tenant-a", request.id)
    assert completed_record is not None
    assert completed_record.reason is None
    assert completed_record.review_rationale is None
    assert (await memory.recall(alice, RecallQuery(query="bridge code"))).items == ()


@pytest.mark.asyncio
async def test_requester_cannot_approve_own_erasure_even_when_administrator() -> None:
    router = InMemoryTenantMemoryRouter(["tenant-a"])
    memory = MemoryModule(router, erasures=InMemoryErasureStore())
    admin = _user("user-admin", "tenant_member", "tenant_administrator")
    item = await memory.retain(
        admin,
        RetainMemory(content="Sensitive note.", idempotency_key="sensitive-note"),
    )
    request = await memory.request_erasure(
        admin,
        RequestErasure(
            memory_id=item.id,
            reason="No longer needed.",
            idempotency_key="erase-sensitive-note",
        ),
    )

    with pytest.raises(PermissionError, match="separate Tenant Administrator"):
        await memory.review_erasure(
            admin,
            ReviewErasure(
                request_id=request.id,
                decision=ErasureDecision.APPROVE,
                rationale="Self approval.",
                idempotency_key="self-approve-erasure",
            ),
        )


@pytest.mark.asyncio
async def test_delegated_erasure_attributes_agent_actor_and_user_owner() -> None:
    router = InMemoryTenantMemoryRouter(["tenant-a"])
    memory = MemoryModule(router, erasures=InMemoryErasureStore())
    delegated_agent = TenantSession(
        tenant_id="tenant-a",
        actor_id="agent-telegram",
        actor_kind=PrincipalKind.AGENT,
        roles=frozenset({"tenant_member"}),
        subject_user_id="user-alice",
        delegation_id="delegation-alice-telegram",
    )
    retained = await memory.retain(
        delegated_agent,
        RetainMemory(content="Alice sensitive note", idempotency_key="delegated-note"),
    )

    request = await memory.request_erasure(
        delegated_agent,
        RequestErasure(
            memory_id=retained.id,
            reason="Alice requested removal",
            idempotency_key="delegated-erasure",
        ),
    )

    assert request.requester_id == "agent-telegram"
    assert request.owner_principal_id == "user-alice"

    owning_admin = _user("user-alice", "tenant_member", "tenant_administrator")
    with pytest.raises(PermissionError, match="separate Tenant Administrator"):
        await memory.review_erasure(
            owning_admin,
            ReviewErasure(
                request_id=request.id,
                decision=ErasureDecision.APPROVE,
                rationale="The delegated owner cannot approve the Agent's request.",
                idempotency_key="delegated-owner-self-approval",
            ),
        )

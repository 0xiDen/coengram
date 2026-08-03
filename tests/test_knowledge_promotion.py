from __future__ import annotations

import pytest

from agent_memory_service.auth import TokenService
from agent_memory_service.control import ControlModule, InMemoryControlStore, MembershipRecord
from agent_memory_service.governance import (
    CandidateStatus,
    InMemoryGovernanceStore,
    ProposeKnowledge,
    ReviewDecision,
    ReviewKnowledge,
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


def _session(
    principal_id: str,
    *roles: str,
    kind: PrincipalKind = PrincipalKind.USER,
) -> TenantSession:
    return TenantSession(
        tenant_id="tenant-a",
        actor_id=principal_id,
        actor_kind=kind,
        roles=frozenset(roles),
    )


@pytest.mark.asyncio
async def test_approved_candidate_is_recallable_only_after_idempotent_publication() -> None:
    router = InMemoryTenantMemoryRouter(["tenant-a"])
    governance = InMemoryGovernanceStore()
    memory = MemoryModule(router, governance)
    alice = _session("user-alice", "tenant_member")
    bob = _session("user-bob", "tenant_member")
    curator = _session("user-curator", "tenant_member", "knowledge_curator")
    source = await memory.retain(
        alice,
        RetainMemory(
            content="Product A database changes use expand-contract migrations.",
            kind=MemoryKind.CONSTRAINT,
            idempotency_key="alice-expand-contract",
        ),
    )

    candidate = await memory.propose_knowledge(
        alice,
        ProposeKnowledge(
            claim="Product A uses expand-contract database migrations.",
            source_memory_ids=(source.id,),
            confidence=0.95,
            duplicate_memory_ids=("tenant-memory-duplicate",),
            conflicting_memory_ids=("tenant-memory-conflict",),
            idempotency_key="candidate-expand-contract",
        ),
    )

    assert candidate.status is CandidateStatus.SUBMITTED
    assert candidate.source_count == 1
    assert candidate.duplicate_memory_ids == ("tenant-memory-duplicate",)
    assert candidate.conflicting_memory_ids == ("tenant-memory-conflict",)
    assert not hasattr(candidate, "source_memory_ids")
    assert (await memory.recall(bob, RecallQuery(query="database migrations"))).items == ()

    approved = await memory.review_knowledge(
        curator,
        ReviewKnowledge(
            candidate_id=candidate.id,
            decision=ReviewDecision.APPROVE,
            rationale="Confirmed engineering convention.",
            idempotency_key="approve-expand-contract",
        ),
    )

    assert approved.status is CandidateStatus.PUBLISHING
    assert (await memory.recall(bob, RecallQuery(query="database migrations"))).items == ()

    published = await memory.publish_next("tenant-a")
    repeated = await memory.publish_next("tenant-a")
    recalled = await memory.recall(bob, RecallQuery(query="database migrations"))

    assert published is not None
    assert published.status is CandidateStatus.PUBLISHED
    assert repeated is None
    assert [item.content for item in recalled.items] == [
        "Product A uses expand-contract database migrations."
    ]


@pytest.mark.asyncio
async def test_agent_can_propose_but_cannot_review_tenant_knowledge() -> None:
    router = InMemoryTenantMemoryRouter(["tenant-a"])
    memory = MemoryModule(router, InMemoryGovernanceStore())
    agent = _session("agent-synthesis", "tenant_member", kind=PrincipalKind.AGENT)
    source = await memory.retain(
        agent,
        RetainMemory(
            content="Compare a proposed claim with published knowledge before submission.",
            kind=MemoryKind.CONSTRAINT,
            idempotency_key="agent-deduplicate",
        ),
    )
    candidate = await memory.propose_knowledge(
        agent,
        ProposeKnowledge(
            claim="Knowledge proposals must be checked for duplicates.",
            source_memory_ids=(source.id,),
            idempotency_key="agent-candidate-deduplicate",
        ),
    )

    with pytest.raises(PermissionError, match="human Knowledge Curator"):
        await memory.review_knowledge(
            agent,
            ReviewKnowledge(
                candidate_id=candidate.id,
                decision=ReviewDecision.APPROVE,
                rationale="Self approved.",
                idempotency_key="agent-self-approve",
            ),
        )


@pytest.mark.asyncio
async def test_only_single_human_tenant_administrator_may_self_approve() -> None:
    router = InMemoryTenantMemoryRouter(["tenant-solo"])
    governance = InMemoryGovernanceStore()
    control_store = InMemoryControlStore()
    control = ControlModule(control_store, TokenService(control_store))
    control.create_tenant("tenant-solo", "Solo tenant")
    control.create_principal("user-admin", "Admin", PrincipalKind.USER.value)
    control.grant_membership("tenant-solo", "user-admin", "tenant_administrator")
    control.grant_membership("tenant-solo", "user-admin", "knowledge_curator")
    memory = MemoryModule(router, governance, self_approval=control)
    admin = TenantSession(
        tenant_id="tenant-solo",
        actor_id="user-admin",
        actor_kind=PrincipalKind.USER,
        roles=frozenset({"tenant_administrator", "knowledge_curator"}),
    )
    source = await memory.retain(
        admin,
        RetainMemory(content="Solo tenant rule.", idempotency_key="solo-rule"),
    )
    candidate = await memory.propose_knowledge(
        admin,
        ProposeKnowledge(
            claim="The solo tenant has a reviewed rule.",
            source_memory_ids=(source.id,),
            idempotency_key="solo-candidate",
        ),
    )

    reviewed = await memory.review_knowledge(
        admin,
        ReviewKnowledge(
            candidate_id=candidate.id,
            decision=ReviewDecision.APPROVE,
            rationale="Only human member.",
            idempotency_key="solo-approve",
        ),
    )

    assert reviewed.status is CandidateStatus.PUBLISHING

    second_source = await memory.retain(
        admin,
        RetainMemory(content="Second solo tenant rule.", idempotency_key="solo-rule-2"),
    )
    second_candidate = await memory.propose_knowledge(
        admin,
        ProposeKnowledge(
            claim="The solo tenant has another reviewed rule.",
            source_memory_ids=(second_source.id,),
            idempotency_key="solo-candidate-2",
        ),
    )
    control.create_principal("user-admin-2", "Second human", PrincipalKind.USER.value)
    control.grant_membership("tenant-solo", "user-admin-2", "tenant_member")

    with pytest.raises(PermissionError, match="Self-approval"):
        await memory.review_knowledge(
            admin,
            ReviewKnowledge(
                candidate_id=second_candidate.id,
                decision=ReviewDecision.APPROVE,
                rationale="There is now another human member.",
                idempotency_key="solo-approve-2-denied",
            ),
        )

    second_membership = control_store.get_membership("tenant-solo", "user-admin-2")
    assert second_membership is not None
    control_store.save_membership(
        MembershipRecord(
            tenant_id=second_membership.tenant_id,
            principal_id=second_membership.principal_id,
            roles=second_membership.roles,
            active=False,
        )
    )
    dynamically_allowed = await memory.review_knowledge(
        admin,
        ReviewKnowledge(
            candidate_id=second_candidate.id,
            decision=ReviewDecision.APPROVE,
            rationale="The second human member is inactive.",
            idempotency_key="solo-approve-2-allowed",
        ),
    )
    assert dynamically_allowed.status is CandidateStatus.PUBLISHING

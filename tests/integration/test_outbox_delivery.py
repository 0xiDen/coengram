"""Real PostgreSQL and RabbitMQ publication behavior through public Interfaces."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import aio_pika
import pytest

from agent_memory_service.governance import (
    CandidateStatus,
    KnowledgeCandidate,
    ProposeKnowledge,
    ReviewDecision,
    ReviewKnowledge,
)
from agent_memory_service.lifecycle import (
    ErasureDecision,
    ErasureStatus,
    RequestErasure,
    ReviewErasure,
)
from agent_memory_service.memory import MemoryModule
from agent_memory_service.models import PrincipalKind, RecallQuery, RetainMemory, TenantSession
from agent_memory_service.outbox import (
    PUBLICATION_ROUTING_KEY,
    DeadLetterRedriver,
    OutboxRecord,
    OutboxRelay,
    PublicationWorker,
    SingleTenantPublicationRouter,
    connect_outbox,
    declare_outbox_topology,
)
from agent_memory_service.stores.memory import InMemoryTenantMemoryRouter
from agent_memory_service.stores.postgres_governance import PostgresGovernanceStore

TENANT_DATABASE_URL = os.environ.get("TENANT_DATABASE_URL")
AMQP_URL = os.environ.get("AMQP_URL")

pytestmark = pytest.mark.skipif(
    not TENANT_DATABASE_URL or not AMQP_URL,
    reason="TENANT_DATABASE_URL and AMQP_URL are required for outbox integration tests",
)


@pytest.fixture(scope="module", autouse=True)
def migrated_tenant_store() -> None:
    if not TENANT_DATABASE_URL or not AMQP_URL:
        return
    from alembic import command
    from alembic.config import Config

    repository_root = Path(__file__).resolve().parents[2]
    command.upgrade(Config(repository_root / "alembic-tenant.ini"), "head")


class _IdempotentProjector:
    def __init__(self, router: InMemoryTenantMemoryRouter) -> None:
        self._router = router
        self.calls: list[str] = []

    async def project(self, candidate: KnowledgeCandidate) -> None:
        self.calls.append(candidate.id)
        await self._router.for_tenant(candidate.tenant_id).publish_tenant_knowledge(
            candidate.id,
            candidate.claim,
            candidate.confidence,
            candidate.proposer_id,
        )


class _FailingProjector:
    def __init__(self) -> None:
        self.attempts = 0

    async def project(self, candidate: KnowledgeCandidate) -> None:
        self.attempts += 1
        raise RuntimeError(f"Graph projection unavailable for {candidate.id}")


@pytest.mark.asyncio
async def test_atomic_review_relay_redelivery_and_dead_letter() -> None:
    assert TENANT_DATABASE_URL is not None
    assert AMQP_URL is not None
    suffix = uuid4().hex
    tenant_id = f"tenant-{suffix}"
    namespace = f"memory.test.{suffix}"
    router = InMemoryTenantMemoryRouter([tenant_id])
    governance = PostgresGovernanceStore(TENANT_DATABASE_URL)
    memory = MemoryModule(router, governance, erasures=governance)
    alice = TenantSession(
        tenant_id=tenant_id,
        actor_id=f"alice-{suffix}",
        actor_kind=PrincipalKind.USER,
        roles=frozenset({"tenant_member"}),
    )
    curator = TenantSession(
        tenant_id=tenant_id,
        actor_id=f"curator-{suffix}",
        actor_kind=PrincipalKind.USER,
        roles=frozenset({"tenant_member", "knowledge_curator", "tenant_administrator"}),
    )

    source = await memory.retain(
        alice,
        RetainMemory(
            content="Product A uses expand-contract database migrations.",
            idempotency_key=f"source-{suffix}",
        ),
    )
    proposed = await memory.propose_knowledge(
        alice,
        ProposeKnowledge(
            claim="Database changes use expand-contract migrations.",
            source_memory_ids=(source.id,),
            confidence=0.95,
            duplicate_memory_ids=("tenant-memory-duplicate",),
            conflicting_memory_ids=("tenant-memory-conflict",),
            idempotency_key=f"proposal-{suffix}",
        ),
    )
    repeated = await memory.propose_knowledge(
        alice,
        ProposeKnowledge(
            claim="A retry must return the original claim.",
            source_memory_ids=(source.id,),
            confidence=0.1,
            idempotency_key=f"proposal-{suffix}",
        ),
    )
    assert repeated.id == proposed.id
    assert repeated.claim == proposed.claim
    assert repeated.duplicate_memory_ids == ("tenant-memory-duplicate",)
    assert repeated.conflicting_memory_ids == ("tenant-memory-conflict",)

    approved = await memory.review_knowledge(
        curator,
        ReviewKnowledge(
            candidate_id=proposed.id,
            decision=ReviewDecision.APPROVE,
            rationale="Confirmed convention.",
            idempotency_key=f"review-{suffix}",
        ),
    )
    repeated_review = await memory.review_knowledge(
        curator,
        ReviewKnowledge(
            candidate_id=proposed.id,
            decision=ReviewDecision.REJECT,
            rationale="A retry must return the original decision.",
            idempotency_key=f"review-{suffix}",
        ),
    )
    publication = await governance.next_publication(tenant_id)
    assert approved.status is CandidateStatus.PUBLISHING
    assert repeated_review.status is CandidateStatus.PUBLISHING
    assert publication is not None
    assert (await memory.recall(curator, RecallQuery(query="expand-contract"))).items == ()

    connection = await connect_outbox(AMQP_URL)
    topology = await declare_outbox_topology(connection, namespace=namespace, retry_limit=3)
    projector = _IdempotentProjector(router)
    publication_routing = SingleTenantPublicationRouter(tenant_id, governance)
    worker = PublicationWorker(publication_routing, projector)
    consumer_tag = await worker.start(topology.queue)
    consumer_active = True
    failing_consumer_tag: str | None = None
    try:
        relay = OutboxRelay(governance, topology.exchange)
        assert await relay.relay_once() == 1
        await _wait_for_candidate(governance, tenant_id, proposed.id, CandidateStatus.PUBLISHED)
        recalled = await memory.recall(curator, RecallQuery(query="expand-contract"))
        assert [item.content for item in recalled.items] == [proposed.claim]

        duplicate = OutboxRecord(
            event_id=publication.id,
            tenant_id=tenant_id,
            aggregate_id=proposed.id,
            event_type="knowledge.approved",
            created_at=publication.created_at,
            lock_id="duplicate-delivery",
        )
        await topology.queue.cancel(consumer_tag)
        consumer_active = False
        await topology.exchange.publish(
            duplicate.message(),
            routing_key=PUBLICATION_ROUTING_KEY,
            mandatory=True,
        )
        duplicate_delivery = await _wait_for_message(topology.queue)
        await worker.handle(duplicate_delivery)
        assert projector.calls == [proposed.id]

        erasure = await memory.request_erasure(
            alice,
            RequestErasure(
                memory_id=source.id,
                reason="Exercise PostgreSQL erasure governance.",
                idempotency_key=f"erasure-{suffix}",
            ),
        )
        approved_erasure = await memory.review_erasure(
            curator,
            ReviewErasure(
                request_id=erasure.id,
                decision=ErasureDecision.APPROVE,
                rationale="Separate administrator approval.",
                idempotency_key=f"erasure-review-{suffix}",
            ),
        )
        completed_erasure = await memory.erase_next(tenant_id)
        assert approved_erasure.status is ErasureStatus.APPROVED
        assert completed_erasure is not None
        assert completed_erasure.status is ErasureStatus.COMPLETED

        second_source = await memory.retain(
            alice,
            RetainMemory(
                content="Poison publications remain authoritative in PostgreSQL.",
                idempotency_key=f"poison-source-{suffix}",
            ),
        )
        poison = await memory.propose_knowledge(
            alice,
            ProposeKnowledge(
                claim="Poison publications are inspectable and retryable.",
                source_memory_ids=(second_source.id,),
                idempotency_key=f"poison-proposal-{suffix}",
            ),
        )
        await memory.review_knowledge(
            curator,
            ReviewKnowledge(
                candidate_id=poison.id,
                decision=ReviewDecision.APPROVE,
                rationale="Exercise the dead-letter path.",
                idempotency_key=f"poison-review-{suffix}",
            ),
        )
        poison_publication = await governance.next_publication(tenant_id)
        assert poison_publication is not None
        failing_projector = _FailingProjector()
        failing_consumer_tag = await PublicationWorker(
            publication_routing,
            failing_projector,
        ).start(topology.queue)
        assert await relay.relay_once() == 1
        await _wait_for_message(topology.dead_letter_queue, acknowledge=False)
        persisted = await governance.get_candidate(tenant_id, poison.id)
        assert persisted is not None
        assert persisted.status is CandidateStatus.PUBLISHING
        assert failing_projector.attempts >= 3

        await topology.queue.cancel(failing_consumer_tag)
        failing_consumer_tag = None
        redrive = await DeadLetterRedriver(publication_routing).redrive_exact(
            topology.dead_letter_queue,
            tenant_id=tenant_id,
            event_id=poison_publication.id,
            operator_id=f"operator-{suffix}",
        )
        assert redrive.state == "scheduled"
        assert await relay.relay_once() == 1
        redelivered = await _wait_for_message(topology.queue)
        await worker.handle(redelivered)
        await _wait_for_candidate(governance, tenant_id, poison.id, CandidateStatus.PUBLISHED)
        assert projector.calls == [proposed.id, poison.id]
        assert await topology.dead_letter_queue.get(fail=False) is None
    finally:
        if consumer_active:
            await topology.queue.cancel(consumer_tag)
        if failing_consumer_tag is not None:
            await topology.queue.cancel(failing_consumer_tag)
        await topology.queue.delete(if_unused=False, if_empty=False)
        await topology.dead_letter_queue.delete(if_unused=False, if_empty=False)
        await topology.exchange.delete(if_unused=False)
        await topology.dead_letter_exchange.delete(if_unused=False)
        await connection.close()


async def _wait_for_candidate(
    governance: PostgresGovernanceStore,
    tenant_id: str,
    candidate_id: str,
    expected: CandidateStatus,
) -> None:
    async with asyncio.timeout(15):
        while True:
            candidate = await governance.get_candidate(tenant_id, candidate_id)
            if candidate is not None and candidate.status is expected:
                return
            await asyncio.sleep(0.05)


async def _wait_for_message(
    queue: aio_pika.abc.AbstractQueue,
    *,
    acknowledge: bool = True,
) -> aio_pika.abc.AbstractIncomingMessage:
    async with asyncio.timeout(15):
        while True:
            message = await queue.get(fail=False, timeout=1)
            if message is not None:
                if not acknowledge:
                    await message.reject(requeue=True)
                return message

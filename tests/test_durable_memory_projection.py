from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agent_memory_service.durable_memory import (
    PrivateMemoryCommand,
    PrivateMemoryCommandState,
    PrivateMemoryCommandType,
)
from agent_memory_service.governance import KnowledgeCandidate, PublicationEvent
from agent_memory_service.models import (
    MemoryItem,
    MemoryKind,
    MemoryScope,
    Provenance,
)
from agent_memory_service.outbox import OutboxRecord, PublicationWorker
from agent_memory_service.stores.memory import InMemoryTenantMemoryRouter
from agent_memory_service.worker import DurableMemoryGraphProjector


class _UnusedKnowledgeProjector:
    async def project(self, candidate: KnowledgeCandidate) -> None:
        raise AssertionError(f"unexpected knowledge projection: {candidate.id}")


class _CrashAfterGraphState:
    def __init__(self, command: PrivateMemoryCommand) -> None:
        self.command = command
        self.fail_marks = 1

    async def get_memory_command(
        self, tenant_id: str, command_id: str
    ) -> PrivateMemoryCommand | None:
        if tenant_id == self.command.tenant_id and command_id == self.command.id:
            return self.command
        return None

    async def mark_memory_command_applied(
        self, tenant_id: str, command_id: str
    ) -> PrivateMemoryCommand:
        if self.fail_marks:
            self.fail_marks -= 1
            raise RuntimeError("simulated crash before PostgreSQL completion")
        self.command = self.command.model_copy(update={"state": PrivateMemoryCommandState.APPLIED})
        return self.command

    async def get_publication(
        self, event_id: str, tenant_id: str, candidate_id: str
    ) -> PublicationEvent | None:
        raise AssertionError("unexpected publication lookup")

    async def get_candidate(self, tenant_id: str, candidate_id: str) -> KnowledgeCandidate | None:
        raise AssertionError("unexpected candidate lookup")

    async def mark_published(self, tenant_id: str, candidate_id: str) -> KnowledgeCandidate:
        raise AssertionError("unexpected publication completion")

    async def schedule_outbox_redelivery(
        self,
        tenant_id: str,
        event_id: str,
        operator_id: str,
    ) -> None:
        raise AssertionError("unexpected dead-letter redelivery")


class _SingleStateRoute:
    def __init__(self, state: _CrashAfterGraphState) -> None:
        self.state = state

    def for_tenant(self, tenant_id: str) -> _CrashAfterGraphState:
        if tenant_id != self.state.command.tenant_id:
            raise LookupError("wrong Tenant")
        return self.state


class _Message:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.message_id = "event-1"
        self.acks = 0
        self.rejects = 0

    async def ack(self) -> None:
        self.acks += 1

    async def reject(self, *, requeue: bool) -> None:
        assert requeue
        self.rejects += 1


def _command(
    command_type: PrivateMemoryCommandType,
    *,
    result_item: MemoryItem | None,
    target_memory_id: str | None,
) -> PrivateMemoryCommand:
    return PrivateMemoryCommand(
        id=f"command-{command_type.value}",
        tenant_id="tenant-a",
        actor_id="user-alice",
        owner_principal_id="user-alice",
        command_type=command_type,
        idempotency_key=f"key-{command_type.value}",
        target_memory_id=target_memory_id,
        result_item=result_item,
        erasure_request_id=None,
        state=PrivateMemoryCommandState.ACCEPTED,
        created_at=datetime.now(UTC),
    )


def _delivery(command: PrivateMemoryCommand) -> _Message:
    outgoing = OutboxRecord(
        event_id="event-1",
        tenant_id=command.tenant_id,
        aggregate_type="private_memory_command",
        aggregate_id=command.id,
        event_type="memory.command.accepted",
        created_at=datetime.now(UTC),
        lock_id="lock-1",
    ).message()
    return _Message(outgoing.body)


@pytest.mark.asyncio
async def test_retain_redelivery_after_completion_crash_is_idempotent() -> None:
    router = InMemoryTenantMemoryRouter(["tenant-a"])
    item = MemoryItem(
        id="memory-exact-id",
        owner_principal_id="user-alice",
        scope=MemoryScope.PRIVATE,
        content="Use expand-contract migrations.",
        kind=MemoryKind.EXPLICIT,
        confidence=0.9,
        provenance=Provenance(actor_id="user-alice", source="explicit"),
    )
    state = _CrashAfterGraphState(
        _command(PrivateMemoryCommandType.RETAIN, result_item=item, target_memory_id=None)
    )
    worker = PublicationWorker(
        _SingleStateRoute(state),
        _UnusedKnowledgeProjector(),
        DurableMemoryGraphProjector(router),
    )
    delivery = _delivery(state.command)

    await worker.handle(delivery)  # type: ignore[arg-type]
    await worker.handle(delivery)  # type: ignore[arg-type]

    assert delivery.rejects == 1
    assert delivery.acks == 1
    assert [
        entry.id for entry in await router.for_tenant("tenant-a").list_private("user-alice")
    ] == ["memory-exact-id"]
    assert state.command.state is PrivateMemoryCommandState.APPLIED


@pytest.mark.asyncio
async def test_erasure_redelivery_treats_already_missing_graph_item_as_success() -> None:
    router = InMemoryTenantMemoryRouter(["tenant-a"])
    item = MemoryItem(
        id="memory-to-erase",
        owner_principal_id="user-alice",
        scope=MemoryScope.PRIVATE,
        content="Forget this after approval.",
        kind=MemoryKind.EXPLICIT,
        confidence=1.0,
        provenance=Provenance(actor_id="user-alice", source="explicit"),
    )
    await router.for_tenant("tenant-a").import_private("user-alice", item)
    state = _CrashAfterGraphState(
        _command(
            PrivateMemoryCommandType.ERASE,
            result_item=None,
            target_memory_id=item.id,
        )
    )
    worker = PublicationWorker(
        _SingleStateRoute(state),
        _UnusedKnowledgeProjector(),
        DurableMemoryGraphProjector(router),
    )
    delivery = _delivery(state.command)

    await worker.handle(delivery)  # type: ignore[arg-type]
    await worker.handle(delivery)  # type: ignore[arg-type]

    assert delivery.rejects == 1
    assert delivery.acks == 1
    assert await router.for_tenant("tenant-a").list_private("user-alice") == ()
    assert state.command.state is PrivateMemoryCommandState.APPLIED

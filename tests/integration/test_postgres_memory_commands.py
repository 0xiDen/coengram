"""Real PostgreSQL contract for authoritative private-memory commands."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from agent_memory_service.durable_memory import PrivateMemoryCommandState
from agent_memory_service.lifecycle import (
    CorrectMemory,
    ErasureDecision,
    ErasureStatus,
    ErasureTombstone,
    RequestErasure,
    ReviewErasure,
)
from agent_memory_service.memory import MemoryModule
from agent_memory_service.models import (
    MemoryItem,
    MemoryKind,
    MemoryScope,
    PrincipalKind,
    Provenance,
    RetainMemory,
    TenantSession,
)
from agent_memory_service.stores.memory import InMemoryTenantMemoryRouter
from agent_memory_service.stores.postgres_governance import PostgresGovernanceStore

TENANT_DATABASE_URL = os.environ.get("TENANT_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TENANT_DATABASE_URL,
    reason="TENANT_DATABASE_URL is required for PostgreSQL integration tests",
)


@pytest.fixture(scope="module", autouse=True)
def migrated_tenant_store() -> None:
    if not TENANT_DATABASE_URL:
        return
    from alembic import command
    from alembic.config import Config

    repository_root = Path(__file__).resolve().parents[2]
    command.upgrade(Config(repository_root / "alembic-tenant.ini"), "head")


@pytest.mark.asyncio
async def test_delegated_owner_cannot_approve_agent_requested_erasure() -> None:
    assert TENANT_DATABASE_URL is not None
    suffix = uuid4().hex
    tenant_id = f"tenant-{suffix}"
    owner_id = f"owner-admin-{suffix}"
    router = InMemoryTenantMemoryRouter([tenant_id])
    governance = PostgresGovernanceStore(TENANT_DATABASE_URL)
    memory = MemoryModule(router, erasures=governance)
    delegated_agent = TenantSession(
        tenant_id=tenant_id,
        actor_id=f"agent-{suffix}",
        actor_kind=PrincipalKind.AGENT,
        roles=frozenset({"tenant_member"}),
        subject_user_id=owner_id,
        delegation_id=f"delegation-{suffix}",
    )
    owning_admin = TenantSession(
        tenant_id=tenant_id,
        actor_id=owner_id,
        actor_kind=PrincipalKind.USER,
        roles=frozenset({"tenant_member", "tenant_administrator"}),
    )
    retained = await memory.retain(
        delegated_agent,
        RetainMemory(content="Delegated private note.", idempotency_key=f"note-{suffix}"),
    )
    erasure = await memory.request_erasure(
        delegated_agent,
        RequestErasure(
            memory_id=retained.id,
            reason="Owner requested removal through the Agent.",
            idempotency_key=f"erase-{suffix}",
        ),
    )

    with pytest.raises(PermissionError, match="separate Tenant Administrator"):
        await memory.review_erasure(
            owning_admin,
            ReviewErasure(
                request_id=erasure.id,
                decision=ErasureDecision.APPROVE,
                rationale="Owner must not approve the delegated request.",
                idempotency_key=f"review-{suffix}",
            ),
        )


@pytest.mark.asyncio
async def test_commands_are_idempotent_atomic_and_erasure_redacts_content() -> None:
    assert TENANT_DATABASE_URL is not None
    suffix = uuid4().hex
    tenant_id = f"tenant-{suffix}"
    actor_id = f"alice-{suffix}"
    reviewer_id = f"admin-{suffix}"
    store = PostgresGovernanceStore(TENANT_DATABASE_URL)

    retained = await store.accept_retain(
        tenant_id,
        actor_id,
        actor_id,
        RetainMemory(
            content="Use expand-contract migrations.",
            kind=MemoryKind.CONSTRAINT,
            confidence=0.9,
            idempotency_key=f"retain-{suffix}",
        ),
    )
    repeated = await store.accept_retain(
        tenant_id,
        actor_id,
        actor_id,
        RetainMemory(
            content="A retry cannot replace committed content.",
            idempotency_key=f"retain-{suffix}",
        ),
    )
    assert repeated == retained

    retained_command_id = await _command_id_for_result(TENANT_DATABASE_URL, tenant_id, retained.id)
    retained_command = await store.get_memory_command(tenant_id, retained_command_id)
    assert retained_command is not None
    assert retained_command.state is PrivateMemoryCommandState.ACCEPTED
    assert await store.mark_memory_command_applied(tenant_id, retained_command_id)

    corrected = await store.accept_correction(
        tenant_id,
        actor_id,
        actor_id,
        CorrectMemory(
            memory_id=retained.id,
            replacement_content="Use expand-contract migrations with bounded backfills.",
            kind=MemoryKind.CONSTRAINT,
            confidence=0.95,
            reason="Add the operational limit.",
            idempotency_key=f"correct-{suffix}",
        ),
    )
    assert corrected.supersedes_id == retained.id
    corrected_command_id = await _command_id_for_result(
        TENANT_DATABASE_URL, tenant_id, corrected.id
    )
    await store.mark_memory_command_applied(tenant_id, corrected_command_id)

    imported_item = MemoryItem(
        id=f"archive-{suffix}",
        owner_principal_id="source-owner",
        scope=MemoryScope.PRIVATE,
        content="Imported private lesson.",
        kind=MemoryKind.EXPLICIT,
        confidence=0.8,
        provenance=Provenance(actor_id="source-owner", source="archive"),
    )
    imported = await store.accept_import(tenant_id, actor_id, actor_id, imported_item)
    skipped = await store.accept_import(tenant_id, actor_id, actor_id, imported_item)
    assert imported.accepted
    assert not skipped.accepted
    assert imported.item.owner_principal_id == actor_id
    import_command_id = await _command_id_for_result(
        TENANT_DATABASE_URL, tenant_id, imported_item.id
    )
    await store.mark_memory_command_applied(tenant_id, import_command_id)

    erasure = await store.request(
        tenant_id,
        actor_id,
        actor_id,
        RequestErasure(
            memory_id=retained.id,
            reason="Remove the superseded source and its durable payload.",
            idempotency_key=f"erase-request-{suffix}",
        ),
    )
    approved = await store.review(
        tenant_id,
        reviewer_id,
        ReviewErasure(
            request_id=erasure.id,
            decision=ErasureDecision.APPROVE,
            rationale="Approved by a separate administrator.",
            idempotency_key=f"erase-review-{suffix}",
        ),
    )
    assert approved.status is ErasureStatus.APPROVED
    erase_command_id = await _command_id_for_erasure(TENANT_DATABASE_URL, tenant_id, erasure.id)
    assert await store.next_approved(tenant_id) is None
    pending_view = {
        item.id: item for item in await store.list_private_memory_state(tenant_id, actor_id)
    }
    assert pending_view[retained.id].state.value == "erasure_pending"
    assert pending_view[retained.id].mutation_state.value == "applied"
    assert pending_view[retained.id].content == retained.content

    completed_command = await store.mark_memory_command_applied(tenant_id, erase_command_id)
    repeated_completion = await store.mark_memory_command_applied(tenant_id, erase_command_id)
    assert completed_command.state is PrivateMemoryCommandState.APPLIED
    assert repeated_completion.state is PrivateMemoryCommandState.APPLIED
    completed_request = await store.get_request(tenant_id, erasure.id)
    assert completed_request is not None
    assert completed_request.status is ErasureStatus.COMPLETED
    assert completed_request.reason is None
    assert completed_request.review_rationale is None
    assert await _erasure_text(TENANT_DATABASE_URL, tenant_id, erasure.id) == (
        None,
        None,
        None,
    )
    tombstones = await store.list_completed(tenant_id, actor_id)
    assert [tombstone.id for tombstone in tombstones] == [erasure.id]
    erased_view = {
        item.id: item for item in await store.list_private_memory_state(tenant_id, actor_id)
    }
    assert erased_view[retained.id].state.value == "erased"
    assert erased_view[retained.id].content is None
    assert erased_view[retained.id].operation_id == retained_command_id
    assert erased_view[retained.id].mutation_state.value == "applied"

    archived_tombstone = ErasureTombstone(
        id=f"archive-erasure-{suffix}",
        memory_id=f"erased-archive-memory-{suffix}",
        requester_id=f"source-requester-{suffix}",
        owner_principal_id="source-owner",
        created_at=datetime.now(UTC),
        reviewed_by=f"source-reviewer-{suffix}",
        completed_at=datetime.now(UTC),
    )
    assert await store.import_tombstone(tenant_id, actor_id, archived_tombstone)
    assert not await store.import_tombstone(tenant_id, actor_id, archived_tombstone)

    state, item_json, payload, result_json, processed = await _erasure_state(
        TENANT_DATABASE_URL,
        tenant_id,
        retained.id,
        retained_command_id,
    )
    assert state == "erased"
    assert item_json is None
    assert payload == {}
    assert result_json is None
    assert processed


async def _command_id_for_result(database_url: str, tenant_id: str, memory_id: str) -> str:
    async with await psycopg.AsyncConnection.connect(database_url) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                """
                SELECT command_id
                FROM memory.private_memory_commands
                WHERE tenant_id = %s AND result_memory_id = %s
                """,
                (tenant_id, memory_id),
            )
            row = await cursor.fetchone()
    assert row is not None
    return str(row[0])


async def _command_id_for_erasure(database_url: str, tenant_id: str, request_id: str) -> str:
    async with await psycopg.AsyncConnection.connect(database_url) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                """
                SELECT command_id
                FROM memory.private_memory_commands
                WHERE tenant_id = %s AND erasure_request_id = %s
                """,
                (tenant_id, request_id),
            )
            row = await cursor.fetchone()
    assert row is not None
    return str(row[0])


async def _erasure_state(
    database_url: str,
    tenant_id: str,
    memory_id: str,
    retain_command_id: str,
) -> tuple[str, object, object, object, bool]:
    async with await psycopg.AsyncConnection.connect(database_url) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                """
                SELECT state, item
                FROM memory.private_memory_items
                WHERE tenant_id = %s AND memory_id = %s
                """,
                (tenant_id, memory_id),
            )
            item_row = await cursor.fetchone()
            await cursor.execute(
                """
                SELECT payload, result_item
                FROM memory.private_memory_commands
                WHERE tenant_id = %s AND command_id = %s
                """,
                (tenant_id, retain_command_id),
            )
            command_row = await cursor.fetchone()
            await cursor.execute(
                """
                SELECT processed_at IS NOT NULL
                FROM memory.outbox
                WHERE tenant_id = %s
                  AND aggregate_id = (
                      SELECT command_id
                      FROM memory.private_memory_commands
                      WHERE tenant_id = %s AND erasure_request_id IS NOT NULL
                      ORDER BY created_at DESC
                      LIMIT 1
                  )
                """,
                (tenant_id, tenant_id),
            )
            outbox_row = await cursor.fetchone()
    assert item_row is not None
    assert command_row is not None
    assert outbox_row is not None
    return (
        str(item_row[0]),
        item_row[1],
        command_row[0],
        command_row[1],
        bool(outbox_row[0]),
    )


async def _erasure_text(
    database_url: str,
    tenant_id: str,
    request_id: str,
) -> tuple[object, object, object]:
    async with await psycopg.AsyncConnection.connect(database_url) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                """
                SELECT r.reason, r.review_rationale, v.rationale
                FROM memory.erasure_requests AS r
                JOIN memory.erasure_reviews AS v ON v.request_id = r.request_id
                WHERE r.tenant_id = %s AND r.request_id = %s
                """,
                (tenant_id, request_id),
            )
            row = await cursor.fetchone()
    assert row is not None
    return row[0], row[1], row[2]

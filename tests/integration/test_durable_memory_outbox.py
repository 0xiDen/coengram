"""Real PostgreSQL and RabbitMQ delivery for private-memory commands."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from agent_memory_service.durable_memory import PrivateMemoryCommandState
from agent_memory_service.governance import KnowledgeCandidate
from agent_memory_service.models import RetainMemory
from agent_memory_service.outbox import (
    OutboxRelay,
    PublicationWorker,
    SingleTenantPublicationRouter,
    connect_outbox,
    declare_outbox_topology,
)
from agent_memory_service.stores.memory import InMemoryTenantMemoryRouter
from agent_memory_service.stores.postgres_governance import PostgresGovernanceStore
from agent_memory_service.worker import DurableMemoryGraphProjector

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


class _UnusedKnowledgeProjector:
    async def project(self, candidate: KnowledgeCandidate) -> None:
        raise AssertionError(f"unexpected knowledge projection: {candidate.id}")


@pytest.mark.asyncio
async def test_committed_retain_is_relayed_and_projected_with_exact_id() -> None:
    assert TENANT_DATABASE_URL is not None
    assert AMQP_URL is not None
    suffix = uuid4().hex
    tenant_id = f"tenant-{suffix}"
    actor_id = f"alice-{suffix}"
    namespace = f"memory.command-test.{suffix}"
    governance = PostgresGovernanceStore(TENANT_DATABASE_URL)
    router = InMemoryTenantMemoryRouter([tenant_id])
    accepted = await governance.accept_retain(
        tenant_id,
        actor_id,
        actor_id,
        RetainMemory(
            content="Private commands are projected from a committed outbox.",
            idempotency_key=f"retain-{suffix}",
        ),
    )
    command_id = await _command_id_for_result(TENANT_DATABASE_URL, tenant_id, accepted.id)

    connection = await connect_outbox(AMQP_URL)
    topology = await declare_outbox_topology(connection, namespace=namespace)
    worker = PublicationWorker(
        SingleTenantPublicationRouter(tenant_id, governance),
        _UnusedKnowledgeProjector(),
        DurableMemoryGraphProjector(router),
    )
    consumer_tag = await worker.start(topology.queue)
    try:
        assert await OutboxRelay(governance, topology.exchange).relay_once() == 1
        await _wait_for_applied(governance, tenant_id, command_id)
        projected = await router.for_tenant(tenant_id).list_private(actor_id)
        assert [item.id for item in projected] == [accepted.id]
        assert [item.content for item in projected] == [accepted.content]
    finally:
        await topology.queue.cancel(consumer_tag)
        await topology.queue.delete(if_unused=False, if_empty=False)
        await topology.dead_letter_queue.delete(if_unused=False, if_empty=False)
        await topology.exchange.delete(if_unused=False)
        await topology.dead_letter_exchange.delete(if_unused=False)
        await connection.close()


async def _wait_for_applied(
    governance: PostgresGovernanceStore,
    tenant_id: str,
    command_id: str,
) -> None:
    async with asyncio.timeout(15):
        while True:
            command = await governance.get_memory_command(tenant_id, command_id)
            if command is not None and command.state is PrivateMemoryCommandState.APPLIED:
                return
            await asyncio.sleep(0.05)


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

"""Release fault tracer across public adapters and real persistence services."""

from __future__ import annotations

import asyncio
import hashlib
import os
import socket
import subprocess
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from neo4j_agent_memory import MemoryClient, MemorySettings, Neo4jConfig
from neo4j_agent_memory.config.settings import ExtractionConfig, ExtractorType, MemoryConfig
from pydantic import SecretStr

from agent_memory_service.auth import InMemoryTokenStore, TokenService
from agent_memory_service.control import ControlModule, InMemoryControlStore
from agent_memory_service.durable_memory import PrivateMemoryCommandState
from agent_memory_service.governance import KnowledgeCandidate
from agent_memory_service.http import create_http_app
from agent_memory_service.mcp_server import create_memory_mcp_server
from agent_memory_service.memory import MemoryModule
from agent_memory_service.models import PrincipalKind, TenantSession
from agent_memory_service.outbox import (
    OutboxRelay,
    PublicationWorker,
    SingleTenantPublicationRouter,
    connect_outbox,
    declare_outbox_topology,
)
from agent_memory_service.stores.memory import TenantMemoryStore
from agent_memory_service.stores.neo4j_memory import Neo4jTenantMemoryStore
from agent_memory_service.stores.postgres_governance import PostgresGovernanceStore
from agent_memory_service.telegram import RecordingTelegramSender, TelegramAdapter
from agent_memory_service.worker import DurableMemoryGraphProjector

TENANT_DATABASE_URL = os.environ.get("TENANT_DATABASE_URL")
AMQP_URL = os.environ.get("AMQP_URL")
NEO4J_URI = os.environ.get("NEO4J_TEST_URI")
NEO4J_PASSWORD = os.environ.get("NEO4J_TEST_PASSWORD")
RABBIT_CONTAINER = os.environ.get("RABBITMQ_TEST_CONTAINER")
NEO4J_CONTAINER = os.environ.get("NEO4J_TEST_CONTAINER")

pytestmark = pytest.mark.skipif(
    not all(
        (
            TENANT_DATABASE_URL,
            AMQP_URL,
            NEO4J_URI,
            NEO4J_PASSWORD,
            RABBIT_CONTAINER,
            NEO4J_CONTAINER,
        )
    ),
    reason="restartable PostgreSQL, RabbitMQ, and Neo4j integration targets are required",
)


@pytest.fixture(scope="module", autouse=True)
def migrated_tenant_store() -> None:
    if not TENANT_DATABASE_URL:
        return
    from alembic import command
    from alembic.config import Config

    command.upgrade(
        Config(Path(__file__).resolve().parents[2] / "alembic-tenant.ini"),
        "head",
    )


class _DeterministicEmbedder:
    dimensions = 16

    async def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for term in text.casefold().split():
            index = hashlib.sha256(term.strip(".,").encode()).digest()[0] % self.dimensions
            vector[index] += 1.0
        norm = sum(value * value for value in vector) ** 0.5 or 1.0
        return [value / norm for value in vector]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed(text) for text in texts]


class _Router:
    def __init__(self, tenant_id: str, store: TenantMemoryStore) -> None:
        self._tenant_id = tenant_id
        self._store = store

    def for_tenant(self, tenant_id: str) -> TenantMemoryStore:
        if tenant_id != self._tenant_id:
            raise LookupError("Tenant route not found")
        return self._store


class _UnusedKnowledgeProjector:
    async def project(self, candidate: KnowledgeCandidate) -> None:
        raise AssertionError(f"unexpected knowledge projection: {candidate.id}")


@pytest.mark.asyncio
async def test_accepted_commands_recover_across_broker_and_graph_restarts() -> None:
    assert TENANT_DATABASE_URL is not None
    assert AMQP_URL is not None
    assert NEO4J_URI is not None
    assert NEO4J_PASSWORD is not None
    assert RABBIT_CONTAINER is not None
    assert NEO4J_CONTAINER is not None
    suffix = uuid4().hex
    tenant_id = f"tenant-release-{suffix}"
    alice_id = f"alice-{suffix}"
    namespace = f"memory.release.{suffix}"
    memory_client = MemoryClient(
        _neo4j_settings(NEO4J_URI, NEO4J_PASSWORD),
        embedder=_DeterministicEmbedder(),
    )
    await memory_client.connect()
    router = _Router(tenant_id, Neo4jTenantMemoryStore(memory_client))
    governance = PostgresGovernanceStore(TENANT_DATABASE_URL)
    memory = MemoryModule(
        router,
        governance,
        erasures=governance,
        commands=governance,
    )
    session = TenantSession(
        tenant_id=tenant_id,
        actor_id=alice_id,
        actor_kind=PrincipalKind.USER,
        roles=frozenset({"tenant_member"}),
    )
    tokens = TokenService(InMemoryTokenStore())
    credential = tokens.issue(session, lifetime=timedelta(days=1)).access_token
    telegram, sender = _telegram_adapter(tenant_id, alice_id, memory, suffix)
    http_app = create_http_app(memory, tokens, telegram=telegram)
    http_transport = httpx.ASGITransport(app=http_app, raise_app_exceptions=False)
    connection = None
    topology = None
    consumer_tag: str | None = None
    rabbit_stopped = False
    neo4j_stopped = False
    try:
        async with httpx.AsyncClient(
            transport=http_transport,
            base_url="http://memory.release.test",
        ) as http_client:
            await asyncio.to_thread(_stop_container, RABBIT_CONTAINER)
            rabbit_stopped = True
            retained_http = await http_client.post(
                "/api/v1/memories",
                headers={"Authorization": f"Bearer {credential}"},
                json={
                    "content": "HTTP command survives a RabbitMQ restart.",
                    "idempotency_key": f"http-rabbit-{suffix}",
                },
            )
            assert retained_http.status_code == 202
            assert retained_http.json()["state"] == "accepted"
            http_memory_id = str(retained_http.json()["item"]["id"])
            http_command_id = await _command_id_for_result(
                TENANT_DATABASE_URL, tenant_id, http_memory_id
            )
            assert await _command_state(governance, tenant_id, http_command_id) is (
                PrivateMemoryCommandState.ACCEPTED
            )

            await asyncio.to_thread(_start_container, RABBIT_CONTAINER)
            rabbit_stopped = False
            await asyncio.to_thread(_wait_for_rabbit, RABBIT_CONTAINER, AMQP_URL)
            connection = await connect_outbox(AMQP_URL)
            topology = await declare_outbox_topology(connection, namespace=namespace)
            worker = PublicationWorker(
                SingleTenantPublicationRouter(tenant_id, governance),
                _UnusedKnowledgeProjector(),
                DurableMemoryGraphProjector(router),
            )
            consumer_tag = await worker.start(topology.queue)
            relay = OutboxRelay(governance, topology.exchange)
            assert await relay.relay_once() == 1
            await _wait_for_applied(governance, tenant_id, http_command_id)
            await topology.queue.cancel(consumer_tag)
            consumer_tag = None

            recalled_http = await http_client.post(
                "/api/v1/memories/recall",
                headers={"Authorization": f"Bearer {credential}"},
                json={"query": "RabbitMQ restart"},
            )
            assert recalled_http.status_code == 200
            assert {item["id"] for item in recalled_http.json()["items"]} == {http_memory_id}

            await asyncio.to_thread(_stop_container, NEO4J_CONTAINER)
            neo4j_stopped = True
            retained_mcp = _mcp_retain(memory, tokens, credential, suffix)
            mcp_memory_id = str(retained_mcp["id"])
            telegram_response = await http_client.post(
                "/api/v1/telegram/webhook",
                headers={"X-Telegram-Bot-Api-Secret-Token": "release-webhook-secret"},
                json=_telegram_update(suffix),
            )
            assert telegram_response.status_code == 204
            telegram_memory_id = (
                sender.messages[-1].text.removeprefix("Remembered as ").removesuffix(".")
            )
            mcp_command_id = await _command_id_for_result(
                TENANT_DATABASE_URL, tenant_id, mcp_memory_id
            )
            telegram_command_id = await _command_id_for_result(
                TENANT_DATABASE_URL, tenant_id, telegram_memory_id
            )
            assert await relay.relay_once(limit=2) == 2
            assert {
                await _command_state(governance, tenant_id, mcp_command_id),
                await _command_state(governance, tenant_id, telegram_command_id),
            } == {PrivateMemoryCommandState.ACCEPTED}

            await asyncio.to_thread(_start_container, NEO4J_CONTAINER)
            neo4j_stopped = False
            await asyncio.to_thread(_wait_for_neo4j, NEO4J_CONTAINER, NEO4J_PASSWORD)
            consumer_tag = await worker.start(topology.queue)
            await _wait_for_applied(governance, tenant_id, mcp_command_id)
            await _wait_for_applied(governance, tenant_id, telegram_command_id)

            recalled_all = await http_client.post(
                "/api/v1/memories/recall",
                headers={"Authorization": f"Bearer {credential}"},
                json={"query": "command restart", "limit": 10},
            )
            assert recalled_all.status_code == 200
            assert {item["content"] for item in recalled_all.json()["items"]} == {
                "HTTP command survives a RabbitMQ restart.",
                "MCP command survives a Neo4j restart.",
                "Telegram command survives a Neo4j restart.",
            }
            projected = await router.for_tenant(tenant_id).list_private(alice_id)
            assert {item.id for item in projected} == {
                http_memory_id,
                mcp_memory_id,
                telegram_memory_id,
            }
    finally:
        if rabbit_stopped:
            await asyncio.to_thread(_start_container, RABBIT_CONTAINER)
            await asyncio.to_thread(_wait_for_rabbit, RABBIT_CONTAINER, AMQP_URL)
        if neo4j_stopped:
            await asyncio.to_thread(_start_container, NEO4J_CONTAINER)
            await asyncio.to_thread(_wait_for_neo4j, NEO4J_CONTAINER, NEO4J_PASSWORD)
        if topology is not None:
            if consumer_tag is not None:
                await topology.queue.cancel(consumer_tag)
            await topology.queue.delete(if_unused=False, if_empty=False)
            await topology.dead_letter_queue.delete(if_unused=False, if_empty=False)
            await topology.exchange.delete(if_unused=False)
            await topology.dead_letter_exchange.delete(if_unused=False)
        if connection is not None:
            await connection.close()
        await memory_client.close()


def _neo4j_settings(uri: str, password: str) -> MemorySettings:
    return MemorySettings(
        neo4j=Neo4jConfig(
            uri=uri,
            username="neo4j",
            password=SecretStr(password),
            database="neo4j",
        ),
        embedding="BAAI/bge-small-en-v1.5",
        llm=None,
        memory=MemoryConfig(multi_tenant=True),
        extraction=ExtractionConfig(
            extractor_type=ExtractorType.NONE,
            enable_llm_fallback=False,
        ),
    )


def _telegram_adapter(
    tenant_id: str,
    alice_id: str,
    memory: MemoryModule,
    suffix: str,
) -> tuple[TelegramAdapter, RecordingTelegramSender]:
    store = InMemoryControlStore()
    control = ControlModule(store, TokenService(store))
    agent_id = f"telegram-agent-{suffix}"
    control.create_tenant(tenant_id, "Release fault tracer")
    control.create_principal(alice_id, "Alice", "user")
    control.create_principal(agent_id, "Telegram Agent", "agent")
    control.grant_membership(tenant_id, alice_id, "tenant_member")
    control.grant_membership(tenant_id, agent_id, "tenant_member")
    delegation = control.create_delegation(
        f"telegram-delegation-{suffix}",
        tenant_id=tenant_id,
        agent_id=agent_id,
        subject_user_id=alice_id,
    )
    control.create_channel_binding(
        f"telegram-binding-{suffix}",
        channel="telegram",
        external_id=str(int(suffix[:12], 16)),
        delegation_id=delegation.delegation_id,
    )
    sender = RecordingTelegramSender()
    return (
        TelegramAdapter(
            control,
            memory,
            sender,
            webhook_secret="release-webhook-secret",
        ),
        sender,
    )


def _telegram_update(suffix: str) -> dict[str, object]:
    external_id = int(suffix[:12], 16)
    return {
        "update_id": external_id,
        "message": {
            "message_id": 1,
            "from": {"id": external_id},
            "chat": {"id": external_id, "type": "private"},
            "text": "/remember Telegram command survives a Neo4j restart.",
        },
    }


def _mcp_retain(
    memory: MemoryModule,
    tokens: TokenService,
    credential: str,
    suffix: str,
) -> dict[str, object]:
    app = create_memory_mcp_server(memory, tokens).streamable_http_app()
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {credential}",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-06-18",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "memory_retain",
                    "arguments": {
                        "content": "MCP command survives a Neo4j restart.",
                        "idempotency_key": f"mcp-neo4j-{suffix}",
                    },
                },
            },
        )
    assert response.status_code == 200
    document = response.json()
    assert document["result"]["isError"] is False
    structured = document["result"]["structuredContent"]
    assert isinstance(structured, dict)
    assert structured["state"] == "accepted"
    return structured


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


async def _command_state(
    governance: PostgresGovernanceStore,
    tenant_id: str,
    command_id: str,
) -> PrivateMemoryCommandState:
    command = await governance.get_memory_command(tenant_id, command_id)
    assert command is not None
    return command.state


async def _wait_for_applied(
    governance: PostgresGovernanceStore,
    tenant_id: str,
    command_id: str,
) -> None:
    async with asyncio.timeout(30):
        while True:
            if await _command_state(governance, tenant_id, command_id) is (
                PrivateMemoryCommandState.APPLIED
            ):
                return
            await asyncio.sleep(0.1)


def _stop_container(name: str) -> None:
    subprocess.run(
        ("docker", "stop", "--time", "2", name),
        check=True,
        capture_output=True,
        text=True,
    )


def _start_container(name: str) -> None:
    subprocess.run(("docker", "start", name), check=True, capture_output=True, text=True)


def _wait_for_rabbit(name: str, amqp_url: str | None = None) -> None:
    _wait_for_container_command(name, ("rabbitmq-diagnostics", "-q", "check_running"), 90)
    if amqp_url is None:
        return
    endpoint = urlsplit(amqp_url)
    assert endpoint.hostname is not None
    assert endpoint.port is not None
    for _attempt in range(90):
        try:
            with socket.create_connection((endpoint.hostname, endpoint.port), timeout=1):
                return
        except OSError:
            time.sleep(1)
    raise TimeoutError(f"RabbitMQ AMQP endpoint did not become ready: {endpoint.hostname}")


def _wait_for_neo4j(name: str, password: str) -> None:
    _wait_for_container_command(
        name,
        ("cypher-shell", "-u", "neo4j", "-p", password, "RETURN 1"),
        120,
    )


def _wait_for_container_command(name: str, command: tuple[str, ...], attempts: int) -> None:
    for _attempt in range(attempts):
        completed = subprocess.run(
            ("docker", "exec", name, *command),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if completed.returncode == 0:
            return
        time.sleep(1)
    raise TimeoutError(f"Container did not become ready: {name}")

"""Authenticated HTTP isolation across two real Neo4j Community instances."""

from __future__ import annotations

import hashlib
import os

import httpx
import pytest
from neo4j_agent_memory import MemoryClient, MemorySettings, Neo4jConfig
from neo4j_agent_memory.config.settings import ExtractionConfig, ExtractorType, MemoryConfig
from pydantic import SecretStr

from agent_memory_service.auth import TokenService
from agent_memory_service.control import ControlModule, InMemoryControlStore
from agent_memory_service.http import create_http_app
from agent_memory_service.memory import MemoryModule
from agent_memory_service.stores.memory import TenantMemoryStore
from agent_memory_service.stores.neo4j_memory import Neo4jTenantMemoryStore

NEO4J_A_URI = os.environ.get("NEO4J_TEST_URI")
NEO4J_A_PASSWORD = os.environ.get("NEO4J_TEST_PASSWORD")
NEO4J_B_URI = os.environ.get("NEO4J_SECOND_TEST_URI")
NEO4J_B_PASSWORD = os.environ.get("NEO4J_SECOND_TEST_PASSWORD")

pytestmark = pytest.mark.skipif(
    not all((NEO4J_A_URI, NEO4J_A_PASSWORD, NEO4J_B_URI, NEO4J_B_PASSWORD)),
    reason="two Neo4j Community test instances are required",
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
    def __init__(self, stores: dict[str, TenantMemoryStore]) -> None:
        self._stores = stores

    def for_tenant(self, tenant_id: str) -> TenantMemoryStore:
        return self._stores[tenant_id]


def _settings(uri: str, password: str) -> MemorySettings:
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


@pytest.mark.asyncio
async def test_authenticated_http_never_observes_the_other_real_tenant() -> None:
    assert NEO4J_A_URI and NEO4J_A_PASSWORD and NEO4J_B_URI and NEO4J_B_PASSWORD
    first_client = MemoryClient(
        _settings(NEO4J_A_URI, NEO4J_A_PASSWORD), embedder=_DeterministicEmbedder()
    )
    second_client = MemoryClient(
        _settings(NEO4J_B_URI, NEO4J_B_PASSWORD), embedder=_DeterministicEmbedder()
    )
    await first_client.connect()
    await second_client.connect()
    try:
        control_store = InMemoryControlStore()
        control = ControlModule(control_store, TokenService(control_store))
        for tenant_id, user_id in (
            ("tenant-real-a", "user-real-a"),
            ("tenant-real-b", "user-real-b"),
        ):
            control.create_tenant(tenant_id, tenant_id)
            control.create_principal(user_id, user_id, "user")
            control.grant_membership(tenant_id, user_id, "tenant_member")
        token_a = control.issue_access_token("tenant-real-a", "user-real-a").access_token
        token_b = control.issue_access_token("tenant-real-b", "user-real-b").access_token
        memory = MemoryModule(
            _Router(
                {
                    "tenant-real-a": Neo4jTenantMemoryStore(first_client),
                    "tenant-real-b": Neo4jTenantMemoryStore(second_client),
                }
            )
        )
        transport = httpx.ASGITransport(app=create_http_app(memory, TokenService(control_store)))
        async with httpx.AsyncClient(transport=transport, base_url="http://memory.test") as client:
            retained = await client.post(
                "/api/v1/memories",
                headers={"Authorization": f"Bearer {token_a}"},
                json={
                    "content": "Tenant A uses a heliotrope release marker.",
                    "idempotency_key": "real-two-tenant-heliotrope",
                },
            )
            own = await client.post(
                "/api/v1/memories/recall",
                headers={"Authorization": f"Bearer {token_a}"},
                json={"query": "heliotrope release"},
            )
            isolated = await client.post(
                "/api/v1/memories/recall",
                headers={"Authorization": f"Bearer {token_b}"},
                json={"query": "heliotrope release"},
            )

        assert retained.status_code == 202
        assert retained.json()["id"] in {item["id"] for item in own.json()["items"]}
        assert isolated.status_code == 200
        assert isolated.json() == {"items": []}
        assert "heliotrope" not in isolated.text
        assert retained.json()["id"] not in isolated.text
    finally:
        await first_client.close()
        await second_client.close()

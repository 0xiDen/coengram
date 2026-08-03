"""Real Neo4j Community contract for the platform Memory Store Adapter."""

from __future__ import annotations

import hashlib
import os

import pytest
from neo4j_agent_memory import MemoryClient, MemorySettings, Neo4jConfig
from neo4j_agent_memory.config.settings import ExtractionConfig, ExtractorType, MemoryConfig
from pydantic import SecretStr

from agent_memory_service.models import RecallQuery, RetainMemory
from agent_memory_service.stores.neo4j_memory import Neo4jTenantMemoryStore

NEO4J_TEST_URI = os.environ.get("NEO4J_TEST_URI")
NEO4J_TEST_PASSWORD = os.environ.get("NEO4J_TEST_PASSWORD")

pytestmark = pytest.mark.skipif(
    not NEO4J_TEST_URI or not NEO4J_TEST_PASSWORD,
    reason="NEO4J_TEST_URI and NEO4J_TEST_PASSWORD are required",
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


@pytest.mark.asyncio
async def test_real_neo4j_retention_isolation_and_shared_knowledge() -> None:
    assert NEO4J_TEST_URI is not None
    assert NEO4J_TEST_PASSWORD is not None
    settings = MemorySettings(
        neo4j=Neo4jConfig(
            uri=NEO4J_TEST_URI,
            username="neo4j",
            password=SecretStr(NEO4J_TEST_PASSWORD),
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
    async with MemoryClient(settings, embedder=_DeterministicEmbedder()) as client:
        store = Neo4jTenantMemoryStore(client)
        alice = await store.retain_private(
            "user-alice",
            RetainMemory(
                content="Caddy terminates public TLS.",
                idempotency_key="neo4j-alice-caddy",
            ),
        )
        await store.retain_private(
            "user-bob",
            RetainMemory(
                content="Bob private deployment secret.",
                idempotency_key="neo4j-bob-secret",
            ),
        )
        knowledge = await store.publish_tenant_knowledge(
            "candidate-neo4j",
            "Caddy uses DNS certificate issuance.",
            0.9,
            "user-curator",
        )
        repeated = await store.publish_tenant_knowledge(
            "candidate-neo4j",
            "ignored duplicate",
            0.1,
            "user-curator",
        )

        recalled = await store.recall(("user-alice",), RecallQuery(query="Caddy", limit=10))

    assert {item.id for item in recalled} == {alice.id, knowledge.id}
    assert all("Bob private" not in item.content for item in recalled)
    assert repeated.id == knowledge.id

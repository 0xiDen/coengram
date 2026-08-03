from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from neo4j.exceptions import ServiceUnavailable

from agent_memory_service.lifecycle import CorrectMemory
from agent_memory_service.models import RecallQuery, RetainMemory
from agent_memory_service.stores.memory import TenantMemoryUnavailable
from agent_memory_service.stores.neo4j_memory import Neo4jTenantMemoryStore


class FakeShortTerm:
    def __init__(self) -> None:
        self.messages: dict[str, list[SimpleNamespace]] = {}
        self.writes: list[tuple[str, dict[str, object]]] = []
        self.conversation_limits: list[int] = []

    @property
    def client(self) -> FakeShortTerm:
        return self

    async def execute_write(
        self,
        query: str,
        parameters: dict[str, object],
    ) -> list[dict[str, object]]:
        self.writes.append((query, parameters))
        deleted = await self.delete_message(parameters["message_id"])
        return [{"deleted": deleted}]

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        **kwargs: object,
    ) -> SimpleNamespace:
        message = SimpleNamespace(
            id=uuid4(),
            role=role,
            content=content,
            metadata=kwargs["metadata"],
            created_at=datetime.now(UTC),
        )
        self.messages.setdefault(session_id, []).append(message)
        return message

    async def get_conversation(self, session_id: str, **kwargs: object) -> SimpleNamespace:
        limit = kwargs["limit"]
        assert isinstance(limit, int)
        self.conversation_limits.append(limit)
        return SimpleNamespace(messages=list(self.messages.get(session_id, ())))

    async def search_messages(
        self,
        query: str,
        *,
        session_id: str | None = None,
        limit: int = 10,
        **_kwargs: object,
    ) -> list[SimpleNamespace]:
        terms = set(query.casefold().split())
        return [
            message
            for message in self.messages.get(session_id or "", ())
            if terms.intersection(message.content.casefold().split())
        ][:limit]

    async def delete_message(self, message_id: object, **_kwargs: object) -> bool:
        for messages in self.messages.values():
            for message in tuple(messages):
                if str(message.id) == str(message_id):
                    messages.remove(message)
                    return True
        return False


class FakeMemoryClient:
    def __init__(self) -> None:
        self.short_term = FakeShortTerm()


class LeakyShortTerm(FakeShortTerm):
    async def search_messages(
        self,
        query: str,
        *,
        session_id: str | None = None,
        limit: int = 10,
        **_kwargs: object,
    ) -> list[SimpleNamespace]:
        del session_id
        terms = set(query.casefold().split())
        return [
            message
            for messages in self.messages.values()
            for message in messages
            if terms.intersection(message.content.casefold().split())
        ][:limit]


class LeakyMemoryClient:
    def __init__(self) -> None:
        self.short_term = LeakyShortTerm()


class FailingOperationShortTerm(FakeShortTerm):
    def __init__(self) -> None:
        super().__init__()
        self.failure_at: str | None = None
        self.failure: Exception = ServiceUnavailable("sensitive Bolt detail")

    def _fail(self, operation: str) -> None:
        if self.failure_at == operation:
            raise self.failure

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        **kwargs: object,
    ) -> SimpleNamespace:
        self._fail("add")
        return await super().add_message(session_id, role, content, **kwargs)

    async def search_messages(
        self,
        query: str,
        *,
        session_id: str | None = None,
        limit: int = 10,
        **kwargs: object,
    ) -> list[SimpleNamespace]:
        self._fail("search")
        return await super().search_messages(query, session_id=session_id, limit=limit, **kwargs)

    async def delete_message(self, message_id: object, **kwargs: object) -> bool:
        self._fail("delete")
        return await super().delete_message(message_id, **kwargs)


class FailingOperationMemoryClient:
    def __init__(self) -> None:
        self.short_term = FailingOperationShortTerm()


@pytest.mark.asyncio
async def test_neo4j_store_preserves_scopes_idempotency_and_corrections() -> None:
    client = FakeMemoryClient()
    store = Neo4jTenantMemoryStore(client)

    retained = await store.retain_private(
        "user-alice",
        RetainMemory(content="Deploy through Caddy", idempotency_key="retain-1"),
    )
    repeated = await store.retain_private(
        "user-alice",
        RetainMemory(content="ignored retry", idempotency_key="retain-1"),
    )
    await store.retain_private(
        "user-bob",
        RetainMemory(content="Bob secret Caddy note", idempotency_key="retain-bob"),
    )
    corrected = await store.correct_private(
        "user-alice",
        CorrectMemory(
            memory_id=retained.id,
            replacement_content="Deploy through authenticated Caddy",
            reason="Add security constraint",
            idempotency_key="correct-1",
        ),
    )
    published = await store.publish_tenant_knowledge(
        "candidate-1",
        "Caddy terminates TLS",
        0.9,
        "user-curator",
    )

    recalled = await store.recall(("user-alice",), RecallQuery(query="Caddy", limit=10))

    assert repeated.id == retained.id
    assert corrected.supersedes_id == retained.id
    assert {item.id for item in recalled} == {corrected.id, published.id}
    assert all("Bob secret" not in item.content for item in recalled)
    history = await store.list_private("user-alice")
    assert history[0].state.value == "superseded"


@pytest.mark.asyncio
async def test_neo4j_store_archive_import_and_erasure_use_stable_platform_ids() -> None:
    client = FakeMemoryClient()
    store = Neo4jTenantMemoryStore(client)
    original = await store.retain_private(
        "user-alice",
        RetainMemory(content="Sensitive memory", idempotency_key="retain-sensitive"),
    )

    assert await store.import_private("user-alice", original) is False
    assert await store.erase_private("user-alice", original.id) is True
    assert await store.erase_private("user-alice", original.id) is False
    assert await store.list_private("user-alice") == ()
    query, parameters = client.short_term.writes[0]
    assert "DETACH DELETE message" in query
    assert parameters["session_id"] == "private:user-alice"
    assert isinstance(parameters["message_id"], str)


@pytest.mark.asyncio
async def test_neo4j_store_reads_the_documented_tenant_capacity_baseline() -> None:
    client = FakeMemoryClient()
    store = Neo4jTenantMemoryStore(client)

    await store.list_private("user-alice")

    assert client.short_term.conversation_limits == [100_000]


@pytest.mark.asyncio
async def test_neo4j_store_rechecks_scope_when_upstream_search_leaks() -> None:
    store = Neo4jTenantMemoryStore(LeakyMemoryClient())
    alice = await store.retain_private(
        "user-alice",
        RetainMemory(content="Shared search term", idempotency_key="alice"),
    )
    await store.retain_private(
        "user-bob",
        RetainMemory(content="Shared search term and secret", idempotency_key="bob"),
    )

    recalled = await store.recall(("user-alice",), RecallQuery(query="Shared", limit=10))

    assert [item.id for item in recalled] == [alice.id]


@pytest.mark.asyncio
async def test_neo4j_store_translates_recall_dependency_failure() -> None:
    client = FailingOperationMemoryClient()
    store = Neo4jTenantMemoryStore(client)
    client.short_term.failure_at = "search"

    with pytest.raises(TenantMemoryUnavailable, match="unavailable"):
        await store.recall(("user-alice",), RecallQuery(query="deploy", limit=10))


@pytest.mark.asyncio
async def test_neo4j_store_translates_write_dependency_failures() -> None:
    client = FailingOperationMemoryClient()
    store = Neo4jTenantMemoryStore(client)
    client.short_term.failure_at = "add"

    with pytest.raises(TenantMemoryUnavailable, match="unavailable"):
        await store.retain_private(
            "user-alice",
            RetainMemory(content="Deploy through Caddy", idempotency_key="outage-write"),
        )

    client.short_term.failure_at = None
    retained = await store.retain_private(
        "user-alice",
        RetainMemory(content="Delete me", idempotency_key="outage-delete"),
    )
    client.short_term.failure_at = "delete"
    with pytest.raises(TenantMemoryUnavailable, match="unavailable"):
        await store.erase_private("user-alice", retained.id)


@pytest.mark.asyncio
async def test_neo4j_store_does_not_mask_domain_or_authorization_errors() -> None:
    client = FailingOperationMemoryClient()
    store = Neo4jTenantMemoryStore(client)
    client.short_term.failure_at = "add"

    client.short_term.failure = ValueError("upstream validation failed")
    with pytest.raises(ValueError, match="validation"):
        await store.retain_private(
            "user-alice",
            RetainMemory(content="Invalid", idempotency_key="invalid"),
        )

    client.short_term.failure = PermissionError("operation is not permitted")
    with pytest.raises(PermissionError, match="not permitted"):
        await store.retain_private(
            "user-alice",
            RetainMemory(content="Forbidden", idempotency_key="forbidden"),
        )

    client.short_term.failure_at = None
    with pytest.raises(LookupError, match="not found"):
        await store.correct_private(
            "user-alice",
            CorrectMemory(
                memory_id="missing-memory",
                replacement_content="Replacement",
                reason="Correction",
                idempotency_key="missing-correction",
            ),
        )

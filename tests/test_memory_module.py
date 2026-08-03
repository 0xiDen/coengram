from __future__ import annotations

import pytest

from agent_memory_service.memory import MemoryModule
from agent_memory_service.models import (
    MemoryKind,
    PrincipalKind,
    RecallQuery,
    RetainMemory,
    TenantSession,
)
from agent_memory_service.stores.memory import (
    InMemoryTenantMemoryRouter,
    TenantMemoryUnavailable,
)


@pytest.mark.asyncio
async def test_user_can_retain_and_recall_private_memory_without_exposing_it_to_a_peer() -> None:
    router = InMemoryTenantMemoryRouter(["tenant-product-a-backend"])
    memory = MemoryModule(router)
    alice = TenantSession(
        tenant_id="tenant-product-a-backend",
        actor_id="user-alice",
        actor_kind=PrincipalKind.USER,
        roles=frozenset({"tenant_member"}),
    )
    bob = TenantSession(
        tenant_id="tenant-product-a-backend",
        actor_id="user-bob",
        actor_kind=PrincipalKind.USER,
        roles=frozenset({"tenant_member"}),
    )

    retained = await memory.retain(
        alice,
        RetainMemory(
            content="Product A deploys risky backend changes behind feature flags.",
            kind=MemoryKind.CONSTRAINT,
            idempotency_key="remember-feature-flags",
        ),
    )

    alice_results = await memory.recall(alice, RecallQuery(query="deploy changes"))
    bob_results = await memory.recall(bob, RecallQuery(query="deploy changes"))

    assert retained.owner_principal_id == "user-alice"
    assert retained.provenance.actor_id == "user-alice"
    assert retained.provenance.source == "explicit"
    assert [item.content for item in alice_results.items] == [
        "Product A deploys risky backend changes behind feature flags."
    ]
    assert bob_results.items == ()


@pytest.mark.asyncio
async def test_repeated_retain_with_same_idempotency_key_returns_original_memory() -> None:
    router = InMemoryTenantMemoryRouter(["tenant-product-a-backend"])
    memory = MemoryModule(router)
    alice = TenantSession(
        tenant_id="tenant-product-a-backend",
        actor_id="user-alice",
        actor_kind=PrincipalKind.USER,
        roles=frozenset({"tenant_member"}),
    )
    command = RetainMemory(
        content="Prefer reversible database migrations.",
        kind=MemoryKind.PREFERENCE,
        idempotency_key="remember-migrations",
    )

    first = await memory.retain(alice, command)
    repeated = await memory.retain(alice, command)

    assert repeated.id == first.id
    assert len((await memory.recall(alice, RecallQuery(query="migrations"))).items) == 1


@pytest.mark.asyncio
async def test_tenant_routing_is_isolated_and_unknown_tenants_fail_closed() -> None:
    router = InMemoryTenantMemoryRouter(["tenant-a", "tenant-b"])
    memory = MemoryModule(router)
    tenant_a = TenantSession(
        tenant_id="tenant-a",
        actor_id="user-alice",
        actor_kind=PrincipalKind.USER,
        roles=frozenset({"tenant_member"}),
    )
    tenant_b = TenantSession(
        tenant_id="tenant-b",
        actor_id="user-alice",
        actor_kind=PrincipalKind.USER,
        roles=frozenset({"tenant_member"}),
    )
    unknown = TenantSession(
        tenant_id="tenant-not-provisioned",
        actor_id="user-alice",
        actor_kind=PrincipalKind.USER,
        roles=frozenset({"tenant_member"}),
    )

    await memory.retain(
        tenant_a,
        RetainMemory(
            content="Tenant A uses PostgreSQL advisory locks.",
            idempotency_key="tenant-a-locking",
        ),
    )

    assert (await memory.recall(tenant_b, RecallQuery(query="advisory locks"))).items == ()
    with pytest.raises(TenantMemoryUnavailable, match="not available"):
        await memory.recall(unknown, RecallQuery(query="advisory locks"))


def test_private_retention_rejects_unclassified_transcript_storage() -> None:
    with pytest.raises(ValueError, match="kind"):
        RetainMemory(
            content="A wholesale transcript must not be retained by default.",
            kind="raw_transcript",  # type: ignore[arg-type]
            idempotency_key="unsafe-transcript",
        )

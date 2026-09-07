"""Run a local in-memory gateway for admin frontend smoke tests."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import uvicorn

from agent_memory_service.auth import TokenService
from agent_memory_service.control import ControlModule, InMemoryControlStore
from agent_memory_service.governance import InMemoryGovernanceStore, ProposeKnowledge
from agent_memory_service.http import create_http_app
from agent_memory_service.memory import MemoryModule
from agent_memory_service.models import MemoryKind, PrincipalKind, RetainMemory, TenantSession
from agent_memory_service.stores.memory import InMemoryTenantMemoryRouter


def main() -> None:
    store = InMemoryControlStore()
    tokens = TokenService(store)
    control = ControlModule(store, tokens)
    control.create_tenant("tenant-a", "Product A")
    control.create_principal("user-alice", "Alice", PrincipalKind.USER.value)
    control.grant_membership("tenant-a", "user-alice", "tenant_member")
    control.create_operator(
        "operator-dev",
        "Dev Operator",
        frozenset(
            {
                "operator_admin",
                "identity_admin",
                "tenant_provisioner",
                "tenant_support",
                "knowledge_admin",
                "token_admin",
                "audit_viewer",
            }
        ),
    )
    credential = control.issue_operator_access_token(
        "operator-dev",
        lifetime=timedelta(days=7),
    )
    router = InMemoryTenantMemoryRouter(["tenant-a"])
    memory = MemoryModule(router, InMemoryGovernanceStore())
    asyncio.run(_seed_memory(memory, router))
    app = create_http_app(memory, tokens, control=control)
    print("Admin dev Operator Access Token:", flush=True)
    print(credential.access_token, flush=True)
    uvicorn.run(app, host="127.0.0.1", port=8080, access_log=False)


async def _seed_memory(memory: MemoryModule, router: InMemoryTenantMemoryRouter) -> None:
    session = TenantSession(
        tenant_id="tenant-a",
        actor_id="user-alice",
        actor_kind=PrincipalKind.USER,
        roles=frozenset({"tenant_member"}),
    )
    source = await memory.retain(
        session,
        RetainMemory(
            content="Private rollout note for Product A.",
            kind=MemoryKind.CONSTRAINT,
            idempotency_key="admin-dev-private-source",
        ),
    )
    await memory.propose_knowledge(
        session,
        ProposeKnowledge(
            claim="Product A uses guarded rollouts for risky changes.",
            source_memory_ids=(source.id,),
            confidence=0.91,
            idempotency_key="admin-dev-knowledge-candidate",
        ),
    )
    await router.for_tenant("tenant-a").publish_tenant_knowledge(
        "admin-dev-published-knowledge",
        "Product A keeps deployment rollback notes in Tenant Knowledge after review.",
        0.88,
        "user-curator",
    )


if __name__ == "__main__":
    main()

from __future__ import annotations

import pytest

from agent_memory_service.auth import TokenService
from agent_memory_service.control import ControlModule, InMemoryControlStore
from agent_memory_service.memory import MemoryModule
from agent_memory_service.models import MemoryKind, RecallQuery, RetainMemory
from agent_memory_service.stores.memory import InMemoryTenantMemoryRouter


def _control() -> ControlModule:
    store = InMemoryControlStore()
    control = ControlModule(store, TokenService(store))
    control.create_tenant("tenant-a", "Product A Backend")
    control.create_principal("user-alice", "Alice", "user")
    control.create_principal("agent-synthesis", "Knowledge Synthesis", "agent")
    control.grant_membership("tenant-a", "user-alice", "tenant_member")
    control.grant_membership("tenant-a", "agent-synthesis", "tenant_member")
    return control


@pytest.mark.asyncio
async def test_delegated_agent_reads_own_and_subject_private_memory_without_copying_ownership() -> (
    None
):
    control = _control()
    alice_token = control.issue_access_token("tenant-a", "user-alice")
    delegation = control.create_delegation(
        "delegation-alice-synthesis",
        tenant_id="tenant-a",
        agent_id="agent-synthesis",
        subject_user_id="user-alice",
    )
    agent_token = control.issue_delegated_access_token(delegation.delegation_id)
    alice = control.authenticate(alice_token.access_token)
    delegated_agent = control.authenticate(agent_token.access_token)
    memory = MemoryModule(InMemoryTenantMemoryRouter(["tenant-a"]))

    alice_item = await memory.retain(
        alice,
        RetainMemory(
            content="Alice prefers deploy summaries with rollback steps.",
            kind=MemoryKind.PREFERENCE,
            idempotency_key="alice-deploy-summary",
        ),
    )
    delegated_user_item = await memory.retain(
        delegated_agent,
        RetainMemory(
            content="Alice wants candidate conflicts summarized first.",
            kind=MemoryKind.PREFERENCE,
            idempotency_key="alice-conflict-summary",
        ),
    )
    agent_item = await memory.retain_agent_private(
        delegated_agent,
        RetainMemory(
            content="The synthesis agent compares candidate conflicts first.",
            kind=MemoryKind.CONSTRAINT,
            idempotency_key="agent-conflict-check",
        ),
    )

    recalled = await memory.recall(delegated_agent, RecallQuery(query="deploy candidate"))

    assert delegated_agent.actor_id == "agent-synthesis"
    assert delegated_agent.subject_user_id == "user-alice"
    assert {item.id for item in recalled.items} == {
        alice_item.id,
        delegated_user_item.id,
        agent_item.id,
    }
    assert alice_item.owner_principal_id == "user-alice"
    assert delegated_user_item.owner_principal_id == "user-alice"
    assert agent_item.owner_principal_id == "agent-synthesis"


@pytest.mark.asyncio
async def test_autonomous_agent_cannot_read_a_users_private_memory() -> None:
    control = _control()
    alice = control.authenticate(control.issue_access_token("tenant-a", "user-alice").access_token)
    autonomous_agent = control.authenticate(
        control.issue_access_token("tenant-a", "agent-synthesis").access_token
    )
    memory = MemoryModule(InMemoryTenantMemoryRouter(["tenant-a"]))
    await memory.retain(
        alice,
        RetainMemory(
            content="Alice's incident review is Friday.",
            idempotency_key="alice-incident-review",
        ),
    )

    assert (await memory.recall(autonomous_agent, RecallQuery(query="incident review"))).items == ()


def test_agent_cannot_receive_human_administration_roles() -> None:
    control = _control()

    with pytest.raises(ValueError, match="human User"):
        control.grant_membership("tenant-a", "agent-synthesis", "knowledge_curator")
    with pytest.raises(ValueError, match="human User"):
        control.grant_membership("tenant-a", "agent-synthesis", "tenant_administrator")


def test_tenant_route_is_server_registered_and_fail_closed() -> None:
    control = _control()

    route = control.register_tenant_route(
        "tenant-a",
        neo4j_service_address="neo4j-tenant-a:7687",
        neo4j_secret_name="tenant-a/neo4j_password",
        tenant_database_name="tenant_tenant_a",
        tenant_database_role="tenant_tenant_a_rw",
        healthy=True,
    )

    assert control.resolve_tenant_route("tenant-a") == route
    with pytest.raises(Exception, match="route"):
        control.resolve_tenant_route("tenant-unknown")

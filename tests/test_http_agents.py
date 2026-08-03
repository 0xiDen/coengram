from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from agent_memory_service.agents import (
    AgentInvocation,
    AgentRuntimeModule,
    KnowledgeSynthesisCapability,
    MemoryModuleKnowledgeSynthesisPort,
    RecordedCompletion,
    RecordedProvider,
)
from agent_memory_service.auth import InMemoryTokenStore, TokenService
from agent_memory_service.governance import InMemoryGovernanceStore
from agent_memory_service.http import create_http_app
from agent_memory_service.memory import MemoryModule
from agent_memory_service.models import PrincipalKind, RetainMemory, TenantSession
from agent_memory_service.stores.memory import InMemoryTenantMemoryRouter


def test_http_starts_and_observes_registered_agent_run_in_token_scope() -> None:
    router = InMemoryTenantMemoryRouter(["tenant-a"])
    memory = MemoryModule(router, InMemoryGovernanceStore())
    session = TenantSession(
        tenant_id="tenant-a",
        actor_id="user-alice",
        actor_kind=PrincipalKind.USER,
        roles=frozenset({"tenant_member"}),
    )
    tokens = TokenService(InMemoryTokenStore())
    credential = tokens.issue(session, lifetime=timedelta(days=90))
    bob_credential = tokens.issue(
        TenantSession(
            tenant_id=session.tenant_id,
            actor_id="user-bob",
            actor_kind=PrincipalKind.USER,
            roles=session.roles,
        ),
        lifetime=timedelta(days=90),
    )
    source = __import__("asyncio").run(
        memory.retain(
            session,
            RetainMemory(content="Use jittered retries", idempotency_key="retry-memory"),
        )
    )
    runtime = AgentRuntimeModule(
        capabilities=(
            KnowledgeSynthesisCapability(MemoryModuleKnowledgeSynthesisPort(memory, router)),
        ),
        provider=RecordedProvider(
            [
                RecordedCompletion(
                    parsed={
                        "claim": "Use jittered retries.",
                        "confidence": 0.9,
                        "duplicate_memory_ids": [],
                        "conflicting_memory_ids": [],
                    },
                    cost_usd=Decimal("0.001"),
                )
            ]
        ),
    )
    client = TestClient(create_http_app(memory, tokens, agents=runtime))
    headers = {"Authorization": f"Bearer {credential.access_token}"}

    started = client.post(
        "/api/v1/agent-runs",
        headers=headers,
        json=AgentInvocation(
            capability="knowledge_synthesis",
            input={"request": "Distill retry guidance", "source_memory_ids": [source.id]},
            idempotency_key="http-agent-1",
        ).model_dump(mode="json"),
    )
    run_id = started.json()["id"]

    assert started.status_code == 202
    with client:
        status = client.get(f"/api/v1/agent-runs/{run_id}", headers=headers)
        bob_headers = {"Authorization": f"Bearer {bob_credential.access_token}"}
        hidden_from_bob = client.get(
            f"/api/v1/agent-runs/{run_id}",
            headers=bob_headers,
        )
        cancellation_hidden_from_bob = client.delete(
            f"/api/v1/agent-runs/{run_id}",
            headers=bob_headers,
        )
    assert status.status_code == 200
    assert status.json()["capability"] == "knowledge_synthesis"
    assert hidden_from_bob.status_code == 404
    assert cancellation_hidden_from_bob.status_code == 404


def test_http_agent_run_rejects_missing_token() -> None:
    memory = MemoryModule(InMemoryTenantMemoryRouter(["tenant-a"]))
    tokens = TokenService(InMemoryTokenStore())
    client = TestClient(create_http_app(memory, tokens))

    assert client.post("/api/v1/agent-runs", json={}).status_code == 404


def test_http_delegated_agent_runs_are_isolated_by_subject_user() -> None:
    router = InMemoryTenantMemoryRouter(["tenant-a"])
    memory = MemoryModule(router, InMemoryGovernanceStore())
    tokens = TokenService(InMemoryTokenStore())
    sessions = tuple(
        TenantSession(
            tenant_id="tenant-a",
            actor_id="agent-shared",
            actor_kind=PrincipalKind.AGENT,
            roles=frozenset({"tenant_member"}),
            subject_user_id=subject_user_id,
            delegation_id=delegation_id,
        )
        for subject_user_id, delegation_id in (
            ("user-alice", "delegation-alice"),
            ("user-bob", "delegation-bob"),
        )
    )
    credentials = tuple(tokens.issue(session, lifetime=timedelta(days=90)) for session in sessions)
    runtime = AgentRuntimeModule(
        capabilities=(
            KnowledgeSynthesisCapability(MemoryModuleKnowledgeSynthesisPort(memory, router)),
        ),
        provider=RecordedProvider([]),
    )
    client = TestClient(create_http_app(memory, tokens, agents=runtime, execute_agent_runs=False))
    request = AgentInvocation(
        capability="knowledge_synthesis",
        input={"request": "Distill selected guidance", "source_memory_ids": ["memory-1"]},
        idempotency_key="same-agent-key",
    ).model_dump(mode="json")
    headers = tuple(
        {"Authorization": f"Bearer {credential.access_token}"} for credential in credentials
    )

    alice_started = client.post("/api/v1/agent-runs", headers=headers[0], json=request)
    bob_started = client.post("/api/v1/agent-runs", headers=headers[1], json=request)
    alice_run_id = alice_started.json()["id"]
    bob_run_id = bob_started.json()["id"]

    assert alice_started.status_code == 202
    assert bob_started.status_code == 202
    assert alice_run_id != bob_run_id
    assert client.get(f"/api/v1/agent-runs/{alice_run_id}", headers=headers[0]).status_code == 200
    assert client.get(f"/api/v1/agent-runs/{alice_run_id}", headers=headers[1]).status_code == 404
    assert (
        client.delete(f"/api/v1/agent-runs/{alice_run_id}", headers=headers[1]).status_code == 404
    )

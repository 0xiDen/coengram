from __future__ import annotations

import asyncio
from datetime import timedelta

from fastapi.testclient import TestClient

from agent_memory_service.auth import InMemoryTokenStore, TokenService
from agent_memory_service.governance import InMemoryGovernanceStore
from agent_memory_service.http import create_http_app
from agent_memory_service.lifecycle import InMemoryErasureStore
from agent_memory_service.memory import MemoryModule
from agent_memory_service.models import PrincipalKind, TenantSession
from agent_memory_service.stores.memory import InMemoryTenantMemoryRouter


def _token(tokens: TokenService, principal_id: str, *roles: str) -> str:
    return tokens.issue(
        TenantSession(
            tenant_id="tenant-a",
            actor_id=principal_id,
            actor_kind=PrincipalKind.USER,
            roles=frozenset(roles),
        ),
        lifetime=timedelta(days=90),
    ).access_token


def test_typed_http_promotion_matches_memory_module_visibility() -> None:
    tokens = TokenService(InMemoryTokenStore())
    alice_token = _token(tokens, "user-alice", "tenant_member")
    curator_token = _token(tokens, "user-curator", "tenant_member", "knowledge_curator")
    bob_token = _token(tokens, "user-bob", "tenant_member")
    memory = MemoryModule(
        InMemoryTenantMemoryRouter(["tenant-a"]),
        InMemoryGovernanceStore(),
        erasures=InMemoryErasureStore(),
    )
    client = TestClient(create_http_app(memory, tokens))

    retained = client.post(
        "/api/v1/memories",
        headers={"Authorization": f"Bearer {alice_token}"},
        json={
            "content": "Product A uses canary deployments.",
            "kind": "constraint",
            "idempotency_key": "alice-canary",
        },
    ).json()
    assert retained["state"] == "applied"
    candidate = client.post(
        "/api/v1/knowledge/candidates",
        headers={"Authorization": f"Bearer {alice_token}"},
        json={
            "claim": "Product A uses canary deployments.",
            "source_memory_ids": [retained["id"]],
            "duplicate_memory_ids": ["tenant-memory-duplicate"],
            "conflicting_memory_ids": ["tenant-memory-conflict"],
            "idempotency_key": "candidate-canary",
        },
    )

    assert candidate.status_code == 201
    candidate_document = candidate.json()
    assert "source_memory_ids" not in candidate_document
    assert candidate_document["duplicate_memory_ids"] == ["tenant-memory-duplicate"]
    assert candidate_document["conflicting_memory_ids"] == ["tenant-memory-conflict"]
    inspected_private = client.get(
        "/api/v1/memories",
        headers={"Authorization": f"Bearer {alice_token}"},
    )
    assert [item["id"] for item in inspected_private.json()] == [retained["id"]]
    assert inspected_private.json()[0]["state"] == "active"
    curator_queue = client.get(
        "/api/v1/knowledge/candidates",
        headers={"Authorization": f"Bearer {curator_token}"},
    )
    assert curator_queue.status_code == 200
    assert curator_queue.json() == [candidate_document]
    forbidden_queue = client.get(
        "/api/v1/knowledge/candidates",
        headers={"Authorization": f"Bearer {bob_token}"},
    )
    assert forbidden_queue.status_code == 403
    reviewed = client.post(
        f"/api/v1/knowledge/candidates/{candidate_document['id']}/reviews",
        headers={"Authorization": f"Bearer {curator_token}"},
        json={
            "decision": "approve",
            "rationale": "Confirmed convention.",
            "idempotency_key": "approve-canary",
        },
    )
    before = client.post(
        "/api/v1/memories/recall",
        headers={"Authorization": f"Bearer {bob_token}"},
        json={"query": "canary deployments"},
    )

    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "publishing"
    assert before.json()["items"] == []

    asyncio.run(memory.publish_next("tenant-a"))
    after = client.post(
        "/api/v1/memories/recall",
        headers={"Authorization": f"Bearer {bob_token}"},
        json={"query": "canary deployments"},
    )
    assert [item["content"] for item in after.json()["items"]] == [
        "Product A uses canary deployments."
    ]

from __future__ import annotations

import httpx

from agent_memory_service.agents import (
    AgentRunState,
    AgentRuntimeModule,
    KnowledgeSynthesisCapability,
    MemoryModuleKnowledgeSynthesisPort,
    RecordedCompletion,
    RecordedProvider,
)
from agent_memory_service.auth import TokenService
from agent_memory_service.control import ControlModule, InMemoryControlStore
from agent_memory_service.governance import InMemoryGovernanceStore
from agent_memory_service.http import create_http_app
from agent_memory_service.memory import MemoryModule
from agent_memory_service.stores.memory import InMemoryTenantMemoryRouter


async def test_release_tracer_proves_reviewed_sharing_and_cross_tenant_isolation() -> None:
    control_store = InMemoryControlStore()
    tokens = TokenService(control_store)
    control = ControlModule(control_store, tokens)
    for tenant_id, name in (("tenant-a", "Product A"), ("tenant-b", "Product B")):
        control.create_tenant(tenant_id, name)
    for principal_id, name, kind in (
        ("user-alice", "Alice", "user"),
        ("user-bob", "Bob", "user"),
        ("user-curator", "Curator", "user"),
        ("agent-reader", "Reader Agent", "agent"),
        ("user-eve", "Eve", "user"),
    ):
        control.create_principal(principal_id, name, kind)
    for principal_id in ("user-alice", "user-bob", "agent-reader"):
        control.grant_membership("tenant-a", principal_id, "tenant_member")
    control.grant_membership("tenant-a", "user-curator", "tenant_member")
    control.grant_membership("tenant-a", "user-curator", "knowledge_curator")
    control.grant_membership("tenant-b", "user-eve", "tenant_member")
    control.grant_membership("tenant-b", "user-eve", "knowledge_curator")

    credentials = {
        principal_id: control.issue_access_token(tenant_id, principal_id).access_token
        for principal_id, tenant_id in (
            ("user-alice", "tenant-a"),
            ("user-bob", "tenant-a"),
            ("user-curator", "tenant-a"),
            ("agent-reader", "tenant-a"),
            ("user-eve", "tenant-b"),
        )
    }
    sessions = {
        principal_id: control.authenticate(access_token)
        for principal_id, access_token in credentials.items()
    }
    router = InMemoryTenantMemoryRouter(("tenant-a", "tenant-b"))
    governance = InMemoryGovernanceStore()
    memory = MemoryModule(router, governance)
    runtime = AgentRuntimeModule(
        capabilities=(
            KnowledgeSynthesisCapability(MemoryModuleKnowledgeSynthesisPort(memory, router)),
        ),
        provider=RecordedProvider(
            [
                RecordedCompletion(
                    parsed={
                        "claim": "Product A deployments require a tested rollback step.",
                        "confidence": 0.97,
                        "duplicate_memory_ids": [],
                        "conflicting_memory_ids": [],
                    }
                )
            ]
        ),
    )
    app = create_http_app(memory, tokens, agents=runtime, execute_agent_runs=False)
    transport = httpx.ASGITransport(app=app)

    def headers(principal_id: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {credentials[principal_id]}"}

    async with httpx.AsyncClient(transport=transport, base_url="http://memory.test") as client:
        retained = await client.post(
            "/api/v1/memories",
            headers=headers("user-alice"),
            json={
                "content": "Product A deploys require a tested rollback step.",
                "idempotency_key": "release-tracer-alice-private",
            },
        )
        assert retained.status_code == 202
        assert retained.json()["state"] == "applied"
        alice_private = retained.json()["item"]

        started = await client.post(
            "/api/v1/agent-runs",
            headers=headers("user-alice"),
            json={
                "capability": "knowledge_synthesis",
                "input": {
                    "request": "Distill our deployment safety rule",
                    "source_memory_ids": [alice_private["id"]],
                },
                "idempotency_key": "release-tracer-synthesis",
            },
        )
        assert started.status_code == 202
        run_id = started.json()["id"]

        completed = await runtime.run_until_terminal(sessions["user-alice"], run_id)
        observed_run = await client.get(
            f"/api/v1/agent-runs/{run_id}", headers=headers("user-alice")
        )
        assert observed_run.status_code == 200
        assert observed_run.json()["state"] == "completed"
        assert completed.state is AgentRunState.COMPLETED
        assert completed.output is not None
        candidate_id = str(completed.output["candidate"]["id"])

        before_publication = await client.post(
            "/api/v1/memories/recall",
            headers=headers("user-bob"),
            json={"query": "tested rollback"},
        )
        assert before_publication.status_code == 200
        assert before_publication.json() == {"items": []}

        reviewed = await client.post(
            f"/api/v1/knowledge/candidates/{candidate_id}/reviews",
            headers=headers("user-curator"),
            json={
                "decision": "approve",
                "rationale": "Verified Product A deployment policy.",
                "idempotency_key": "release-tracer-approval",
            },
        )
        assert reviewed.status_code == 200
        assert reviewed.json()["status"] == "publishing"

        still_private = await client.post(
            "/api/v1/memories/recall",
            headers=headers("user-bob"),
            json={"query": "tested rollback"},
        )
        assert still_private.json() == {"items": []}

        published = await memory.publish_next("tenant-a")
        assert published is not None

        bob_recall = await client.post(
            "/api/v1/memories/recall",
            headers=headers("user-bob"),
            json={"query": "tested rollback"},
        )
        agent_recall = await client.post(
            "/api/v1/memories/recall",
            headers=headers("agent-reader"),
            json={"query": "tested rollback"},
        )
        other_tenant_recall = await client.post(
            "/api/v1/memories/recall",
            headers=headers("user-eve"),
            json={"query": "tested rollback"},
        )
        foreign_candidate_queue = await client.get(
            "/api/v1/knowledge/candidates", headers=headers("user-eve")
        )
        foreign_run = await client.get(f"/api/v1/agent-runs/{run_id}", headers=headers("user-eve"))

    assert [item["content"] for item in bob_recall.json()["items"]] == [
        "Product A deployments require a tested rollback step."
    ]
    assert [item["content"] for item in agent_recall.json()["items"]] == [
        "Product A deployments require a tested rollback step."
    ]
    assert all(item["id"] != alice_private["id"] for item in bob_recall.json()["items"])
    assert other_tenant_recall.json() == {"items": []}
    assert "rollback" not in other_tenant_recall.text
    assert alice_private["id"] not in other_tenant_recall.text
    assert foreign_candidate_queue.status_code == 200
    assert foreign_candidate_queue.json() == []
    assert foreign_run.status_code == 404
    assert foreign_run.json() == {
        "code": "not_found",
        "message": "Resource was not found",
    }

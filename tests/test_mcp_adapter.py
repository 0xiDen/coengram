from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from starlette.testclient import TestClient

from agent_memory_service.agents import (
    AgentRuntimeModule,
    KnowledgeSynthesisCapability,
    MemoryModuleKnowledgeSynthesisPort,
    RecordedProvider,
)
from agent_memory_service.auth import InMemoryTokenStore, TokenService
from agent_memory_service.governance import InMemoryGovernanceStore
from agent_memory_service.mcp_server import create_memory_mcp_server
from agent_memory_service.memory import MemoryModule
from agent_memory_service.models import PrincipalKind, TenantSession
from agent_memory_service.stores.memory import InMemoryTenantMemoryRouter

ROOT = Path(__file__).resolve().parents[1]


def _mcp_client() -> tuple[TestClient, str]:
    tokens = TokenService(InMemoryTokenStore())
    credential = tokens.issue(
        TenantSession(
            tenant_id="tenant-a",
            actor_id="user-alice",
            actor_kind=PrincipalKind.USER,
            roles=frozenset({"tenant_member"}),
        ),
        lifetime=timedelta(days=90),
    )
    mcp = create_memory_mcp_server(
        MemoryModule(InMemoryTenantMemoryRouter(["tenant-a"])),
        tokens,
    )
    return TestClient(mcp.streamable_http_app()), credential.access_token


def _rpc(
    method: str,
    request_id: int,
    params: dict[str, object] | None = None,
) -> dict[str, object]:
    document: dict[str, object] = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
    }
    if params is not None:
        document["params"] = params
    return document


def test_streamable_http_mcp_requires_an_opaque_access_token() -> None:
    client, _ = _mcp_client()

    response = client.post(
        "/mcp",
        headers={"Accept": "application/json, text/event-stream"},
        json=_rpc(
            "initialize",
            1,
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        ),
    )

    assert response.status_code == 401


def test_authenticated_mcp_exposes_only_intent_oriented_tools() -> None:
    client, token = _mcp_client()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }

    with client:
        response = client.post("/mcp", headers=headers, json=_rpc("tools/list", 2))

    assert response.status_code == 200
    tools = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert tools == {
        "knowledge_propose",
        "knowledge_review",
        "memory_correct",
        "memory_inspect_private",
        "memory_recall",
        "memory_request_erasure",
        "memory_retain",
    }
    assert "memory_add_entity" not in tools
    assert "memory_read_graph" not in tools
    assert "memory_query_readonly" not in tools


def test_mcp_retain_and_recall_use_token_derived_scope() -> None:
    client, token = _mcp_client()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }
    with client:
        retained = client.post(
            "/mcp",
            headers=headers,
            json=_rpc(
                "tools/call",
                3,
                {
                    "name": "memory_retain",
                    "arguments": {
                        "content": "MCP requests use server-derived tenant scope.",
                        "kind": "constraint",
                        "confidence": 1.0,
                        "idempotency_key": "mcp-derived-scope",
                    },
                },
            ),
        )
        recalled = client.post(
            "/mcp",
            headers=headers,
            json=_rpc(
                "tools/call",
                4,
                {
                    "name": "memory_recall",
                    "arguments": {"query": "tenant scope", "limit": 10},
                },
            ),
        )

    assert retained.status_code == 200
    assert retained.json()["result"]["isError"] is False
    structured = recalled.json()["result"]["structuredContent"]
    assert [item["content"] for item in structured["items"]] == [
        "MCP requests use server-derived tenant scope."
    ]


def test_claude_code_example_uses_remote_http_and_environment_token() -> None:
    document = json.loads((ROOT / ".mcp.json.example").read_text(encoding="utf-8"))

    server = document["mcpServers"]["coengram"]
    assert server == {
        "type": "http",
        "url": "https://memory.example.com/mcp",
        "headers": {"Authorization": "Bearer ${MEMORY_MCP_TOKEN}"},
    }


def test_mcp_agent_tools_queue_inspect_and_cancel_a_tenant_run() -> None:
    tokens = TokenService(InMemoryTokenStore())
    credential = tokens.issue(
        TenantSession(
            tenant_id="tenant-a",
            actor_id="user-alice",
            actor_kind=PrincipalKind.USER,
            roles=frozenset({"tenant_member"}),
        ),
        lifetime=timedelta(days=90),
    )
    router = InMemoryTenantMemoryRouter(["tenant-a"])
    memory = MemoryModule(router, InMemoryGovernanceStore())
    agents = AgentRuntimeModule(
        capabilities=(
            KnowledgeSynthesisCapability(MemoryModuleKnowledgeSynthesisPort(memory, router)),
        ),
        provider=RecordedProvider([]),
    )
    mcp = create_memory_mcp_server(memory, tokens, agents=agents)
    client = TestClient(mcp.streamable_http_app())
    headers = {
        "Authorization": f"Bearer {credential.access_token}",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }

    with client:
        listed = client.post("/mcp", headers=headers, json=_rpc("tools/list", 10))
        started = client.post(
            "/mcp",
            headers=headers,
            json=_rpc(
                "tools/call",
                11,
                {
                    "name": "knowledge_synthesize",
                    "arguments": {
                        "request": "Distill this selected lesson",
                        "source_memory_ids": ["memory-selected"],
                        "idempotency_key": "mcp-agent-run",
                    },
                },
            ),
        )
        run_id = started.json()["result"]["structuredContent"]["id"]
        status_response = client.post(
            "/mcp",
            headers=headers,
            json=_rpc(
                "tools/call",
                12,
                {"name": "agent_run_status", "arguments": {"run_id": run_id}},
            ),
        )
        cancelled = client.post(
            "/mcp",
            headers=headers,
            json=_rpc(
                "tools/call",
                13,
                {"name": "agent_run_cancel", "arguments": {"run_id": run_id}},
            ),
        )

    tool_names = {tool["name"] for tool in listed.json()["result"]["tools"]}
    assert {"knowledge_synthesize", "agent_run_status", "agent_run_cancel"} <= tool_names
    assert status_response.json()["result"]["structuredContent"]["state"] == "queued"
    assert cancelled.json()["result"]["structuredContent"]["state"] == "cancel_requested"


def test_mcp_delegated_agent_runs_are_isolated_by_subject_user() -> None:
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
    router = InMemoryTenantMemoryRouter(["tenant-a"])
    memory = MemoryModule(router, InMemoryGovernanceStore())
    runtime = AgentRuntimeModule(
        capabilities=(
            KnowledgeSynthesisCapability(MemoryModuleKnowledgeSynthesisPort(memory, router)),
        ),
        provider=RecordedProvider([]),
    )
    client = TestClient(
        create_memory_mcp_server(memory, tokens, agents=runtime).streamable_http_app()
    )
    headers = tuple(
        {
            "Authorization": f"Bearer {credential.access_token}",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
        }
        for credential in credentials
    )
    arguments = {
        "request": "Distill selected guidance",
        "source_memory_ids": ["memory-1"],
        "idempotency_key": "same-agent-key",
    }

    with client:
        starts = tuple(
            client.post(
                "/mcp",
                headers=scope_headers,
                json=_rpc(
                    "tools/call",
                    20 + index,
                    {"name": "knowledge_synthesize", "arguments": arguments},
                ),
            )
            for index, scope_headers in enumerate(headers)
        )
        run_ids = tuple(response.json()["result"]["structuredContent"]["id"] for response in starts)
        hidden = client.post(
            "/mcp",
            headers=headers[1],
            json=_rpc(
                "tools/call",
                22,
                {"name": "agent_run_status", "arguments": {"run_id": run_ids[0]}},
            ),
        )
        cancellation_hidden = client.post(
            "/mcp",
            headers=headers[1],
            json=_rpc(
                "tools/call",
                23,
                {"name": "agent_run_cancel", "arguments": {"run_id": run_ids[0]}},
            ),
        )

    assert all(response.status_code == 200 for response in starts)
    assert run_ids[0] != run_ids[1]
    assert hidden.json()["result"]["isError"] is True
    assert cancellation_hidden.json()["result"]["isError"] is True

from __future__ import annotations

import tomllib
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from agent_memory_service.auth import InMemoryTokenStore, TokenService
from agent_memory_service.http import create_http_app
from agent_memory_service.memory import MemoryModule
from agent_memory_service.models import PrincipalKind, TenantSession
from agent_memory_service.stores.memory import InMemoryTenantMemoryRouter


def _client() -> tuple[TestClient, str]:
    token_store = InMemoryTokenStore()
    tokens = TokenService(token_store)
    credential = tokens.issue(
        TenantSession(
            tenant_id="tenant-product-a-backend",
            actor_id="user-alice",
            actor_kind=PrincipalKind.USER,
            roles=frozenset({"tenant_member"}),
        ),
        lifetime=timedelta(days=90),
    )
    app = create_http_app(
        MemoryModule(InMemoryTenantMemoryRouter(["tenant-product-a-backend"])),
        tokens,
    )
    return TestClient(app), credential.access_token


def test_openapi_version_matches_package_metadata() -> None:
    client, _token = _client()
    package = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert client.get("/openapi.json").json()["info"]["version"] == package["project"]["version"]


def test_bearer_token_scopes_http_retain_and_recall() -> None:
    client, token = _client()
    headers = {"Authorization": f"Bearer {token}"}

    retained = client.post(
        "/api/v1/memories",
        headers=headers,
        json={
            "content": "Use expand-contract for risky schema changes.",
            "kind": "constraint",
            "idempotency_key": "schema-change-rule",
            # Extra selectors are rejected rather than trusted.
            "tenant_id": "tenant-attacker-selected",
        },
    )
    recalled = client.post(
        "/api/v1/memories/recall",
        headers=headers,
        json={"query": "schema changes"},
    )

    assert retained.status_code == 422
    assert recalled.status_code == 200
    assert recalled.json() == {"items": []}

    retained = client.post(
        "/api/v1/memories",
        headers=headers,
        json={
            "content": "Use expand-contract for risky schema changes.",
            "kind": "constraint",
            "idempotency_key": "schema-change-rule",
        },
    )
    recalled = client.post(
        "/api/v1/memories/recall",
        headers=headers,
        json={"query": "schema changes"},
    )

    assert retained.status_code == 202
    assert retained.json()["state"] == "applied"
    assert recalled.status_code == 200
    assert [item["content"] for item in recalled.json()["items"]] == [
        "Use expand-contract for risky schema changes."
    ]


def test_http_memory_operations_require_a_valid_non_revoked_token() -> None:
    client, token = _client()

    missing = client.post("/api/v1/memories/recall", json={"query": "anything"})
    invalid = client.post(
        "/api/v1/memories/recall",
        headers={"Authorization": "Bearer not-a-token"},
        json={"query": "anything"},
    )

    assert missing.status_code == 401
    assert invalid.status_code == 401

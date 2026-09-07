from __future__ import annotations

import asyncio
from datetime import timedelta

from fastapi.testclient import TestClient

from agent_memory_service.auth import TokenService
from agent_memory_service.control import ControlModule, InMemoryControlStore
from agent_memory_service.governance import InMemoryGovernanceStore, ProposeKnowledge
from agent_memory_service.http import create_http_app
from agent_memory_service.memory import MemoryModule
from agent_memory_service.models import PrincipalKind, RetainMemory, TenantSession
from agent_memory_service.stores.memory import InMemoryTenantMemoryRouter


def _client() -> tuple[TestClient, str]:
    store = InMemoryControlStore()
    control = ControlModule(store, TokenService(store))
    control.create_tenant("tenant-a", "Product A")
    control.create_operator(
        "operator-alice",
        "Alice",
        frozenset({"identity_admin", "operator_admin", "tenant_support"}),
    )
    credential = control.issue_operator_access_token(
        "operator-alice",
        lifetime=timedelta(days=7),
    )
    app = create_http_app(
        MemoryModule(InMemoryTenantMemoryRouter(["tenant-a"])),
        TokenService(store),
        control=control,
    )
    return TestClient(app), credential.access_token


def _manifest_document(tenant_id: str = "tenant-b") -> dict[str, object]:
    return {
        "version": 1,
        "tenant_id": tenant_id,
        "name": "Product B",
        "principals": [
            {
                "principal_id": "user-admin",
                "name": "Admin User",
                "kind": "user",
            }
        ],
        "memberships": [
            {
                "principal_id": "user-admin",
                "roles": ["tenant_administrator"],
            }
        ],
    }


def test_admin_session_cookie_login_and_logout_require_csrf() -> None:
    client, access_token = _client()

    logged_in = client.post("/api/v1/admin/session", json={"access_token": access_token})

    assert logged_in.status_code == 201
    body = logged_in.json()
    assert body["operator_id"] == "operator-alice"
    assert body["roles"] == ["identity_admin", "operator_admin", "tenant_support"]
    assert body["csrf_token"]
    assert "coengram_admin_session" in logged_in.headers["set-cookie"]
    assert "HttpOnly" in logged_in.headers["set-cookie"]

    current = client.get("/api/v1/admin/session")
    assert current.status_code == 200
    assert current.json()["operator_id"] == "operator-alice"

    missing_csrf = client.delete("/api/v1/admin/session")
    assert missing_csrf.status_code == 401

    logged_out = client.delete(
        "/api/v1/admin/session",
        headers={"X-CoEngram-CSRF": body["csrf_token"]},
    )
    assert logged_out.status_code == 204
    assert client.get("/api/v1/admin/session").status_code == 401


def test_admin_routes_reject_invalid_session_credentials() -> None:
    client, _access_token = _client()

    assert client.get("/api/v1/admin/session").status_code == 401
    response = client.post("/api/v1/admin/session", json={"access_token": "not-an-operator-token"})
    assert response.status_code == 401


def test_admin_can_list_tenants_and_onboard_user_with_one_time_token() -> None:
    client, access_token = _client()
    login = client.post("/api/v1/admin/session", json={"access_token": access_token})
    csrf_token = login.json()["csrf_token"]

    tenants = client.get("/api/v1/admin/tenants")
    assert tenants.status_code == 200
    assert tenants.json()["tenants"] == [
        {"active": True, "name": "Product A", "tenant_id": "tenant-a"}
    ]

    created = client.post(
        "/api/v1/admin/users",
        headers={"X-CoEngram-CSRF": csrf_token},
        json={
            "tenant_id": "tenant-a",
            "principal_id": "user-bob",
            "name": "Bob",
            "roles": ["tenant_member"],
            "issue_token": True,
            "token_lifetime_days": 7,
        },
    )

    assert created.status_code == 201
    body = created.json()
    assert body["principal"] == {
        "active": True,
        "kind": "user",
        "name": "Bob",
        "principal_id": "user-bob",
    }
    assert body["membership"] == {
        "active": True,
        "principal_id": "user-bob",
        "roles": ["tenant_member"],
        "tenant_id": "tenant-a",
    }
    assert body["credential"]["access_token"].startswith("mem1.")
    assert body["credential"]["warning"]

    audit = client.get("/api/v1/admin/audit-events")
    assert audit.status_code == 200
    audit_body = audit.json()
    assert audit_body["events"][0]["action"] == "principal.create_user"
    assert audit_body["events"][0]["target_ids"] == {
        "principal_id": "user-bob",
        "tenant_id": "tenant-a",
    }
    assert body["credential"]["access_token"] not in audit.text


def test_admin_can_create_list_and_cancel_provisioning_job_idempotently() -> None:
    client, access_token = _client()
    login = client.post("/api/v1/admin/session", json={"access_token": access_token})
    csrf_token = login.json()["csrf_token"]
    payload = {
        "idempotency_key": "tenant-b-create-1",
        "manifest": _manifest_document(),
    }

    created = client.post(
        "/api/v1/admin/provisioning-jobs",
        headers={"X-CoEngram-CSRF": csrf_token},
        json=payload,
    )
    repeated = client.post(
        "/api/v1/admin/provisioning-jobs",
        headers={"X-CoEngram-CSRF": csrf_token},
        json=payload,
    )

    assert created.status_code == 201
    assert repeated.status_code == 201
    body = created.json()
    assert body["job_id"] == repeated.json()["job_id"]
    assert body["tenant_id"] == "tenant-b"
    assert body["state"] == "queued"
    assert body["completed_steps"] == []

    jobs = client.get("/api/v1/admin/provisioning-jobs")
    assert jobs.status_code == 200
    assert jobs.json()["jobs"][0]["job_id"] == body["job_id"]

    canceled = client.post(
        f"/api/v1/admin/provisioning-jobs/{body['job_id']}/cancel",
        headers={"X-CoEngram-CSRF": csrf_token},
    )

    assert canceled.status_code == 200
    assert canceled.json()["state"] == "cancel_requested"
    audit = client.get("/api/v1/admin/audit-events")
    actions = {event["action"] for event in audit.json()["events"]}
    assert {"provisioning_job.create", "provisioning_job.cancel_requested"} <= actions


def test_admin_can_manage_operators_and_operator_tokens() -> None:
    client, access_token = _client()
    login = client.post("/api/v1/admin/session", json={"access_token": access_token})
    csrf_token = login.json()["csrf_token"]

    created_operator = client.post(
        "/api/v1/admin/operators",
        headers={"X-CoEngram-CSRF": csrf_token},
        json={
            "operator_id": "operator-bob",
            "name": "Bob",
            "roles": ["tenant_provisioner"],
        },
    )

    assert created_operator.status_code == 201
    assert created_operator.json() == {
        "active": True,
        "name": "Bob",
        "operator_id": "operator-bob",
        "roles": ["tenant_provisioner"],
    }
    operators = client.get("/api/v1/admin/operators")
    assert operators.status_code == 200
    assert "operator-bob" in operators.text

    issued = client.post(
        "/api/v1/admin/operators/operator-bob/tokens",
        headers={"X-CoEngram-CSRF": csrf_token},
        json={"lifetime_days": 7},
    )
    assert issued.status_code == 201
    issued_body = issued.json()
    assert issued_body["access_token"].startswith("op1.")

    token_list = client.get("/api/v1/admin/operators/operator-bob/tokens")
    assert token_list.status_code == 200
    assert token_list.json()["tokens"][0]["token_id"] == issued_body["token_id"]
    assert issued_body["access_token"] not in token_list.text

    rotated = client.post(
        f"/api/v1/admin/operator-tokens/{issued_body['token_id']}/rotate",
        headers={"X-CoEngram-CSRF": csrf_token},
        json={"overlap_minutes": 10, "lifetime_days": 7},
    )
    assert rotated.status_code == 200
    assert rotated.json()["credential"]["access_token"].startswith("op1.")

    revoked = client.delete(
        f"/api/v1/admin/operator-tokens/{rotated.json()['credential']['token_id']}",
        headers={"X-CoEngram-CSRF": csrf_token},
    )
    assert revoked.status_code == 204

    disabled = client.patch(
        "/api/v1/admin/operators/operator-bob",
        headers={"X-CoEngram-CSRF": csrf_token},
        json={"active": False},
    )
    assert disabled.status_code == 200
    assert disabled.json()["active"] is False


def test_admin_can_list_principals_and_manage_principal_tokens() -> None:
    client, access_token = _client()
    login = client.post("/api/v1/admin/session", json={"access_token": access_token})
    csrf_token = login.json()["csrf_token"]
    created = client.post(
        "/api/v1/admin/users",
        headers={"X-CoEngram-CSRF": csrf_token},
        json={
            "tenant_id": "tenant-a",
            "principal_id": "user-cora",
            "name": "Cora",
            "roles": ["tenant_member"],
            "issue_token": False,
        },
    )
    assert created.status_code == 201

    principals = client.get("/api/v1/admin/principals")
    assert principals.status_code == 200
    assert principals.json()["principals"] == [
        {"active": True, "kind": "user", "name": "Cora", "principal_id": "user-cora"}
    ]
    assert principals.json()["memberships"] == [
        {
            "active": True,
            "principal_id": "user-cora",
            "roles": ["tenant_member"],
            "tenant_id": "tenant-a",
        }
    ]

    issued = client.post(
        "/api/v1/admin/tokens",
        headers={"X-CoEngram-CSRF": csrf_token},
        json={"tenant_id": "tenant-a", "principal_id": "user-cora", "lifetime_days": 7},
    )
    assert issued.status_code == 201
    issued_body = issued.json()
    assert issued_body["access_token"].startswith("mem1.")

    token_list = client.get(
        "/api/v1/admin/tokens",
        params={"tenant_id": "tenant-a", "principal_id": "user-cora"},
    )
    assert token_list.status_code == 200
    assert token_list.json()["tokens"][0]["token_id"] == issued_body["token_id"]
    assert token_list.json()["tokens"][0]["active"] is True
    assert issued_body["access_token"] not in token_list.text

    rotated = client.post(
        f"/api/v1/admin/tokens/{issued_body['token_id']}/rotate",
        headers={"X-CoEngram-CSRF": csrf_token},
        json={"overlap_minutes": 10, "lifetime_days": 7},
    )
    assert rotated.status_code == 200
    rotated_body = rotated.json()
    assert rotated_body["previous_token_id"] == issued_body["token_id"]
    assert rotated_body["credential"]["access_token"].startswith("mem1.")

    revoked = client.delete(
        f"/api/v1/admin/tokens/{rotated_body['credential']['token_id']}",
        headers={"X-CoEngram-CSRF": csrf_token},
    )
    assert revoked.status_code == 204


def test_admin_can_review_knowledge_candidates_without_private_sources_in_audit() -> None:
    store = InMemoryControlStore()
    control = ControlModule(store, TokenService(store))
    control.create_tenant("tenant-a", "Product A")
    control.create_operator(
        "operator-knowledge",
        "Knowledge",
        frozenset({"audit_viewer", "knowledge_admin"}),
    )
    credential = control.issue_operator_access_token("operator-knowledge")
    memory = MemoryModule(
        InMemoryTenantMemoryRouter(["tenant-a"]),
        InMemoryGovernanceStore(),
    )

    async def seed_candidate() -> str:
        session = TenantSession(
            tenant_id="tenant-a",
            actor_id="user-alice",
            actor_kind=PrincipalKind.USER,
            roles=frozenset({"tenant_member"}),
        )
        source = await memory.retain(
            session,
            RetainMemory(
                content="Private deployment detail for candidate review.",
                idempotency_key="admin-knowledge-source",
            ),
        )
        candidate = await memory.propose_knowledge(
            session,
            ProposeKnowledge(
                claim="Product A deploys with a guarded rollout.",
                source_memory_ids=(source.id,),
                idempotency_key="admin-knowledge-candidate",
            ),
        )
        return candidate.id

    candidate_id = asyncio.run(seed_candidate())
    client = TestClient(create_http_app(memory, TokenService(store), control=control))
    login = client.post("/api/v1/admin/session", json={"access_token": credential.access_token})
    csrf_token = login.json()["csrf_token"]

    listed = client.get("/api/v1/admin/tenants/tenant-a/knowledge-candidates")
    missing_csrf = client.post(
        f"/api/v1/admin/tenants/tenant-a/knowledge-candidates/{candidate_id}/reviews",
        json={
            "decision": "approve",
            "rationale": "Confirmed by release owner.",
            "idempotency_key": "admin-knowledge-review-missing-csrf",
        },
    )
    reviewed = client.post(
        f"/api/v1/admin/tenants/tenant-a/knowledge-candidates/{candidate_id}/reviews",
        headers={"X-CoEngram-CSRF": csrf_token},
        json={
            "decision": "approve",
            "rationale": "Confirmed by release owner.",
            "idempotency_key": "admin-knowledge-review",
        },
    )

    assert listed.status_code == 200
    listed_body = listed.json()
    assert listed_body["candidates"][0]["id"] == candidate_id
    assert listed_body["candidates"][0]["source_count"] == 1
    assert "source_memory_ids" not in listed.text
    assert missing_csrf.status_code == 401
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "publishing"
    assert reviewed.json()["reviewed_by"] == "operator:operator-knowledge"

    audit = client.get("/api/v1/admin/audit-events")
    assert audit.status_code == 200
    event = audit.json()["events"][0]
    assert event["action"] == "knowledge_candidate.review"
    assert event["target_ids"] == {"candidate_id": candidate_id, "tenant_id": "tenant-a"}
    assert event["after_metadata"] == {"decision": "approve", "status": "publishing"}
    assert "Private deployment detail" not in audit.text
    assert "Confirmed by release owner" not in audit.text


def test_knowledge_candidate_admin_routes_require_knowledge_admin_role() -> None:
    store = InMemoryControlStore()
    control = ControlModule(store, TokenService(store))
    control.create_operator("operator-support", "Support", frozenset({"tenant_support"}))
    credential = control.issue_operator_access_token("operator-support")
    app = create_http_app(
        MemoryModule(InMemoryTenantMemoryRouter(["tenant-a"]), InMemoryGovernanceStore()),
        TokenService(store),
        control=control,
    )
    client = TestClient(app)
    login = client.post("/api/v1/admin/session", json={"access_token": credential.access_token})
    csrf_token = login.json()["csrf_token"]

    listed = client.get("/api/v1/admin/tenants/tenant-a/knowledge-candidates")
    reviewed = client.post(
        "/api/v1/admin/tenants/tenant-a/knowledge-candidates/candidate-a/reviews",
        headers={"X-CoEngram-CSRF": csrf_token},
        json={
            "decision": "reject",
            "rationale": "Not verified.",
            "idempotency_key": "support-review-denied",
        },
    )

    assert listed.status_code == 403
    assert reviewed.status_code == 403


def test_knowledge_candidate_admin_routes_reject_unknown_tenant() -> None:
    store = InMemoryControlStore()
    control = ControlModule(store, TokenService(store))
    control.create_operator("operator-knowledge", "Knowledge", frozenset({"knowledge_admin"}))
    credential = control.issue_operator_access_token("operator-knowledge")
    app = create_http_app(
        MemoryModule(InMemoryTenantMemoryRouter(["tenant-a"]), InMemoryGovernanceStore()),
        TokenService(store),
        control=control,
    )
    client = TestClient(app)
    client.post("/api/v1/admin/session", json={"access_token": credential.access_token})

    listed = client.get("/api/v1/admin/tenants/tenant-missing/knowledge-candidates")

    assert listed.status_code == 404


def test_admin_user_onboarding_requires_identity_role_and_csrf() -> None:
    store = InMemoryControlStore()
    control = ControlModule(store, TokenService(store))
    control.create_tenant("tenant-a", "Product A")
    control.create_operator("operator-support", "Support", frozenset({"tenant_support"}))
    credential = control.issue_operator_access_token("operator-support")
    app = create_http_app(
        MemoryModule(InMemoryTenantMemoryRouter(["tenant-a"])),
        TokenService(store),
        control=control,
    )
    client = TestClient(app)
    login = client.post("/api/v1/admin/session", json={"access_token": credential.access_token})
    csrf_token = login.json()["csrf_token"]
    payload = {
        "tenant_id": "tenant-a",
        "principal_id": "user-bob",
        "name": "Bob",
        "roles": ["tenant_member"],
        "issue_token": True,
    }

    missing_csrf = client.post("/api/v1/admin/users", json=payload)
    forbidden = client.post(
        "/api/v1/admin/users",
        headers={"X-CoEngram-CSRF": csrf_token},
        json=payload,
    )

    assert missing_csrf.status_code == 401
    assert forbidden.status_code == 403


def test_provisioning_mutations_require_tenant_provisioner_or_operator_admin() -> None:
    store = InMemoryControlStore()
    control = ControlModule(store, TokenService(store))
    control.create_operator("operator-support", "Support", frozenset({"tenant_support"}))
    credential = control.issue_operator_access_token("operator-support")
    app = create_http_app(
        MemoryModule(InMemoryTenantMemoryRouter(["tenant-a"])),
        TokenService(store),
        control=control,
    )
    client = TestClient(app)
    login = client.post("/api/v1/admin/session", json={"access_token": credential.access_token})
    csrf_token = login.json()["csrf_token"]

    listed = client.get("/api/v1/admin/provisioning-jobs")
    created = client.post(
        "/api/v1/admin/provisioning-jobs",
        headers={"X-CoEngram-CSRF": csrf_token},
        json={
            "idempotency_key": "tenant-b-create-1",
            "manifest": _manifest_document(),
        },
    )

    assert listed.status_code == 200
    assert created.status_code == 403


def test_audit_events_require_audit_viewer_or_operator_admin() -> None:
    store = InMemoryControlStore()
    control = ControlModule(store, TokenService(store))
    control.create_operator("operator-token", "Token", frozenset({"token_admin"}))
    credential = control.issue_operator_access_token("operator-token")
    app = create_http_app(
        MemoryModule(InMemoryTenantMemoryRouter(["tenant-a"])),
        TokenService(store),
        control=control,
    )
    client = TestClient(app)
    client.post("/api/v1/admin/session", json={"access_token": credential.access_token})

    response = client.get("/api/v1/admin/audit-events")

    assert response.status_code == 403

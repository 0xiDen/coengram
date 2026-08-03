"""Real PostgreSQL checks for durable decommission state and access suspension."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from agent_memory_service.auth import AuthenticationError, TokenService
from agent_memory_service.control import ControlModule, TenantRouteRecord
from agent_memory_service.decommission import (
    DecommissionConflict,
    DecommissionRecord,
    DecommissionState,
    DecommissionTombstone,
    ProtectionEvidence,
    ProtectionKind,
    TenantResourceIdentity,
)
from agent_memory_service.stores.postgres_control import PostgresControlStore
from agent_memory_service.stores.postgres_decommission import (
    PostgresDecommissionRetentionAdapter,
    PostgresDecommissionStore,
    PostgresTenantAccessAdapter,
)

CONTROL_DATABASE_URL = os.environ.get("CONTROL_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not CONTROL_DATABASE_URL,
    reason="CONTROL_DATABASE_URL is required for PostgreSQL decommission integration tests",
)


@pytest.fixture(scope="module", autouse=True)
def migrated_control_store() -> None:
    if not CONTROL_DATABASE_URL:
        return
    from alembic import command
    from alembic.config import Config

    repository_root = Path(__file__).resolve().parents[2]
    command.upgrade(Config(repository_root / "alembic-control.ini"), "head")


def resources(tenant_id: str) -> TenantResourceIdentity:
    normalized = tenant_id.replace("-", "_")
    return TenantResourceIdentity(
        tenant_id=tenant_id,
        compose_project=f"memory-tenant-{tenant_id}",
        neo4j_volume=f"memory-tenant-{tenant_id}-neo4j-data",
        database_name=f"tenant_{normalized}",
        database_role=f"tenant_{normalized}_rw",
        route_id=f"route:{tenant_id}",
        tenant_secret_ref=f"/run/memory-tenants/{tenant_id}",
    )


def test_request_revisions_are_durable_and_stale_writes_fail() -> None:
    assert CONTROL_DATABASE_URL is not None
    suffix = uuid4().hex[:20]
    tenant_id = f"tenant-{suffix}"
    request_id = f"decom-{suffix}"
    store = PostgresDecommissionStore(CONTROL_DATABASE_URL)
    now = datetime.now(UTC)
    initial = DecommissionRecord(
        request_id=request_id,
        resources=resources(tenant_id),
        requested_by="operator-alice",
        reason="integration test",
        requested_at=now,
        protection_policy="recent-valid-backup-or-accepted-export",
        state=DecommissionState.SUSPENDING,
    )

    first = store.save_record(initial)
    assert first.revision == 1
    assert store.get_record(request_id) == first
    evidence = ProtectionEvidence(
        kind=ProtectionKind.BACKUP,
        artifact_id=f"backup-{suffix}",
        created_at=now - timedelta(hours=2),
        verified_at=now - timedelta(hours=1),
        verified_by="operator-backup",
        integrity_verified=True,
        complete=True,
    )
    grace_ends_at = now + timedelta(days=30)
    second = store.save_record(
        first.model_copy(
            update={
                "state": DecommissionState.GRACE_PERIOD,
                "confirmed_by": "operator-bob",
                "confirmed_at": now,
                "grace_ends_at": grace_ends_at,
                "protection_evidence": evidence,
            }
        )
    )
    assert second.revision == 2
    with pytest.raises(DecommissionConflict, match="concurrently"):
        store.save_record(first)

    tombstone = DecommissionTombstone(
        request_id=request_id,
        tenant_id=tenant_id,
        requested_by="operator-alice",
        confirmed_by="operator-bob",
        destroyed_at=grace_ends_at,
    )
    store.replace_with_tombstone(second, tombstone)
    assert store.get_record(request_id) is None
    assert store.get_tombstone(request_id) == tombstone
    retention = PostgresDecommissionRetentionAdapter(CONTROL_DATABASE_URL)
    pins = retention.pins_for_tenant(
        tenant_id,
        now=grace_ends_at + timedelta(days=29),
    )
    assert pins[0].backup_id == evidence.artifact_id
    assert pins[0].reason == "post-destruction-recovery"
    assert (
        retention.pins_for_tenant(
            tenant_id,
            now=grace_ends_at + timedelta(days=30),
        )
        == ()
    )


def test_access_adapter_fail_closes_tenant_and_old_tokens_stay_revoked() -> None:
    assert CONTROL_DATABASE_URL is not None
    suffix = uuid4().hex[:20]
    tenant_id = f"tenant-{suffix}"
    user_id = f"user-{suffix}"
    resource = resources(tenant_id)
    control_store = PostgresControlStore(CONTROL_DATABASE_URL)
    control = ControlModule(control_store, TokenService(control_store))
    control.create_tenant(tenant_id, "Decommission access test")
    control.create_principal(user_id, "Alice", "user")
    control.grant_membership(tenant_id, user_id, "tenant_member")
    control_store.save_tenant_route(
        TenantRouteRecord(
            tenant_id=tenant_id,
            neo4j_service_address=f"neo4j-{tenant_id}:7687",
            neo4j_secret_name=f"{tenant_id}/neo4j_password",
            tenant_database_name=resource.database_name,
            tenant_database_role=resource.database_role,
            healthy=True,
        )
    )
    credential = control.issue_access_token(tenant_id, user_id)
    access = PostgresTenantAccessAdapter(CONTROL_DATABASE_URL)

    access.suspend_sessions(resource)
    access.block_token_issuance(resource)
    access.revoke_active_credentials(resource)
    with pytest.raises(AuthenticationError):
        control.authenticate(credential.access_token)
    assert control_store.list_token_records(tenant_id, user_id)[0].revoked_at is not None

    access.reactivate_without_issuing_tokens(resource)
    with pytest.raises(AuthenticationError):
        control.authenticate(credential.access_token)
    replacement = control.issue_access_token(tenant_id, user_id)
    assert control.authenticate(replacement.access_token).tenant_id == tenant_id

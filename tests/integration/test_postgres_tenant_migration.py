"""PostgreSQL progress checks for active-Tenant schema migration."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from agent_memory_service.control import TenantRecord, TenantRouteRecord
from agent_memory_service.stores.postgres_control import PostgresControlStore
from agent_memory_service.stores.postgres_tenant_migration import (
    PostgresActiveMigrationStateStore,
)
from agent_memory_service.tenant_migration import (
    ACTIVE_MIGRATION_STEPS,
    ActiveMigrationStatus,
)

CONTROL_DATABASE_URL = os.environ.get("CONTROL_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not CONTROL_DATABASE_URL,
    reason="CONTROL_DATABASE_URL is required for PostgreSQL Tenant migration tests",
)


@pytest.fixture(scope="module", autouse=True)
def migrated_control_store() -> None:
    if not CONTROL_DATABASE_URL:
        return
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[2]
    command.upgrade(Config(root / "alembic-control.ini"), "head")


def test_active_migration_state_persists_failure_and_resume_cursor() -> None:
    assert CONTROL_DATABASE_URL is not None
    suffix = uuid4().hex[:16]
    tenant_id = f"tenant-{suffix}"
    normalized = tenant_id.replace("-", "_")
    control = PostgresControlStore(CONTROL_DATABASE_URL)
    control.add_tenant(TenantRecord(tenant_id, "Migration integration"))
    control.save_tenant_route(
        TenantRouteRecord(
            tenant_id=tenant_id,
            neo4j_service_address=f"neo4j-{tenant_id}:7687",
            neo4j_secret_name=f"{tenant_id}/neo4j_password",
            tenant_database_name=f"tenant_{normalized}",
            tenant_database_role=f"tenant_{normalized}_rw",
            healthy=True,
        )
    )
    states = PostgresActiveMigrationStateStore(CONTROL_DATABASE_URL)

    started = states.start(tenant_id)
    assert started.status is ActiveMigrationStatus.APPLYING
    first = states.mark_completed(tenant_id, ACTIVE_MIGRATION_STEPS[0])
    failed = states.mark_failed(tenant_id, ACTIVE_MIGRATION_STEPS[1], "InjectedFailure")
    assert failed.completed_steps == first.completed_steps
    assert failed.status is ActiveMigrationStatus.FAILED

    resumed = states.resume(tenant_id)
    assert resumed.attempt == 2
    for step in ACTIVE_MIGRATION_STEPS[1:]:
        states.mark_completed(tenant_id, step)
    completed = states.complete(tenant_id)
    assert completed.status is ActiveMigrationStatus.COMPLETED
    assert completed.completed_steps == ACTIVE_MIGRATION_STEPS

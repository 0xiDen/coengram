from __future__ import annotations

import io
import json

import pytest

from agent_memory_service.auth import TokenService
from agent_memory_service.cli import run_cli
from agent_memory_service.control import ControlModule, InMemoryControlStore, TenantRouteRecord
from agent_memory_service.tenant_migration import (
    ACTIVE_MIGRATION_STEPS,
    ActiveMigrationFailed,
    ActiveMigrationStatus,
    ActiveTenantMigrationService,
    InMemoryActiveMigrationStateStore,
)


class MigrationAdapter:
    def __init__(self, *, fail_once_at: str | None = None) -> None:
        self.fail_once_at = fail_once_at
        self.calls: list[str] = []

    def _call(self, name: str, route: TenantRouteRecord) -> None:
        self.calls.append(f"{name}:{route.tenant_id}")
        if self.fail_once_at == name:
            self.fail_once_at = None
            raise RuntimeError("simulated migration dependency failure")

    def verify_recovery_point(self, route: TenantRouteRecord) -> None:
        self._call("verify_recovery_point", route)

    def migrate_postgres(self, route: TenantRouteRecord) -> None:
        self._call("migrate_postgres", route)

    def migrate_neo4j(self, route: TenantRouteRecord) -> None:
        self._call("migrate_neo4j", route)

    def verify_neo4j_health(self, route: TenantRouteRecord) -> None:
        self._call("verify_neo4j_health", route)

    def verify_routing(self, route: TenantRouteRecord) -> None:
        self._call("verify_routing", route)

    def verify_isolation(self, route: TenantRouteRecord) -> None:
        self._call("verify_isolation", route)


def active_control(*, healthy: bool = True) -> ControlModule:
    store = InMemoryControlStore()
    control = ControlModule(store, TokenService(store))
    control.create_tenant("tenant-a", "Product A")
    control.register_tenant_route(
        "tenant-a",
        neo4j_service_address="neo4j-tenant-a:7687",
        neo4j_secret_name="tenant-a/neo4j_password",
        tenant_database_name="tenant_tenant_a",
        tenant_database_role="tenant_tenant_a_rw",
        healthy=healthy,
    )
    return control


def test_plan_is_read_only_and_lists_only_fixed_steps() -> None:
    states = InMemoryActiveMigrationStateStore()
    adapter = MigrationAdapter()
    migration = ActiveTenantMigrationService(active_control(), states, adapter)

    plan = migration.plan("tenant-a")

    assert plan.current_status is ActiveMigrationStatus.NOT_STARTED
    assert plan.pending_steps == ACTIVE_MIGRATION_STEPS
    assert states.get("tenant-a") is None
    assert adapter.calls == []


def test_apply_completes_fixed_steps_and_is_idempotent_after_success() -> None:
    states = InMemoryActiveMigrationStateStore()
    adapter = MigrationAdapter()
    migration = ActiveTenantMigrationService(active_control(), states, adapter)

    completed = migration.apply("tenant-a")

    assert completed.status is ActiveMigrationStatus.COMPLETED
    assert completed.completed_steps == ACTIVE_MIGRATION_STEPS
    assert completed.attempt == 1
    assert migration.apply("tenant-a") == completed
    assert adapter.calls == [f"{step.value}:tenant-a" for step in ACTIVE_MIGRATION_STEPS]


def test_apply_fails_before_schema_mutation_without_recovery_evidence() -> None:
    states = InMemoryActiveMigrationStateStore()
    adapter = MigrationAdapter(fail_once_at="verify_recovery_point")
    migration = ActiveTenantMigrationService(active_control(), states, adapter)

    with pytest.raises(ActiveMigrationFailed) as raised:
        migration.apply("tenant-a")

    assert raised.value.step.value == "verify_recovery_point"
    failed = states.get("tenant-a")
    assert failed is not None
    assert failed.completed_steps == ()
    assert adapter.calls == ["verify_recovery_point:tenant-a"]


def test_resume_skips_persisted_steps_after_failure() -> None:
    states = InMemoryActiveMigrationStateStore()
    adapter = MigrationAdapter(fail_once_at="migrate_neo4j")
    migration = ActiveTenantMigrationService(active_control(), states, adapter)

    try:
        migration.apply("tenant-a")
    except ActiveMigrationFailed as error:
        assert error.step.value == "migrate_neo4j"
    else:  # pragma: no cover - test requires the injected failure
        raise AssertionError("expected migration failure")
    failed = states.get("tenant-a")
    assert failed is not None
    assert failed.status is ActiveMigrationStatus.FAILED
    assert [step.value for step in failed.completed_steps] == [
        "verify_recovery_point",
        "migrate_postgres",
    ]

    resumed = migration.resume("tenant-a")

    assert resumed.status is ActiveMigrationStatus.COMPLETED
    assert resumed.attempt == 2
    assert adapter.calls.count("migrate_postgres:tenant-a") == 1
    assert adapter.calls.count("migrate_neo4j:tenant-a") == 2


def test_unhealthy_tenant_cannot_plan_or_mutate_migration_state() -> None:
    states = InMemoryActiveMigrationStateStore()
    migration = ActiveTenantMigrationService(
        active_control(healthy=False), states, MigrationAdapter()
    )

    try:
        migration.plan("tenant-a")
    except Exception as error:
        assert "route" in str(error)
    else:  # pragma: no cover
        raise AssertionError("unhealthy route must fail closed")
    assert states.get("tenant-a") is None


def test_memoryctl_plan_apply_and_resume_require_exact_active_tenant() -> None:
    control = active_control()
    states = InMemoryActiveMigrationStateStore()
    adapter = MigrationAdapter(fail_once_at="migrate_neo4j")
    migration = ActiveTenantMigrationService(control, states, adapter)
    output = io.StringIO()

    run_cli(
        ["migration", "plan", "--tenant-id", "tenant-a"],
        control,
        output,
        tenant_migrator=migration,
    )
    with pytest.raises(ValueError, match="immutable active Tenant ID"):
        run_cli(
            [
                "migration",
                "apply",
                "--tenant-id",
                "tenant-a",
                "--confirm",
                "wrong-tenant",
            ],
            control,
            output,
            tenant_migrator=migration,
        )
    with pytest.raises(ActiveMigrationFailed):
        run_cli(
            [
                "migration",
                "apply",
                "--tenant-id",
                "tenant-a",
                "--confirm",
                "tenant-a",
            ],
            control,
            output,
            tenant_migrator=migration,
        )
    run_cli(
        [
            "migration",
            "resume",
            "--tenant-id",
            "tenant-a",
            "--confirm",
            "tenant-a",
        ],
        control,
        output,
        tenant_migrator=migration,
    )

    documents = [json.loads(line) for line in output.getvalue().splitlines()]
    assert documents[0]["operation"] == "plan"
    assert documents[0]["current_status"] == "not_started"
    assert documents[1]["operation"] == "resume"
    assert documents[1]["result"]["status"] == "completed"
    assert documents[1]["result"]["attempt"] == 2

"""Durable, resumable schema migration for already-active Tenants."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol

from agent_memory_service.control import TenantRouteRecord
from agent_memory_service.schema import TENANT_SCHEMA_REVISION

POSTGRES_TARGET_VERSION = TENANT_SCHEMA_REVISION
NEO4J_TARGET_VERSION = "1"
ACTIVE_MIGRATION_ID = (
    f"tenant-schema:{POSTGRES_TARGET_VERSION}:neo4j-{NEO4J_TARGET_VERSION}:recovery-gated-v1"
)


class ActiveMigrationConflict(RuntimeError):
    """The requested active-Tenant migration transition is not safe."""


class ActiveMigrationFailed(RuntimeError):
    """A fixed migration or verification step failed and can be resumed."""

    def __init__(self, step: ActiveMigrationStep) -> None:
        self.step = step
        super().__init__(f"Active Tenant migration failed at: {step.value}")


class ActiveMigrationStatus(StrEnum):
    NOT_STARTED = "not_started"
    APPLYING = "applying"
    FAILED = "failed"
    COMPLETED = "completed"


class ActiveMigrationStep(StrEnum):
    VERIFY_RECOVERY_POINT = "verify_recovery_point"
    MIGRATE_POSTGRES = "migrate_postgres"
    MIGRATE_NEO4J = "migrate_neo4j"
    VERIFY_NEO4J_HEALTH = "verify_neo4j_health"
    VERIFY_ROUTING = "verify_routing"
    VERIFY_ISOLATION = "verify_isolation"


ACTIVE_MIGRATION_STEPS = tuple(ActiveMigrationStep)
_STEP_ACTIONS = {
    ActiveMigrationStep.VERIFY_RECOVERY_POINT: (
        "verify a complete, checksummed Tenant recovery point within the 24-hour RPO"
    ),
    ActiveMigrationStep.MIGRATE_POSTGRES: "apply the repository-pinned Tenant Alembic head",
    ActiveMigrationStep.MIGRATE_NEO4J: "apply the registered idempotent Neo4j schema script",
    ActiveMigrationStep.VERIFY_NEO4J_HEALTH: "execute the registered Neo4j health check",
    ActiveMigrationStep.VERIFY_ROUTING: "verify the active exact Tenant route",
    ActiveMigrationStep.VERIFY_ISOLATION: "verify the Tenant database role remains isolated",
}


@dataclass(frozen=True, slots=True)
class ActiveMigrationState:
    tenant_id: str
    migration_id: str
    postgres_target_version: str
    neo4j_target_version: str
    status: ActiveMigrationStatus
    completed_steps: tuple[ActiveMigrationStep, ...] = ()
    failed_step: ActiveMigrationStep | None = None
    failure_code: str | None = None
    attempt: int = 0
    revision: int = 0


@dataclass(frozen=True, slots=True)
class ActiveMigrationPlannedStep:
    step: ActiveMigrationStep
    action: str
    completed: bool


@dataclass(frozen=True, slots=True)
class ActiveMigrationPlan:
    tenant_id: str
    migration_id: str
    postgres_target_version: str
    neo4j_target_version: str
    current_status: ActiveMigrationStatus
    steps: tuple[ActiveMigrationPlannedStep, ...]

    @property
    def pending_steps(self) -> tuple[ActiveMigrationStep, ...]:
        return tuple(item.step for item in self.steps if not item.completed)

    @property
    def is_noop(self) -> bool:
        return not self.pending_steps


class ActiveMigrationStateStore(Protocol):
    def get(self, tenant_id: str) -> ActiveMigrationState | None: ...

    def start(self, tenant_id: str) -> ActiveMigrationState: ...

    def resume(self, tenant_id: str) -> ActiveMigrationState: ...

    def mark_completed(self, tenant_id: str, step: ActiveMigrationStep) -> ActiveMigrationState: ...

    def mark_failed(
        self, tenant_id: str, step: ActiveMigrationStep, failure_code: str
    ) -> ActiveMigrationState: ...

    def complete(self, tenant_id: str) -> ActiveMigrationState: ...


class ActiveTenantMigrationAdapter(Protocol):
    def verify_recovery_point(self, route: TenantRouteRecord) -> None: ...

    def migrate_postgres(self, route: TenantRouteRecord) -> None: ...

    def migrate_neo4j(self, route: TenantRouteRecord) -> None: ...

    def verify_neo4j_health(self, route: TenantRouteRecord) -> None: ...

    def verify_routing(self, route: TenantRouteRecord) -> None: ...

    def verify_isolation(self, route: TenantRouteRecord) -> None: ...


class ActiveTenantRouteProvider(Protocol):
    def resolve_tenant_route(self, tenant_id: str) -> TenantRouteRecord: ...


class InMemoryActiveMigrationStateStore:
    def __init__(self) -> None:
        self._states: dict[str, ActiveMigrationState] = {}

    def get(self, tenant_id: str) -> ActiveMigrationState | None:
        return self._states.get(tenant_id)

    def start(self, tenant_id: str) -> ActiveMigrationState:
        if tenant_id in self._states:
            raise ActiveMigrationConflict("Active Tenant migration already exists")
        state = ActiveMigrationState(
            tenant_id=tenant_id,
            migration_id=ACTIVE_MIGRATION_ID,
            postgres_target_version=POSTGRES_TARGET_VERSION,
            neo4j_target_version=NEO4J_TARGET_VERSION,
            status=ActiveMigrationStatus.APPLYING,
            attempt=1,
            revision=1,
        )
        self._states[tenant_id] = state
        return state

    def resume(self, tenant_id: str) -> ActiveMigrationState:
        current = self._require(tenant_id)
        if current.status not in {
            ActiveMigrationStatus.FAILED,
            ActiveMigrationStatus.APPLYING,
        }:
            raise ActiveMigrationConflict("Only an interrupted/failed migration can resume")
        state = replace(
            current,
            status=ActiveMigrationStatus.APPLYING,
            failed_step=None,
            failure_code=None,
            attempt=current.attempt + 1,
            revision=current.revision + 1,
        )
        self._states[tenant_id] = state
        return state

    def mark_completed(self, tenant_id: str, step: ActiveMigrationStep) -> ActiveMigrationState:
        current = self._require_applying(tenant_id)
        expected = _next_step(current.completed_steps)
        if step is not expected:
            raise ActiveMigrationConflict("Active migration steps must complete in order")
        state = replace(
            current,
            completed_steps=(*current.completed_steps, step),
            revision=current.revision + 1,
        )
        self._states[tenant_id] = state
        return state

    def mark_failed(
        self, tenant_id: str, step: ActiveMigrationStep, failure_code: str
    ) -> ActiveMigrationState:
        current = self._require_applying(tenant_id)
        if step is not _next_step(current.completed_steps):
            raise ActiveMigrationConflict("Only the current migration step can fail")
        state = replace(
            current,
            status=ActiveMigrationStatus.FAILED,
            failed_step=step,
            failure_code=failure_code[:128],
            revision=current.revision + 1,
        )
        self._states[tenant_id] = state
        return state

    def complete(self, tenant_id: str) -> ActiveMigrationState:
        current = self._require_applying(tenant_id)
        if current.completed_steps != ACTIVE_MIGRATION_STEPS:
            raise ActiveMigrationConflict("Every migration verification must complete")
        state = replace(
            current,
            status=ActiveMigrationStatus.COMPLETED,
            revision=current.revision + 1,
        )
        self._states[tenant_id] = state
        return state

    def _require(self, tenant_id: str) -> ActiveMigrationState:
        state = self._states.get(tenant_id)
        if state is None:
            raise ActiveMigrationConflict("Active Tenant migration has not started")
        return state

    def _require_applying(self, tenant_id: str) -> ActiveMigrationState:
        state = self._require(tenant_id)
        if state.status is not ActiveMigrationStatus.APPLYING:
            raise ActiveMigrationConflict("Active Tenant migration is not applying")
        _validate_prefix(state.completed_steps)
        return state


class ActiveTenantMigrationService:
    """Plan and execute only the repository-fixed migration against an active Tenant."""

    def __init__(
        self,
        routes: ActiveTenantRouteProvider,
        states: ActiveMigrationStateStore,
        adapter: ActiveTenantMigrationAdapter,
    ) -> None:
        self._routes = routes
        self._states = states
        self._adapter = adapter

    def plan(self, tenant_id: str) -> ActiveMigrationPlan:
        self._routes.resolve_tenant_route(tenant_id)
        state = self._states.get(tenant_id)
        completed = () if state is None else state.completed_steps
        return ActiveMigrationPlan(
            tenant_id=tenant_id,
            migration_id=ACTIVE_MIGRATION_ID,
            postgres_target_version=POSTGRES_TARGET_VERSION,
            neo4j_target_version=NEO4J_TARGET_VERSION,
            current_status=(ActiveMigrationStatus.NOT_STARTED if state is None else state.status),
            steps=tuple(
                ActiveMigrationPlannedStep(
                    step=step,
                    action=_STEP_ACTIONS[step],
                    completed=step in completed,
                )
                for step in ACTIVE_MIGRATION_STEPS
            ),
        )

    def apply(self, tenant_id: str) -> ActiveMigrationState:
        self._routes.resolve_tenant_route(tenant_id)
        current = self._states.get(tenant_id)
        if current is not None:
            if current.status is ActiveMigrationStatus.COMPLETED:
                return current
            raise ActiveMigrationConflict("Migration exists; use resume after inspection")
        return self._run(tenant_id, self._states.start(tenant_id))

    def resume(self, tenant_id: str) -> ActiveMigrationState:
        self._routes.resolve_tenant_route(tenant_id)
        return self._run(tenant_id, self._states.resume(tenant_id))

    def _run(self, tenant_id: str, state: ActiveMigrationState) -> ActiveMigrationState:
        route = self._routes.resolve_tenant_route(tenant_id)
        actions = {
            ActiveMigrationStep.VERIFY_RECOVERY_POINT: self._adapter.verify_recovery_point,
            ActiveMigrationStep.MIGRATE_POSTGRES: self._adapter.migrate_postgres,
            ActiveMigrationStep.MIGRATE_NEO4J: self._adapter.migrate_neo4j,
            ActiveMigrationStep.VERIFY_NEO4J_HEALTH: self._adapter.verify_neo4j_health,
            ActiveMigrationStep.VERIFY_ROUTING: self._adapter.verify_routing,
            ActiveMigrationStep.VERIFY_ISOLATION: self._adapter.verify_isolation,
        }
        for step in ACTIVE_MIGRATION_STEPS:
            if step in state.completed_steps:
                continue
            try:
                actions[step](route)
            except Exception as error:
                self._states.mark_failed(tenant_id, step, type(error).__name__)
                raise ActiveMigrationFailed(step) from error
            state = self._states.mark_completed(tenant_id, step)
        return self._states.complete(tenant_id)


def _next_step(completed: tuple[ActiveMigrationStep, ...]) -> ActiveMigrationStep | None:
    _validate_prefix(completed)
    return (
        None
        if len(completed) == len(ACTIVE_MIGRATION_STEPS)
        else ACTIVE_MIGRATION_STEPS[len(completed)]
    )


def _validate_prefix(completed: tuple[ActiveMigrationStep, ...]) -> None:
    if tuple(ACTIVE_MIGRATION_STEPS[: len(completed)]) != completed:
        raise ActiveMigrationConflict("Completed migration steps are not a valid prefix")

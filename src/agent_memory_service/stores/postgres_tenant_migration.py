"""PostgreSQL state adapter for controlled active-Tenant migrations."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from agent_memory_service.database_url import (
    psycopg_database_url as _psycopg_database_url,
)
from agent_memory_service.tenant_migration import (
    ACTIVE_MIGRATION_ID,
    ACTIVE_MIGRATION_STEPS,
    NEO4J_TARGET_VERSION,
    POSTGRES_TARGET_VERSION,
    ActiveMigrationConflict,
    ActiveMigrationState,
    ActiveMigrationStatus,
    ActiveMigrationStep,
)

_STATE_SELECT = """
    SELECT tenant_id, migration_id, postgres_target_version, neo4j_target_version,
           state, completed_steps, failed_step, failure_code, attempt, revision
    FROM control.active_tenant_migrations
"""


class PostgresActiveMigrationStateStore:
    def __init__(self, database_url: str) -> None:
        if not database_url.strip():
            raise ValueError("Control Store database URL cannot be empty")
        self._database_url = _psycopg_database_url(database_url)

    def get(self, tenant_id: str) -> ActiveMigrationState | None:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"{_STATE_SELECT} WHERE tenant_id = %s AND migration_id = %s",
                    (tenant_id, ACTIVE_MIGRATION_ID),
                )
                row = cursor.fetchone()
        return None if row is None else _decode_state(row)

    def start(self, tenant_id: str) -> ActiveMigrationState:
        try:
            with psycopg.connect(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO control.active_tenant_migrations (
                            tenant_id, migration_id, postgres_target_version,
                            neo4j_target_version, state, attempt, revision
                        )
                        SELECT t.tenant_id, %s, %s, %s, 'applying', 1, 1
                        FROM control.tenants AS t
                        JOIN control.routing AS r ON r.tenant_id = t.tenant_id
                        WHERE t.tenant_id = %s AND t.active AND r.healthy
                        RETURNING tenant_id
                        """,
                        (
                            ACTIVE_MIGRATION_ID,
                            POSTGRES_TARGET_VERSION,
                            NEO4J_TARGET_VERSION,
                            tenant_id,
                        ),
                    )
                    if cursor.fetchone() is None:
                        raise ActiveMigrationConflict("Active healthy Tenant route not found")
                    return _required_state(cursor, tenant_id)
        except psycopg.IntegrityError as error:
            raise ActiveMigrationConflict("Active Tenant migration already exists") from error

    def resume(self, tenant_id: str) -> ActiveMigrationState:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                current = _locked_state(cursor, tenant_id)
                if current is None or current.status not in {
                    ActiveMigrationStatus.FAILED,
                    ActiveMigrationStatus.APPLYING,
                }:
                    raise ActiveMigrationConflict("Only an interrupted/failed migration can resume")
                cursor.execute(
                    """
                    UPDATE control.active_tenant_migrations
                    SET state = 'applying', failed_step = NULL, failure_code = NULL,
                        attempt = attempt + 1, revision = revision + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND migration_id = %s AND revision = %s
                    """,
                    (tenant_id, ACTIVE_MIGRATION_ID, current.revision),
                )
                return _required_state(cursor, tenant_id)

    def mark_completed(self, tenant_id: str, step: ActiveMigrationStep) -> ActiveMigrationState:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                current = _require_applying(cursor, tenant_id)
                expected = _next_step(current.completed_steps)
                if step is not expected:
                    raise ActiveMigrationConflict("Migration steps must complete in order")
                completed = (*current.completed_steps, step)
                cursor.execute(
                    """
                    UPDATE control.active_tenant_migrations
                    SET completed_steps = %s, revision = revision + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND migration_id = %s AND revision = %s
                    """,
                    (
                        Jsonb([item.value for item in completed]),
                        tenant_id,
                        ACTIVE_MIGRATION_ID,
                        current.revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ActiveMigrationConflict("Migration state changed concurrently")
                return _required_state(cursor, tenant_id)

    def mark_failed(
        self, tenant_id: str, step: ActiveMigrationStep, failure_code: str
    ) -> ActiveMigrationState:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                current = _require_applying(cursor, tenant_id)
                if step is not _next_step(current.completed_steps):
                    raise ActiveMigrationConflict("Only the current migration step can fail")
                cursor.execute(
                    """
                    UPDATE control.active_tenant_migrations
                    SET state = 'failed', failed_step = %s, failure_code = %s,
                        revision = revision + 1, updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND migration_id = %s AND revision = %s
                    """,
                    (
                        step.value,
                        failure_code[:128],
                        tenant_id,
                        ACTIVE_MIGRATION_ID,
                        current.revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ActiveMigrationConflict("Migration state changed concurrently")
                return _required_state(cursor, tenant_id)

    def complete(self, tenant_id: str) -> ActiveMigrationState:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                current = _require_applying(cursor, tenant_id)
                if current.completed_steps != ACTIVE_MIGRATION_STEPS:
                    raise ActiveMigrationConflict("Every migration verification must complete")
                cursor.execute(
                    """
                    UPDATE control.active_tenant_migrations
                    SET state = 'completed', completed_at = CURRENT_TIMESTAMP,
                        revision = revision + 1, updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND migration_id = %s AND revision = %s
                    """,
                    (tenant_id, ACTIVE_MIGRATION_ID, current.revision),
                )
                if cursor.rowcount != 1:
                    raise ActiveMigrationConflict("Migration state changed concurrently")
                return _required_state(cursor, tenant_id)


def _locked_state(
    cursor: psycopg.Cursor[tuple[Any, ...]], tenant_id: str
) -> ActiveMigrationState | None:
    cursor.execute(
        f"""
        {_STATE_SELECT}
        WHERE tenant_id = %s AND migration_id = %s
        FOR UPDATE
        """,
        (tenant_id, ACTIVE_MIGRATION_ID),
    )
    row = cursor.fetchone()
    return None if row is None else _decode_state(row)


def _required_state(
    cursor: psycopg.Cursor[tuple[Any, ...]], tenant_id: str
) -> ActiveMigrationState:
    cursor.execute(
        f"{_STATE_SELECT} WHERE tenant_id = %s AND migration_id = %s",
        (tenant_id, ACTIVE_MIGRATION_ID),
    )
    row = cursor.fetchone()
    if row is None:
        raise ActiveMigrationConflict("Active Tenant migration does not exist")
    return _decode_state(row)


def _require_applying(
    cursor: psycopg.Cursor[tuple[Any, ...]], tenant_id: str
) -> ActiveMigrationState:
    current = _locked_state(cursor, tenant_id)
    if current is None or current.status is not ActiveMigrationStatus.APPLYING:
        raise ActiveMigrationConflict("Active Tenant migration is not applying")
    return current


def _decode_state(row: tuple[Any, ...]) -> ActiveMigrationState:
    completed = _steps(row[5])
    return ActiveMigrationState(
        tenant_id=str(row[0]),
        migration_id=str(row[1]),
        postgres_target_version=str(row[2]),
        neo4j_target_version=str(row[3]),
        status=ActiveMigrationStatus(str(row[4])),
        completed_steps=completed,
        failed_step=None if row[6] is None else ActiveMigrationStep(str(row[6])),
        failure_code=None if row[7] is None else str(row[7]),
        attempt=int(row[8]),
        revision=int(row[9]),
    )


def _steps(value: object) -> tuple[ActiveMigrationStep, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("Control Store contains invalid migration steps")
    steps = tuple(ActiveMigrationStep(item) for item in value)
    if steps != ACTIVE_MIGRATION_STEPS[: len(steps)]:
        raise ValueError("Control Store migration steps are not a valid prefix")
    return steps


def _next_step(completed: tuple[ActiveMigrationStep, ...]) -> ActiveMigrationStep | None:
    return (
        None
        if len(completed) == len(ACTIVE_MIGRATION_STEPS)
        else ACTIVE_MIGRATION_STEPS[len(completed)]
    )

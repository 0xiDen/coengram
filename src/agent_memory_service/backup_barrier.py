"""Tenant-scoped cross-store consistency barrier for host backups."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import cast
from urllib.parse import quote
from uuid import uuid4

import psycopg
from activegraph.store.postgres import PostgresEventStore  # type: ignore[import-untyped]

from agent_memory_service.agents.persistence import AgentRunSnapshot
from agent_memory_service.backup import (
    BackupBarrierRecord,
    BackupPlan,
    BackupValidationError,
    projected_agent_run_events,
)
from agent_memory_service.database_barrier import (
    ACQUIRE_BACKUP_EXCLUSIVE_LOCK_SQL,
    RELEASE_BACKUP_EXCLUSIVE_LOCK_SQL,
    TRY_ACQUIRE_BACKUP_EXCLUSIVE_LOCK_SQL,
)
from agent_memory_service.database_url import psycopg_database_url
from agent_memory_service.tenant_credentials import read_tenant_credential

_SAFE_TENANT_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,38}[a-z0-9])?$")


class PostgresBackupConsistencyBarrier:
    """Suspend routing and fence every Tenant mutation across a backup capture."""

    def __init__(
        self,
        *,
        control_database_url: str,
        tenant_secrets_directory: Path,
        postgres_host: str,
        postgres_port: int,
    ) -> None:
        if not control_database_url.strip():
            raise ValueError("Control Store database URL cannot be empty")
        if not postgres_host.strip() or postgres_port < 1:
            raise ValueError("Tenant PostgreSQL endpoint is invalid")
        secrets = tenant_secrets_directory.resolve()
        if tenant_secrets_directory.is_symlink() or not secrets.is_dir():
            raise ValueError("Tenant secrets directory must be an existing directory")
        self._control_database_url = psycopg_database_url(control_database_url)
        self._tenant_secrets_directory = secrets
        self._postgres_host = postgres_host
        self._postgres_port = postgres_port
        self._tenant_connection: psycopg.Connection[tuple[object, ...]] | None = None
        self._tenant_id: str | None = None
        self._barrier_id: str | None = None

    @property
    def barrier_id(self) -> str | None:
        return self._barrier_id

    def enter(self, plan: BackupPlan) -> None:
        """Make a Tenant unroutable, then wait for every in-flight mutation."""

        if self._tenant_connection is not None or self._barrier_id is not None:
            raise BackupValidationError("Backup consistency barrier is already held")
        barrier_id = str(uuid4())
        self._suspend_tenant(plan, barrier_id)
        connection: psycopg.Connection[tuple[object, ...]] | None = None
        try:
            connection = psycopg.connect(self._tenant_database_url(plan), autocommit=True)
            connection.execute(ACQUIRE_BACKUP_EXCLUSIVE_LOCK_SQL)
        except Exception:
            if connection is not None:
                connection.close()
            self._resume_tenant(plan.tenant_id, barrier_id)
            raise
        self._tenant_connection = connection
        self._tenant_id = plan.tenant_id
        self._barrier_id = barrier_id

    def verify_quiescent(self, plan: BackupPlan) -> None:
        """Reject a cut with unfinished projections, outbox work, or Agent leases."""

        connection = self._required_connection(plan)
        checks = (
            (
                "SELECT count(*) FROM memory.private_memory_commands WHERE tenant_id = %s "
                "AND state = 'accepted'",
                "Private Memory projections are pending",
            ),
            (
                "SELECT count(*) FROM memory.knowledge_candidates WHERE tenant_id = %s "
                "AND status = 'publishing'",
                "Tenant Knowledge publication is pending",
            ),
            (
                "SELECT count(*) FROM memory.outbox WHERE tenant_id = %s AND processed_at IS NULL",
                "Tenant outbox projections are pending",
            ),
            (
                "SELECT count(*) FROM memory.agent_runs WHERE tenant_id = %s "
                "AND lease_token IS NOT NULL AND lease_expires_at > CURRENT_TIMESTAMP",
                "Agent Run worker leases are active",
            ),
        )
        for statement, error in checks:
            row = connection.execute(statement, (plan.tenant_id,)).fetchone()
            if row is None or int(str(row[0])) != 0:
                raise BackupValidationError(error)
        self._verify_activegraph_projection(connection, plan)

    def exit(self, plan: BackupPlan) -> None:
        """Release the mutation fence, then make the exact Tenant routable again."""

        connection = self._required_connection(plan)
        barrier_id = self._required_barrier_id(plan)
        try:
            row = connection.execute(RELEASE_BACKUP_EXCLUSIVE_LOCK_SQL).fetchone()
            if row is None or not bool(row[0]):
                raise BackupValidationError("Backup consistency lock was not held")
        finally:
            connection.close()
            self._tenant_connection = None
            self._tenant_id = None
        try:
            self._resume_tenant(plan.tenant_id, barrier_id)
        finally:
            self._barrier_id = None

    def recover(self, tenant_id: str, barrier_id: str) -> None:
        """Recover one exact abandoned barrier only when no process holds its lock."""

        if self._tenant_connection is not None or self._barrier_id is not None:
            raise BackupValidationError("Cannot recover while this process holds a barrier")
        if _SAFE_TENANT_ID.fullmatch(tenant_id) is None or not barrier_id.strip():
            raise BackupValidationError("Backup barrier recovery identity is invalid")
        plan_values = self._barrier_route(tenant_id, barrier_id)
        database_name, database_role = plan_values
        password = self._read_tenant_password(tenant_id)
        database_url = self._database_url(database_name, database_role, password)
        with psycopg.connect(database_url, autocommit=True) as connection:
            acquired = connection.execute(TRY_ACQUIRE_BACKUP_EXCLUSIVE_LOCK_SQL).fetchone()
            if acquired is None or not bool(acquired[0]):
                raise BackupValidationError("Backup consistency barrier is still held")
            connection.execute(RELEASE_BACKUP_EXCLUSIVE_LOCK_SQL)
        self._resume_tenant(tenant_id, barrier_id)

    def inspect(self, tenant_id: str) -> BackupBarrierRecord | None:
        """Return the content-free identity needed for exact crash recovery."""

        if _SAFE_TENANT_ID.fullmatch(tenant_id) is None:
            raise BackupValidationError("Backup barrier Tenant identity is invalid")
        with psycopg.connect(self._control_database_url) as connection:
            row = connection.execute(
                """
                SELECT barrier.tenant_id, barrier.barrier_id, barrier.started_at
                FROM control.backup_barriers AS barrier
                JOIN control.tenants AS tenant USING (tenant_id)
                WHERE barrier.tenant_id = %s AND tenant.active = false
                """,
                (tenant_id,),
            ).fetchone()
        if row is None:
            return None
        return BackupBarrierRecord(
            tenant_id=str(row[0]),
            barrier_id=str(row[1]),
            started_at=cast(datetime, row[2]),
        )

    def _suspend_tenant(self, plan: BackupPlan, barrier_id: str) -> None:
        with psycopg.connect(self._control_database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT tenant.active, route.tenant_database_name,
                           route.tenant_database_role, route.healthy
                    FROM control.tenants AS tenant
                    JOIN control.routing AS route USING (tenant_id)
                    WHERE tenant.tenant_id = %s
                    FOR UPDATE OF tenant, route
                    """,
                    (plan.tenant_id,),
                )
                row = cursor.fetchone()
                if row is None or not bool(row[0]) or not bool(row[3]):
                    raise BackupValidationError("Backup requires one active healthy Tenant route")
                if (str(row[1]), str(row[2])) != (plan.database_name, plan.database_role):
                    raise BackupValidationError("Tenant route changed before backup fencing")
                cursor.execute(
                    """
                    INSERT INTO control.backup_barriers (tenant_id, barrier_id)
                    VALUES (%s, %s)
                    """,
                    (plan.tenant_id, barrier_id),
                )
                cursor.execute(
                    """
                    UPDATE control.tenants
                    SET active = false, updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s
                    """,
                    (plan.tenant_id,),
                )

    def _resume_tenant(self, tenant_id: str, barrier_id: str) -> None:
        with psycopg.connect(self._control_database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM control.backup_barriers
                    WHERE tenant_id = %s AND barrier_id = %s
                    RETURNING tenant_id
                    """,
                    (tenant_id, barrier_id),
                )
                if cursor.fetchone() is None:
                    raise BackupValidationError("Exact backup consistency barrier was not found")
                cursor.execute(
                    """
                    UPDATE control.tenants
                    SET active = true, updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND active = false
                    RETURNING tenant_id
                    """,
                    (tenant_id,),
                )
                if cursor.fetchone() is None:
                    raise BackupValidationError("Suspended backup Tenant could not be resumed")

    def _barrier_route(self, tenant_id: str, barrier_id: str) -> tuple[str, str]:
        with psycopg.connect(self._control_database_url) as connection:
            row = connection.execute(
                """
                SELECT route.tenant_database_name, route.tenant_database_role
                FROM control.backup_barriers AS barrier
                JOIN control.routing AS route USING (tenant_id)
                JOIN control.tenants AS tenant USING (tenant_id)
                WHERE barrier.tenant_id = %s AND barrier.barrier_id = %s
                  AND tenant.active = false AND route.healthy
                """,
                (tenant_id, barrier_id),
            ).fetchone()
        if row is None:
            raise BackupValidationError("Recoverable backup consistency barrier was not found")
        return str(row[0]), str(row[1])

    def _verify_activegraph_projection(
        self,
        connection: psycopg.Connection[tuple[object, ...]],
        plan: BackupPlan,
    ) -> None:
        rows = connection.execute(
            """
            SELECT run_id, snapshot
            FROM memory.agent_runs
            WHERE tenant_id = %s
            ORDER BY run_id
            """,
            (plan.tenant_id,),
        ).fetchall()
        expected_run_ids = {str(row[0]) for row in rows}
        try:
            native_rows = connection.execute(
                """
                SELECT DISTINCT run_id
                FROM events
                WHERE payload ? 'agent_memory_run_event'
                """
            ).fetchall()
        except psycopg.errors.UndefinedTable as exc:
            if rows:
                raise BackupValidationError(
                    "ActiveGraph projection tables are unavailable"
                ) from exc
            return
        if {str(row[0]) for row in native_rows} != expected_run_ids:
            raise BackupValidationError("ActiveGraph Agent Run set diverges from snapshots")
        for row in rows:
            snapshot = AgentRunSnapshot.model_validate(row[1])
            store = PostgresEventStore(connection, snapshot.run_id)
            native_events = tuple(store.iter_events())
            if (
                store.get_run() is None
                or projected_agent_run_events(native_events) != snapshot.events
            ):
                raise BackupValidationError(
                    "ActiveGraph Agent Run projection diverges from its snapshot"
                )

    def _required_connection(
        self,
        plan: BackupPlan,
    ) -> psycopg.Connection[tuple[object, ...]]:
        if self._tenant_connection is None or self._tenant_id != plan.tenant_id:
            raise BackupValidationError("Exact backup consistency barrier is not held")
        return self._tenant_connection

    def _required_barrier_id(self, plan: BackupPlan) -> str:
        if self._barrier_id is None or self._tenant_id != plan.tenant_id:
            raise BackupValidationError("Exact backup consistency barrier is not held")
        return self._barrier_id

    def _tenant_database_url(self, plan: BackupPlan) -> str:
        return self._database_url(
            plan.database_name,
            plan.database_role,
            self._read_tenant_password(plan.tenant_id),
        )

    def _database_url(self, database_name: str, database_role: str, password: str) -> str:
        return (
            f"postgresql://{quote(database_role, safe='')}:{quote(password, safe='')}@"
            f"{self._postgres_host}:{self._postgres_port}/{quote(database_name, safe='')}"
        )

    def _read_tenant_password(self, tenant_id: str) -> str:
        try:
            return read_tenant_credential(
                self._tenant_secrets_directory,
                tenant_id,
                "postgres_password",
            )
        except ValueError as exc:
            raise BackupValidationError("Tenant PostgreSQL password file is invalid") from exc

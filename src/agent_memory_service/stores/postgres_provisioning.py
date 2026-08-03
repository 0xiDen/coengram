"""PostgreSQL Adapter for resumable, fail-closed Tenant provisioning state."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from agent_memory_service.database_url import (
    psycopg_database_url as _psycopg_database_url,
)
from agent_memory_service.manifest import TenantManifest
from agent_memory_service.provisioning import (
    PROVISIONING_STEPS,
    ProvisioningConflict,
    ProvisioningState,
    ProvisioningStatus,
    ProvisioningStep,
)

_STATE_SELECT = """
    SELECT
        p.tenant_id,
        p.manifest_checksum,
        r.tenant_database_name,
        r.tenant_database_role,
        r.neo4j_service_address,
        p.state,
        p.completed_steps,
        p.current_step,
        p.last_error_code,
        p.attempt
    FROM control.provisioning AS p
    LEFT JOIN control.routing AS r ON r.tenant_id = p.tenant_id
"""


class PostgresProvisioningStateStore:
    """Persist every provisioning boundary in the content-free Control Store."""

    def __init__(self, database_url: str) -> None:
        if not database_url.strip():
            raise ValueError("Control Store database URL cannot be empty")
        self._database_url = _psycopg_database_url(database_url)

    def get(self, tenant_id: str) -> ProvisioningState | None:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"{_STATE_SELECT} WHERE p.tenant_id = %s",
                    (tenant_id,),
                )
                row = cursor.fetchone()
        return None if row is None else _decode_state(row)

    def start(self, manifest: TenantManifest) -> ProvisioningState:
        try:
            with psycopg.connect(self._database_url) as connection:
                with connection.cursor() as cursor:
                    _lock_tenant(cursor, manifest.tenant_id)
                    current = _get_locked_state(cursor, manifest.tenant_id)
                    if current is not None:
                        _require_matching_manifest(current, manifest.fingerprint)
                        _validate_completed_prefix(current)
                        if current.status is ProvisioningStatus.ACTIVE:
                            return current
                        cursor.execute(
                            """
                            UPDATE control.provisioning
                            SET state = 'provisioning',
                                current_step = %s,
                                last_error_code = NULL,
                                attempt = attempt + 1,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE tenant_id = %s
                            """,
                            (_next_step_value(current.completed_steps), manifest.tenant_id),
                        )
                        return _required_state(cursor, manifest.tenant_id)

                    _record_manifest_control_state(cursor, manifest)
                    return _required_state(cursor, manifest.tenant_id)
        except psycopg.IntegrityError as exc:
            raise ProvisioningConflict(
                "Tenant Manifest conflicts with existing Control Store state"
            ) from exc

    def mark_completed(
        self,
        tenant_id: str,
        step: ProvisioningStep,
    ) -> ProvisioningState:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                _lock_tenant(cursor, tenant_id)
                current = _require_provisioning(cursor, tenant_id)
                expected = _next_step(current.completed_steps)
                if step is ProvisioningStep.ACTIVATE:
                    raise ProvisioningConflict(
                        "Activation requires the final activation transition"
                    )
                if step is not expected:
                    raise ProvisioningConflict("Provisioning steps must complete in order")
                completed = (*current.completed_steps, step)
                cursor.execute(
                    """
                    UPDATE control.provisioning
                    SET completed_steps = %s,
                        current_step = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s
                    """,
                    (
                        Jsonb([item.value for item in completed]),
                        _next_step_value(completed),
                        tenant_id,
                    ),
                )
                return _required_state(cursor, tenant_id)

    def mark_failed(
        self,
        tenant_id: str,
        step: ProvisioningStep,
        failure_code: str,
    ) -> ProvisioningState:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                _lock_tenant(cursor, tenant_id)
                current = _require_provisioning(cursor, tenant_id)
                if step is not _next_step(current.completed_steps):
                    raise ProvisioningConflict("Only the current provisioning step can fail")
                cursor.execute(
                    """
                    UPDATE control.provisioning
                    SET state = 'failed',
                        current_step = %s,
                        last_error_code = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s
                    """,
                    (step.value, failure_code[:128], tenant_id),
                )
                _write_audit(cursor, tenant_id, "tenant.provisioning_failed", "failed")
                return _required_state(cursor, tenant_id)

    def activate(self, tenant_id: str) -> ProvisioningState:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                _lock_tenant(cursor, tenant_id)
                current = _require_provisioning(cursor, tenant_id)
                if _next_step(current.completed_steps) is not ProvisioningStep.ACTIVATE:
                    raise ProvisioningConflict(
                        "Tenant cannot activate before every verification passes"
                    )
                completed = (*current.completed_steps, ProvisioningStep.ACTIVATE)
                cursor.execute(
                    """
                    UPDATE control.tenants
                    SET active = true, updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND active = false
                    """,
                    (tenant_id,),
                )
                if cursor.rowcount != 1:
                    raise ProvisioningConflict("Inactive Tenant not found for activation")
                cursor.execute(
                    """
                    UPDATE control.memberships
                    SET active = true, updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s
                    """,
                    (tenant_id,),
                )
                cursor.execute(
                    """
                    UPDATE control.routing
                    SET healthy = true,
                        checked_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND healthy = false
                    """,
                    (tenant_id,),
                )
                if cursor.rowcount != 1:
                    raise ProvisioningConflict("Fail-closed Tenant route not found")
                cursor.execute(
                    """
                    UPDATE control.provisioning
                    SET state = 'active',
                        completed_steps = %s,
                        current_step = NULL,
                        last_error_code = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s
                    """,
                    (Jsonb([item.value for item in completed]), tenant_id),
                )
                _write_audit(cursor, tenant_id, "tenant.activated", "active")
                return _required_state(cursor, tenant_id)


def _record_manifest_control_state(
    cursor: psycopg.Cursor[tuple[Any, ...]],
    manifest: TenantManifest,
) -> None:
    cursor.execute(
        """
        INSERT INTO control.tenants (tenant_id, name, active)
        VALUES (%s, %s, false)
        """,
        (manifest.tenant_id, manifest.name),
    )
    for principal in manifest.principals:
        cursor.execute(
            """
            INSERT INTO control.principals (principal_id, name, kind, active)
            VALUES (%s, %s, %s, true)
            ON CONFLICT (principal_id) DO NOTHING
            """,
            (principal.principal_id, principal.name, principal.kind.value),
        )
        cursor.execute(
            """
            SELECT name, kind, active
            FROM control.principals
            WHERE principal_id = %s
            """,
            (principal.principal_id,),
        )
        row = cursor.fetchone()
        if row != (principal.name, principal.kind.value, True):
            raise ProvisioningConflict("Declared Principal conflicts with existing global identity")
    membership_by_principal = {
        membership.principal_id: membership for membership in manifest.memberships
    }
    for principal_id, membership in membership_by_principal.items():
        cursor.execute(
            """
            INSERT INTO control.memberships (tenant_id, principal_id, roles, active)
            VALUES (%s, %s, %s, false)
            """,
            (manifest.tenant_id, principal_id, Jsonb(list(membership.roles))),
        )
    cursor.execute(
        """
        INSERT INTO control.routing (
            tenant_id,
            neo4j_service_address,
            neo4j_secret_name,
            tenant_database_name,
            tenant_database_role,
            healthy
        )
        VALUES (%s, %s, %s, %s, %s, false)
        """,
        (
            manifest.tenant_id,
            f"{manifest.neo4j_service_name}:7687",
            f"{manifest.tenant_id}/neo4j_password",
            manifest.database_name,
            manifest.database_role,
        ),
    )
    cursor.execute(
        """
        INSERT INTO control.provisioning (
            tenant_id,
            manifest_version,
            manifest_checksum,
            desired_state,
            state,
            current_step,
            completed_steps,
            policies,
            attempt
        )
        VALUES (%s, %s, %s, 'active', 'provisioning', %s, %s, %s, 1)
        """,
        (
            manifest.tenant_id,
            manifest.version,
            manifest.fingerprint,
            ProvisioningStep.WRITE_SECRETS.value,
            Jsonb([ProvisioningStep.RECORD_CONTROL_STATE.value]),
            Jsonb(manifest.policies.model_dump(mode="json")),
        ),
    )
    _write_audit(cursor, manifest.tenant_id, "tenant.provisioning_started", "provisioning")


def _get_locked_state(
    cursor: psycopg.Cursor[tuple[Any, ...]],
    tenant_id: str,
) -> ProvisioningState | None:
    cursor.execute(
        f"{_STATE_SELECT} WHERE p.tenant_id = %s FOR UPDATE OF p",
        (tenant_id,),
    )
    row = cursor.fetchone()
    return None if row is None else _decode_state(row)


def _required_state(
    cursor: psycopg.Cursor[tuple[Any, ...]],
    tenant_id: str,
) -> ProvisioningState:
    cursor.execute(f"{_STATE_SELECT} WHERE p.tenant_id = %s", (tenant_id,))
    row = cursor.fetchone()
    if row is None:
        raise ProvisioningConflict("Tenant provisioning state not found")
    return _decode_state(row)


def _require_provisioning(
    cursor: psycopg.Cursor[tuple[Any, ...]],
    tenant_id: str,
) -> ProvisioningState:
    state = _get_locked_state(cursor, tenant_id)
    if state is None:
        raise ProvisioningConflict("Tenant provisioning has not started")
    if state.status is not ProvisioningStatus.PROVISIONING:
        raise ProvisioningConflict("Tenant is not in the provisioning state")
    _validate_completed_prefix(state)
    return state


def _lock_tenant(cursor: psycopg.Cursor[tuple[Any, ...]], tenant_id: str) -> None:
    cursor.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"tenant-provisioning:{tenant_id}",),
    )


def _write_audit(
    cursor: psycopg.Cursor[tuple[Any, ...]],
    tenant_id: str,
    action: str,
    outcome: str,
) -> None:
    cursor.execute(
        """
        INSERT INTO control.operator_audit (
            audit_id,
            operator_id,
            action,
            target_type,
            target_id,
            tenant_id,
            outcome,
            metadata
        )
        VALUES (%s, 'memoryctl', %s, 'tenant', %s, %s, %s, %s)
        """,
        (str(uuid4()), action, tenant_id, tenant_id, outcome, Jsonb({})),
    )


def _decode_state(row: tuple[Any, ...]) -> ProvisioningState:
    completed_value = row[6]
    if not isinstance(completed_value, list) or not all(
        isinstance(item, str) for item in completed_value
    ):
        raise ProvisioningConflict("Provisioning completed steps are invalid")
    completed = tuple(ProvisioningStep(item) for item in completed_value)
    status = ProvisioningStatus(str(row[5]))
    failed_step = (
        ProvisioningStep(str(row[7]))
        if status is ProvisioningStatus.FAILED and row[7] is not None
        else None
    )
    service_address = str(row[4])
    service_suffix = ":7687"
    if not service_address.endswith(service_suffix):
        raise ProvisioningConflict("Provisioned Neo4j service address is invalid")
    state = ProvisioningState(
        tenant_id=str(row[0]),
        manifest_fingerprint=str(row[1]),
        database_name=str(row[2]),
        database_role=str(row[3]),
        neo4j_service_name=service_address[: -len(service_suffix)],
        status=status,
        completed_steps=completed,
        failed_step=failed_step,
        failure_code=None if row[8] is None else str(row[8]),
        attempt=int(row[9]),
    )
    _validate_completed_prefix(state)
    return state


def _next_step(completed_steps: tuple[ProvisioningStep, ...]) -> ProvisioningStep | None:
    if len(completed_steps) >= len(PROVISIONING_STEPS):
        return None
    return PROVISIONING_STEPS[len(completed_steps)]


def _next_step_value(completed_steps: tuple[ProvisioningStep, ...]) -> str | None:
    step = _next_step(completed_steps)
    return None if step is None else step.value


def _validate_completed_prefix(state: ProvisioningState) -> None:
    if state.completed_steps != PROVISIONING_STEPS[: len(state.completed_steps)]:
        raise ProvisioningConflict("Provisioning progress is not a valid completed prefix")
    active = ProvisioningStep.ACTIVATE in state.completed_steps
    if active != (state.status is ProvisioningStatus.ACTIVE):
        raise ProvisioningConflict("Provisioning activation state is inconsistent")


def _require_matching_manifest(state: ProvisioningState, fingerprint: str) -> None:
    if state.manifest_fingerprint != fingerprint:
        raise ProvisioningConflict("Cannot resume Tenant provisioning with a changed manifest")

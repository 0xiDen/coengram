"""Durable PostgreSQL adapters for Tenant decommission state and access."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from agent_memory_service.backup import RetentionPin
from agent_memory_service.database_url import (
    psycopg_database_url as _psycopg_database_url,
)
from agent_memory_service.decommission import (
    DEFAULT_POST_DESTRUCTION_RETENTION,
    DecommissionConflict,
    DecommissionRecord,
    DecommissionState,
    DecommissionTombstone,
    DestructionStep,
    ProtectionEvidence,
    ProtectionKind,
    SuspensionStep,
    TenantResourceIdentity,
)

_REQUEST_COLUMNS = """
    request_id,
    tenant_id,
    compose_project,
    neo4j_volume,
    database_name,
    database_role,
    route_id,
    tenant_secret_ref,
    requested_by,
    reason,
    requested_at,
    protection_policy,
    state,
    suspension_steps,
    confirmed_by,
    confirmed_at,
    grace_ends_at,
    protection_evidence,
    destruction_steps,
    cancelled_by,
    cancelled_at,
    last_failed_action,
    revision
"""


class PostgresDecommissionStore:
    """Persist each state-machine boundary with optimistic concurrency control."""

    def __init__(self, database_url: str) -> None:
        if not database_url.strip():
            raise ValueError("Control Store database URL cannot be empty")
        self._database_url = _psycopg_database_url(database_url)

    def get_record(self, request_id: str) -> DecommissionRecord | None:
        row = self._fetch_one(
            f"""
            SELECT {_REQUEST_COLUMNS}
            FROM control.decommission_requests
            WHERE request_id = %s
            """,
            (request_id,),
        )
        return None if row is None else _decode_record(row)

    def find_open_for_tenant(self, tenant_id: str) -> DecommissionRecord | None:
        row = self._fetch_one(
            f"""
            SELECT {_REQUEST_COLUMNS}
            FROM control.decommission_requests
            WHERE tenant_id = %s AND state <> 'cancelled'
            """,
            (tenant_id,),
        )
        return None if row is None else _decode_record(row)

    def save_record(self, record: DecommissionRecord) -> DecommissionRecord:
        try:
            with psycopg.connect(self._database_url) as connection:
                with connection.cursor() as cursor:
                    if record.revision == 0:
                        cursor.execute(
                            f"""
                            INSERT INTO control.decommission_requests (
                                {_REQUEST_COLUMNS}
                            )
                            VALUES (
                                %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s, %s, 1
                            )
                            RETURNING {_REQUEST_COLUMNS}
                            """,
                            _record_parameters(record),
                        )
                    else:
                        cursor.execute(
                            f"""
                            UPDATE control.decommission_requests
                            SET compose_project = %s,
                                neo4j_volume = %s,
                                database_name = %s,
                                database_role = %s,
                                route_id = %s,
                                tenant_secret_ref = %s,
                                requested_by = %s,
                                reason = %s,
                                requested_at = %s,
                                protection_policy = %s,
                                state = %s,
                                suspension_steps = %s,
                                confirmed_by = %s,
                                confirmed_at = %s,
                                grace_ends_at = %s,
                                protection_evidence = %s,
                                destruction_steps = %s,
                                cancelled_by = %s,
                                cancelled_at = %s,
                                last_failed_action = %s,
                                revision = revision + 1,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE request_id = %s
                              AND tenant_id = %s
                              AND revision = %s
                            RETURNING {_REQUEST_COLUMNS}
                            """,
                            (
                                *_update_parameters(record),
                                record.request_id,
                                record.resources.tenant_id,
                                record.revision,
                            ),
                        )
                    row = cursor.fetchone()
                    if row is None:
                        raise DecommissionConflict("decommission request changed concurrently")
                    return _decode_record(row)
        except psycopg.IntegrityError as error:
            raise DecommissionConflict(
                "decommission request conflicts with durable state"
            ) from error

    def get_tombstone(self, request_id: str) -> DecommissionTombstone | None:
        row = self._fetch_one(
            """
            SELECT request_id, tenant_id, requested_by, confirmed_by, destroyed_at, status
            FROM control.decommission_tombstones
            WHERE request_id = %s
            """,
            (request_id,),
        )
        if row is None:
            return None
        destroyed_at = _datetime(row[4], "destroyed_at")
        return DecommissionTombstone(
            request_id=str(row[0]),
            tenant_id=str(row[1]),
            requested_by=str(row[2]),
            confirmed_by=str(row[3]),
            destroyed_at=destroyed_at,
            status=str(row[5]),
        )

    def replace_with_tombstone(
        self, record: DecommissionRecord, tombstone: DecommissionTombstone
    ) -> None:
        try:
            with psycopg.connect(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        DELETE FROM control.decommission_requests
                        WHERE request_id = %s AND tenant_id = %s AND revision = %s
                        RETURNING request_id
                        """,
                        (record.request_id, record.resources.tenant_id, record.revision),
                    )
                    if cursor.fetchone() is None:
                        raise DecommissionConflict(
                            "decommission request changed before tombstone replacement"
                        )
                    cursor.execute(
                        """
                        INSERT INTO control.decommission_tombstones (
                            request_id,
                            tenant_id,
                            requested_by,
                            confirmed_by,
                            destroyed_at,
                            status
                        )
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            tombstone.request_id,
                            tombstone.tenant_id,
                            tombstone.requested_by,
                            tombstone.confirmed_by,
                            tombstone.destroyed_at,
                            tombstone.status,
                        ),
                    )
                    evidence = record.protection_evidence
                    if evidence is not None and evidence.kind is ProtectionKind.BACKUP:
                        cursor.execute(
                            """
                            INSERT INTO control.decommission_recovery_pins (
                                request_id, tenant_id, backup_id, protected_until
                            )
                            VALUES (%s, %s, %s, %s)
                            """,
                            (
                                tombstone.request_id,
                                tombstone.tenant_id,
                                evidence.artifact_id,
                                tombstone.destroyed_at + DEFAULT_POST_DESTRUCTION_RETENTION,
                            ),
                        )
        except psycopg.IntegrityError as error:
            raise DecommissionConflict(
                "decommission tombstone conflicts with durable state"
            ) from error

    def _fetch_one(self, statement: str, parameters: Sequence[object]) -> tuple[Any, ...] | None:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(statement, parameters)
                return cursor.fetchone()


class PostgresDecommissionRetentionAdapter:
    """Project open and recently destroyed decommissions into backup pins."""

    def __init__(self, database_url: str) -> None:
        if not database_url.strip():
            raise ValueError("Control Store database URL cannot be empty")
        self._database_url = _psycopg_database_url(database_url)

    def pins_for_tenant(self, tenant_id: str, *, now: datetime) -> tuple[RetentionPin, ...]:
        if now.tzinfo is None:
            raise ValueError("Retention evaluation time must be timezone-aware")
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT protection_evidence ->> 'artifact_id', state, grace_ends_at
                    FROM control.decommission_requests
                    WHERE tenant_id = %s
                      AND state IN ('suspended', 'grace-period', 'finalizing')
                      AND protection_evidence ->> 'kind' = 'backup'
                    """,
                    (tenant_id,),
                )
                open_rows = cursor.fetchall()
                cursor.execute(
                    """
                    SELECT backup_id, protected_until
                    FROM control.decommission_recovery_pins
                    WHERE tenant_id = %s
                    """,
                    (tenant_id,),
                )
                destroyed_rows = cursor.fetchall()
        return _decode_retention_pins(open_rows, destroyed_rows, now=now)


class PostgresTenantAccessAdapter:
    """Suspend one exact Tenant and revoke its credentials without deleting data."""

    def __init__(self, database_url: str) -> None:
        if not database_url.strip():
            raise ValueError("Control Store database URL cannot be empty")
        self._database_url = _psycopg_database_url(database_url)

    def suspend_sessions(self, resources: TenantResourceIdentity) -> None:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                _lock_and_verify_resources(cursor, resources)
                cursor.execute(
                    """
                    SELECT 1
                    FROM control.backup_barriers
                    WHERE tenant_id = %s
                    """,
                    (resources.tenant_id,),
                )
                if cursor.fetchone() is not None:
                    raise DecommissionConflict("tenant has an active backup barrier")
                cursor.execute(
                    """
                    UPDATE control.tenants
                    SET active = false, updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s
                    """,
                    (resources.tenant_id,),
                )
                cursor.execute(
                    """
                    UPDATE control.provisioning
                    SET desired_state = 'suspended',
                        state = 'suspended',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s
                    """,
                    (resources.tenant_id,),
                )
                _write_audit(cursor, resources.tenant_id, "tenant.decommission_suspended")

    def block_token_issuance(self, resources: TenantResourceIdentity) -> None:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                active = _lock_and_verify_resources(cursor, resources)
                if active:
                    raise DecommissionConflict("tenant must be inactive before token blocking")
                _write_audit(cursor, resources.tenant_id, "tenant.token_issuance_blocked")

    def revoke_active_credentials(self, resources: TenantResourceIdentity) -> None:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                active = _lock_and_verify_resources(cursor, resources)
                if active:
                    raise DecommissionConflict("tenant must be inactive before token revocation")
                cursor.execute(
                    """
                    UPDATE control.access_tokens
                    SET revoked_at = COALESCE(revoked_at, CURRENT_TIMESTAMP)
                    WHERE tenant_id = %s
                    """,
                    (resources.tenant_id,),
                )
                _write_audit(cursor, resources.tenant_id, "tenant.credentials_revoked")

    def reactivate_without_issuing_tokens(self, resources: TenantResourceIdentity) -> None:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                _lock_and_verify_resources(cursor, resources)
                cursor.execute(
                    """
                    UPDATE control.tenants
                    SET active = true, updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s
                    """,
                    (resources.tenant_id,),
                )
                cursor.execute(
                    """
                    UPDATE control.provisioning
                    SET desired_state = 'active',
                        state = 'active',
                        last_error_code = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s
                    """,
                    (resources.tenant_id,),
                )
                _write_audit(cursor, resources.tenant_id, "tenant.decommission_cancelled")


def _decode_retention_pins(
    open_rows: Sequence[tuple[Any, ...]],
    destroyed_rows: Sequence[tuple[Any, ...]],
    *,
    now: datetime,
) -> tuple[RetentionPin, ...]:
    pins: list[RetentionPin] = []
    for row in open_rows:
        state = DecommissionState(str(row[1]))
        pins.append(
            RetentionPin(
                backup_id=str(row[0]),
                reason=f"open-decommission:{state.value}",
                protected_until=_optional_datetime(row[2], "grace_ends_at"),
            )
        )
    for row in destroyed_rows:
        protected_until = _datetime(row[1], "protected_until")
        if now < protected_until:
            pins.append(
                RetentionPin(
                    backup_id=str(row[0]),
                    reason="post-destruction-recovery",
                    protected_until=protected_until,
                )
            )
    return tuple(pins)


def _record_parameters(record: DecommissionRecord) -> tuple[object, ...]:
    return (
        record.request_id,
        record.resources.tenant_id,
        record.resources.compose_project,
        record.resources.neo4j_volume,
        record.resources.database_name,
        record.resources.database_role,
        record.resources.route_id,
        record.resources.tenant_secret_ref,
        record.requested_by,
        record.reason,
        record.requested_at,
        record.protection_policy,
        record.state.value,
        Jsonb([step.value for step in record.suspension_steps]),
        record.confirmed_by,
        record.confirmed_at,
        record.grace_ends_at,
        _jsonb_or_none(record.protection_evidence),
        Jsonb([step.value for step in record.destruction_steps]),
        record.cancelled_by,
        record.cancelled_at,
        record.last_failed_action,
    )


def _update_parameters(record: DecommissionRecord) -> tuple[object, ...]:
    return _record_parameters(record)[2:]


def _jsonb_or_none(value: ProtectionEvidence | None) -> Jsonb | None:
    return None if value is None else Jsonb(value.model_dump(mode="json"))


def _decode_record(row: tuple[Any, ...]) -> DecommissionRecord:
    protection = row[17]
    if protection is not None and not isinstance(protection, dict):
        raise ValueError("Control Store contains invalid protection evidence")
    return DecommissionRecord(
        request_id=str(row[0]),
        resources=TenantResourceIdentity(
            tenant_id=str(row[1]),
            compose_project=str(row[2]),
            neo4j_volume=str(row[3]),
            database_name=str(row[4]),
            database_role=str(row[5]),
            route_id=str(row[6]),
            tenant_secret_ref=str(row[7]),
        ),
        requested_by=str(row[8]),
        reason=str(row[9]),
        requested_at=_datetime(row[10], "requested_at"),
        protection_policy=str(row[11]),
        state=DecommissionState(str(row[12])),
        suspension_steps=tuple(SuspensionStep(str(value)) for value in _string_list(row[13])),
        confirmed_by=None if row[14] is None else str(row[14]),
        confirmed_at=_optional_datetime(row[15], "confirmed_at"),
        grace_ends_at=_optional_datetime(row[16], "grace_ends_at"),
        protection_evidence=(
            None if protection is None else ProtectionEvidence.model_validate(protection)
        ),
        destruction_steps=tuple(DestructionStep(str(value)) for value in _string_list(row[18])),
        cancelled_by=None if row[19] is None else str(row[19]),
        cancelled_at=_optional_datetime(row[20], "cancelled_at"),
        last_failed_action=None if row[21] is None else str(row[21]),
        revision=int(row[22]),
    )


def _lock_and_verify_resources(
    cursor: psycopg.Cursor[tuple[Any, ...]], resources: TenantResourceIdentity
) -> bool:
    cursor.execute(
        """
        SELECT
            t.active,
            r.tenant_database_name,
            r.tenant_database_role,
            r.neo4j_service_address,
            r.neo4j_secret_name
        FROM control.tenants AS t
        JOIN control.routing AS r ON r.tenant_id = t.tenant_id
        WHERE t.tenant_id = %s
        FOR UPDATE OF t, r
        """,
        (resources.tenant_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise DecommissionConflict("exact tenant route does not exist")
    expected = (
        resources.database_name,
        resources.database_role,
        f"neo4j-{resources.tenant_id}:7687",
        f"{resources.tenant_id}/neo4j_password",
    )
    if tuple(str(value) for value in row[1:]) != expected:
        raise DecommissionConflict("tenant resources differ from the Control Store")
    return bool(row[0])


def _write_audit(cursor: psycopg.Cursor[tuple[Any, ...]], tenant_id: str, action: str) -> None:
    cursor.execute(
        """
        INSERT INTO control.operator_audit (
            audit_id, operator_id, action, target_type, target_id, tenant_id, outcome
        )
        VALUES (%s, 'decommission-service', %s, 'tenant', %s, %s, 'succeeded')
        """,
        (str(uuid4()), action, tenant_id, tenant_id),
    )


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("Control Store contains an invalid decommission step list")
    return value


def _datetime(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"Control Store contains invalid {name}")
    return value.astimezone(UTC)


def _optional_datetime(value: object, name: str) -> datetime | None:
    return None if value is None else _datetime(value, name)

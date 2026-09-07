"""PostgreSQL Adapter for the content-free Control Store."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from agent_memory_service.auth import TokenRecord
from agent_memory_service.control import (
    ChannelBindingRecord,
    ControlConflict,
    ControlNotFound,
    DelegationRecord,
    MembershipRecord,
    OperatorRecord,
    PrincipalRecord,
    TenantRecord,
    TenantRouteRecord,
)
from agent_memory_service.database_url import (
    psycopg_database_url as _psycopg_database_url,
)
from agent_memory_service.manifest import TenantManifest
from agent_memory_service.models import PrincipalKind, TenantSession
from agent_memory_service.operator_audit import OperatorAuditEvent
from agent_memory_service.operator_auth import (
    AdminSessionRecord,
    OperatorSession,
    OperatorTokenRecord,
)
from agent_memory_service.operator_provisioning import (
    ProvisioningJobRecord,
    ProvisioningJobState,
)
from agent_memory_service.provisioning import ProvisioningStep

_PROVISIONING_JOB_COLUMNS = """
    job_id,
    tenant_id,
    manifest_fingerprint,
    manifest,
    idempotency_key,
    requested_by_operator_id,
    state,
    created_at,
    updated_at,
    attempt,
    completed_steps,
    failed_step,
    failure_code,
    claimed_by,
    claimed_at,
    heartbeat_at,
    cancel_requested_at,
    cleanup_requested_at,
    cleanup_completed_at
"""


class PostgresControlStore:
    """Persist ControlStore and TokenStore records in PostgreSQL.

    A connection is opened for each public operation. Psycopg's connection context
    commits successful writes and rolls back exceptions, so callers never observe a
    partially persisted record.
    """

    def __init__(self, database_url: str) -> None:
        if not database_url.strip():
            raise ValueError("Control Store database URL cannot be empty")
        self._database_url = _psycopg_database_url(database_url)

    def add_operator(self, record: OperatorRecord) -> None:
        self._write_once(
            """
            INSERT INTO control.operators (operator_id, name, roles, active)
            VALUES (%s, %s, %s, %s)
            """,
            (record.operator_id, record.name, Jsonb(sorted(record.roles)), record.active),
            conflict_message="Operator already exists",
        )

    def get_operator(self, operator_id: str) -> OperatorRecord | None:
        row = self._fetch_one(
            """
            SELECT operator_id, name, roles, active
            FROM control.operators
            WHERE operator_id = %s
            """,
            (operator_id,),
        )
        return None if row is None else _decode_operator(row)

    def list_operators(self) -> tuple[OperatorRecord, ...]:
        rows = self._fetch_all(
            """
            SELECT operator_id, name, roles, active
            FROM control.operators
            ORDER BY operator_id
            """,
            (),
        )
        return tuple(_decode_operator(row) for row in rows)

    def update_operator(
        self,
        operator_id: str,
        *,
        name: str,
        roles: frozenset[str],
        active: bool,
        changed_at: datetime,
    ) -> OperatorRecord:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT operator_id, name, roles, active
                    FROM control.operators
                    WHERE operator_id = %s
                    FOR UPDATE
                    """,
                    (operator_id,),
                )
                current = cursor.fetchone()
                if current is None:
                    raise ControlNotFound("Operator not found")
                cursor.execute(
                    """
                    UPDATE control.operators
                    SET name = %s,
                        roles = %s,
                        active = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE operator_id = %s
                    RETURNING operator_id, name, roles, active
                    """,
                    (name, Jsonb(sorted(roles)), active, operator_id),
                )
                updated = cursor.fetchone()
                assert updated is not None
                if _decode_roles(current[2]) != roles or bool(current[3]) != active:
                    cursor.execute(
                        """
                        UPDATE control.operator_access_tokens
                        SET revoked_at = COALESCE(revoked_at, %s)
                        WHERE operator_id = %s
                        """,
                        (changed_at, operator_id),
                    )
                    cursor.execute(
                        """
                        UPDATE control.admin_sessions
                        SET revoked_at = COALESCE(revoked_at, %s)
                        WHERE operator_id = %s
                        """,
                        (changed_at, operator_id),
                    )
        return _decode_operator(updated)

    def count_active_operator_admins(self) -> int:
        row = self._fetch_one(
            """
            SELECT count(*)
            FROM control.operators
            WHERE active AND roles ? 'operator_admin'
            """,
            (),
        )
        return 0 if row is None else int(row[0])

    def save_operator_token(self, record: OperatorTokenRecord) -> None:
        self._write_once(
            """
            INSERT INTO control.operator_access_tokens (
                token_id,
                operator_id,
                roles,
                verifier,
                expires_at,
                revoked_at,
                created_at,
                last_used_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                record.token_id,
                record.session.operator_id,
                Jsonb(sorted(record.session.roles)),
                record.verifier,
                record.expires_at,
                record.revoked_at,
                record.issued_at,
                record.last_used_at,
            ),
            conflict_message="Operator Access Token could not be saved",
        )

    def get_operator_token(self, token_id: str) -> OperatorTokenRecord | None:
        row = self._fetch_one(
            """
            SELECT
                token.token_id,
                token.verifier,
                token.operator_id,
                token.roles,
                token.expires_at,
                token.revoked_at,
                token.last_used_at,
                token.created_at
            FROM control.operator_access_tokens AS token
            JOIN control.operators AS operator
              ON operator.operator_id = token.operator_id
             AND operator.active
            WHERE token.token_id = %s
            """,
            (token_id,),
        )
        return None if row is None else _decode_operator_token(row)

    def revoke_operator_token(self, token_id: str, revoked_at: datetime) -> bool:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE control.operator_access_tokens
                    SET revoked_at = COALESCE(revoked_at, %s)
                    WHERE token_id = %s
                    RETURNING token_id
                    """,
                    (revoked_at, token_id),
                )
                return cursor.fetchone() is not None

    def mark_operator_token_used(self, token_id: str, used_at: datetime) -> None:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE control.operator_access_tokens
                    SET last_used_at = GREATEST(COALESCE(last_used_at, %s), %s)
                    WHERE token_id = %s
                    """,
                    (used_at, used_at, token_id),
                )

    def rotate_operator_token(
        self,
        previous_token_id: str,
        replacement: OperatorTokenRecord,
        *,
        previous_valid_until: datetime,
        rotated_at: datetime,
    ) -> bool:
        try:
            with psycopg.connect(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT operator_id
                        FROM control.operator_access_tokens
                        WHERE token_id = %s AND revoked_at IS NULL AND expires_at > %s
                        FOR UPDATE
                        """,
                        (previous_token_id, rotated_at),
                    )
                    previous = cursor.fetchone()
                    if previous is None or str(previous[0]) != replacement.session.operator_id:
                        return False
                    cursor.execute(
                        """
                        SELECT 1
                        FROM control.operators
                        WHERE operator_id = %s AND active
                        FOR KEY SHARE
                        """,
                        (replacement.session.operator_id,),
                    )
                    if cursor.fetchone() is None:
                        return False
                    cursor.execute(
                        """
                        UPDATE control.operator_access_tokens
                        SET expires_at = LEAST(expires_at, %s)
                        WHERE token_id = %s
                        """,
                        (previous_valid_until, previous_token_id),
                    )
                    cursor.execute(
                        """
                        INSERT INTO control.operator_access_tokens (
                            token_id,
                            operator_id,
                            roles,
                            verifier,
                            expires_at,
                            revoked_at,
                            created_at,
                            last_used_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            replacement.token_id,
                            replacement.session.operator_id,
                            Jsonb(sorted(replacement.session.roles)),
                            replacement.verifier,
                            replacement.expires_at,
                            replacement.revoked_at,
                            replacement.issued_at,
                            replacement.last_used_at,
                        ),
                    )
                    return True
        except psycopg.IntegrityError as exc:
            raise ControlConflict("Operator Access Token rotation conflicted") from exc

    def list_operator_token_records(self, operator_id: str) -> tuple[OperatorTokenRecord, ...]:
        rows = self._fetch_all(
            """
            SELECT
                token_id,
                verifier,
                operator_id,
                roles,
                expires_at,
                revoked_at,
                last_used_at,
                created_at
            FROM control.operator_access_tokens
            WHERE operator_id = %s
            ORDER BY created_at, token_id
            """,
            (operator_id,),
        )
        return tuple(_decode_operator_token(row) for row in rows)

    def save_admin_session(self, record: AdminSessionRecord) -> None:
        self._write_once(
            """
            INSERT INTO control.admin_sessions (
                session_id,
                operator_id,
                token_id,
                roles,
                verifier,
                csrf_verifier,
                absolute_expires_at,
                idle_expires_at,
                revoked_at,
                created_at,
                last_used_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                record.session_id,
                record.session.operator_id,
                record.session.token_id,
                Jsonb(sorted(record.session.roles)),
                record.verifier,
                record.csrf_verifier,
                record.absolute_expires_at,
                record.idle_expires_at,
                record.revoked_at,
                record.issued_at,
                record.last_used_at,
            ),
            conflict_message="Admin Session could not be saved",
        )

    def get_admin_session(self, session_id: str) -> AdminSessionRecord | None:
        row = self._fetch_one(
            """
            SELECT
                session.session_id,
                session.verifier,
                session.csrf_verifier,
                session.operator_id,
                session.token_id,
                session.roles,
                session.absolute_expires_at,
                session.idle_expires_at,
                session.revoked_at,
                session.last_used_at,
                session.created_at
            FROM control.admin_sessions AS session
            JOIN control.operators AS operator
              ON operator.operator_id = session.operator_id
             AND operator.active
            WHERE session.session_id = %s
            """,
            (session_id,),
        )
        return None if row is None else _decode_admin_session(row)

    def revoke_admin_session(self, session_id: str, revoked_at: datetime) -> bool:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE control.admin_sessions
                    SET revoked_at = COALESCE(revoked_at, %s)
                    WHERE session_id = %s
                    RETURNING session_id
                    """,
                    (revoked_at, session_id),
                )
                return cursor.fetchone() is not None

    def mark_admin_session_used(
        self,
        session_id: str,
        used_at: datetime,
        idle_expires_at: datetime,
    ) -> None:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE control.admin_sessions
                    SET last_used_at = GREATEST(COALESCE(last_used_at, %s), %s),
                        idle_expires_at = %s
                    WHERE session_id = %s
                    """,
                    (used_at, used_at, idle_expires_at, session_id),
                )

    def save_operator_audit_event(self, event: OperatorAuditEvent) -> None:
        self._write_once(
            """
            INSERT INTO control.operator_audit_events (
                event_id,
                operator_id,
                roles,
                action,
                target_type,
                target_ids,
                request_ref,
                outcome,
                before_metadata,
                after_metadata,
                created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event.event_id,
                event.operator_id,
                Jsonb(sorted(event.roles)),
                event.action,
                event.target_type,
                Jsonb(event.target_ids),
                event.request_ref,
                event.outcome,
                Jsonb(event.before_metadata or {}),
                Jsonb(event.after_metadata or {}),
                event.created_at,
            ),
            conflict_message="Operator Audit Event could not be saved",
        )

    def list_operator_audit_events(self, *, limit: int) -> tuple[OperatorAuditEvent, ...]:
        rows = self._fetch_all(
            """
            SELECT
                event_id,
                operator_id,
                roles,
                action,
                target_type,
                target_ids,
                outcome,
                request_ref,
                before_metadata,
                after_metadata,
                created_at
            FROM control.operator_audit_events
            ORDER BY created_at DESC, event_id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return tuple(_decode_operator_audit_event(row) for row in rows)

    def create_provisioning_job(self, record: ProvisioningJobRecord) -> ProvisioningJobRecord:
        try:
            with psycopg.connect(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"""
                        INSERT INTO control.provisioning_jobs (
                            job_id,
                            tenant_id,
                            manifest_fingerprint,
                            manifest,
                            idempotency_key,
                            requested_by_operator_id,
                            state,
                            created_at,
                            updated_at,
                            attempt,
                            completed_steps,
                            failed_step,
                            failure_code,
                            claimed_by,
                            claimed_at,
                            heartbeat_at,
                            cancel_requested_at,
                            cleanup_requested_at,
                            cleanup_completed_at
                        )
                        VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        ON CONFLICT (requested_by_operator_id, idempotency_key) DO NOTHING
                        RETURNING {_PROVISIONING_JOB_COLUMNS}
                        """,
                        (
                            record.job_id,
                            record.tenant_id,
                            record.manifest_fingerprint,
                            Jsonb(record.manifest.model_dump(mode="json")),
                            record.idempotency_key,
                            record.requested_by_operator_id,
                            record.state.value,
                            record.created_at,
                            record.updated_at,
                            record.attempt,
                            Jsonb([step.value for step in record.completed_steps]),
                            None if record.failed_step is None else record.failed_step.value,
                            record.failure_code,
                            record.claimed_by,
                            record.claimed_at,
                            record.heartbeat_at,
                            record.cancel_requested_at,
                            record.cleanup_requested_at,
                            record.cleanup_completed_at,
                        ),
                    )
                    row = cursor.fetchone()
                    if row is not None:
                        return _decode_provisioning_job(row)
                    cursor.execute(
                        f"""
                        SELECT {_PROVISIONING_JOB_COLUMNS}
                        FROM control.provisioning_jobs
                        WHERE requested_by_operator_id = %s
                          AND idempotency_key = %s
                        """,
                        (record.requested_by_operator_id, record.idempotency_key),
                    )
                    existing = cursor.fetchone()
                    if existing is None:
                        raise ControlConflict("Provisioning Job could not be saved")
                    return _decode_provisioning_job(existing)
        except psycopg.IntegrityError as exc:
            existing = self._fetch_one(
                f"""
                SELECT {_PROVISIONING_JOB_COLUMNS}
                FROM control.provisioning_jobs
                WHERE tenant_id = %s
                  AND manifest_fingerprint = %s
                  AND state IN (
                      'queued',
                      'running',
                      'failed',
                      'cancel_requested',
                      'canceled',
                      'cleanup_requested'
                  )
                ORDER BY created_at DESC, job_id DESC
                LIMIT 1
                """,
                (record.tenant_id, record.manifest_fingerprint),
            )
            if existing is not None:
                return _decode_provisioning_job(existing)
            raise ControlConflict("Provisioning Job could not be saved") from exc

    def get_provisioning_job(self, job_id: str) -> ProvisioningJobRecord | None:
        row = self._fetch_one(
            f"""
            SELECT {_PROVISIONING_JOB_COLUMNS}
            FROM control.provisioning_jobs
            WHERE job_id = %s
            """,
            (job_id,),
        )
        return None if row is None else _decode_provisioning_job(row)

    def list_provisioning_jobs(self) -> tuple[ProvisioningJobRecord, ...]:
        rows = self._fetch_all(
            f"""
            SELECT {_PROVISIONING_JOB_COLUMNS}
            FROM control.provisioning_jobs
            ORDER BY created_at DESC, job_id DESC
            """,
            (),
        )
        return tuple(_decode_provisioning_job(row) for row in rows)

    def update_provisioning_job_state(
        self,
        job_id: str,
        *,
        state: ProvisioningJobState,
        changed_at: datetime,
    ) -> ProvisioningJobRecord:
        row = self._fetch_one(
            f"""
            UPDATE control.provisioning_jobs
            SET state = %s,
                updated_at = %s,
                claimed_by = CASE
                    WHEN %s = 'queued' THEN NULL
                    ELSE claimed_by
                END,
                claimed_at = CASE
                    WHEN %s = 'queued' THEN NULL
                    ELSE claimed_at
                END,
                heartbeat_at = CASE
                    WHEN %s = 'queued' THEN NULL
                    ELSE heartbeat_at
                END,
                completed_steps = CASE
                    WHEN %s = 'queued' THEN '[]'::jsonb
                    ELSE completed_steps
                END,
                failed_step = CASE
                    WHEN %s = 'queued' THEN NULL
                    ELSE failed_step
                END,
                failure_code = CASE
                    WHEN %s = 'queued' THEN NULL
                    ELSE failure_code
                END,
                cancel_requested_at = CASE
                    WHEN %s = 'cancel_requested'
                    THEN COALESCE(cancel_requested_at, %s)
                    ELSE cancel_requested_at
                END,
                cleanup_requested_at = CASE
                    WHEN %s = 'cleanup_requested'
                    THEN COALESCE(cleanup_requested_at, %s)
                    ELSE cleanup_requested_at
                END,
                cleanup_completed_at = CASE
                    WHEN %s = 'cleaned_up'
                    THEN COALESCE(cleanup_completed_at, %s)
                    ELSE cleanup_completed_at
                END
            WHERE job_id = %s
            RETURNING {_PROVISIONING_JOB_COLUMNS}
            """,
            (
                state.value,
                changed_at,
                state.value,
                state.value,
                state.value,
                state.value,
                state.value,
                state.value,
                state.value,
                changed_at,
                state.value,
                changed_at,
                state.value,
                changed_at,
                job_id,
            ),
        )
        if row is None:
            raise ControlNotFound("Provisioning Job not found")
        return _decode_provisioning_job(row)

    def claim_next_provisioning_job(
        self,
        *,
        worker_id: str,
        claimed_at: datetime,
    ) -> ProvisioningJobRecord | None:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    WITH candidate AS (
                        SELECT job_id
                        FROM control.provisioning_jobs
                        WHERE state = 'queued'
                        ORDER BY created_at, job_id
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE control.provisioning_jobs AS job
                    SET state = 'running',
                        attempt = job.attempt + 1,
                        claimed_by = %s,
                        claimed_at = %s,
                        heartbeat_at = %s,
                        updated_at = %s
                    FROM candidate
                    WHERE job.job_id = candidate.job_id
                    RETURNING {_PROVISIONING_JOB_COLUMNS}
                    """,
                    (worker_id, claimed_at, claimed_at, claimed_at),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                job = _decode_provisioning_job(row)
                cursor.execute(
                    """
                    INSERT INTO control.provisioning_job_attempts (
                        job_id,
                        attempt,
                        state,
                        started_at
                    )
                    VALUES (%s, %s, 'running', %s)
                    """,
                    (job.job_id, job.attempt, claimed_at),
                )
                return job

    def complete_provisioning_job(
        self,
        job_id: str,
        *,
        worker_id: str,
        completed_steps: tuple[ProvisioningStep, ...],
        completed_at: datetime,
    ) -> ProvisioningJobRecord:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    UPDATE control.provisioning_jobs
                    SET state = 'succeeded',
                        completed_steps = %s,
                        failed_step = NULL,
                        failure_code = NULL,
                        updated_at = %s
                    WHERE job_id = %s
                      AND claimed_by = %s
                      AND state = 'running'
                    RETURNING {_PROVISIONING_JOB_COLUMNS}
                    """,
                    (
                        Jsonb([step.value for step in completed_steps]),
                        completed_at,
                        job_id,
                        worker_id,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ControlConflict("Provisioning Job is not claimed by this worker")
                job = _decode_provisioning_job(row)
                cursor.execute(
                    """
                    UPDATE control.provisioning_job_attempts
                    SET state = 'succeeded',
                        finished_at = %s,
                        failed_step = NULL,
                        failure_code = NULL
                    WHERE job_id = %s AND attempt = %s
                    """,
                    (completed_at, job.job_id, job.attempt),
                )
                return job

    def fail_provisioning_job(
        self,
        job_id: str,
        *,
        worker_id: str,
        failed_step: ProvisioningStep | None,
        failure_code: str,
        failed_at: datetime,
    ) -> ProvisioningJobRecord:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    UPDATE control.provisioning_jobs
                    SET state = 'failed',
                        failed_step = %s,
                        failure_code = %s,
                        updated_at = %s
                    WHERE job_id = %s
                      AND claimed_by = %s
                      AND state = 'running'
                    RETURNING {_PROVISIONING_JOB_COLUMNS}
                    """,
                    (
                        None if failed_step is None else failed_step.value,
                        failure_code,
                        failed_at,
                        job_id,
                        worker_id,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ControlConflict("Provisioning Job is not claimed by this worker")
                job = _decode_provisioning_job(row)
                cursor.execute(
                    """
                    UPDATE control.provisioning_job_attempts
                    SET state = 'failed',
                        finished_at = %s,
                        failed_step = %s,
                        failure_code = %s
                    WHERE job_id = %s AND attempt = %s
                    """,
                    (
                        failed_at,
                        None if failed_step is None else failed_step.value,
                        failure_code,
                        job.job_id,
                        job.attempt,
                    ),
                )
                return job

    def add_tenant(self, record: TenantRecord) -> None:
        self._write_once(
            """
            INSERT INTO control.tenants (tenant_id, name, active)
            VALUES (%s, %s, %s)
            """,
            (record.tenant_id, record.name, record.active),
            conflict_message="Tenant already exists",
        )

    def get_tenant(self, tenant_id: str) -> TenantRecord | None:
        row = self._fetch_one(
            """
            SELECT tenant_id, name, active
            FROM control.tenants
            WHERE tenant_id = %s
            """,
            (tenant_id,),
        )
        if row is None:
            return None
        return TenantRecord(tenant_id=str(row[0]), name=str(row[1]), active=bool(row[2]))

    def list_tenants(self) -> tuple[TenantRecord, ...]:
        rows = self._fetch_all(
            """
            SELECT tenant_id, name, active
            FROM control.tenants
            ORDER BY tenant_id
            """,
            (),
        )
        return tuple(
            TenantRecord(tenant_id=str(row[0]), name=str(row[1]), active=bool(row[2]))
            for row in rows
        )

    def add_principal(self, record: PrincipalRecord) -> None:
        self._write_once(
            """
            INSERT INTO control.principals (principal_id, name, kind, active)
            VALUES (%s, %s, %s, %s)
            """,
            (record.principal_id, record.name, record.kind.value, record.active),
            conflict_message="Principal already exists",
        )

    def get_principal(self, principal_id: str) -> PrincipalRecord | None:
        row = self._fetch_one(
            """
            SELECT principal_id, name, kind, active
            FROM control.principals
            WHERE principal_id = %s
            """,
            (principal_id,),
        )
        if row is None:
            return None
        return PrincipalRecord(
            principal_id=str(row[0]),
            name=str(row[1]),
            kind=PrincipalKind(str(row[2])),
            active=bool(row[3]),
        )

    def list_principals(self) -> tuple[PrincipalRecord, ...]:
        rows = self._fetch_all(
            """
            SELECT principal_id, name, kind, active
            FROM control.principals
            ORDER BY principal_id
            """,
            (),
        )
        return tuple(_decode_principal(row) for row in rows)

    def update_principal(
        self, principal_id: str, *, name: str, active: bool, changed_at: datetime
    ) -> PrincipalRecord:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE control.principals
                    SET name = %s, active = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE principal_id = %s
                    RETURNING principal_id, name, kind, active
                    """,
                    (name, active, principal_id),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ControlNotFound("Principal not found")
                if not active:
                    cursor.execute(
                        """
                        UPDATE control.memberships
                        SET active = false, updated_at = CURRENT_TIMESTAMP
                        WHERE principal_id = %s
                        """,
                        (principal_id,),
                    )
                    cursor.execute(
                        """
                        UPDATE control.delegations
                        SET active = false, updated_at = CURRENT_TIMESTAMP
                        WHERE agent_id = %s OR subject_user_id = %s
                        """,
                        (principal_id, principal_id),
                    )
                    cursor.execute(
                        """
                        UPDATE control.channel_bindings
                        SET active = false, updated_at = CURRENT_TIMESTAMP
                        WHERE agent_id = %s OR user_id = %s
                        """,
                        (principal_id, principal_id),
                    )
                    cursor.execute(
                        """
                        UPDATE control.access_tokens
                        SET revoked_at = COALESCE(revoked_at, %s)
                        WHERE principal_id = %s OR subject_user_id = %s
                        """,
                        (changed_at, principal_id, principal_id),
                    )
        return _decode_principal(row)

    def disable_principal(self, principal_id: str, disabled_at: datetime) -> PrincipalRecord:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE control.principals
                    SET active = false, updated_at = CURRENT_TIMESTAMP
                    WHERE principal_id = %s
                    RETURNING principal_id, name, kind, active
                    """,
                    (principal_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ControlNotFound("Principal not found")
                cursor.execute(
                    """
                    UPDATE control.memberships
                    SET active = false, updated_at = CURRENT_TIMESTAMP
                    WHERE principal_id = %s
                    """,
                    (principal_id,),
                )
                cursor.execute(
                    """
                    UPDATE control.delegations
                    SET active = false, updated_at = CURRENT_TIMESTAMP
                    WHERE agent_id = %s OR subject_user_id = %s
                    """,
                    (principal_id, principal_id),
                )
                cursor.execute(
                    """
                    UPDATE control.channel_bindings
                    SET active = false, updated_at = CURRENT_TIMESTAMP
                    WHERE agent_id = %s OR user_id = %s
                    """,
                    (principal_id, principal_id),
                )
                cursor.execute(
                    """
                    UPDATE control.access_tokens
                    SET revoked_at = COALESCE(revoked_at, %s)
                    WHERE principal_id = %s OR subject_user_id = %s
                    """,
                    (disabled_at, principal_id, principal_id),
                )
        return PrincipalRecord(
            principal_id=str(row[0]),
            name=str(row[1]),
            kind=PrincipalKind(str(row[2])),
            active=bool(row[3]),
        )

    def save_membership(self, record: MembershipRecord) -> None:
        self._write_once(
            """
            INSERT INTO control.memberships (tenant_id, principal_id, roles, active)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (tenant_id, principal_id) DO UPDATE
            SET roles = EXCLUDED.roles,
                active = EXCLUDED.active,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                record.tenant_id,
                record.principal_id,
                Jsonb(sorted(record.roles)),
                record.active,
            ),
            conflict_message="Tenant Membership could not be saved",
        )

    def get_membership(self, tenant_id: str, principal_id: str) -> MembershipRecord | None:
        row = self._fetch_one(
            """
            SELECT tenant_id, principal_id, roles, active
            FROM control.memberships
            WHERE tenant_id = %s AND principal_id = %s
            """,
            (tenant_id, principal_id),
        )
        if row is None:
            return None
        return MembershipRecord(
            tenant_id=str(row[0]),
            principal_id=str(row[1]),
            roles=_decode_roles(row[2]),
            active=bool(row[3]),
        )

    def list_memberships(self, tenant_id: str) -> tuple[MembershipRecord, ...]:
        rows = self._fetch_all(
            """
            SELECT tenant_id, principal_id, roles, active
            FROM control.memberships
            WHERE tenant_id = %s
            ORDER BY principal_id
            """,
            (tenant_id,),
        )
        return tuple(_decode_membership(row) for row in rows)

    def update_membership(self, record: MembershipRecord, changed_at: datetime) -> MembershipRecord:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT tenant_id, principal_id, roles, active
                    FROM control.memberships
                    WHERE tenant_id = %s AND principal_id = %s
                    FOR UPDATE
                    """,
                    (record.tenant_id, record.principal_id),
                )
                current = cursor.fetchone()
                if current is None:
                    raise ControlNotFound("Tenant Membership not found")
                if not record.active:
                    _disable_membership(
                        cursor,
                        record.tenant_id,
                        record.principal_id,
                        changed_at,
                    )
                cursor.execute(
                    """
                    UPDATE control.memberships
                    SET roles = %s, active = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND principal_id = %s
                    RETURNING tenant_id, principal_id, roles, active
                    """,
                    (
                        Jsonb(sorted(record.roles)),
                        record.active,
                        record.tenant_id,
                        record.principal_id,
                    ),
                )
                updated = cursor.fetchone()
                assert updated is not None
                if _decode_roles(current[2]) != record.roles or bool(current[3]) != record.active:
                    cursor.execute(
                        """
                        UPDATE control.access_tokens
                        SET revoked_at = COALESCE(revoked_at, %s)
                        WHERE tenant_id = %s AND principal_id = %s
                        """,
                        (changed_at, record.tenant_id, record.principal_id),
                    )
        return _decode_membership(updated)

    def disable_membership(
        self, tenant_id: str, principal_id: str, disabled_at: datetime
    ) -> MembershipRecord:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                return _disable_membership(cursor, tenant_id, principal_id, disabled_at)

    def revoke_membership_role(
        self, tenant_id: str, principal_id: str, role: str, revoked_at: datetime
    ) -> MembershipRecord:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT tenant_id, principal_id, roles, active
                    FROM control.memberships
                    WHERE tenant_id = %s AND principal_id = %s
                    FOR UPDATE
                    """,
                    (tenant_id, principal_id),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ControlNotFound("Tenant Membership not found")
                roles = _decode_roles(row[2])
                if role not in roles:
                    raise ControlNotFound("Tenant Membership role not found")
                remaining = roles - {role}
                if not remaining:
                    return _disable_membership(cursor, tenant_id, principal_id, revoked_at)
                cursor.execute(
                    """
                    UPDATE control.memberships
                    SET roles = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND principal_id = %s
                    RETURNING tenant_id, principal_id, roles, active
                    """,
                    (Jsonb(sorted(remaining)), tenant_id, principal_id),
                )
                updated = cursor.fetchone()
                assert updated is not None
                cursor.execute(
                    """
                    UPDATE control.access_tokens
                    SET revoked_at = COALESCE(revoked_at, %s)
                    WHERE tenant_id = %s AND principal_id = %s
                    """,
                    (revoked_at, tenant_id, principal_id),
                )
                return _decode_membership(updated)

    def list_active_human_member_ids(
        self,
        tenant_id: str,
    ) -> tuple[str, ...]:
        rows = self._fetch_all(
            """
            SELECT m.principal_id
            FROM control.memberships AS m
            JOIN control.principals AS p ON p.principal_id = m.principal_id
            JOIN control.tenants AS t ON t.tenant_id = m.tenant_id
            WHERE m.tenant_id = %s
              AND m.active
              AND p.active
              AND t.active
              AND p.kind = 'user'
            ORDER BY m.principal_id
            """,
            (tenant_id,),
        )
        return tuple(str(row[0]) for row in rows)

    def save_delegation(self, record: DelegationRecord) -> None:
        self._write_once(
            """
            INSERT INTO control.delegations (
                delegation_id,
                tenant_id,
                agent_id,
                subject_user_id,
                active
            )
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                record.delegation_id,
                record.tenant_id,
                record.agent_id,
                record.subject_user_id,
                record.active,
            ),
            conflict_message="Delegation already exists",
        )

    def get_delegation(self, delegation_id: str) -> DelegationRecord | None:
        row = self._fetch_one(
            """
            SELECT delegation_id, tenant_id, agent_id, subject_user_id, active
            FROM control.delegations
            WHERE delegation_id = %s
            """,
            (delegation_id,),
        )
        if row is None:
            return None
        return DelegationRecord(
            delegation_id=str(row[0]),
            tenant_id=str(row[1]),
            agent_id=str(row[2]),
            subject_user_id=str(row[3]),
            active=bool(row[4]),
        )

    def list_delegations(self, tenant_id: str) -> tuple[DelegationRecord, ...]:
        rows = self._fetch_all(
            """
            SELECT delegation_id, tenant_id, agent_id, subject_user_id, active
            FROM control.delegations
            WHERE tenant_id = %s
            ORDER BY delegation_id
            """,
            (tenant_id,),
        )
        return tuple(_decode_delegation(row) for row in rows)

    def update_delegation_active(
        self, delegation_id: str, *, active: bool, changed_at: datetime
    ) -> DelegationRecord:
        if not active:
            return self.revoke_delegation(delegation_id, changed_at)
        row = self._fetch_one(
            """
            UPDATE control.delegations
            SET active = true, updated_at = CURRENT_TIMESTAMP
            WHERE delegation_id = %s
            RETURNING delegation_id, tenant_id, agent_id, subject_user_id, active
            """,
            (delegation_id,),
        )
        if row is None:
            raise ControlNotFound("Delegation not found")
        return _decode_delegation(row)

    def revoke_delegation(self, delegation_id: str, revoked_at: datetime) -> DelegationRecord:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE control.delegations
                    SET active = false, updated_at = CURRENT_TIMESTAMP
                    WHERE delegation_id = %s
                    RETURNING delegation_id, tenant_id, agent_id, subject_user_id, active
                    """,
                    (delegation_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ControlNotFound("Delegation not found")
                cursor.execute(
                    """
                    UPDATE control.channel_bindings
                    SET active = false, updated_at = CURRENT_TIMESTAMP
                    WHERE delegation_id = %s
                    """,
                    (delegation_id,),
                )
                cursor.execute(
                    """
                    UPDATE control.access_tokens
                    SET revoked_at = COALESCE(revoked_at, %s)
                    WHERE delegation_id = %s
                    """,
                    (revoked_at, delegation_id),
                )
        return DelegationRecord(
            delegation_id=str(row[0]),
            tenant_id=str(row[1]),
            agent_id=str(row[2]),
            subject_user_id=str(row[3]),
            active=bool(row[4]),
        )

    def save_channel_binding(self, record: ChannelBindingRecord) -> None:
        self._write_once(
            """
            INSERT INTO control.channel_bindings (
                binding_id,
                channel,
                external_id,
                tenant_id,
                user_id,
                agent_id,
                delegation_id,
                active
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                record.binding_id,
                record.channel,
                record.external_id,
                record.tenant_id,
                record.user_id,
                record.agent_id,
                record.delegation_id,
                record.active,
            ),
            conflict_message="Channel Binding already exists",
        )

    def get_channel_binding(
        self,
        channel: str,
        external_id: str,
    ) -> ChannelBindingRecord | None:
        row = self._fetch_one(
            """
            SELECT
                binding_id,
                channel,
                external_id,
                tenant_id,
                user_id,
                agent_id,
                delegation_id,
                active
            FROM control.channel_bindings
            WHERE channel = %s AND external_id = %s
            """,
            (channel, external_id),
        )
        if row is None:
            return None
        return ChannelBindingRecord(
            binding_id=str(row[0]),
            channel=str(row[1]),
            external_id=str(row[2]),
            tenant_id=str(row[3]),
            user_id=str(row[4]),
            agent_id=str(row[5]),
            delegation_id=str(row[6]),
            active=bool(row[7]),
        )

    def update_channel_binding(self, record: ChannelBindingRecord) -> None:
        try:
            with psycopg.connect(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE control.channel_bindings
                        SET tenant_id = %s,
                            user_id = %s,
                            agent_id = %s,
                            delegation_id = %s,
                            active = %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE binding_id = %s
                          AND channel = %s
                          AND external_id = %s
                        RETURNING binding_id
                        """,
                        (
                            record.tenant_id,
                            record.user_id,
                            record.agent_id,
                            record.delegation_id,
                            record.active,
                            record.binding_id,
                            record.channel,
                            record.external_id,
                        ),
                    )
                    if cursor.fetchone() is None:
                        raise ControlNotFound("Channel Binding not found")
        except psycopg.IntegrityError as exc:
            raise ControlConflict("Channel Binding could not be updated") from exc

    def delete_channel_binding(self, channel: str, external_id: str) -> bool:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM control.channel_bindings
                    WHERE channel = %s AND external_id = %s
                    RETURNING binding_id
                    """,
                    (channel, external_id),
                )
                return cursor.fetchone() is not None

    def save_tenant_route(self, record: TenantRouteRecord) -> None:
        self._write_once(
            """
            INSERT INTO control.routing (
                tenant_id,
                neo4j_service_address,
                neo4j_secret_name,
                tenant_database_name,
                tenant_database_role,
                healthy,
                checked_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (tenant_id) DO UPDATE
            SET neo4j_service_address = EXCLUDED.neo4j_service_address,
                neo4j_secret_name = EXCLUDED.neo4j_secret_name,
                tenant_database_name = EXCLUDED.tenant_database_name,
                tenant_database_role = EXCLUDED.tenant_database_role,
                healthy = EXCLUDED.healthy,
                checked_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                record.tenant_id,
                record.neo4j_service_address,
                record.neo4j_secret_name,
                record.tenant_database_name,
                record.tenant_database_role,
                record.healthy,
            ),
            conflict_message="Tenant route could not be saved",
        )

    def get_tenant_route(self, tenant_id: str) -> TenantRouteRecord | None:
        row = self._fetch_one(
            """
            SELECT tenant_id, neo4j_service_address, neo4j_secret_name,
                   tenant_database_name, tenant_database_role, healthy
            FROM control.routing
            WHERE tenant_id = %s
            """,
            (tenant_id,),
        )
        return None if row is None else _decode_route(row)

    def list_tenant_routes(self) -> tuple[TenantRouteRecord, ...]:
        rows = self._fetch_all(
            """
            SELECT tenant_id, neo4j_service_address, neo4j_secret_name,
                   tenant_database_name, tenant_database_role, healthy
            FROM control.routing
            ORDER BY tenant_id
            """,
            (),
        )
        return tuple(_decode_route(row) for row in rows)

    def token_lifetime_days(
        self,
        tenant_id: str,
        actor_kind: PrincipalKind,
        *,
        delegated: bool,
    ) -> int | None:
        if actor_kind is PrincipalKind.USER:
            policy_name = "user_token_lifetime_days"
        elif delegated:
            policy_name = "delegated_agent_token_lifetime_days"
        else:
            policy_name = "autonomous_agent_token_lifetime_days"
        row = self._fetch_one(
            """
            SELECT policies ->> %s
            FROM control.provisioning
            WHERE tenant_id = %s AND state = 'active'
            """,
            (policy_name, tenant_id),
        )
        if row is None or row[0] is None:
            return None
        days = int(str(row[0]))
        if days < 1:
            raise ValueError("Control Store contains an invalid token lifetime policy")
        return days

    def save(self, record: TokenRecord) -> None:
        """Save only the token's non-reversible verifier and fixed session context."""
        self._write_once(
            """
            INSERT INTO control.access_tokens (
                token_id,
                tenant_id,
                principal_id,
                actor_kind,
                roles,
                verifier,
                subject_user_id,
                delegation_id,
                expires_at,
                revoked_at,
                created_at,
                last_used_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                record.token_id,
                record.session.tenant_id,
                record.session.actor_id,
                record.session.actor_kind.value,
                Jsonb(sorted(record.session.roles)),
                record.verifier,
                record.session.subject_user_id,
                record.session.delegation_id,
                record.expires_at,
                record.revoked_at,
                record.issued_at,
                record.last_used_at,
            ),
            conflict_message="Access Token could not be saved",
        )

    def get(self, token_id: str) -> TokenRecord | None:
        row = self._fetch_one(
            """
            SELECT
                a.token_id,
                a.verifier,
                a.tenant_id,
                a.principal_id,
                a.actor_kind,
                a.roles,
                a.subject_user_id,
                a.delegation_id,
                a.expires_at,
                a.revoked_at,
                a.last_used_at,
                a.created_at
            FROM control.access_tokens AS a
            JOIN control.tenants AS t ON t.tenant_id = a.tenant_id AND t.active
            JOIN control.principals AS p
              ON p.principal_id = a.principal_id AND p.active
            JOIN control.memberships AS m
              ON m.tenant_id = a.tenant_id
             AND m.principal_id = a.principal_id
             AND m.active
            LEFT JOIN control.delegations AS d
              ON d.delegation_id = a.delegation_id
            WHERE a.token_id = %s
              AND (a.delegation_id IS NULL OR d.active)
            """,
            (token_id,),
        )
        return None if row is None else _decode_token(row)

    def revoke(self, token_id: str, revoked_at: datetime) -> bool:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE control.access_tokens
                    SET revoked_at = COALESCE(revoked_at, %s)
                    WHERE token_id = %s
                    RETURNING token_id
                    """,
                    (revoked_at, token_id),
                )
                return cursor.fetchone() is not None

    def mark_used(self, token_id: str, used_at: datetime) -> None:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE control.access_tokens
                    SET last_used_at = GREATEST(COALESCE(last_used_at, %s), %s)
                    WHERE token_id = %s
                    """,
                    (used_at, used_at, token_id),
                )

    def rotate(
        self,
        previous_token_id: str,
        replacement: TokenRecord,
        *,
        previous_valid_until: datetime,
        rotated_at: datetime,
    ) -> bool:
        try:
            with psycopg.connect(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT tenant_id, principal_id, subject_user_id, delegation_id
                        FROM control.access_tokens
                        WHERE token_id = %s AND revoked_at IS NULL AND expires_at > %s
                        FOR UPDATE
                        """,
                        (previous_token_id, rotated_at),
                    )
                    previous = cursor.fetchone()
                    expected = (
                        replacement.session.tenant_id,
                        replacement.session.actor_id,
                        replacement.session.subject_user_id,
                        replacement.session.delegation_id,
                    )
                    if previous is None or previous != expected:
                        return False
                    cursor.execute(
                        """
                        SELECT 1
                        FROM control.tenants AS t
                        JOIN control.principals AS p ON p.principal_id = %s
                        JOIN control.memberships AS m
                          ON m.tenant_id = t.tenant_id AND m.principal_id = p.principal_id
                        WHERE t.tenant_id = %s AND t.active AND p.active AND m.active
                        FOR KEY SHARE OF t, p, m
                        """,
                        (replacement.session.actor_id, replacement.session.tenant_id),
                    )
                    if cursor.fetchone() is None:
                        return False
                    if replacement.session.delegation_id is not None:
                        cursor.execute(
                            """
                            SELECT 1
                            FROM control.delegations AS d
                            JOIN control.principals AS subject
                              ON subject.principal_id = d.subject_user_id
                            JOIN control.memberships AS membership
                              ON membership.tenant_id = d.tenant_id
                             AND membership.principal_id = d.subject_user_id
                            WHERE d.delegation_id = %s
                              AND d.tenant_id = %s
                              AND d.agent_id = %s
                              AND d.subject_user_id = %s
                              AND d.active AND subject.active AND membership.active
                            FOR KEY SHARE OF d, subject, membership
                            """,
                            (
                                replacement.session.delegation_id,
                                replacement.session.tenant_id,
                                replacement.session.actor_id,
                                replacement.session.subject_user_id,
                            ),
                        )
                        if cursor.fetchone() is None:
                            return False
                    cursor.execute(
                        """
                        UPDATE control.access_tokens
                        SET expires_at = LEAST(expires_at, %s)
                        WHERE token_id = %s
                        """,
                        (previous_valid_until, previous_token_id),
                    )
                    cursor.execute(
                        """
                        INSERT INTO control.access_tokens (
                            token_id, tenant_id, principal_id, actor_kind, roles,
                            verifier, subject_user_id, delegation_id, expires_at,
                            revoked_at, created_at, last_used_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            replacement.token_id,
                            replacement.session.tenant_id,
                            replacement.session.actor_id,
                            replacement.session.actor_kind.value,
                            Jsonb(sorted(replacement.session.roles)),
                            replacement.verifier,
                            replacement.session.subject_user_id,
                            replacement.session.delegation_id,
                            replacement.expires_at,
                            replacement.revoked_at,
                            replacement.issued_at,
                            replacement.last_used_at,
                        ),
                    )
                    return True
        except psycopg.IntegrityError as exc:
            raise ControlConflict("Access Token rotation conflicted") from exc

    def list_token_records(self, tenant_id: str, principal_id: str) -> tuple[TokenRecord, ...]:
        rows = self._fetch_all(
            """
            SELECT
                token_id,
                verifier,
                tenant_id,
                principal_id,
                actor_kind,
                roles,
                subject_user_id,
                delegation_id,
                expires_at,
                revoked_at,
                last_used_at,
                created_at
            FROM control.access_tokens
            WHERE tenant_id = %s AND principal_id = %s
            ORDER BY created_at, token_id
            """,
            (tenant_id, principal_id),
        )
        return tuple(_decode_token(row) for row in rows)

    def token_usage_signals(
        self,
        *,
        checked_at: datetime,
        inactive_before: datetime,
    ) -> tuple[int, int]:
        """Return aggregate active-token signals without identifiers or token material."""

        row = self._fetch_one(
            """
            SELECT
                count(*) FILTER (WHERE last_used_at IS NULL),
                count(*) FILTER (
                    WHERE COALESCE(last_used_at, created_at) < %s
                )
            FROM control.access_tokens
            WHERE revoked_at IS NULL AND expires_at > %s
            """,
            (inactive_before, checked_at),
        )
        if row is None:
            return 0, 0
        return int(row[0]), int(row[1])

    def _write_once(
        self,
        statement: str,
        parameters: Sequence[object],
        *,
        conflict_message: str,
    ) -> None:
        try:
            with psycopg.connect(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(statement, parameters)
        except psycopg.IntegrityError as exc:
            raise ControlConflict(conflict_message) from exc

    def _fetch_one(
        self,
        statement: str,
        parameters: Sequence[object],
    ) -> tuple[Any, ...] | None:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(statement, parameters)
                return cursor.fetchone()

    def _fetch_all(
        self,
        statement: str,
        parameters: Sequence[object],
    ) -> list[tuple[Any, ...]]:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(statement, parameters)
                return cursor.fetchall()


def _disable_membership(
    cursor: psycopg.Cursor[tuple[Any, ...]],
    tenant_id: str,
    principal_id: str,
    disabled_at: datetime,
) -> MembershipRecord:
    cursor.execute(
        """
        UPDATE control.memberships
        SET active = false, updated_at = CURRENT_TIMESTAMP
        WHERE tenant_id = %s AND principal_id = %s
        RETURNING tenant_id, principal_id, roles, active
        """,
        (tenant_id, principal_id),
    )
    row = cursor.fetchone()
    if row is None:
        raise ControlNotFound("Tenant Membership not found")
    cursor.execute(
        """
        UPDATE control.delegations
        SET active = false, updated_at = CURRENT_TIMESTAMP
        WHERE tenant_id = %s AND (agent_id = %s OR subject_user_id = %s)
        """,
        (tenant_id, principal_id, principal_id),
    )
    cursor.execute(
        """
        UPDATE control.channel_bindings
        SET active = false, updated_at = CURRENT_TIMESTAMP
        WHERE tenant_id = %s AND (agent_id = %s OR user_id = %s)
        """,
        (tenant_id, principal_id, principal_id),
    )
    cursor.execute(
        """
        UPDATE control.access_tokens
        SET revoked_at = COALESCE(revoked_at, %s)
        WHERE tenant_id = %s AND (principal_id = %s OR subject_user_id = %s)
        """,
        (disabled_at, tenant_id, principal_id, principal_id),
    )
    return _decode_membership(row)


def _decode_membership(row: tuple[Any, ...]) -> MembershipRecord:
    return MembershipRecord(
        tenant_id=str(row[0]),
        principal_id=str(row[1]),
        roles=_decode_roles(row[2]),
        active=bool(row[3]),
    )


def _decode_principal(row: tuple[Any, ...]) -> PrincipalRecord:
    return PrincipalRecord(
        principal_id=str(row[0]),
        name=str(row[1]),
        kind=PrincipalKind(str(row[2])),
        active=bool(row[3]),
    )


def _decode_operator(row: tuple[Any, ...]) -> OperatorRecord:
    return OperatorRecord(
        operator_id=str(row[0]),
        name=str(row[1]),
        roles=_decode_roles(row[2]),
        active=bool(row[3]),
    )


def _decode_operator_audit_event(row: tuple[Any, ...]) -> OperatorAuditEvent:
    created_at = row[10]
    if not isinstance(created_at, datetime):
        raise ValueError("Control Store contains an invalid Operator Audit Event time")
    return OperatorAuditEvent(
        event_id=str(row[0]),
        operator_id=str(row[1]),
        roles=_decode_roles(row[2]),
        action=str(row[3]),
        target_type=str(row[4]),
        target_ids=_decode_string_dict(row[5], "target identifiers"),
        outcome=str(row[6]),
        request_ref=None if row[7] is None else str(row[7]),
        before_metadata=_decode_string_dict(row[8], "before metadata"),
        after_metadata=_decode_string_dict(row[9], "after metadata"),
        created_at=created_at,
    )


def _decode_provisioning_job(row: tuple[Any, ...]) -> ProvisioningJobRecord:
    manifest_document = row[3]
    completed_steps = row[10]
    if not isinstance(manifest_document, dict):
        raise ValueError("Control Store contains an invalid Provisioning Job manifest")
    if not isinstance(completed_steps, list) or not all(
        isinstance(step, str) for step in completed_steps
    ):
        raise ValueError("Control Store contains invalid Provisioning Job completed steps")
    return ProvisioningJobRecord(
        job_id=str(row[0]),
        tenant_id=str(row[1]),
        manifest_fingerprint=str(row[2]),
        manifest=TenantManifest.model_validate(manifest_document),
        idempotency_key=str(row[4]),
        requested_by_operator_id=str(row[5]),
        state=ProvisioningJobState(str(row[6])),
        created_at=_required_datetime(row[7], "Provisioning Job creation time"),
        updated_at=_required_datetime(row[8], "Provisioning Job update time"),
        attempt=int(row[9]),
        completed_steps=tuple(ProvisioningStep(step) for step in completed_steps),
        failed_step=None if row[11] is None else ProvisioningStep(str(row[11])),
        failure_code=None if row[12] is None else str(row[12]),
        claimed_by=None if row[13] is None else str(row[13]),
        claimed_at=_optional_datetime(row[14], "Provisioning Job claim time"),
        heartbeat_at=_optional_datetime(row[15], "Provisioning Job heartbeat time"),
        cancel_requested_at=_optional_datetime(
            row[16],
            "Provisioning Job cancel request time",
        ),
        cleanup_requested_at=_optional_datetime(
            row[17],
            "Provisioning Job cleanup request time",
        ),
        cleanup_completed_at=_optional_datetime(
            row[18],
            "Provisioning Job cleanup completion time",
        ),
    )


def _decode_delegation(row: tuple[Any, ...]) -> DelegationRecord:
    return DelegationRecord(
        delegation_id=str(row[0]),
        tenant_id=str(row[1]),
        agent_id=str(row[2]),
        subject_user_id=str(row[3]),
        active=bool(row[4]),
    )


def _decode_roles(value: object) -> frozenset[str]:
    if not isinstance(value, list) or not all(isinstance(role, str) for role in value):
        raise ValueError("Control Store contains invalid role data")
    return frozenset(value)


def _decode_string_dict(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(child, str) for key, child in value.items()
    ):
        raise ValueError(f"Control Store contains invalid Operator Audit Event {label}")
    return dict(value)


def _required_datetime(value: object, label: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"Control Store contains an invalid {label}")
    return value


def _optional_datetime(value: object, label: str) -> datetime | None:
    if value is None:
        return None
    return _required_datetime(value, label)


def _decode_token(row: tuple[Any, ...]) -> TokenRecord:
    verifier = row[1]
    if not isinstance(verifier, bytes | bytearray | memoryview):
        raise ValueError("Control Store contains an invalid Access Token verifier")
    expires_at = row[8]
    revoked_at = row[9]
    last_used_at = row[10]
    issued_at = row[11]
    if not isinstance(expires_at, datetime):
        raise ValueError("Control Store contains an invalid Access Token expiry")
    if revoked_at is not None and not isinstance(revoked_at, datetime):
        raise ValueError("Control Store contains an invalid Access Token revocation time")
    if last_used_at is not None and not isinstance(last_used_at, datetime):
        raise ValueError("Control Store contains an invalid Access Token use time")
    if not isinstance(issued_at, datetime):
        raise ValueError("Control Store contains an invalid Access Token issue time")
    return TokenRecord(
        token_id=str(row[0]),
        verifier=bytes(verifier),
        session=TenantSession(
            tenant_id=str(row[2]),
            actor_id=str(row[3]),
            actor_kind=PrincipalKind(str(row[4])),
            roles=_decode_roles(row[5]),
            subject_user_id=None if row[6] is None else str(row[6]),
            delegation_id=None if row[7] is None else str(row[7]),
            token_id=str(row[0]),
        ),
        expires_at=expires_at,
        issued_at=issued_at,
        revoked_at=revoked_at,
        last_used_at=last_used_at,
    )


def _decode_operator_token(row: tuple[Any, ...]) -> OperatorTokenRecord:
    verifier = row[1]
    if not isinstance(verifier, bytes | bytearray | memoryview):
        raise ValueError("Control Store contains an invalid Operator Access Token verifier")
    expires_at = row[4]
    revoked_at = row[5]
    last_used_at = row[6]
    issued_at = row[7]
    if not isinstance(expires_at, datetime):
        raise ValueError("Control Store contains an invalid Operator Access Token expiry")
    if revoked_at is not None and not isinstance(revoked_at, datetime):
        raise ValueError("Control Store contains an invalid Operator Access Token revocation time")
    if last_used_at is not None and not isinstance(last_used_at, datetime):
        raise ValueError("Control Store contains an invalid Operator Access Token use time")
    if not isinstance(issued_at, datetime):
        raise ValueError("Control Store contains an invalid Operator Access Token issue time")
    return OperatorTokenRecord(
        token_id=str(row[0]),
        verifier=bytes(verifier),
        session=OperatorSession(
            operator_id=str(row[2]),
            roles=_decode_roles(row[3]),
            token_id=str(row[0]),
        ),
        expires_at=expires_at,
        issued_at=issued_at,
        revoked_at=revoked_at,
        last_used_at=last_used_at,
    )


def _decode_admin_session(row: tuple[Any, ...]) -> AdminSessionRecord:
    verifier = row[1]
    csrf_verifier = row[2]
    if not isinstance(verifier, bytes | bytearray | memoryview):
        raise ValueError("Control Store contains an invalid Admin Session verifier")
    if not isinstance(csrf_verifier, bytes | bytearray | memoryview):
        raise ValueError("Control Store contains an invalid Admin Session CSRF verifier")
    absolute_expires_at = row[6]
    idle_expires_at = row[7]
    revoked_at = row[8]
    last_used_at = row[9]
    issued_at = row[10]
    if not isinstance(absolute_expires_at, datetime):
        raise ValueError("Control Store contains an invalid Admin Session absolute expiry")
    if not isinstance(idle_expires_at, datetime):
        raise ValueError("Control Store contains an invalid Admin Session idle expiry")
    if revoked_at is not None and not isinstance(revoked_at, datetime):
        raise ValueError("Control Store contains an invalid Admin Session revocation time")
    if last_used_at is not None and not isinstance(last_used_at, datetime):
        raise ValueError("Control Store contains an invalid Admin Session use time")
    if not isinstance(issued_at, datetime):
        raise ValueError("Control Store contains an invalid Admin Session issue time")
    return AdminSessionRecord(
        session_id=str(row[0]),
        verifier=bytes(verifier),
        csrf_verifier=bytes(csrf_verifier),
        session=OperatorSession(
            operator_id=str(row[3]),
            roles=_decode_roles(row[5]),
            token_id=None if row[4] is None else str(row[4]),
            session_id=str(row[0]),
        ),
        absolute_expires_at=absolute_expires_at,
        idle_expires_at=idle_expires_at,
        issued_at=issued_at,
        revoked_at=revoked_at,
        last_used_at=last_used_at,
    )


def _decode_route(row: tuple[Any, ...]) -> TenantRouteRecord:
    return TenantRouteRecord(
        tenant_id=str(row[0]),
        neo4j_service_address=str(row[1]),
        neo4j_secret_name=str(row[2]),
        tenant_database_name=str(row[3]),
        tenant_database_role=str(row[4]),
        healthy=bool(row[5]),
    )

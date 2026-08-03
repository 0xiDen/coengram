"""Durable application-owned Agent Run snapshots in each Tenant database."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import RLock
from typing import Any, Protocol
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from agent_memory_service.agents.models import (
    AgentBudget,
    AgentInvocation,
    AgentModelSettings,
    AgentRunEvent,
    AgentRunState,
)
from agent_memory_service.database_barrier import acquire_backup_shared_lock
from agent_memory_service.models import PrincipalKind, TenantSession


class AgentRunSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    tenant_id: str
    actor_id: str
    actor_kind: PrincipalKind
    roles: tuple[str, ...]
    subject_user_id: str | None = None
    delegation_id: str | None = None
    invocation: AgentInvocation
    command: dict[str, Any]
    settings: AgentModelSettings
    budget: AgentBudget
    state: AgentRunState
    model_calls: int
    tool_calls: int
    cost_usd: Decimal
    events: tuple[AgentRunEvent, ...]
    output: dict[str, Any] | None = None
    failure: str | None = None
    resumable: bool = False

    def session(self) -> TenantSession:
        return TenantSession(
            tenant_id=self.tenant_id,
            actor_id=self.actor_id,
            actor_kind=self.actor_kind,
            roles=frozenset(self.roles),
            subject_user_id=self.subject_user_id,
            delegation_id=self.delegation_id,
        )


class AgentRunClaim(BaseModel):
    """A bounded, fenced lease for executing one durable Agent Run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lease_token: str = Field(min_length=1)
    lease_version: int = Field(ge=1)
    expires_at: datetime
    snapshot: AgentRunSnapshot

    @property
    def tenant_id(self) -> str:
        return self.snapshot.tenant_id

    @property
    def run_id(self) -> str:
        return self.snapshot.run_id


class AgentRunLeaseLost(RuntimeError):
    """Raised when a worker attempts to persist through a stale lease."""


class AgentRunCancellationRequested(RuntimeError):
    """Raised when cancellation wins a race with a claimed worker transition."""


class AgentRunRepository(Protocol):
    def create_or_get(self, snapshot: AgentRunSnapshot) -> AgentRunSnapshot: ...

    def save(
        self,
        snapshot: AgentRunSnapshot,
        *,
        claim: AgentRunClaim | None = None,
    ) -> None: ...

    def get(self, tenant_id: str, run_id: str) -> AgentRunSnapshot | None: ...

    def request_cancellation(
        self,
        session: TenantSession,
        run_id: str,
    ) -> AgentRunSnapshot | None: ...

    def get_by_idempotency(
        self,
        tenant_id: str,
        actor_id: str,
        idempotency_key: str,
        *,
        actor_kind: PrincipalKind,
        subject_user_id: str | None = None,
        delegation_id: str | None = None,
    ) -> AgentRunSnapshot | None: ...

    def claim_run(
        self,
        tenant_id: str,
        run_id: str,
        *,
        lease_seconds: int,
    ) -> AgentRunClaim | None: ...

    def claim_runnable(
        self,
        tenant_id: str,
        *,
        lease_seconds: int,
    ) -> AgentRunClaim | None: ...

    def list_runnable(self, tenant_id: str, *, limit: int = 100) -> tuple[str, ...]: ...


class InMemoryAgentRunRepository:
    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._runs: dict[tuple[str, str], AgentRunSnapshot] = {}
        self._idempotency: dict[tuple[str, str, PrincipalKind, str, str, str], str] = {}
        self._leases: dict[tuple[str, str], AgentRunClaim] = {}
        self._lease_versions: dict[tuple[str, str], int] = {}
        self._lock = RLock()
        self._clock = clock or (lambda: datetime.now(UTC))

    def create_or_get(self, snapshot: AgentRunSnapshot) -> AgentRunSnapshot:
        key = _idempotency_context(snapshot)
        with self._lock:
            run_id = self._idempotency.get(key)
            if run_id is not None:
                return self._runs[(snapshot.tenant_id, run_id)]
            self._save(snapshot)
            return snapshot

    def save(
        self,
        snapshot: AgentRunSnapshot,
        *,
        claim: AgentRunClaim | None = None,
    ) -> None:
        with self._lock:
            key = (snapshot.tenant_id, snapshot.run_id)
            active_claim = self._active_claim(key)
            if claim is not None:
                if not _same_lease(active_claim, claim):
                    raise AgentRunLeaseLost("Agent Run lease is no longer owned")
                current = self._runs.get(key)
                if (
                    current is not None
                    and current.state is AgentRunState.CANCEL_REQUESTED
                    and snapshot.state
                    not in {AgentRunState.CANCEL_REQUESTED, AgentRunState.CANCELLED}
                ):
                    raise AgentRunCancellationRequested("Agent Run cancellation was requested")
            elif active_claim is not None and snapshot.state is not AgentRunState.CANCEL_REQUESTED:
                raise AgentRunLeaseLost("Agent Run has an active worker lease")
            self._save(snapshot)
            if snapshot.state.terminal:
                self._leases.pop(key, None)

    def _save(self, snapshot: AgentRunSnapshot) -> None:
        current = self._runs.get((snapshot.tenant_id, snapshot.run_id))
        if current is not None and _reject_state_regression(current.state, snapshot.state):
            return
        self._runs[(snapshot.tenant_id, snapshot.run_id)] = snapshot
        self._idempotency[_idempotency_context(snapshot)] = snapshot.run_id

    def get(self, tenant_id: str, run_id: str) -> AgentRunSnapshot | None:
        with self._lock:
            return self._runs.get((tenant_id, run_id))

    def request_cancellation(
        self,
        session: TenantSession,
        run_id: str,
    ) -> AgentRunSnapshot | None:
        with self._lock:
            key = (session.tenant_id, run_id)
            snapshot = self._runs.get(key)
            if snapshot is None or not _same_run_context(snapshot, session):
                return None
            if snapshot.state.terminal or snapshot.state is AgentRunState.CANCEL_REQUESTED:
                return snapshot
            updated = _with_cancellation_requested(snapshot)
            self._runs[key] = updated
            return updated

    def get_by_idempotency(
        self,
        tenant_id: str,
        actor_id: str,
        idempotency_key: str,
        *,
        actor_kind: PrincipalKind,
        subject_user_id: str | None = None,
        delegation_id: str | None = None,
    ) -> AgentRunSnapshot | None:
        with self._lock:
            run_id = self._idempotency.get(
                (
                    tenant_id,
                    actor_id,
                    actor_kind,
                    subject_user_id or "",
                    delegation_id or "",
                    idempotency_key,
                )
            )
            return None if run_id is None else self._runs[(tenant_id, run_id)]

    def claim_run(
        self,
        tenant_id: str,
        run_id: str,
        *,
        lease_seconds: int,
    ) -> AgentRunClaim | None:
        _require_lease_seconds(lease_seconds)
        with self._lock:
            snapshot = self._runs.get((tenant_id, run_id))
            if snapshot is None or not _is_runnable(snapshot.state):
                return None
            return self._claim(snapshot, lease_seconds)

    def claim_runnable(
        self,
        tenant_id: str,
        *,
        lease_seconds: int,
    ) -> AgentRunClaim | None:
        _require_lease_seconds(lease_seconds)
        with self._lock:
            for snapshot in self._runs.values():
                if snapshot.tenant_id == tenant_id and _is_runnable(snapshot.state):
                    claim = self._claim(snapshot, lease_seconds)
                    if claim is not None:
                        return claim
        return None

    def list_runnable(self, tenant_id: str, *, limit: int = 100) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                snapshot.run_id
                for snapshot in self._runs.values()
                if snapshot.tenant_id == tenant_id
                and _is_runnable(snapshot.state)
                and self._active_claim((snapshot.tenant_id, snapshot.run_id)) is None
            )[:limit]

    def _claim(self, snapshot: AgentRunSnapshot, lease_seconds: int) -> AgentRunClaim | None:
        key = (snapshot.tenant_id, snapshot.run_id)
        if self._active_claim(key) is not None:
            return None
        lease_version = self._lease_versions.get(key, 0) + 1
        claim = AgentRunClaim(
            lease_token=str(uuid4()),
            lease_version=lease_version,
            expires_at=self._clock() + timedelta(seconds=lease_seconds),
            snapshot=snapshot,
        )
        self._lease_versions[key] = lease_version
        self._leases[key] = claim
        return claim

    def _active_claim(self, key: tuple[str, str]) -> AgentRunClaim | None:
        claim = self._leases.get(key)
        if claim is not None and claim.expires_at <= self._clock():
            self._leases.pop(key, None)
            return None
        return claim


class PostgresAgentRunRepository:
    """Store a content-bearing snapshot only in its isolated Tenant database."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)

    def create_or_get(self, snapshot: AgentRunSnapshot) -> AgentRunSnapshot:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                acquire_backup_shared_lock(cursor)
                cursor.execute(
                    """
                    INSERT INTO memory.agent_runs (
                        run_id, tenant_id, actor_id, actor_kind, subject_user_id,
                        delegation_id, idempotency_key, capability, state, snapshot
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    RETURNING snapshot
                    """,
                    (
                        snapshot.run_id,
                        snapshot.tenant_id,
                        snapshot.actor_id,
                        snapshot.actor_kind.value,
                        snapshot.subject_user_id or "",
                        snapshot.delegation_id or "",
                        snapshot.invocation.idempotency_key,
                        snapshot.invocation.capability,
                        snapshot.state.value,
                        Jsonb(snapshot.model_dump(mode="json")),
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        """
                        SELECT snapshot FROM memory.agent_runs
                        WHERE tenant_id = %s
                          AND actor_id = %s
                          AND actor_kind = %s
                          AND subject_user_id = %s
                          AND delegation_id = %s
                          AND idempotency_key = %s
                        """,
                        (
                            snapshot.tenant_id,
                            snapshot.actor_id,
                            snapshot.actor_kind.value,
                            snapshot.subject_user_id or "",
                            snapshot.delegation_id or "",
                            snapshot.invocation.idempotency_key,
                        ),
                    )
                    row = cursor.fetchone()
                if row is None:
                    raise RuntimeError("Agent Run identifier conflicts with an existing run")
        return AgentRunSnapshot.model_validate(row[0])

    def save(
        self,
        snapshot: AgentRunSnapshot,
        *,
        claim: AgentRunClaim | None = None,
    ) -> None:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                acquire_backup_shared_lock(cursor)
                if claim is not None:
                    self._save_claimed(cursor, snapshot, claim)
                    return
                cursor.execute(
                    """
                    INSERT INTO memory.agent_runs (
                        run_id, tenant_id, actor_id, actor_kind, subject_user_id,
                        delegation_id, idempotency_key, capability, state, snapshot
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (run_id) DO UPDATE
                    SET state = EXCLUDED.state,
                        snapshot = EXCLUDED.snapshot,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE memory.agent_runs.state NOT IN (
                              'completed', 'cancelled', 'budget_exhausted', 'failed'
                          )
                      AND NOT (
                          memory.agent_runs.state = 'cancel_requested'
                          AND EXCLUDED.state NOT IN ('cancel_requested', 'cancelled')
                      )
                      AND (
                          memory.agent_runs.lease_token IS NULL
                          OR memory.agent_runs.lease_expires_at <= CURRENT_TIMESTAMP
                          OR EXCLUDED.state = 'cancel_requested'
                      )
                    """,
                    (
                        snapshot.run_id,
                        snapshot.tenant_id,
                        snapshot.actor_id,
                        snapshot.actor_kind.value,
                        snapshot.subject_user_id or "",
                        snapshot.delegation_id or "",
                        snapshot.invocation.idempotency_key,
                        snapshot.invocation.capability,
                        snapshot.state.value,
                        Jsonb(snapshot.model_dump(mode="json")),
                    ),
                )

    def _save_claimed(
        self,
        cursor: psycopg.Cursor[Any],
        snapshot: AgentRunSnapshot,
        claim: AgentRunClaim,
    ) -> None:
        if snapshot.tenant_id != claim.tenant_id or snapshot.run_id != claim.run_id:
            raise AgentRunLeaseLost("Agent Run lease is scoped to a different run")
        cursor.execute(
            """
            UPDATE memory.agent_runs
            SET state = %s,
                snapshot = %s,
                updated_at = CURRENT_TIMESTAMP,
                lease_token = CASE WHEN %s THEN NULL ELSE lease_token END,
                lease_expires_at = CASE WHEN %s THEN NULL ELSE lease_expires_at END
            WHERE tenant_id = %s
              AND run_id = %s
              AND lease_token = %s
              AND lease_version = %s
              AND lease_expires_at > CURRENT_TIMESTAMP
              AND state NOT IN ('completed', 'cancelled', 'budget_exhausted', 'failed')
              AND NOT (
                  state = 'cancel_requested'
                  AND %s NOT IN ('cancel_requested', 'cancelled')
              )
            RETURNING run_id
            """,
            (
                snapshot.state.value,
                Jsonb(snapshot.model_dump(mode="json")),
                snapshot.state.terminal,
                snapshot.state.terminal,
                snapshot.tenant_id,
                snapshot.run_id,
                claim.lease_token,
                claim.lease_version,
                snapshot.state.value,
            ),
        )
        if cursor.fetchone() is not None:
            return
        cursor.execute(
            """
            SELECT state, lease_token, lease_version, lease_expires_at > CURRENT_TIMESTAMP
            FROM memory.agent_runs
            WHERE tenant_id = %s AND run_id = %s
            """,
            (snapshot.tenant_id, snapshot.run_id),
        )
        row = cursor.fetchone()
        if (
            row is not None
            and str(row[0]) == AgentRunState.CANCEL_REQUESTED.value
            and row[1] == claim.lease_token
            and int(row[2]) == claim.lease_version
            and bool(row[3])
        ):
            raise AgentRunCancellationRequested("Agent Run cancellation was requested")
        raise AgentRunLeaseLost("Agent Run lease is no longer owned")

    def get(self, tenant_id: str, run_id: str) -> AgentRunSnapshot | None:
        return self._fetch(
            "SELECT snapshot FROM memory.agent_runs WHERE tenant_id = %s AND run_id = %s",
            (tenant_id, run_id),
        )

    def request_cancellation(
        self,
        session: TenantSession,
        run_id: str,
    ) -> AgentRunSnapshot | None:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                acquire_backup_shared_lock(cursor)
                cursor.execute(
                    """
                    SELECT snapshot FROM memory.agent_runs
                    WHERE tenant_id = %s AND run_id = %s
                    FOR UPDATE
                    """,
                    (session.tenant_id, run_id),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                snapshot = AgentRunSnapshot.model_validate(row[0])
                if not _same_run_context(snapshot, session):
                    return None
                if snapshot.state.terminal or snapshot.state is AgentRunState.CANCEL_REQUESTED:
                    return snapshot
                updated = _with_cancellation_requested(snapshot)
                cursor.execute(
                    """
                    UPDATE memory.agent_runs
                    SET state = %s,
                        snapshot = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND run_id = %s
                    """,
                    (
                        updated.state.value,
                        Jsonb(updated.model_dump(mode="json")),
                        updated.tenant_id,
                        updated.run_id,
                    ),
                )
                return updated

    def get_by_idempotency(
        self,
        tenant_id: str,
        actor_id: str,
        idempotency_key: str,
        *,
        actor_kind: PrincipalKind,
        subject_user_id: str | None = None,
        delegation_id: str | None = None,
    ) -> AgentRunSnapshot | None:
        return self._fetch(
            """
            SELECT snapshot FROM memory.agent_runs
            WHERE tenant_id = %s
              AND actor_id = %s
              AND actor_kind = %s
              AND subject_user_id = %s
              AND delegation_id = %s
              AND idempotency_key = %s
            """,
            (
                tenant_id,
                actor_id,
                actor_kind.value,
                subject_user_id or "",
                delegation_id or "",
                idempotency_key,
            ),
        )

    def claim_run(
        self,
        tenant_id: str,
        run_id: str,
        *,
        lease_seconds: int,
    ) -> AgentRunClaim | None:
        return self._claim(tenant_id, lease_seconds=lease_seconds, run_id=run_id)

    def claim_runnable(
        self,
        tenant_id: str,
        *,
        lease_seconds: int,
    ) -> AgentRunClaim | None:
        return self._claim(tenant_id, lease_seconds=lease_seconds)

    def _claim(
        self,
        tenant_id: str,
        *,
        lease_seconds: int,
        run_id: str | None = None,
    ) -> AgentRunClaim | None:
        _require_lease_seconds(lease_seconds)
        lease_token = str(uuid4())
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                acquire_backup_shared_lock(cursor)
                cursor.execute(
                    """
                    WITH candidate AS (
                        SELECT run_id
                        FROM memory.agent_runs
                        WHERE tenant_id = %s
                          AND state IN ('queued', 'running', 'cancel_requested')
                          AND (lease_token IS NULL OR lease_expires_at <= CURRENT_TIMESTAMP)
                          AND (%s::text IS NULL OR run_id = %s)
                        ORDER BY created_at, run_id
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE memory.agent_runs AS run
                    SET lease_token = %s,
                        lease_version = run.lease_version + 1,
                        lease_expires_at = CURRENT_TIMESTAMP + make_interval(secs => %s)
                    FROM candidate
                    WHERE run.run_id = candidate.run_id
                    RETURNING run.snapshot, run.lease_version, run.lease_expires_at
                    """,
                    (tenant_id, run_id, run_id, lease_token, lease_seconds),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return AgentRunClaim(
            lease_token=lease_token,
            lease_version=int(row[1]),
            expires_at=row[2],
            snapshot=AgentRunSnapshot.model_validate(row[0]),
        )

    def list_runnable(self, tenant_id: str, *, limit: int = 100) -> tuple[str, ...]:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT run_id FROM memory.agent_runs
                    WHERE tenant_id = %s
                      AND state IN ('queued', 'running', 'cancel_requested')
                      AND (lease_token IS NULL OR lease_expires_at <= CURRENT_TIMESTAMP)
                    ORDER BY created_at, run_id
                    LIMIT %s
                    """,
                    (tenant_id, limit),
                )
                return tuple(str(row[0]) for row in cursor.fetchall())

    def _fetch(self, statement: str, parameters: tuple[object, ...]) -> AgentRunSnapshot | None:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(statement, parameters)
                row = cursor.fetchone()
        return None if row is None else AgentRunSnapshot.model_validate(row[0])


def _reject_state_regression(current: AgentRunState, incoming: AgentRunState) -> bool:
    if current.terminal:
        return True
    return current is AgentRunState.CANCEL_REQUESTED and incoming not in {
        AgentRunState.CANCEL_REQUESTED,
        AgentRunState.CANCELLED,
    }


def _idempotency_context(
    snapshot: AgentRunSnapshot,
) -> tuple[str, str, PrincipalKind, str, str, str]:
    return (
        snapshot.tenant_id,
        snapshot.actor_id,
        snapshot.actor_kind,
        snapshot.subject_user_id or "",
        snapshot.delegation_id or "",
        snapshot.invocation.idempotency_key,
    )


def _same_run_context(snapshot: AgentRunSnapshot, session: TenantSession) -> bool:
    return (
        snapshot.tenant_id,
        snapshot.actor_id,
        snapshot.actor_kind,
        snapshot.subject_user_id,
        snapshot.delegation_id,
    ) == (
        session.tenant_id,
        session.actor_id,
        session.actor_kind,
        session.subject_user_id,
        session.delegation_id,
    )


def _with_cancellation_requested(snapshot: AgentRunSnapshot) -> AgentRunSnapshot:
    event = AgentRunEvent(
        sequence=len(snapshot.events) + 1,
        type="agent.run.cancellation_requested",
        data={},
    )
    return snapshot.model_copy(
        update={
            "state": AgentRunState.CANCEL_REQUESTED,
            "events": (*snapshot.events, event),
        }
    )


def _is_runnable(state: AgentRunState) -> bool:
    return state in {
        AgentRunState.QUEUED,
        AgentRunState.RUNNING,
        AgentRunState.CANCEL_REQUESTED,
    }


def _same_lease(current: AgentRunClaim | None, expected: AgentRunClaim) -> bool:
    return (
        current is not None
        and current.lease_token == expected.lease_token
        and current.lease_version == expected.lease_version
    )


def _require_lease_seconds(lease_seconds: int) -> None:
    if not 1 <= lease_seconds <= 900:
        raise ValueError("Agent Run lease must be between 1 and 900 seconds")

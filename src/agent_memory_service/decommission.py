"""Guarded, resumable tenant decommissioning.

No method in this module invokes a shell or accepts an unscoped deletion target.
Every external effect receives an immutable :class:`TenantResourceIdentity`, and
every successful destructive step is persisted before the next one begins.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_GRACE_PERIOD = timedelta(days=30)
DEFAULT_BACKUP_MAX_AGE = timedelta(hours=24)
DEFAULT_POST_DESTRUCTION_RETENTION = timedelta(days=30)


class DecommissionError(RuntimeError):
    """Base error for a rejected or failed decommission transition."""


class DecommissionConflict(DecommissionError):
    """The requested operation conflicts with persisted state or identity."""


class DecommissionNotReady(DecommissionError):
    """A required approval, recovery point, or health check has not passed."""


class DecommissionOperationFailed(DecommissionError):
    """An injected external operation failed and can be retried safely."""

    def __init__(self, action: str) -> None:
        super().__init__(f"decommission action failed: {action}")
        self.action = action


class DecommissionState(StrEnum):
    SUSPENDING = "suspending"
    SUSPENDED = "suspended"
    GRACE_PERIOD = "grace-period"
    FINALIZING = "finalizing"
    CANCELLED = "cancelled"


class SuspensionStep(StrEnum):
    SUSPEND_SESSIONS = "suspend-sessions"
    BLOCK_TOKEN_ISSUANCE = "block-token-issuance"
    REVOKE_ACTIVE_CREDENTIALS = "revoke-active-credentials"


class DestructionStep(StrEnum):
    REMOVE_COMPOSE_PROJECT = "remove-compose-project"
    REMOVE_NEO4J_VOLUME = "remove-neo4j-volume"
    REMOVE_TENANT_DATABASE_AND_ROLE = "remove-tenant-database-and-role"
    REMOVE_ROUTE = "remove-route"
    REMOVE_TENANT_SECRET_FILES = "remove-tenant-secret-files"
    PURGE_TENANT_DOMAIN_RECORDS = "purge-tenant-domain-records"


class ProtectionKind(StrEnum):
    BACKUP = "backup"
    ACCEPTED_EXPORT = "accepted-export"


class TenantResourceIdentity(BaseModel):
    """Exact tenant-owned targets captured before any destructive operation."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    tenant_id: str = Field(min_length=3, max_length=40, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    compose_project: str = Field(
        min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
    )
    neo4j_volume: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    database_name: str = Field(min_length=1, max_length=63, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    database_role: str = Field(min_length=1, max_length=63, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    route_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    tenant_secret_ref: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def validate_tenant_derived_targets(self) -> TenantResourceIdentity:
        normalized = self.tenant_id.replace("-", "_").replace(".", "_")
        if self.compose_project != f"memory-tenant-{self.tenant_id}":
            raise ValueError("compose_project must be derived from tenant_id")
        if self.neo4j_volume != f"memory-tenant-{self.tenant_id}-neo4j-data":
            raise ValueError("neo4j_volume must be derived from tenant_id")
        if self.database_name != f"tenant_{normalized}":
            raise ValueError("database_name must be derived from tenant_id")
        if self.database_role != f"tenant_{normalized}_rw":
            raise ValueError("database_role must be derived from tenant_id")
        if self.route_id != f"route:{self.tenant_id}":
            raise ValueError("route_id must be derived from tenant_id")
        secret_path = PurePosixPath(self.tenant_secret_ref)
        if (
            not secret_path.is_absolute()
            or ".." in secret_path.parts
            or secret_path.name != self.tenant_id
        ):
            raise ValueError("tenant_secret_ref must be an exact absolute tenant path")
        return self


class ProtectionEvidence(BaseModel):
    """Content-free proof that recovery data exists outside the live tenant."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ProtectionKind
    artifact_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    created_at: datetime
    verified_at: datetime
    verified_by: str = Field(min_length=1, max_length=128)
    integrity_verified: bool
    complete: bool
    export_explicitly_accepted: bool = False

    @model_validator(mode="after")
    def validate_times(self) -> ProtectionEvidence:
        if self.created_at.tzinfo is None or self.verified_at.tzinfo is None:
            raise ValueError("evidence timestamps must be timezone-aware")
        if self.verified_at < self.created_at:
            raise ValueError("verified_at cannot precede created_at")
        return self


class DecommissionRecord(BaseModel):
    """Persisted state while the tenant still has live or recoverable resources."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str = Field(min_length=1, max_length=160)
    resources: TenantResourceIdentity
    requested_by: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=500)
    requested_at: datetime
    protection_policy: str = Field(min_length=1, max_length=160)
    state: DecommissionState
    suspension_steps: tuple[SuspensionStep, ...] = ()
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    grace_ends_at: datetime | None = None
    protection_evidence: ProtectionEvidence | None = None
    destruction_steps: tuple[DestructionStep, ...] = ()
    cancelled_by: str | None = None
    cancelled_at: datetime | None = None
    last_failed_action: str | None = None
    revision: int = Field(default=0, ge=0)


class DecommissionTombstone(BaseModel):
    """The only tenant record retained after destruction; contains no domain data."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str
    tenant_id: str
    requested_by: str
    confirmed_by: str
    destroyed_at: datetime
    status: str = "destroyed"


class DecommissionRequestDocument(BaseModel):
    """Versioned operator input for the non-destructive request transition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    request_id: str = Field(min_length=1, max_length=160)
    resources: TenantResourceIdentity
    actor_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=500)
    requested_at: datetime
    protection_policy: Literal["recent-valid-backup-or-accepted-export"] = (
        "recent-valid-backup-or-accepted-export"
    )


class DecommissionConfirmationDocument(BaseModel):
    """Versioned second-operator confirmation input."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    request_id: str = Field(min_length=1, max_length=160)
    tenant_id: str = Field(min_length=3, max_length=40)
    actor_id: str = Field(min_length=1, max_length=128)
    evidence: ProtectionEvidence
    confirmed_at: datetime


class DecommissionCancellationDocument(BaseModel):
    """Versioned cancellation input used only during the grace period."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    request_id: str = Field(min_length=1, max_length=160)
    tenant_id: str = Field(min_length=3, max_length=40)
    actor_id: str = Field(min_length=1, max_length=128)
    cancelled_at: datetime


class DecommissionFinalizationDocument(BaseModel):
    """Versioned finalization input used only after the exact grace deadline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    request_id: str = Field(min_length=1, max_length=160)
    tenant_id: str = Field(min_length=3, max_length=40)
    finalized_at: datetime


class DecommissionStore(Protocol):
    """Durable state store; each save must be atomic."""

    def get_record(self, request_id: str) -> DecommissionRecord | None: ...

    def find_open_for_tenant(self, tenant_id: str) -> DecommissionRecord | None: ...

    def save_record(self, record: DecommissionRecord) -> DecommissionRecord: ...

    def get_tombstone(self, request_id: str) -> DecommissionTombstone | None: ...

    def replace_with_tombstone(
        self, record: DecommissionRecord, tombstone: DecommissionTombstone
    ) -> None: ...


class TenantAccessPort(Protocol):
    """Tenant-scoped access operations; none of these delete tenant data."""

    def suspend_sessions(self, resources: TenantResourceIdentity) -> None: ...

    def block_token_issuance(self, resources: TenantResourceIdentity) -> None: ...

    def revoke_active_credentials(self, resources: TenantResourceIdentity) -> None: ...

    def reactivate_without_issuing_tokens(self, resources: TenantResourceIdentity) -> None: ...


class ProtectionEvidencePort(Protocol):
    """Confirms the referenced encrypted backup/export exists and is readable."""

    def verify(self, tenant_id: str, evidence: ProtectionEvidence) -> bool: ...


class ReactivationVerificationPort(Protocol):
    """Read-only checks required before cancelling during the grace period."""

    def verify_route(self, resources: TenantResourceIdentity) -> bool: ...

    def verify_schema(self, resources: TenantResourceIdentity) -> bool: ...

    def verify_isolation(self, resources: TenantResourceIdentity) -> bool: ...

    def verify_credentials(self, resources: TenantResourceIdentity) -> bool: ...

    def verify_health(self, resources: TenantResourceIdentity) -> bool: ...


class TenantDestructionPort(Protocol):
    """Exact, idempotent removal operations for tenant-owned resources only."""

    def remove_compose_project(self, resources: TenantResourceIdentity) -> None: ...

    def remove_neo4j_volume(self, resources: TenantResourceIdentity) -> None: ...

    def remove_tenant_database_and_role(self, resources: TenantResourceIdentity) -> None: ...

    def remove_route(self, resources: TenantResourceIdentity) -> None: ...

    def remove_tenant_secret_files(self, resources: TenantResourceIdentity) -> None: ...

    def purge_tenant_domain_records(self, resources: TenantResourceIdentity) -> None: ...


class DecommissionService:
    """Apply the two-operator, 30-day tenant destruction protocol."""

    _SUSPENSION_ORDER = (
        SuspensionStep.SUSPEND_SESSIONS,
        SuspensionStep.BLOCK_TOKEN_ISSUANCE,
        SuspensionStep.REVOKE_ACTIVE_CREDENTIALS,
    )
    _DESTRUCTION_ORDER = (
        DestructionStep.REMOVE_COMPOSE_PROJECT,
        DestructionStep.REMOVE_NEO4J_VOLUME,
        DestructionStep.REMOVE_TENANT_DATABASE_AND_ROLE,
        DestructionStep.REMOVE_ROUTE,
        DestructionStep.REMOVE_TENANT_SECRET_FILES,
        DestructionStep.PURGE_TENANT_DOMAIN_RECORDS,
    )

    def __init__(
        self,
        *,
        store: DecommissionStore,
        access: TenantAccessPort,
        protection: ProtectionEvidencePort,
        reactivation: ReactivationVerificationPort,
        destruction: TenantDestructionPort,
        grace_period: timedelta = DEFAULT_GRACE_PERIOD,
        backup_max_age: timedelta = DEFAULT_BACKUP_MAX_AGE,
    ) -> None:
        self._store = store
        self._access = access
        self._protection = protection
        self._reactivation = reactivation
        self._destruction = destruction
        self._grace_period = grace_period
        self._backup_max_age = backup_max_age

    def request(
        self,
        *,
        request_id: str,
        resources: TenantResourceIdentity,
        actor_id: str,
        reason: str,
        requested_at: datetime,
        protection_policy: str = "recent-valid-backup-or-accepted-export",
    ) -> DecommissionRecord:
        """Persist an immutable request, then suspend access without deleting data."""

        if requested_at.tzinfo is None:
            raise ValueError("requested_at must be timezone-aware")
        existing = self._store.get_record(request_id)
        if existing is None:
            tombstone = self._store.get_tombstone(request_id)
            if tombstone is not None:
                raise DecommissionConflict("request has already been finalized")
            open_record = self._store.find_open_for_tenant(resources.tenant_id)
            if open_record is not None:
                raise DecommissionConflict("tenant already has an open decommission request")
            record = DecommissionRecord(
                request_id=request_id,
                resources=resources,
                requested_by=actor_id,
                reason=reason,
                requested_at=requested_at,
                protection_policy=protection_policy,
                state=DecommissionState.SUSPENDING,
            )
            record = self._store.save_record(record)
        else:
            record = existing
            if (
                record.resources != resources
                or record.requested_by != actor_id
                or record.reason != reason
                or record.protection_policy != protection_policy
            ):
                raise DecommissionConflict("idempotent request payload does not match")
            if record.state is not DecommissionState.SUSPENDING:
                return record

        actions = {
            SuspensionStep.SUSPEND_SESSIONS: self._access.suspend_sessions,
            SuspensionStep.BLOCK_TOKEN_ISSUANCE: self._access.block_token_issuance,
            SuspensionStep.REVOKE_ACTIVE_CREDENTIALS: self._access.revoke_active_credentials,
        }
        completed = list(record.suspension_steps)
        for step in self._SUSPENSION_ORDER:
            if step in completed:
                continue
            try:
                actions[step](resources)
            except Exception as error:
                failed = record.model_copy(
                    update={
                        "suspension_steps": tuple(completed),
                        "last_failed_action": step.value,
                    }
                )
                self._store.save_record(failed)
                raise DecommissionOperationFailed(step.value) from error
            completed.append(step)
            record = record.model_copy(
                update={
                    "suspension_steps": tuple(completed),
                    "last_failed_action": None,
                }
            )
            record = self._store.save_record(record)

        record = record.model_copy(
            update={"state": DecommissionState.SUSPENDED, "last_failed_action": None}
        )
        return self._store.save_record(record)

    def confirm(
        self,
        *,
        request_id: str,
        tenant_id: str,
        actor_id: str,
        evidence: ProtectionEvidence,
        confirmed_at: datetime,
    ) -> DecommissionRecord:
        """Require a second operator and recovery evidence before starting grace."""

        record = self._require_exact_record(request_id, tenant_id)
        if actor_id == record.requested_by:
            raise DecommissionConflict("requester cannot confirm their own request")
        if record.state is DecommissionState.GRACE_PERIOD:
            if record.confirmed_by == actor_id and record.protection_evidence == evidence:
                return record
            raise DecommissionConflict("request already has a different confirmation")
        if record.state is not DecommissionState.SUSPENDED:
            raise DecommissionConflict(f"cannot confirm request in {record.state.value}")
        if confirmed_at.tzinfo is None:
            raise ValueError("confirmed_at must be timezone-aware")
        if confirmed_at < record.requested_at:
            raise DecommissionConflict("confirmation cannot precede the request")
        self._validate_protection(record, evidence, confirmed_at)

        confirmed = record.model_copy(
            update={
                "state": DecommissionState.GRACE_PERIOD,
                "confirmed_by": actor_id,
                "confirmed_at": confirmed_at,
                "grace_ends_at": confirmed_at + self._grace_period,
                "protection_evidence": evidence,
                "last_failed_action": None,
            }
        )
        return self._store.save_record(confirmed)

    def cancel(
        self,
        *,
        request_id: str,
        tenant_id: str,
        actor_id: str,
        cancelled_at: datetime,
    ) -> DecommissionRecord:
        """Reactivate only after all checks; this never issues replacement tokens."""

        record = self._require_exact_record(request_id, tenant_id)
        if record.state is DecommissionState.CANCELLED:
            return record
        if record.state is not DecommissionState.GRACE_PERIOD:
            raise DecommissionConflict(f"cannot cancel request in {record.state.value}")
        if cancelled_at.tzinfo is None:
            raise ValueError("cancelled_at must be timezone-aware")
        if record.grace_ends_at is None or cancelled_at >= record.grace_ends_at:
            raise DecommissionConflict("cancellation is allowed only during the grace period")

        checks = (
            ("verify-route", self._reactivation.verify_route),
            ("verify-schema", self._reactivation.verify_schema),
            ("verify-isolation", self._reactivation.verify_isolation),
            ("verify-credentials", self._reactivation.verify_credentials),
            ("verify-health", self._reactivation.verify_health),
        )
        for name, check in checks:
            try:
                passed = check(record.resources)
            except Exception as error:
                self._save_failure(record, name)
                raise DecommissionOperationFailed(name) from error
            if not passed:
                self._save_failure(record, name)
                raise DecommissionNotReady(f"reactivation check failed: {name}")
        try:
            self._access.reactivate_without_issuing_tokens(record.resources)
        except Exception as error:
            self._save_failure(record, "reactivate-without-tokens")
            raise DecommissionOperationFailed("reactivate-without-tokens") from error

        cancelled = record.model_copy(
            update={
                "state": DecommissionState.CANCELLED,
                "cancelled_by": actor_id,
                "cancelled_at": cancelled_at,
                "last_failed_action": None,
            }
        )
        return self._store.save_record(cancelled)

    def finalize(
        self,
        *,
        request_id: str,
        tenant_id: str,
        finalized_at: datetime,
    ) -> DecommissionTombstone:
        """After grace, remove only exact tenant resources and retain a tombstone."""

        tombstone = self._store.get_tombstone(request_id)
        if tombstone is not None:
            if tombstone.tenant_id != tenant_id:
                raise DecommissionConflict("tenant_id does not match finalized request")
            return tombstone
        record = self._require_exact_record(request_id, tenant_id)
        if finalized_at.tzinfo is None:
            raise ValueError("finalized_at must be timezone-aware")
        if record.state is DecommissionState.GRACE_PERIOD:
            if record.grace_ends_at is None or finalized_at < record.grace_ends_at:
                raise DecommissionNotReady("30-day grace period has not elapsed")
            record = record.model_copy(
                update={"state": DecommissionState.FINALIZING, "last_failed_action": None}
            )
            record = self._store.save_record(record)
        elif record.state is not DecommissionState.FINALIZING:
            raise DecommissionConflict(f"cannot finalize request in {record.state.value}")

        actions = {
            DestructionStep.REMOVE_COMPOSE_PROJECT: self._destruction.remove_compose_project,
            DestructionStep.REMOVE_NEO4J_VOLUME: self._destruction.remove_neo4j_volume,
            DestructionStep.REMOVE_TENANT_DATABASE_AND_ROLE: (
                self._destruction.remove_tenant_database_and_role
            ),
            DestructionStep.REMOVE_ROUTE: self._destruction.remove_route,
            DestructionStep.REMOVE_TENANT_SECRET_FILES: (
                self._destruction.remove_tenant_secret_files
            ),
            DestructionStep.PURGE_TENANT_DOMAIN_RECORDS: (
                self._destruction.purge_tenant_domain_records
            ),
        }
        completed = list(record.destruction_steps)
        for step in self._DESTRUCTION_ORDER:
            if step in completed:
                continue
            try:
                actions[step](record.resources)
            except Exception as error:
                failed = record.model_copy(
                    update={
                        "destruction_steps": tuple(completed),
                        "last_failed_action": step.value,
                    }
                )
                self._store.save_record(failed)
                raise DecommissionOperationFailed(step.value) from error
            completed.append(step)
            record = record.model_copy(
                update={
                    "destruction_steps": tuple(completed),
                    "last_failed_action": None,
                }
            )
            record = self._store.save_record(record)

        if record.confirmed_by is None:
            raise DecommissionConflict("finalization requires a confirming operator")
        tombstone = DecommissionTombstone(
            request_id=record.request_id,
            tenant_id=record.resources.tenant_id,
            requested_by=record.requested_by,
            confirmed_by=record.confirmed_by,
            destroyed_at=finalized_at,
        )
        self._store.replace_with_tombstone(record, tombstone)
        return tombstone

    def _require_exact_record(self, request_id: str, tenant_id: str) -> DecommissionRecord:
        record = self._store.get_record(request_id)
        if record is None:
            raise DecommissionConflict("decommission request does not exist")
        if record.resources.tenant_id != tenant_id:
            raise DecommissionConflict("tenant_id does not match request")
        return record

    def _validate_protection(
        self,
        record: DecommissionRecord,
        evidence: ProtectionEvidence,
        now: datetime,
    ) -> None:
        if not evidence.complete or not evidence.integrity_verified:
            raise DecommissionNotReady("recovery evidence is incomplete or unverified")
        if evidence.kind is ProtectionKind.BACKUP:
            if now - evidence.created_at > self._backup_max_age:
                raise DecommissionNotReady("backup is older than the accepted recovery window")
            if evidence.created_at > now:
                raise DecommissionNotReady("backup evidence is from the future")
        elif not evidence.export_explicitly_accepted:
            raise DecommissionNotReady("export policy has not been explicitly accepted")
        if evidence.verified_at > now:
            raise DecommissionNotReady("evidence verification is from the future")
        try:
            valid = self._protection.verify(record.resources.tenant_id, evidence)
        except Exception as error:
            raise DecommissionOperationFailed("verify-protection-evidence") from error
        if not valid:
            raise DecommissionNotReady("recovery artifact could not be verified")

    def _save_failure(self, record: DecommissionRecord, action: str) -> None:
        self._store.save_record(record.model_copy(update={"last_failed_action": action}))


class InMemoryDecommissionStore:
    """Small reference adapter useful for tests and local operator tooling."""

    def __init__(self) -> None:
        self.records: dict[str, DecommissionRecord] = {}
        self.tombstones: dict[str, DecommissionTombstone] = {}

    def get_record(self, request_id: str) -> DecommissionRecord | None:
        return self.records.get(request_id)

    def find_open_for_tenant(self, tenant_id: str) -> DecommissionRecord | None:
        return next(
            (
                record
                for record in self.records.values()
                if record.resources.tenant_id == tenant_id
                and record.state is not DecommissionState.CANCELLED
            ),
            None,
        )

    def save_record(self, record: DecommissionRecord) -> DecommissionRecord:
        current = self.records.get(record.request_id)
        if current is None:
            if record.revision != 0:
                raise DecommissionConflict("new request must start at revision zero")
        elif current.revision != record.revision:
            raise DecommissionConflict("decommission request changed concurrently")
        saved = record.model_copy(update={"revision": record.revision + 1})
        self.records[record.request_id] = saved
        return saved

    def get_tombstone(self, request_id: str) -> DecommissionTombstone | None:
        return self.tombstones.get(request_id)

    def replace_with_tombstone(
        self, record: DecommissionRecord, tombstone: DecommissionTombstone
    ) -> None:
        current = self.records.get(record.request_id)
        if current != record:
            raise DecommissionConflict("record changed before tombstone replacement")
        self.records.pop(record.request_id)
        self.tombstones[tombstone.request_id] = tombstone

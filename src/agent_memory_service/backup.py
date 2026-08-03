"""Backup, retention, and isolated-restore safety contracts.

The module deliberately owns orchestration, validation, and command construction,
but not a remote object store or a scheduler.  Those are deployment concerns.  All
external effects are injected so a restore drill can prove the same contract used
in production without running destructive commands in the test process.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_memory_service.agents.models import AgentRunEvent
from agent_memory_service.lifecycle import ErasureTombstone
from agent_memory_service.models import MemoryState, MutationState, PrivateMemoryInspection

SHA256_LENGTH = 64
DEFAULT_RPO = timedelta(hours=24)
DEFAULT_RTO = timedelta(hours=4)


class BackupError(RuntimeError):
    """Base error for backup and restore safety failures."""


class BackupValidationError(BackupError):
    """An artifact or manifest is incomplete, corrupt, or incompatible."""


class EncryptionCommandFailed(BackupError):
    """The external encryption/decryption tool rejected the operation."""


class RestoreSafetyError(BackupError):
    """A restore was rejected because its target or verification is unsafe."""


class NativeActiveGraphEvent(Protocol):
    id: str
    type: str
    payload: dict[str, Any]
    actor: str | None
    frame_id: str | None
    caused_by: str | None
    timestamp: str


class BackupStore(StrEnum):
    """Stores required for a complete tenant recovery point."""

    CONTROL_POSTGRES = "control-postgres"
    TENANT_POSTGRES = "tenant-postgres"
    TENANT_NEO4J = "tenant-neo4j"


class EncryptionMetadata(BaseModel):
    """Non-secret information needed to select the decryption identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scheme: Literal["age-x25519"] = "age-x25519"
    key_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")


class BackupArtifactManifest(BaseModel):
    """Content-free description of one encrypted backup object."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    artifact_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    object_key: str = Field(min_length=1, max_length=512)
    store: BackupStore
    tenant_id: str | None = Field(default=None, min_length=1, max_length=128)
    store_version: str = Field(min_length=1, max_length=80)
    schema_version: str = Field(min_length=1, max_length=80)
    created_at: datetime
    ciphertext_sha256: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    ciphertext_bytes: int = Field(gt=0)
    encryption: EncryptionMetadata
    complete: bool

    @model_validator(mode="after")
    def validate_scope_and_checksum(self) -> BackupArtifactManifest:
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        if self.store is BackupStore.CONTROL_POSTGRES and self.tenant_id is not None:
            raise ValueError("the control backup cannot be tenant-scoped")
        if self.store is not BackupStore.CONTROL_POSTGRES and self.tenant_id is None:
            raise ValueError("tenant store backups require tenant_id")
        try:
            bytes.fromhex(self.ciphertext_sha256)
        except ValueError as error:
            raise ValueError("ciphertext_sha256 must be hexadecimal") from error
        return self


class RestoreProofTarget(BaseModel):
    """A complete content-safe proof set with an optional representative."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    count: int = Field(ge=0)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    representative_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_representative(self) -> RestoreProofTarget:
        if (self.count > 0) != (self.representative_digest is not None):
            raise ValueError("Non-empty proof targets require one representative digest")
        return self


class PrivateMemoryRestoreProof(BaseModel):
    """Representative proofs for the three durable Private Memory states."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    active: RestoreProofTarget
    correction_chain: RestoreProofTarget
    completed_erasure: RestoreProofTarget


class RestoreExpectations(BaseModel):
    """Content-free proof targets captured from live stores before backup."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    tenant_knowledge_digests: tuple[str, ...] = Field(min_length=1, max_length=10_000)
    governance_candidate_count: int = Field(ge=0)
    governance_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    private_memory: PrivateMemoryRestoreProof
    agent_run_count: int = Field(ge=0)
    representative_agent_run_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
    )
    representative_agent_run_digest: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    representative_activegraph_event_count: int | None = Field(default=None, ge=1)
    representative_activegraph_event_digest: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )

    @model_validator(mode="after")
    def validate_representative_run(self) -> RestoreExpectations:
        try:
            decoded = tuple(bytes.fromhex(value) for value in self.tenant_knowledge_digests)
        except ValueError as error:
            raise ValueError("tenant knowledge digests must be hexadecimal") from error
        if any(len(value) != 32 for value in decoded) or any(
            value != value.lower() for value in self.tenant_knowledge_digests
        ):
            raise ValueError("tenant knowledge digests must be SHA-256 values")
        if len(set(self.tenant_knowledge_digests)) != len(self.tenant_knowledge_digests):
            raise ValueError("tenant knowledge digests must be unique")
        representative_fields = (
            self.representative_agent_run_id,
            self.representative_agent_run_digest,
            self.representative_activegraph_event_count,
            self.representative_activegraph_event_digest,
        )
        representative_present = self.representative_agent_run_id is not None
        missing_representative_fields = sum(value is None for value in representative_fields)
        if missing_representative_fields not in {0, len(representative_fields)}:
            raise ValueError("Agent Run and ActiveGraph representative proofs must be paired")
        if (self.agent_run_count > 0) != representative_present:
            raise ValueError("Agent Run expectations require one representative when non-empty")
        return self


class BackupManifest(BaseModel):
    """A complete tenant recovery point and its content-free routing metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[2] = 2
    backup_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    tenant_id: str = Field(min_length=1, max_length=128)
    created_at: datetime
    database_name: str = Field(min_length=1, max_length=128)
    database_role: str = Field(min_length=1, max_length=128)
    neo4j_service_name: str = Field(min_length=1, max_length=128)
    expectations: RestoreExpectations
    artifacts: tuple[BackupArtifactManifest, ...]
    complete: bool

    @model_validator(mode="after")
    def validate_recovery_point(self) -> BackupManifest:
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        by_store = {artifact.store: artifact for artifact in self.artifacts}
        required = set(BackupStore)
        if set(by_store) != required or len(self.artifacts) != len(required):
            raise ValueError("manifest requires exactly one artifact for every required store")
        for artifact in self.artifacts:
            if artifact.store is not BackupStore.CONTROL_POSTGRES:
                if artifact.tenant_id != self.tenant_id:
                    raise ValueError("tenant artifact scope does not match manifest")
            if artifact.created_at > self.created_at:
                raise ValueError("artifact cannot be newer than its manifest")
        if self.complete and not all(artifact.complete for artifact in self.artifacts):
            raise ValueError("a complete manifest cannot contain incomplete artifacts")
        return self


def content_safe_digest(value: object) -> str:
    """Hash canonical JSON without copying restored domain content into evidence."""

    document = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(document).hexdigest()


def private_memory_item_digest(item: PrivateMemoryInspection) -> str:
    """Digest one visible, fully applied active Private Memory inspection."""

    if (
        item.state is not MemoryState.ACTIVE
        or item.mutation_state is not MutationState.APPLIED
        or item.content is None
        or item.kind is None
        or item.confidence is None
    ):
        raise ValueError("Active Private Memory proof requires visible active content")
    return content_safe_digest(
        {
            "proof": "active_private_memory",
            "inspection": item.model_dump(mode="json"),
        }
    )


def private_memory_correction_digest(
    ancestor: PrivateMemoryInspection,
    replacement: PrivateMemoryInspection,
) -> str:
    """Digest a visible correction edge while proving its ancestor was superseded."""

    if (
        ancestor.owner_principal_id != replacement.owner_principal_id
        or ancestor.state is not MemoryState.SUPERSEDED
        or ancestor.mutation_state is not MutationState.APPLIED
        or replacement.mutation_state is not MutationState.APPLIED
        or ancestor.content is None
        or replacement.content is None
        or replacement.supersedes_id != ancestor.id
        or replacement.state is not MemoryState.ACTIVE
    ):
        raise ValueError("Private Memory correction proof is not a coherent chain")
    return content_safe_digest(
        {
            "proof": "private_memory_correction_chain",
            "ancestor": ancestor.model_dump(mode="json"),
            "replacement": replacement.model_dump(mode="json"),
        }
    )


def private_memory_erasure_digest(
    inspection: PrivateMemoryInspection,
    tombstone: ErasureTombstone,
) -> str:
    """Digest a completed tombstone paired with a content-free erased inspection."""

    if (
        inspection.owner_principal_id != tombstone.owner_principal_id
        or inspection.id != tombstone.memory_id
        or inspection.state is not MemoryState.ERASED
        or inspection.mutation_state is not MutationState.APPLIED
        or inspection.content is not None
        or inspection.kind is not None
        or inspection.confidence is not None
        or inspection.supersedes_id is not None
    ):
        raise ValueError("Completed erasure proof must be content-free and coherent")
    return content_safe_digest(
        {
            "proof": "completed_private_memory_erasure",
            "inspection": inspection.model_dump(mode="json"),
            "tombstone": tombstone.model_dump(mode="json"),
        }
    )


def activegraph_event_sequence_digest(events: Sequence[NativeActiveGraphEvent]) -> str:
    """Digest the exact native ActiveGraph sequence without exposing event payloads."""

    return content_safe_digest(
        [
            {
                "id": event.id,
                "type": event.type,
                "payload": event.payload,
                "actor": event.actor,
                "frame_id": event.frame_id,
                "caused_by": event.caused_by,
                "timestamp": event.timestamp,
            }
            for event in events
        ]
    )


def projected_agent_run_events(
    native_events: Sequence[NativeActiveGraphEvent],
) -> tuple[AgentRunEvent, ...]:
    """Decode the application-owned event projection from a native ActiveGraph log."""

    projected: list[AgentRunEvent] = []
    for native_event in native_events:
        payload = native_event.payload
        if "agent_memory_run_event" not in payload:
            continue
        envelope = payload["agent_memory_run_event"]
        if not isinstance(envelope, dict) or envelope.get("version") != 1:
            raise ValueError("ActiveGraph Agent Run event envelope is invalid")
        projected.append(
            AgentRunEvent.model_validate(
                {
                    "sequence": len(projected) + 1,
                    "type": native_event.type,
                    "data": envelope.get("data"),
                    "created_at": envelope.get("created_at"),
                }
            )
        )
    return tuple(projected)


def tenant_knowledge_digest(
    *,
    content: str,
    confidence: float,
    actor_id: str,
    source: str,
) -> str:
    """Digest stable Tenant Knowledge fields while excluding generated graph IDs."""

    return content_safe_digest(
        {
            "scope": "tenant_knowledge",
            "content": content,
            "kind": "explicit",
            "confidence": confidence,
            "provenance": {"actor_id": actor_id, "source": source},
            "state": "active",
        }
    )


class BackupArtifactPlan(BaseModel):
    """One content-free local artifact planned before any host command executes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    store: BackupStore
    plaintext_path: Path
    ciphertext_path: Path
    object_key: str = Field(min_length=1, max_length=512)


class BackupPlan(BaseModel):
    """Exact Tenant route and local paths selected for a host backup run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backup_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    tenant_id: str = Field(min_length=1, max_length=128)
    created_at: datetime
    database_name: str
    database_role: str
    neo4j_service_name: str
    staging_directory: Path
    artifact_directory: Path
    artifacts: tuple[BackupArtifactPlan, ...]

    @model_validator(mode="after")
    def validate_plan(self) -> BackupPlan:
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        if {artifact.store for artifact in self.artifacts} != set(BackupStore):
            raise ValueError("backup plan requires every store")
        if len(self.artifacts) != len(BackupStore):
            raise ValueError("backup plan requires exactly one artifact per store")
        return self


class BackupBarrierRecord(BaseModel):
    """Content-free identity for inspecting and recovering an abandoned fence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(min_length=1)
    barrier_id: str = Field(min_length=1)
    started_at: datetime

    @model_validator(mode="after")
    def validate_started_at(self) -> BackupBarrierRecord:
        if self.started_at.tzinfo is None:
            raise ValueError("started_at must be timezone-aware")
        return self


class BackupCompatibility(BaseModel):
    """Versions accepted by the current isolated restore implementation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    store_versions: Mapping[BackupStore, frozenset[str]]
    schema_versions: Mapping[BackupStore, frozenset[str]]

    def validate_artifact(self, artifact: BackupArtifactManifest) -> None:
        if artifact.store_version not in self.store_versions.get(artifact.store, frozenset()):
            raise BackupValidationError(
                f"unsupported {artifact.store.value} store version: {artifact.store_version}"
            )
        if artifact.schema_version not in self.schema_versions.get(artifact.store, frozenset()):
            raise BackupValidationError(
                f"unsupported {artifact.store.value} schema version: {artifact.schema_version}"
            )


class CommandRunner(Protocol):
    """Executes an argv vector without invoking a shell."""

    def run(self, argv: Sequence[str]) -> None: ...


@dataclass(frozen=True, slots=True)
class AgeEncryptionCommandAdapter:
    """Construct safe ``age`` commands while keeping key material out of argv."""

    runner: CommandRunner
    executable: str = "age"

    @staticmethod
    def _validate_key_file(path: Path) -> None:
        if not path.is_file():
            raise BackupValidationError(f"encryption credential is not a file: {path}")
        permissions = path.stat().st_mode & 0o777
        if permissions & 0o077:
            raise BackupValidationError(
                f"encryption credential must not be group/world accessible: {path}"
            )

    @staticmethod
    def _validate_paths(source: Path, destination: Path) -> None:
        if source.resolve() == destination.resolve():
            raise BackupValidationError("source and destination must be different files")

    def encrypt(self, source: Path, destination: Path, recipients_file: Path) -> None:
        self._validate_paths(source, destination)
        self._validate_key_file(recipients_file)
        source = source.resolve()
        destination = destination.resolve()
        recipients_file = recipients_file.resolve()
        argv = (
            self.executable,
            "--encrypt",
            "--recipients-file",
            os.fspath(recipients_file),
            "--output",
            os.fspath(destination),
            os.fspath(source),
        )
        try:
            self.runner.run(argv)
        except Exception as error:
            raise EncryptionCommandFailed("age encryption failed") from error

    def decrypt(self, source: Path, destination: Path, identity_file: Path) -> None:
        self._validate_paths(source, destination)
        self._validate_key_file(identity_file)
        source = source.resolve()
        destination = destination.resolve()
        identity_file = identity_file.resolve()
        argv = (
            self.executable,
            "--decrypt",
            "--identity",
            os.fspath(identity_file),
            "--output",
            os.fspath(destination),
            os.fspath(source),
        )
        try:
            self.runner.run(argv)
        except Exception as error:
            raise EncryptionCommandFailed("age decryption failed") from error


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Return a streaming SHA-256 checksum for an artifact."""

    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_ciphertext(path: Path, artifact: BackupArtifactManifest) -> None:
    """Reject missing, incomplete, truncated, or corrupt encrypted artifacts."""

    if not artifact.complete:
        raise BackupValidationError(f"artifact {artifact.artifact_id} is incomplete")
    if not path.is_file():
        raise BackupValidationError(f"artifact {artifact.artifact_id} is missing")
    actual_size = path.stat().st_size
    if actual_size != artifact.ciphertext_bytes:
        raise BackupValidationError(f"artifact {artifact.artifact_id} size mismatch: {actual_size}")
    if sha256_file(path) != artifact.ciphertext_sha256:
        raise BackupValidationError(f"artifact {artifact.artifact_id} checksum mismatch")


class RetentionPin(BaseModel):
    """Content-free reason an exact recovery point cannot be removed yet."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backup_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    status: Literal["pinned"] = "pinned"
    reason: str = Field(min_length=1, max_length=160)
    protected_until: datetime | None = None

    @model_validator(mode="after")
    def validate_deadline(self) -> RetentionPin:
        if self.protected_until is not None and self.protected_until.tzinfo is None:
            raise ValueError("retention pin deadline must be timezone-aware")
        return self


class RetentionProtectionPort(Protocol):
    """Find active legal/lifecycle pins for one immutable Tenant."""

    def pins_for_tenant(self, tenant_id: str, *, now: datetime) -> tuple[RetentionPin, ...]: ...


class RetentionPlan(BaseModel):
    """A non-destructive retention decision; deletion is a separate operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    keep_backup_ids: tuple[str, ...]
    delete_backup_ids: tuple[str, ...]
    pins: tuple[RetentionPin, ...] = ()
    warnings: tuple[str, ...] = ()


def plan_retention(
    manifests: Sequence[BackupManifest],
    *,
    daily: int = 7,
    weekly: int = 4,
    pins: Sequence[RetentionPin] = (),
) -> RetentionPlan:
    """Keep seven daily and four weekly recovery points plus every unsafe candidate.

    Incomplete manifests are retained for operator inspection rather than silently
    discarded.  The newest complete recovery point is always retained, even when
    both retention windows are configured to zero.
    """

    if daily < 0 or weekly < 0:
        raise ValueError("retention counts must be non-negative")
    if len({manifest.backup_id for manifest in manifests}) != len(manifests):
        raise ValueError("backup_id values must be unique")

    ordered = sorted(manifests, key=lambda item: item.created_at, reverse=True)
    unsafe = [item for item in ordered if not item.complete]
    pinned = tuple(pins)
    pinned_ids = {pin.backup_id for pin in pinned}
    keep: set[str] = {item.backup_id for item in unsafe} | pinned_ids
    warnings = [f"incomplete backup retained for inspection: {item.backup_id}" for item in unsafe]
    warnings.extend(f"backup pinned by {pin.reason}: {pin.backup_id}" for pin in pinned)
    tenant_ids = {item.tenant_id for item in ordered}
    for tenant_id in tenant_ids:
        complete = [item for item in ordered if item.tenant_id == tenant_id and item.complete]
        if complete:
            keep.add(complete[0].backup_id)

        seen_days: set[int] = set()
        seen_weeks: set[tuple[int, int]] = set()
        for manifest in complete:
            utc_created = manifest.created_at.astimezone(UTC)
            day_key = utc_created.date().toordinal()
            iso = utc_created.isocalendar()
            week_key = (iso.year, iso.week)
            if len(seen_days) < daily and day_key not in seen_days:
                keep.add(manifest.backup_id)
                seen_days.add(day_key)
            if len(seen_weeks) < weekly and week_key not in seen_weeks:
                keep.add(manifest.backup_id)
                seen_weeks.add(week_key)

    return RetentionPlan(
        keep_backup_ids=tuple(item.backup_id for item in ordered if item.backup_id in keep),
        delete_backup_ids=tuple(item.backup_id for item in ordered if item.backup_id not in keep),
        pins=pinned,
        warnings=tuple(warnings),
    )


class BackupDeletionPort(Protocol):
    """Deletes one exact backup set from the configured artifact store."""

    def delete_backup(self, backup_id: str) -> None: ...


class RetentionExecution(BaseModel):
    """Observable result of applying a retention plan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    deleted_backup_ids: tuple[str, ...]
    failed_backup_ids: tuple[str, ...]
    pins: tuple[RetentionPin, ...] = ()


def apply_retention(plan: RetentionPlan, deletion: BackupDeletionPort) -> RetentionExecution:
    """Apply exact deletions and report every failure for alerting/retry."""

    deleted: list[str] = []
    failed: list[str] = []
    protected = set(plan.keep_backup_ids) | {pin.backup_id for pin in plan.pins}
    for backup_id in plan.delete_backup_ids:
        if backup_id in protected:
            raise BackupValidationError(f"retention plan tries to delete protected {backup_id}")
        try:
            deletion.delete_backup(backup_id)
        except Exception:  # noqa: BLE001 - the external adapter defines its own failures
            failed.append(backup_id)
        else:
            deleted.append(backup_id)
    return RetentionExecution(
        deleted_backup_ids=tuple(deleted),
        failed_backup_ids=tuple(failed),
        pins=plan.pins,
    )


class BackupHealth(BaseModel):
    """RPO status suitable for a metric, log event, or alert rule."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    latest_valid_backup_id: str | None
    age_seconds: float | None
    rpo_seconds: float
    overdue: bool


def assess_rpo(
    tenant_id: str,
    manifests: Sequence[BackupManifest],
    *,
    now: datetime,
    rpo: timedelta = DEFAULT_RPO,
) -> BackupHealth:
    """Report whether a tenant has a complete recovery point within its RPO."""

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    valid = [item for item in manifests if item.tenant_id == tenant_id and item.complete]
    latest = max(valid, key=lambda item: item.created_at, default=None)
    age = None if latest is None else max(0.0, (now - latest.created_at).total_seconds())
    return BackupHealth(
        tenant_id=tenant_id,
        latest_valid_backup_id=None if latest is None else latest.backup_id,
        age_seconds=age,
        rpo_seconds=rpo.total_seconds(),
        overdue=age is None or age > rpo.total_seconds(),
    )


class IsolatedRestoreTarget(BaseModel):
    """A disposable destination that cannot resolve as an active tenant route."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_id: str = Field(min_length=1, max_length=160)
    source_tenant_id: str = Field(min_length=1, max_length=128)
    isolated: bool = True
    active_route: bool = False

    @model_validator(mode="after")
    def reject_active_target(self) -> IsolatedRestoreTarget:
        if not self.isolated or self.active_route:
            raise ValueError("restore target must be isolated and have no active route")
        if self.target_id == self.source_tenant_id:
            raise ValueError("restore target must not reuse the active tenant identifier")
        return self


class RestorePort(Protocol):
    """Performs restore operations against an already isolated target."""

    def prepare(self, target: IsolatedRestoreTarget) -> None: ...

    def restore_control_metadata(
        self, path: Path, artifact: BackupArtifactManifest, target: IsolatedRestoreTarget
    ) -> None: ...

    def restore_tenant_postgres(
        self, path: Path, artifact: BackupArtifactManifest, target: IsolatedRestoreTarget
    ) -> None: ...

    def restore_tenant_neo4j(
        self, path: Path, artifact: BackupArtifactManifest, target: IsolatedRestoreTarget
    ) -> None: ...


class RestoreVerificationPort(Protocol):
    """Exercises public behavior and proves tenant isolation after restore."""

    def verify_public_recall(self, target: IsolatedRestoreTarget) -> bool: ...

    def verify_governance(self, target: IsolatedRestoreTarget) -> bool: ...

    def verify_agent_runs(self, target: IsolatedRestoreTarget) -> bool: ...

    def verify_no_other_tenant_routing(self, target: IsolatedRestoreTarget) -> bool: ...


class RestoreDrillRecord(BaseModel):
    """Content-free evidence of a restore drill and its RTO outcome."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backup_id: str
    target_id: str
    tenant_id: str
    operator_id: str
    started_at: datetime
    completed_at: datetime
    duration_seconds: float = Field(ge=0)
    passed_checks: tuple[str, ...]
    outcome: Literal["passed", "failed"]
    within_rto: bool


class BackupOperator(Protocol):
    """Operator-facing plan/create/verify/restore-drill surface used by coengramctl."""

    def plan(self, tenant_id: str, *, backup_id: str | None = None) -> BackupPlan: ...

    def create(self, tenant_id: str, *, backup_id: str | None = None) -> BackupManifest: ...

    def verify(self, manifest_path: Path) -> BackupManifest: ...

    def restore_drill(
        self,
        manifest_path: Path,
        *,
        target_id: str,
        operator_id: str,
    ) -> RestoreDrillRecord: ...

    def restore_latest_drill(
        self,
        tenant_id: str,
        *,
        target_id: str,
        operator_id: str,
    ) -> RestoreDrillRecord: ...

    def cleanup_restore_drill(self, target_id: str) -> None: ...

    def retention_plan(self, tenant_id: str) -> RetentionPlan: ...

    def apply_retention(self, tenant_id: str) -> RetentionExecution: ...

    def health(self, tenant_id: str) -> BackupHealth: ...

    def barrier_status(self, tenant_id: str) -> BackupBarrierRecord | None: ...

    def recover_barrier(self, tenant_id: str, barrier_id: str) -> None: ...


class BackupRestoreService:
    """Validate and restore a tenant backup only into a disposable target."""

    def __init__(
        self,
        *,
        restore: RestorePort,
        verifier: RestoreVerificationPort,
        compatibility: BackupCompatibility,
        rto: timedelta = DEFAULT_RTO,
    ) -> None:
        self._restore = restore
        self._verifier = verifier
        self._compatibility = compatibility
        self._rto = rto

    def restore_and_verify(
        self,
        *,
        manifest: BackupManifest,
        ciphertext_paths: Mapping[str, Path],
        target: IsolatedRestoreTarget,
        operator_id: str,
        started_at: datetime,
        completed_at: datetime,
    ) -> RestoreDrillRecord:
        if not manifest.complete:
            raise BackupValidationError("incomplete backup cannot be restored")
        if target.source_tenant_id != manifest.tenant_id:
            raise RestoreSafetyError("restore target tenant does not match backup tenant")
        if completed_at < started_at:
            raise ValueError("completed_at cannot precede started_at")

        by_store: dict[BackupStore, tuple[Path, BackupArtifactManifest]] = {}
        for artifact in manifest.artifacts:
            path = ciphertext_paths.get(artifact.artifact_id)
            if path is None:
                raise BackupValidationError(f"missing path for {artifact.artifact_id}")
            verify_ciphertext(path, artifact)
            self._compatibility.validate_artifact(artifact)
            by_store[artifact.store] = (path, artifact)

        self._restore.prepare(target)
        control_path, control = by_store[BackupStore.CONTROL_POSTGRES]
        postgres_path, postgres = by_store[BackupStore.TENANT_POSTGRES]
        neo4j_path, neo4j = by_store[BackupStore.TENANT_NEO4J]
        self._restore.restore_control_metadata(control_path, control, target)
        self._restore.restore_tenant_postgres(postgres_path, postgres, target)
        self._restore.restore_tenant_neo4j(neo4j_path, neo4j, target)

        checks = (
            ("public-recall", self._verifier.verify_public_recall(target)),
            ("governance", self._verifier.verify_governance(target)),
            ("agent-runs", self._verifier.verify_agent_runs(target)),
            ("tenant-isolation", self._verifier.verify_no_other_tenant_routing(target)),
        )
        passed = tuple(name for name, result in checks if result)
        duration = (completed_at - started_at).total_seconds()
        outcome: Literal["passed", "failed"] = "passed" if len(passed) == len(checks) else "failed"
        return RestoreDrillRecord(
            backup_id=manifest.backup_id,
            target_id=target.target_id,
            tenant_id=manifest.tenant_id,
            operator_id=operator_id,
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=duration,
            passed_checks=passed,
            outcome=outcome,
            within_rto=outcome == "passed" and duration <= self._rto.total_seconds(),
        )

"""Safe host orchestration for encrypted, route-exact local backup artifacts."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import quote
from uuid import uuid4

import psycopg
from activegraph.store.postgres import PostgresEventStore  # type: ignore[import-untyped]

from agent_memory_service.agents.persistence import PostgresAgentRunRepository
from agent_memory_service.backup import (
    AgeEncryptionCommandAdapter,
    BackupArtifactManifest,
    BackupArtifactPlan,
    BackupBarrierRecord,
    BackupHealth,
    BackupManifest,
    BackupPlan,
    BackupStore,
    BackupValidationError,
    EncryptionMetadata,
    PrivateMemoryRestoreProof,
    RestoreDrillRecord,
    RestoreExpectations,
    RestoreProofTarget,
    RestoreSafetyError,
    RetentionExecution,
    RetentionPin,
    RetentionPlan,
    RetentionProtectionPort,
    activegraph_event_sequence_digest,
    apply_retention,
    assess_rpo,
    content_safe_digest,
    plan_retention,
    private_memory_correction_digest,
    private_memory_erasure_digest,
    private_memory_item_digest,
    projected_agent_run_events,
    sha256_file,
    tenant_knowledge_digest,
    verify_ciphertext,
)
from agent_memory_service.control import ControlModule, TenantRouteRecord
from agent_memory_service.decommission import ProtectionEvidence, ProtectionKind
from agent_memory_service.governance import CandidateStatus, candidate_view
from agent_memory_service.host_decommission import ProtectionEvidenceReceipt
from agent_memory_service.pseudonyms import TelemetryPseudonymizer
from agent_memory_service.stores.postgres_governance import PostgresGovernanceStore
from agent_memory_service.tenant_credentials import read_tenant_credential

_SAFE_TENANT_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,38}[a-z0-9])?$")
_SAFE_BACKUP_ID = re.compile(r"^[A-Za-z0-9._:-]+$")
_NEO4J_ADMIN_IMAGE = (
    "neo4j:5.26.28-community"
    "@sha256:362542416de6c09a971484d1893878016cc3b5cdec166e54b1c824a220ecd6b9"
)


class BackupHostCommandRunner(Protocol):
    """Run an explicit argv vector with an optional non-logged environment."""

    def run(
        self,
        arguments: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None: ...


class RestoreDrillExecutor(Protocol):
    """Provision and verify an isolated restore using protected identity configuration."""

    def execute(
        self,
        *,
        manifest: BackupManifest,
        ciphertext_paths: Mapping[str, Path],
        target_id: str,
        operator_id: str,
        age_identity_file: Path,
    ) -> RestoreDrillRecord: ...

    def cleanup(self, target_id: str) -> None: ...


class BackupExpectationCollector(Protocol):
    """Capture content-free restore proof targets before any backup command runs."""

    def collect(self, plan: BackupPlan) -> RestoreExpectations: ...


class BackupConsistencyBarrier(Protocol):
    """Fence Tenant writes and prove a coherent cut across every durable store."""

    def enter(self, plan: BackupPlan) -> None: ...

    def verify_quiescent(self, plan: BackupPlan) -> None: ...

    def exit(self, plan: BackupPlan) -> None: ...

    def inspect(self, tenant_id: str) -> BackupBarrierRecord | None: ...

    def recover(self, tenant_id: str, barrier_id: str) -> None: ...


class PostgresBackupExpectationCollector:
    """Derive graph and Operations Store expectations from authoritative PostgreSQL."""

    def __init__(
        self,
        tenant_secrets_directory: Path,
        *,
        postgres_host: str,
        postgres_port: int,
    ) -> None:
        self._tenant_secrets = tenant_secrets_directory.resolve()
        self._postgres_host = postgres_host
        self._postgres_port = postgres_port

    def collect(self, plan: BackupPlan) -> RestoreExpectations:
        try:
            password = read_tenant_credential(
                self._tenant_secrets,
                plan.tenant_id,
                "postgres_password",
            )
        except ValueError as exc:
            raise BackupValidationError("Tenant PostgreSQL password file is invalid") from exc
        database_url = (
            f"postgresql://{quote(plan.database_role, safe='')}:"
            f"{quote(password, safe='')}@{self._postgres_host}:{self._postgres_port}/"
            f"{quote(plan.database_name, safe='')}"
        )
        governance_store = PostgresGovernanceStore(database_url)
        candidates = asyncio.run(governance_store.list_candidates(plan.tenant_id))
        views = tuple(candidate_view(candidate) for candidate in candidates)
        tenant_knowledge_digests = tuple(
            sorted(
                tenant_knowledge_digest(
                    content=candidate.claim,
                    confidence=candidate.confidence,
                    actor_id=candidate.proposer_id,
                    source=f"candidate:{candidate.id}",
                )
                for candidate in candidates
                if candidate.status is CandidateStatus.PUBLISHED
            )
        )
        if not tenant_knowledge_digests:
            raise BackupValidationError("Backup requires representative published Tenant Knowledge")
        with psycopg.connect(database_url) as connection:
            owner_rows = connection.execute(
                """
                SELECT DISTINCT owner_principal_id
                FROM memory.private_memory_items
                WHERE tenant_id = %s
                ORDER BY owner_principal_id
                """,
                (plan.tenant_id,),
            ).fetchall()
            count_row = connection.execute(
                "SELECT count(*) FROM memory.agent_runs WHERE tenant_id = %s",
                (plan.tenant_id,),
            ).fetchone()
            run_row = connection.execute(
                """
                SELECT run_id
                FROM memory.agent_runs
                WHERE tenant_id = %s
                ORDER BY created_at, run_id
                LIMIT 1
                """,
                (plan.tenant_id,),
            ).fetchone()
        owners = tuple(str(row[0]) for row in owner_rows)
        private_memory = asyncio.run(
            self._private_memory_proof(
                governance_store,
                tenant_id=plan.tenant_id,
                owner_principal_ids=owners,
            )
        )
        if count_row is None:
            raise BackupValidationError("Agent Run expectation count is unavailable")
        agent_run_count = int(count_row[0])
        representative_id = None if run_row is None else str(run_row[0])
        representative_digest: str | None = None
        native_event_count: int | None = None
        native_event_digest: str | None = None
        if representative_id is not None:
            snapshot = PostgresAgentRunRepository(database_url).get(
                plan.tenant_id,
                representative_id,
            )
            if snapshot is None:
                raise BackupValidationError("Representative Agent Run is unavailable")
            representative_digest = content_safe_digest(snapshot.model_dump(mode="json"))
            native_store = PostgresEventStore(database_url, representative_id)
            try:
                native_events = tuple(native_store.iter_events())
                if native_store.get_run() is None or not native_events:
                    raise BackupValidationError(
                        "Representative ActiveGraph Agent Run projection is unavailable"
                    )
            finally:
                native_store.close()
            if projected_agent_run_events(native_events) != snapshot.events:
                raise BackupValidationError(
                    "Representative ActiveGraph projection diverges from its Agent Run snapshot"
                )
            native_event_count = len(native_events)
            native_event_digest = activegraph_event_sequence_digest(native_events)
        return RestoreExpectations(
            tenant_knowledge_digests=tenant_knowledge_digests,
            governance_candidate_count=len(views),
            governance_digest=content_safe_digest([view.model_dump(mode="json") for view in views]),
            private_memory=private_memory,
            agent_run_count=agent_run_count,
            representative_agent_run_id=representative_id,
            representative_agent_run_digest=representative_digest,
            representative_activegraph_event_count=native_event_count,
            representative_activegraph_event_digest=native_event_digest,
        )

    async def _private_memory_proof(
        self,
        store: PostgresGovernanceStore,
        *,
        tenant_id: str,
        owner_principal_ids: tuple[str, ...],
    ) -> PrivateMemoryRestoreProof:
        active_digests: list[str] = []
        correction_digests: list[str] = []
        erasure_digests: list[str] = []
        for owner_id in owner_principal_ids:
            inspections = await store.list_private_memory_state(tenant_id, owner_id)
            by_id = {item.id: item for item in inspections}
            for item in inspections:
                try:
                    active_digests.append(private_memory_item_digest(item))
                except ValueError:
                    pass
            for replacement in inspections:
                ancestor = (
                    None
                    if replacement.supersedes_id is None
                    else by_id.get(replacement.supersedes_id)
                )
                if ancestor is None:
                    continue
                try:
                    correction_digests.append(
                        private_memory_correction_digest(ancestor, replacement)
                    )
                except ValueError:
                    pass
            for tombstone in await store.list_completed(tenant_id, owner_id):
                inspection = by_id.get(tombstone.memory_id)
                if inspection is None:
                    continue
                try:
                    erasure_digests.append(private_memory_erasure_digest(inspection, tombstone))
                except ValueError:
                    pass
        return PrivateMemoryRestoreProof(
            active=_restore_proof_target(active_digests),
            correction_chain=_restore_proof_target(correction_digests),
            completed_erasure=_restore_proof_target(erasure_digests),
        )


def _restore_proof_target(digests: Sequence[str]) -> RestoreProofTarget:
    ordered = tuple(sorted(set(digests)))
    return RestoreProofTarget(
        count=len(ordered),
        digest=content_safe_digest(ordered),
        representative_digest=None if not ordered else ordered[0],
    )


@dataclass(frozen=True, slots=True)
class PostgresBackupSource:
    host: str
    port: int
    database: str
    user: str
    password_file: Path


@dataclass(frozen=True, slots=True)
class HostBackupConfig:
    staging_directory: Path
    artifact_directory: Path
    tenant_compose_file: Path
    tenant_secrets_directory: Path
    age_recipients_file: Path
    age_key_id: str
    control_postgres: PostgresBackupSource
    postgres_store_version: str
    control_schema_version: str
    tenant_schema_version: str
    neo4j_store_version: str
    neo4j_schema_version: str
    evidence_directory: Path | None = None
    tenant_postgres_host: str | None = None
    tenant_postgres_port: int | None = None
    age_identity_file: Path | None = None
    neo4j_admin_image: str = _NEO4J_ADMIN_IMAGE


class LocalBackupArtifactPublisher:
    """Publish ciphertext first and atomically expose the complete manifest last."""

    def __init__(
        self,
        artifact_directory: Path,
        *,
        replace: Callable[[Path, Path], None] = os.replace,
    ) -> None:
        self._root = _existing_directory(artifact_directory, "artifact directory")
        self._replace = replace

    @property
    def root(self) -> Path:
        return self._root

    def publish(
        self,
        manifest: BackupManifest,
        ciphertext_paths: Mapping[str, Path],
    ) -> Path:
        target = self._root / manifest.backup_id
        target.mkdir(mode=0o700, exist_ok=False)
        for artifact in manifest.artifacts:
            source = ciphertext_paths.get(artifact.artifact_id)
            if source is None or not source.is_file():
                raise BackupValidationError(f"ciphertext is unavailable for {artifact.artifact_id}")
            destination = _contained_path(self._root, artifact.object_key)
            if destination.parent != target:
                raise BackupValidationError("artifact object key does not match backup directory")
            self._replace(source, destination)

        manifest_path = target / "manifest.json"
        temporary_manifest = target / ".manifest.json.partial"
        document = json.dumps(
            manifest.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        descriptor = os.open(
            temporary_manifest,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            os.write(descriptor, document)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._replace(temporary_manifest, manifest_path)
        return manifest_path

    def delete_backup(self, backup_id: str) -> None:
        if _SAFE_BACKUP_ID.fullmatch(backup_id) is None:
            raise BackupValidationError("backup identifier is not safe for deletion")
        target = _contained_path(self._root, backup_id)
        if target.parent != self._root or target.is_symlink() or not target.is_dir():
            raise BackupValidationError("backup deletion target is not an exact directory")
        expected = {
            "manifest.json",
            *(f"{store.value}.age" for store in BackupStore),
        }
        entries = tuple(target.iterdir())
        if {entry.name for entry in entries} != expected or any(
            entry.is_symlink() or not entry.is_file() for entry in entries
        ):
            raise BackupValidationError("backup directory has unexpected contents")
        for entry in entries:
            entry.unlink()
        target.rmdir()


class HostBackupOrchestrator:
    """Back up exactly one healthy Tenant route without using a host shell."""

    def __init__(
        self,
        *,
        control: ControlModule,
        runner: BackupHostCommandRunner,
        publisher: LocalBackupArtifactPublisher,
        config: HostBackupConfig,
        clock: Callable[[], datetime] | None = None,
        restore_drill: RestoreDrillExecutor | None = None,
        retention_protection: RetentionProtectionPort | None = None,
        expectation_collector: BackupExpectationCollector,
        consistency_barrier: BackupConsistencyBarrier,
        telemetry_pseudonymizer: TelemetryPseudonymizer,
    ) -> None:
        self._control = control
        self._runner = runner
        self._publisher = publisher
        self._config = config
        self._clock = clock or (lambda: datetime.now(UTC))
        self._restore_drill = restore_drill
        self._retention_protection = retention_protection
        self._expectation_collector = expectation_collector
        self._consistency_barrier = consistency_barrier
        self._telemetry_pseudonymizer = telemetry_pseudonymizer
        self._staging = _existing_directory(config.staging_directory, "staging directory")
        artifacts = _existing_directory(config.artifact_directory, "artifact directory")
        if artifacts != publisher.root:
            raise ValueError("Publisher and backup artifact directories must match")
        self._tenant_secrets = _existing_directory(
            config.tenant_secrets_directory,
            "Tenant secrets directory",
        )
        self._compose_file = _regular_file(config.tenant_compose_file, "Tenant Compose file")
        _validate_protected_file(config.age_recipients_file, "age recipients file")
        _validate_postgres_source(config.control_postgres)
        self._evidence_directory = (
            None
            if config.evidence_directory is None
            else _existing_directory(config.evidence_directory, "recovery evidence directory")
        )
        if not config.age_key_id or not all(
            character.isalnum() or character in "._:-" for character in config.age_key_id
        ):
            raise ValueError("age key identifier is invalid")
        for version in (
            config.postgres_store_version,
            config.control_schema_version,
            config.tenant_schema_version,
            config.neo4j_store_version,
            config.neo4j_schema_version,
        ):
            if not version.strip():
                raise ValueError("Backup version values cannot be empty")

    def plan(self, tenant_id: str, *, backup_id: str | None = None) -> BackupPlan:
        if _SAFE_TENANT_ID.fullmatch(tenant_id) is None:
            raise BackupValidationError("Tenant identifier is invalid for host backup")
        route = self._exact_route(tenant_id)
        created_at = self._clock()
        if created_at.tzinfo is None:
            raise ValueError("Backup clock must be timezone-aware")
        selected_id = backup_id or _backup_id(tenant_id, created_at)
        stage = self._staging / selected_id
        artifact_directory = self._publisher.root / selected_id
        artifacts = tuple(
            BackupArtifactPlan(
                artifact_id=f"{selected_id}:{store.value}",
                store=store,
                plaintext_path=stage / _plaintext_name(store),
                ciphertext_path=stage / f"{store.value}.age",
                object_key=f"{selected_id}/{store.value}.age",
            )
            for store in BackupStore
        )
        return BackupPlan(
            backup_id=selected_id,
            tenant_id=tenant_id,
            created_at=created_at,
            database_name=route.tenant_database_name,
            database_role=route.tenant_database_role,
            neo4j_service_name=route.neo4j_service_address.removesuffix(":7687"),
            staging_directory=stage,
            artifact_directory=artifact_directory,
            artifacts=artifacts,
        )

    def create(self, tenant_id: str, *, backup_id: str | None = None) -> BackupManifest:
        plan = self.plan(tenant_id, backup_id=backup_id)
        if plan.artifact_directory.exists():
            raise BackupValidationError("backup artifact directory already exists")
        if plan.staging_directory.exists():
            raise BackupValidationError("backup staging directory already exists")
        encrypted: dict[str, Path] = {}
        artifacts: list[BackupArtifactManifest] = []
        self._consistency_barrier.enter(plan)
        try:
            try:
                self._consistency_barrier.verify_quiescent(plan)
                expectations = self._expectation_collector.collect(plan)
                plan.staging_directory.mkdir(mode=0o700, exist_ok=False)
                for artifact_plan in plan.artifacts:
                    self._create_plaintext(plan, artifact_plan)
            finally:
                self._consistency_barrier.exit(plan)

            for artifact_plan in plan.artifacts:
                try:
                    AgeEncryptionCommandAdapter(self._runner).encrypt(
                        artifact_plan.plaintext_path,
                        artifact_plan.ciphertext_path,
                        self._config.age_recipients_file,
                    )
                finally:
                    artifact_plan.plaintext_path.unlink(missing_ok=True)
                if (
                    not artifact_plan.ciphertext_path.is_file()
                    or artifact_plan.ciphertext_path.stat().st_size < 1
                ):
                    raise BackupValidationError(
                        f"encrypted artifact was not created: {artifact_plan.artifact_id}"
                    )
                artifact = self._artifact_manifest(plan, artifact_plan)
                verify_ciphertext(artifact_plan.ciphertext_path, artifact)
                encrypted[artifact.artifact_id] = artifact_plan.ciphertext_path
                artifacts.append(artifact)

            manifest = BackupManifest(
                backup_id=plan.backup_id,
                tenant_id=plan.tenant_id,
                created_at=plan.created_at,
                database_name=plan.database_name,
                database_role=plan.database_role,
                neo4j_service_name=plan.neo4j_service_name,
                expectations=expectations,
                artifacts=tuple(artifacts),
                complete=True,
            )
            manifest_path = self._publisher.publish(manifest, encrypted)
            self._publish_evidence(manifest, manifest_path)
            return manifest
        finally:
            for artifact_plan in plan.artifacts:
                artifact_plan.plaintext_path.unlink(missing_ok=True)
                artifact_plan.ciphertext_path.unlink(missing_ok=True)
            try:
                plan.staging_directory.rmdir()
            except OSError:
                pass

    def verify(self, manifest_path: Path) -> BackupManifest:
        path = _regular_file(manifest_path, "Backup Manifest")
        expected_root = self._publisher.root
        try:
            path.relative_to(expected_root)
        except ValueError as exc:
            raise BackupValidationError(
                "Backup Manifest is outside the artifact directory"
            ) from exc
        if path.stat().st_size > 1_000_000:
            raise BackupValidationError("Backup Manifest exceeds the operator input limit")
        try:
            manifest = BackupManifest.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise BackupValidationError("Backup Manifest is invalid") from exc
        if not manifest.complete:
            raise BackupValidationError("Backup Manifest is incomplete")
        expected_manifest = expected_root / manifest.backup_id / "manifest.json"
        if path != expected_manifest:
            raise BackupValidationError("Backup Manifest path does not match its backup identifier")
        for artifact in manifest.artifacts:
            expected_key = f"{manifest.backup_id}/{artifact.store.value}.age"
            if artifact.object_key != expected_key:
                raise BackupValidationError("Backup artifact object key is not canonical")
            verify_ciphertext(_contained_path(expected_root, artifact.object_key), artifact)
        return manifest

    def restore_drill(
        self,
        manifest_path: Path,
        *,
        target_id: str,
        operator_id: str,
    ) -> RestoreDrillRecord:
        manifest = self.verify(manifest_path)
        if target_id == manifest.tenant_id or not target_id.startswith("restore-drill-"):
            raise RestoreSafetyError("restore drill target must be isolated and explicitly named")
        if self._restore_drill is None or self._config.age_identity_file is None:
            raise RestoreSafetyError("Host restore drill is not configured")
        identity = _validate_protected_file(
            self._config.age_identity_file,
            "age identity file",
        )
        ciphertext_paths = {
            artifact.artifact_id: _contained_path(
                self._publisher.root,
                artifact.object_key,
            )
            for artifact in manifest.artifacts
        }
        return self._restore_drill.execute(
            manifest=manifest,
            ciphertext_paths=ciphertext_paths,
            target_id=target_id,
            operator_id=operator_id,
            age_identity_file=identity,
        )

    def restore_latest_drill(
        self,
        tenant_id: str,
        *,
        target_id: str,
        operator_id: str,
    ) -> RestoreDrillRecord:
        latest = self.health(tenant_id).latest_valid_backup_id
        if latest is None:
            raise RestoreSafetyError("Tenant has no complete recovery point")
        return self.restore_drill(
            self._publisher.root / latest / "manifest.json",
            target_id=target_id,
            operator_id=operator_id,
        )

    def cleanup_restore_drill(self, target_id: str) -> None:
        if self._restore_drill is None:
            raise RestoreSafetyError("Host restore drill is not configured")
        self._restore_drill.cleanup(target_id)

    def retention_plan(self, tenant_id: str) -> RetentionPlan:
        return plan_retention(
            self._manifests(tenant_id),
            pins=self._retention_pins(tenant_id),
        )

    def apply_retention(self, tenant_id: str) -> RetentionExecution:
        return apply_retention(self.retention_plan(tenant_id), self)

    def delete_backup(self, backup_id: str) -> None:
        if self._retention_protection is not None:
            manifest_path = self._publisher.root / backup_id / "manifest.json"
            manifest = self.verify(manifest_path)
            if any(pin.backup_id == backup_id for pin in self._retention_pins(manifest.tenant_id)):
                raise BackupValidationError(
                    "backup is pinned by an active decommission retention policy"
                )
        self._publisher.delete_backup(backup_id)
        if self._evidence_directory is None:
            return
        receipt = self._evidence_directory / f"{backup_id}.json"
        if receipt.is_symlink():
            raise BackupValidationError("recovery evidence target must not be a symlink")
        if receipt.exists() and not receipt.is_file():
            raise BackupValidationError("recovery evidence target must be a regular file")
        receipt.unlink(missing_ok=True)

    def health(self, tenant_id: str) -> BackupHealth:
        return assess_rpo(tenant_id, self._manifests(tenant_id), now=self._clock())

    def barrier_status(self, tenant_id: str) -> BackupBarrierRecord | None:
        return self._consistency_barrier.inspect(tenant_id)

    def recover_barrier(self, tenant_id: str, barrier_id: str) -> None:
        self._consistency_barrier.recover(tenant_id, barrier_id)

    def _retention_pins(self, tenant_id: str) -> tuple[RetentionPin, ...]:
        if self._retention_protection is None:
            return ()
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("Backup clock must be timezone-aware")
        return self._retention_protection.pins_for_tenant(tenant_id, now=now)

    def _manifests(self, tenant_id: str) -> tuple[BackupManifest, ...]:
        if _SAFE_TENANT_ID.fullmatch(tenant_id) is None:
            raise BackupValidationError("Tenant identifier is invalid for host backup")
        manifests: list[BackupManifest] = []
        for candidate in sorted(self._publisher.root.iterdir()):
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            manifest_path = candidate / "manifest.json"
            if not manifest_path.is_file() or manifest_path.is_symlink():
                continue
            manifest = self.verify(manifest_path)
            if manifest.tenant_id == tenant_id:
                manifests.append(manifest)
        return tuple(manifests)

    def _create_plaintext(
        self,
        plan: BackupPlan,
        artifact: BackupArtifactPlan,
    ) -> None:
        if artifact.store is BackupStore.CONTROL_POSTGRES:
            self._pg_dump(self._config.control_postgres, artifact.plaintext_path)
            return
        if artifact.store is BackupStore.TENANT_POSTGRES:
            source = PostgresBackupSource(
                host=(self._config.tenant_postgres_host or self._config.control_postgres.host),
                port=(self._config.tenant_postgres_port or self._config.control_postgres.port),
                database=plan.database_name,
                user=plan.database_role,
                password_file=(self._tenant_secrets / plan.tenant_id / "postgres_password"),
            )
            _validate_postgres_endpoint(source)
            try:
                password = read_tenant_credential(
                    self._tenant_secrets,
                    plan.tenant_id,
                    "postgres_password",
                )
            except ValueError as exc:
                raise BackupValidationError("Tenant PostgreSQL password file is invalid") from exc
            self._pg_dump(source, artifact.plaintext_path, password=password)
            return
        self._archive_neo4j(plan, artifact.plaintext_path)

    def _pg_dump(
        self,
        source: PostgresBackupSource,
        destination: Path,
        *,
        password: str | None = None,
    ) -> None:
        selected_password = password or _read_protected_file(
            source.password_file, "PostgreSQL password file"
        )
        self._runner.run(
            (
                "pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-acl",
                "--file",
                os.fspath(destination),
                "--host",
                source.host,
                "--port",
                str(source.port),
                "--username",
                source.user,
                source.database,
            ),
            environment={"PGPASSWORD": selected_password},
        )
        if not destination.is_file() or destination.stat().st_size < 1:
            raise BackupValidationError("pg_dump did not create a backup file")

    def _archive_neo4j(self, plan: BackupPlan, destination: Path) -> None:
        handoff = plan.staging_directory / ".neo4j-dump"
        handoff.mkdir(mode=0o770, exist_ok=False)
        handoff.chmod(0o770)
        handoff_dump = handoff / "neo4j.dump"
        compose = (
            "docker",
            "compose",
            "--project-name",
            f"memory-tenant-{plan.tenant_id}",
            "--file",
            os.fspath(self._compose_file),
        )
        environment = {
            "TENANT_ID": plan.tenant_id,
            "TENANT_SECRETS_DIR": os.fspath(self._tenant_secrets / plan.tenant_id),
            "TENANT_TELEMETRY_REF": self._telemetry_pseudonymizer.reference(
                "tenant", plan.tenant_id
            ),
        }
        stopped = False
        try:
            self._runner.run((*compose, "stop", "neo4j"), environment=environment)
            stopped = True
            self._runner.run(
                (
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges:true",
                    "--user",
                    f"7474:{os.getgid()}",
                    "--group-add",
                    "7474",
                    "--tmpfs",
                    f"/tmp:rw,nosuid,nodev,exec,size=64m,mode=1777,uid=7474,gid={os.getgid()}",
                    "--tmpfs",
                    f"/logs:rw,nosuid,nodev,noexec,size=64m,mode=0750,uid=7474,gid={os.getgid()}",
                    "--volume",
                    f"memory-tenant-{plan.tenant_id}-neo4j-data:/data",
                    "--volume",
                    f"{handoff}:/backups",
                    "--entrypoint",
                    "/var/lib/neo4j/bin/neo4j-admin",
                    self._config.neo4j_admin_image,
                    "database",
                    "dump",
                    "neo4j",
                    "--to-path=/backups",
                    "--overwrite-destination=true",
                )
            )
            if not handoff_dump.is_file() or handoff_dump.stat().st_size < 1:
                raise BackupValidationError("Neo4j archive was not created")
            os.replace(handoff_dump, destination)
        finally:
            try:
                if stopped:
                    self._runner.run(
                        (*compose, "up", "-d", "--wait", "neo4j"),
                        environment=environment,
                    )
            finally:
                handoff_dump.unlink(missing_ok=True)
                handoff.rmdir()

    def _artifact_manifest(
        self,
        plan: BackupPlan,
        artifact: BackupArtifactPlan,
    ) -> BackupArtifactManifest:
        if artifact.store is BackupStore.CONTROL_POSTGRES:
            schema_version = self._config.control_schema_version
            store_version = self._config.postgres_store_version
            tenant_id = None
        elif artifact.store is BackupStore.TENANT_POSTGRES:
            schema_version = self._config.tenant_schema_version
            store_version = self._config.postgres_store_version
            tenant_id = plan.tenant_id
        else:
            schema_version = self._config.neo4j_schema_version
            store_version = self._config.neo4j_store_version
            tenant_id = plan.tenant_id
        return BackupArtifactManifest(
            artifact_id=artifact.artifact_id,
            object_key=artifact.object_key,
            store=artifact.store,
            tenant_id=tenant_id,
            store_version=store_version,
            schema_version=schema_version,
            created_at=plan.created_at,
            ciphertext_sha256=sha256_file(artifact.ciphertext_path),
            ciphertext_bytes=artifact.ciphertext_path.stat().st_size,
            encryption=EncryptionMetadata(key_id=self._config.age_key_id),
            complete=True,
        )

    def _publish_evidence(self, manifest: BackupManifest, manifest_path: Path) -> None:
        if self._evidence_directory is None:
            return
        evidence = ProtectionEvidence(
            kind=ProtectionKind.BACKUP,
            artifact_id=manifest.backup_id,
            created_at=manifest.created_at,
            verified_at=self._clock(),
            verified_by="memoryctl-backup",
            integrity_verified=True,
            complete=True,
        )
        receipt = ProtectionEvidenceReceipt(
            tenant_id=manifest.tenant_id,
            evidence=evidence,
            artifact_manifest_path=os.fspath(manifest_path),
            artifact_manifest_sha256=sha256_file(manifest_path),
        )
        destination = self._evidence_directory / f"{manifest.backup_id}.json"
        if destination.is_symlink():
            raise BackupValidationError("recovery evidence target must not be a symlink")
        document = receipt.model_dump_json().encode("utf-8")
        if destination.exists():
            try:
                existing = ProtectionEvidenceReceipt.model_validate_json(
                    destination.read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                raise BackupValidationError("existing recovery evidence is invalid") from exc
            if existing != receipt:
                raise BackupValidationError("existing recovery evidence does not match backup")
            return
        temporary = self._evidence_directory / f".{manifest.backup_id}.partial"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, document)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, destination)

    def _exact_route(self, tenant_id: str) -> TenantRouteRecord:
        route = self._control.resolve_tenant_route(tenant_id)
        normalized = tenant_id.replace("-", "_")
        expected = (
            f"neo4j-{tenant_id}:7687",
            f"{tenant_id}/neo4j_password",
            f"tenant_{normalized}",
            f"tenant_{normalized}_rw",
        )
        actual = (
            route.neo4j_service_address,
            route.neo4j_secret_name,
            route.tenant_database_name,
            route.tenant_database_role,
        )
        if actual != expected:
            raise BackupValidationError("Control Store Tenant route is not canonical")
        return route


def _existing_directory(path: Path, label: str) -> Path:
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symlink")
    resolved = path.resolve()
    if not resolved.is_dir():
        raise ValueError(f"{label} must be an existing directory")
    return resolved


def _regular_file(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise BackupValidationError(f"{label} must be a regular file")
    return path.resolve()


def _validate_protected_file(path: Path, label: str) -> Path:
    protected = _regular_file(path, label)
    if protected.stat().st_mode & 0o077:
        raise BackupValidationError(f"{label} must not be group/world accessible")
    return protected


def _read_protected_file(path: Path, label: str) -> str:
    protected = _validate_protected_file(path, label)
    value = protected.read_text(encoding="utf-8").strip()
    if not value:
        raise BackupValidationError(f"{label} is empty")
    return value


def _validate_postgres_source(source: PostgresBackupSource) -> None:
    _validate_postgres_endpoint(source)
    _validate_protected_file(source.password_file, "PostgreSQL password file")


def _validate_postgres_endpoint(source: PostgresBackupSource) -> None:
    if not source.host or any(character.isspace() for character in source.host):
        raise ValueError("PostgreSQL backup host is invalid")
    if not 1 <= source.port <= 65535:
        raise ValueError("PostgreSQL backup port is invalid")
    for value, label in ((source.database, "database"), (source.user, "user")):
        if not value or not value.replace("_", "a").isalnum():
            raise ValueError(f"PostgreSQL backup {label} is invalid")


def _contained_path(root: Path, relative_name: str) -> Path:
    candidate = (root / relative_name).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise BackupValidationError(
            "Backup artifact path escapes its configured directory"
        ) from exc
    if candidate.is_symlink():
        raise BackupValidationError("Backup artifact path must not be a symlink")
    return candidate


def _plaintext_name(store: BackupStore) -> str:
    if store is BackupStore.TENANT_NEO4J:
        return "neo4j.dump"
    return f"{store.value}.dump"


def _backup_id(tenant_id: str, created_at: datetime) -> str:
    timestamp = created_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{tenant_id}-{timestamp}-{uuid4().hex[:12]}"

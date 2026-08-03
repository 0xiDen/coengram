from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_memory_service.backup import (
    BackupArtifactManifest,
    BackupManifest,
    BackupStore,
    EncryptionMetadata,
    PrivateMemoryRestoreProof,
    RestoreExpectations,
    RestoreProofTarget,
)
from agent_memory_service.control import TenantRouteRecord
from agent_memory_service.decommission import ProtectionEvidence, ProtectionKind
from agent_memory_service.host_decommission import ProtectionEvidenceReceipt
from agent_memory_service.host_tenant_migration import (
    FilesystemMigrationRecoveryGate,
    FixedActiveTenantMigrationRunner,
)
from agent_memory_service.manifest import TenantManifest
from agent_memory_service.routing import FileSecretReader
from agent_memory_service.tenant_migration import POSTGRES_TARGET_VERSION

NOW = datetime(2026, 8, 2, 12, tzinfo=UTC)


class Commands:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], Mapping[str, str] | None]] = []

    def run(
        self,
        arguments: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.calls.append((tuple(arguments), environment))


class Neo4jMigrations:
    def __init__(self) -> None:
        self.manifests: list[TenantManifest] = []

    def migrate_neo4j(self, manifest: TenantManifest) -> None:
        self.manifests.append(manifest)


def _route(*, tenant_id: str = "tenant-a") -> TenantRouteRecord:
    normalized = tenant_id.replace("-", "_")
    return TenantRouteRecord(
        tenant_id=tenant_id,
        neo4j_service_address=f"neo4j-{tenant_id}:7687",
        neo4j_secret_name=f"{tenant_id}/neo4j_password",
        tenant_database_name=f"tenant_{normalized}",
        tenant_database_role=f"tenant_{normalized}_rw",
        healthy=True,
    )


def _write_recovery_receipt(
    root: Path,
    *,
    route: TenantRouteRecord,
    created_at: datetime = NOW,
) -> tuple[Path, Path]:
    backup_id = f"{route.tenant_id}-backup-001"
    ciphertext = b"encrypted-recovery-artifact"
    ciphertext_checksum = hashlib.sha256(ciphertext).hexdigest()
    encryption = EncryptionMetadata(key_id="backup-key-1")
    artifacts = tuple(
        BackupArtifactManifest(
            artifact_id=f"{backup_id}:{store.value}",
            object_key=f"{backup_id}/{store.value}.age",
            store=store,
            tenant_id=(None if store is BackupStore.CONTROL_POSTGRES else route.tenant_id),
            store_version=("17" if store is not BackupStore.TENANT_NEO4J else "5.26"),
            schema_version="1",
            created_at=created_at,
            ciphertext_sha256=ciphertext_checksum,
            ciphertext_bytes=len(ciphertext),
            encryption=encryption,
            complete=True,
        )
        for store in BackupStore
    )
    manifest = BackupManifest(
        backup_id=backup_id,
        tenant_id=route.tenant_id,
        created_at=created_at,
        database_name=route.tenant_database_name,
        database_role=route.tenant_database_role,
        neo4j_service_name=route.neo4j_service_address.removesuffix(":7687"),
        expectations=RestoreExpectations(
            tenant_knowledge_digests=(hashlib.sha256(b"tenant-knowledge").hexdigest(),),
            governance_candidate_count=0,
            governance_digest=hashlib.sha256(b"[]").hexdigest(),
            private_memory=PrivateMemoryRestoreProof(
                active=RestoreProofTarget(
                    count=0,
                    digest=hashlib.sha256(b"[]").hexdigest(),
                ),
                correction_chain=RestoreProofTarget(
                    count=0,
                    digest=hashlib.sha256(b"[]").hexdigest(),
                ),
                completed_erasure=RestoreProofTarget(
                    count=0,
                    digest=hashlib.sha256(b"[]").hexdigest(),
                ),
            ),
            agent_run_count=0,
        ),
        artifacts=artifacts,
        complete=True,
    )
    manifest_path = root / "manifest.json"
    manifest_path.write_text(manifest.model_dump_json(), encoding="utf-8")
    evidence = ProtectionEvidence(
        kind=ProtectionKind.BACKUP,
        artifact_id=backup_id,
        created_at=created_at,
        verified_at=created_at + timedelta(minutes=1),
        verified_by="memoryctl-backup",
        integrity_verified=True,
        complete=True,
    )
    receipt = ProtectionEvidenceReceipt(
        tenant_id=route.tenant_id,
        evidence=evidence,
        artifact_manifest_path=str(manifest_path.resolve()),
        artifact_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )
    receipt_path = root / f"{backup_id}.json"
    receipt_path.write_text(receipt.model_dump_json(), encoding="utf-8")
    return receipt_path, manifest_path


def test_active_migration_runner_uses_fixed_revision_and_secret_only_in_environment(
    tmp_path: Path,
) -> None:
    tenant_id = "tenant-a"
    secrets_root = tmp_path / "secrets"
    tenant_secrets = secrets_root / tenant_id
    tenant_secrets.mkdir(parents=True)
    password = tenant_secrets / "postgres_password"
    password.write_text("tenant-database-password", encoding="utf-8")
    password.chmod(0o600)
    alembic = tmp_path / "alembic-tenant.ini"
    alembic.write_text("[alembic]\n", encoding="utf-8")
    commands = Commands()
    neo4j = Neo4jMigrations()
    runner = FixedActiveTenantMigrationRunner(
        commands,
        neo4j,  # type: ignore[arg-type]
        alembic,
        FileSecretReader(secrets_root),
        postgres_host="postgres",
        postgres_port=5432,
    )
    route = _route(tenant_id=tenant_id)

    runner.migrate_postgres(route)
    runner.migrate_neo4j(route)

    arguments, environment = commands.calls[0]
    assert arguments[-2:] == ("upgrade", POSTGRES_TARGET_VERSION)
    assert "tenant-database-password" not in " ".join(arguments)
    assert environment is not None
    assert "tenant-database-password" in environment["TENANT_DATABASE_URL"]
    assert neo4j.manifests[0].tenant_id == tenant_id


def test_recovery_gate_accepts_exact_recent_backup_receipt(tmp_path: Path) -> None:
    route = _route()
    _write_recovery_receipt(tmp_path, route=route)
    gate = FilesystemMigrationRecoveryGate(
        tmp_path,
        clock=lambda: NOW + timedelta(hours=1),
    )

    gate.verify(route)


def test_recovery_gate_rejects_stale_or_wrong_tenant_backup(tmp_path: Path) -> None:
    route = _route()
    _write_recovery_receipt(
        tmp_path,
        route=route,
        created_at=NOW - timedelta(hours=25),
    )
    gate = FilesystemMigrationRecoveryGate(tmp_path, clock=lambda: NOW)

    with pytest.raises(RuntimeError, match="within the recovery-point RPO"):
        gate.verify(route)
    with pytest.raises(RuntimeError, match="within the recovery-point RPO"):
        gate.verify(_route(tenant_id="tenant-b"))


def test_recovery_gate_rejects_manifest_changed_after_receipt(tmp_path: Path) -> None:
    route = _route()
    _, manifest_path = _write_recovery_receipt(tmp_path, route=route)
    manifest_path.write_text('{"complete": false}', encoding="utf-8")
    gate = FilesystemMigrationRecoveryGate(
        tmp_path,
        clock=lambda: NOW + timedelta(hours=1),
    )

    with pytest.raises(RuntimeError, match="within the recovery-point RPO"):
        gate.verify(route)

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_memory_service.backup import (
    AgeEncryptionCommandAdapter,
    BackupArtifactManifest,
    BackupCompatibility,
    BackupManifest,
    BackupRestoreService,
    BackupStore,
    BackupValidationError,
    EncryptionCommandFailed,
    EncryptionMetadata,
    IsolatedRestoreTarget,
    PrivateMemoryRestoreProof,
    RestoreDrillRecord,
    RestoreExpectations,
    RestoreProofTarget,
    RetentionPin,
    RetentionPlan,
    apply_retention,
    assess_rpo,
    plan_retention,
    verify_ciphertext,
)

NOW = datetime(2026, 8, 2, 12, tzinfo=UTC)


def private_memory_proof() -> PrivateMemoryRestoreProof:
    return PrivateMemoryRestoreProof(
        active=RestoreProofTarget(
            count=1,
            digest=hashlib.sha256(b"active-private-memory").hexdigest(),
            representative_digest=hashlib.sha256(b"active-representative").hexdigest(),
        ),
        correction_chain=RestoreProofTarget(
            count=1,
            digest=hashlib.sha256(b"private-correction-chain").hexdigest(),
            representative_digest=hashlib.sha256(b"correction-representative").hexdigest(),
        ),
        completed_erasure=RestoreProofTarget(
            count=1,
            digest=hashlib.sha256(b"private-erasure-tombstone").hexdigest(),
            representative_digest=hashlib.sha256(b"erasure-representative").hexdigest(),
        ),
    )


def backup_manifest(
    *,
    backup_id: str = "backup-001",
    tenant_id: str = "tenant-product-a",
    created_at: datetime = NOW,
    complete: bool = True,
    payload: bytes = b"encrypted-backup",
) -> BackupManifest:
    checksum = hashlib.sha256(payload).hexdigest()
    encryption = EncryptionMetadata(key_id="prod-backup-key-1")
    artifacts = tuple(
        BackupArtifactManifest(
            artifact_id=f"{backup_id}:{store.value}",
            object_key=f"nightly/{backup_id}/{store.value}.age",
            store=store,
            tenant_id=None if store is BackupStore.CONTROL_POSTGRES else tenant_id,
            store_version="17" if store is not BackupStore.TENANT_NEO4J else "2026.01",
            schema_version="1",
            created_at=created_at,
            ciphertext_sha256=checksum,
            ciphertext_bytes=len(payload),
            encryption=encryption,
            complete=complete,
        )
        for store in BackupStore
    )
    return BackupManifest(
        backup_id=backup_id,
        tenant_id=tenant_id,
        created_at=created_at,
        database_name="memory_tenant_product_a",
        database_role="memory_tenant_product_a_role",
        neo4j_service_name="memory-neo4j-tenant-product-a",
        expectations=RestoreExpectations(
            tenant_knowledge_digests=(hashlib.sha256(b"tenant-knowledge").hexdigest(),),
            governance_candidate_count=0,
            governance_digest=hashlib.sha256(b"[]").hexdigest(),
            agent_run_count=0,
            private_memory=private_memory_proof(),
        ),
        artifacts=artifacts,
        complete=complete,
    )


def compatibility() -> BackupCompatibility:
    return BackupCompatibility(
        store_versions={
            BackupStore.CONTROL_POSTGRES: frozenset({"17"}),
            BackupStore.TENANT_POSTGRES: frozenset({"17"}),
            BackupStore.TENANT_NEO4J: frozenset({"2026.01"}),
        },
        schema_versions={store: frozenset({"1"}) for store in BackupStore},
    )


def test_manifest_requires_every_store_and_rejects_domain_content() -> None:
    valid = backup_manifest()
    assert {artifact.store for artifact in valid.artifacts} == set(BackupStore)

    with pytest.raises(ValidationError, match="exactly one artifact"):
        BackupManifest(**{**valid.model_dump(), "artifacts": valid.artifacts[:-1]})
    with pytest.raises(ValidationError, match="Extra inputs"):
        BackupManifest(**{**valid.model_dump(), "memory_text": "a domain secret"})
    with pytest.raises(ValidationError, match="expectations"):
        BackupManifest(
            **{key: value for key, value in valid.model_dump().items() if key != "expectations"}
        )
    serialized = valid.model_dump_json()
    assert "tenant-knowledge" not in serialized
    assert '"version":2' in serialized


def test_verify_ciphertext_rejects_corruption_truncation_and_incomplete(
    tmp_path: Path,
) -> None:
    manifest = backup_manifest()
    artifact = manifest.artifacts[0]
    path = tmp_path / "artifact.age"
    path.write_bytes(b"encrypted-backup")
    verify_ciphertext(path, artifact)

    path.write_bytes(b"corrupted-backup")
    with pytest.raises(BackupValidationError, match="checksum mismatch"):
        verify_ciphertext(path, artifact)
    path.write_bytes(b"short")
    with pytest.raises(BackupValidationError, match="size mismatch"):
        verify_ciphertext(path, artifact)
    incomplete = artifact.model_copy(update={"complete": False})
    with pytest.raises(BackupValidationError, match="incomplete"):
        verify_ciphertext(path, incomplete)


class RecordingRunner:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.commands: list[tuple[str, ...]] = []

    def run(self, argv: Sequence[str]) -> None:
        self.commands.append(tuple(argv))
        if self.fail:
            raise RuntimeError("wrong identity")


def test_age_adapter_uses_argv_no_secret_content_and_protected_key_file(
    tmp_path: Path,
) -> None:
    source = tmp_path / "plain.dump"
    encrypted = tmp_path / "backup.age"
    identity = tmp_path / "identity.txt"
    source.write_bytes(b"database")
    identity.write_text("AGE-SECRET-KEY-DO-NOT-LOG")
    identity.chmod(0o600)
    runner = RecordingRunner()
    adapter = AgeEncryptionCommandAdapter(runner)

    adapter.encrypt(source, encrypted, identity)
    command = runner.commands[0]
    assert command[0:2] == ("age", "--encrypt")
    assert "AGE-SECRET-KEY-DO-NOT-LOG" not in " ".join(command)
    assert command[-1] == str(source)

    identity.chmod(0o644)
    with pytest.raises(BackupValidationError, match="group/world"):
        adapter.decrypt(encrypted, source, identity)


def test_age_adapter_surfaces_wrong_decryption_key_without_partial_success(
    tmp_path: Path,
) -> None:
    source = tmp_path / "backup.age"
    destination = tmp_path / "restore.dump"
    identity = tmp_path / "wrong-key.txt"
    source.write_bytes(b"ciphertext")
    identity.write_text("wrong")
    identity.chmod(0o600)

    with pytest.raises(EncryptionCommandFailed, match="decryption"):
        AgeEncryptionCommandAdapter(RecordingRunner(fail=True)).decrypt(
            source, destination, identity
        )


def test_retention_keeps_latest_seven_daily_four_weekly_and_incomplete() -> None:
    manifests = [
        backup_manifest(backup_id=f"backup-{day:02}", created_at=NOW - timedelta(days=day))
        for day in range(20)
    ]
    incomplete = backup_manifest(
        backup_id="backup-incomplete", created_at=NOW + timedelta(minutes=1), complete=False
    )
    plan = plan_retention([*manifests, incomplete])

    assert "backup-00" in plan.keep_backup_ids
    assert "backup-incomplete" in plan.keep_backup_ids
    assert len(plan.keep_backup_ids) >= 8
    assert plan.warnings == ("incomplete backup retained for inspection: backup-incomplete",)
    assert set(plan.keep_backup_ids).isdisjoint(plan.delete_backup_ids)


def test_retention_protects_newest_valid_recovery_point_for_every_tenant() -> None:
    tenant_a = backup_manifest(backup_id="tenant-a-newest", tenant_id="tenant-a", created_at=NOW)
    tenant_b = backup_manifest(
        backup_id="tenant-b-newest",
        tenant_id="tenant-b",
        created_at=NOW - timedelta(days=90),
    )

    plan = plan_retention([tenant_a, tenant_b], daily=0, weekly=0)

    assert set(plan.keep_backup_ids) == {"tenant-a-newest", "tenant-b-newest"}
    assert plan.delete_backup_ids == ()


def test_retention_pin_is_kept_with_machine_readable_status_and_reason() -> None:
    newest = backup_manifest(backup_id="backup-newest", created_at=NOW)
    pinned = backup_manifest(
        backup_id="backup-decommission",
        created_at=NOW - timedelta(days=90),
    )
    pin = RetentionPin(
        backup_id=pinned.backup_id,
        reason="open-decommission:grace-period",
        protected_until=NOW + timedelta(days=30),
    )

    plan = plan_retention([newest, pinned], daily=0, weekly=0, pins=(pin,))

    assert set(plan.keep_backup_ids) == {newest.backup_id, pinned.backup_id}
    assert plan.delete_backup_ids == ()
    assert plan.pins == (pin,)
    assert plan.pins[0].status == "pinned"
    assert plan.warnings == (
        "backup pinned by open-decommission:grace-period: backup-decommission",
    )


class FailingDeletion:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def delete_backup(self, backup_id: str) -> None:
        self.calls.append(backup_id)
        if backup_id == "old-2":
            raise OSError("object store unavailable")


def test_retention_execution_reports_failures_for_retry() -> None:
    deletion = FailingDeletion()
    result = apply_retention(
        RetentionPlan(keep_backup_ids=("newest",), delete_backup_ids=("old-1", "old-2")),
        deletion,
    )
    assert result.deleted_backup_ids == ("old-1",)
    assert result.failed_backup_ids == ("old-2",)
    assert deletion.calls == ["old-1", "old-2"]


def test_retention_execution_carries_pin_evidence() -> None:
    pin = RetentionPin(
        backup_id="decommission-backup",
        reason="open-decommission:finalizing",
    )

    result = apply_retention(
        RetentionPlan(
            keep_backup_ids=(pin.backup_id,),
            delete_backup_ids=(),
            pins=(pin,),
        ),
        FailingDeletion(),
    )

    assert result.pins == (pin,)


def test_rpo_is_overdue_after_24_hours_or_without_valid_backup() -> None:
    current = backup_manifest(created_at=NOW - timedelta(hours=23))
    assert not assess_rpo("tenant-product-a", [current], now=NOW).overdue
    assert assess_rpo("tenant-product-a", [current], now=NOW + timedelta(hours=2)).overdue
    assert assess_rpo("unknown-tenant", [current], now=NOW).overdue


class RecordingRestore:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def prepare(self, target: IsolatedRestoreTarget) -> None:
        self.calls.append(f"prepare:{target.target_id}")

    def restore_control_metadata(
        self,
        path: Path,
        artifact: BackupArtifactManifest,
        target: IsolatedRestoreTarget,
    ) -> None:
        self.calls.append(artifact.store.value)

    def restore_tenant_postgres(
        self,
        path: Path,
        artifact: BackupArtifactManifest,
        target: IsolatedRestoreTarget,
    ) -> None:
        self.calls.append(artifact.store.value)

    def restore_tenant_neo4j(
        self,
        path: Path,
        artifact: BackupArtifactManifest,
        target: IsolatedRestoreTarget,
    ) -> None:
        self.calls.append(artifact.store.value)


class Verification:
    def __init__(self, *, isolation: bool = True) -> None:
        self.isolation = isolation

    def verify_public_recall(self, target: IsolatedRestoreTarget) -> bool:
        return True

    def verify_governance(self, target: IsolatedRestoreTarget) -> bool:
        return True

    def verify_agent_runs(self, target: IsolatedRestoreTarget) -> bool:
        return True

    def verify_no_other_tenant_routing(self, target: IsolatedRestoreTarget) -> bool:
        return self.isolation


def write_artifacts(tmp_path: Path, manifest: BackupManifest) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for artifact in manifest.artifacts:
        path = tmp_path / f"{artifact.store.value}.age"
        path.write_bytes(b"encrypted-backup")
        paths[artifact.artifact_id] = path
    return paths


def restore(
    tmp_path: Path,
    *,
    verifier: Verification | None = None,
    manifest: BackupManifest | None = None,
) -> tuple[RestoreDrillRecord, RecordingRestore]:
    actual_manifest = manifest or backup_manifest()
    restore_port = RecordingRestore()
    service = BackupRestoreService(
        restore=restore_port,
        verifier=verifier or Verification(),
        compatibility=compatibility(),
    )
    report = service.restore_and_verify(
        manifest=actual_manifest,
        ciphertext_paths=write_artifacts(tmp_path, actual_manifest),
        target=IsolatedRestoreTarget(
            target_id="restore-drill-product-a-001",
            source_tenant_id="tenant-product-a",
        ),
        operator_id="operator-alice",
        started_at=NOW,
        completed_at=NOW + timedelta(hours=3, minutes=30),
    )
    return report, restore_port


def test_restore_uses_isolated_target_and_verifies_public_contracts(tmp_path: Path) -> None:
    report, restore_port = restore(tmp_path)
    assert report.outcome == "passed"
    assert report.within_rto
    assert report.passed_checks == (
        "public-recall",
        "governance",
        "agent-runs",
        "tenant-isolation",
    )
    assert restore_port.calls == [
        "prepare:restore-drill-product-a-001",
        "control-postgres",
        "tenant-postgres",
        "tenant-neo4j",
    ]


def test_restore_records_failed_isolation_and_rto(tmp_path: Path) -> None:
    report, _ = restore(tmp_path, verifier=Verification(isolation=False))
    assert report.outcome == "failed"
    assert not report.within_rto
    assert "tenant-isolation" not in report.passed_checks


def test_restore_rejects_active_or_incompatible_targets(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="isolated"):
        IsolatedRestoreTarget(
            target_id="restore-target",
            source_tenant_id="tenant-product-a",
            active_route=True,
        )

    incompatible = backup_manifest()
    artifacts = tuple(
        artifact.model_copy(update={"schema_version": "999"})
        if artifact.store is BackupStore.TENANT_NEO4J
        else artifact
        for artifact in incompatible.artifacts
    )
    incompatible = incompatible.model_copy(update={"artifacts": artifacts})
    with pytest.raises(BackupValidationError, match="unsupported.*schema"):
        restore(tmp_path, manifest=incompatible)

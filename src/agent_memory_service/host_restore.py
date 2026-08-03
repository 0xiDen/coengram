"""Production host orchestration for isolated, encrypted restore drills."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

from agent_memory_service.backup import (
    DEFAULT_RTO,
    AgeEncryptionCommandAdapter,
    BackupArtifactManifest,
    BackupManifest,
    BackupStore,
    BackupValidationError,
    RestoreDrillRecord,
    RestoreSafetyError,
    verify_ciphertext,
)
from agent_memory_service.restore_verify import (
    RestoreVerificationContext,
    RestoreVerificationReport,
)
from agent_memory_service.schema import CONTROL_SCHEMA_REVISION, TENANT_SCHEMA_REVISION

_SAFE_TENANT_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
_SAFE_TARGET_ID = re.compile(r"^restore-drill-[a-z0-9](?:[a-z0-9-]{0,119}[a-z0-9])?$")
_SAFE_OPERATOR_ID = re.compile(r"^[A-Za-z0-9._:@-]{1,160}$")
_SAFE_SECRET = re.compile(r"^[A-Za-z0-9_-]{16,256}$")
_SAFE_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,255}$")
_SAFE_POSTGRES_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_NEO4J_SCHEMA_REVISION = "1"
_POSTGRES_IMAGE = (
    "postgres:17.6-alpine@sha256:ef257d85f76e48da1c64832459b59fcaba1a4dac97bf5d7450c77753542eee94"
)
_NEO4J_IMAGE = (
    "neo4j:5.26.28-community"
    "@sha256:362542416de6c09a971484d1893878016cc3b5cdec166e54b1c824a220ecd6b9"
)

_NEO4J_START_SCRIPT = """\
set -euo pipefail
password="$(cat /run/secrets/neo4j_password)"
export NEO4J_AUTH="neo4j/${password}"
exec /startup/docker-entrypoint.sh neo4j
"""

_NEO4J_READY_SCRIPT = """\
set -euo pipefail
password="$(cat /run/secrets/neo4j_password)"
exec cypher-shell --username neo4j --password "${password}" "RETURN 1"
"""


class HostRestoreCommandRunner(Protocol):
    """Run one fixed argv vector without invoking a host shell."""

    def run(
        self,
        arguments: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None: ...

    def remove_docker_resource(
        self,
        kind: Literal["container", "volume", "network"],
        name: str,
    ) -> None: ...


class RestoreDrillVerifier(Protocol):
    """Verify restored behavior through canonical application interfaces."""

    def verify(self, context: RestoreVerificationContext) -> RestoreVerificationReport: ...


@dataclass(frozen=True, slots=True)
class HostRestoreConfig:
    """Pinned images, compatibility heads, and local restore workspace."""

    workspace_root: Path
    postgres_image: str = _POSTGRES_IMAGE
    postgres_store_version: str = "17.6-alpine"
    neo4j_image: str = _NEO4J_IMAGE
    neo4j_store_version: str = "5.26.28-community"
    neo4j_schema_version: str = _NEO4J_SCHEMA_REVISION
    verifier_embedding_model: str = "/opt/coengram/models/bge-small-en-v1.5"
    control_database_name: str = "memory_control"
    postgres_port: int = 5432
    rto: timedelta = DEFAULT_RTO
    ready_attempts: int = 30
    ready_interval_seconds: float = 2.0

    def __post_init__(self) -> None:
        root = self.workspace_root
        if root.is_symlink() or not root.is_dir():
            raise ValueError("Restore workspace root must be an existing real directory")
        if root.resolve() == Path(root.resolve().anchor):
            raise ValueError("Restore workspace root cannot be a filesystem root")
        for image in (self.postgres_image, self.neo4j_image):
            if _SAFE_IMAGE.fullmatch(image) is None:
                raise ValueError("Restore image reference is invalid")
        for value in (
            self.postgres_store_version,
            self.neo4j_store_version,
            self.neo4j_schema_version,
            self.verifier_embedding_model,
            self.control_database_name,
        ):
            if not value.strip() or any(character.isspace() for character in value):
                raise ValueError("Restore configuration contains an invalid value")
        if _SAFE_POSTGRES_IDENTIFIER.fullmatch(self.control_database_name) is None:
            raise ValueError("Restore Control database name is invalid")
        if not 1 <= self.postgres_port <= 65535:
            raise ValueError("Restore PostgreSQL port is invalid")
        if self.ready_attempts < 1 or self.ready_interval_seconds < 0:
            raise ValueError("Restore readiness policy is invalid")
        if self.rto <= timedelta(0):
            raise ValueError("Restore RTO must be positive")


@dataclass(frozen=True, slots=True)
class _RestoreResources:
    network: str
    postgres_container: str
    postgres_volume: str
    neo4j_container: str
    neo4j_volume: str
    neo4j_loader: str
    verifier_container: str

    @classmethod
    def for_target(cls, target_id: str) -> _RestoreResources:
        prefix = f"memory-{target_id}"
        return cls(
            network=prefix,
            postgres_container=f"{prefix}-postgres",
            postgres_volume=f"{prefix}-postgres-data",
            neo4j_container=f"{prefix}-neo4j",
            neo4j_volume=f"{prefix}-neo4j-data",
            neo4j_loader=f"{prefix}-neo4j-loader",
            verifier_container=f"{prefix}-verifier",
        )


class HostRestoreDrillExecutor:
    """Restore one recovery point into an internal disposable Docker namespace."""

    def __init__(
        self,
        *,
        runner: HostRestoreCommandRunner,
        verifier: RestoreDrillVerifier,
        config: HostRestoreConfig,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
        secret_factory: Callable[[], str] | None = None,
    ) -> None:
        self._runner = runner
        self._verifier = verifier
        self._config = config
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleeper = sleeper or time.sleep
        self._secret_factory = secret_factory or (lambda: secrets.token_urlsafe(32))
        self._workspace_root = config.workspace_root.resolve()

    def execute(
        self,
        *,
        manifest: BackupManifest,
        ciphertext_paths: Mapping[str, Path],
        target_id: str,
        operator_id: str,
        age_identity_file: Path,
    ) -> RestoreDrillRecord:
        artifacts = self._validate_request(
            manifest,
            ciphertext_paths,
            target_id,
            operator_id,
            age_identity_file,
        )
        started_at = self._now("Restore start")
        workspace = self._create_workspace(target_id)
        resources = _RestoreResources.for_target(target_id)
        secret_directory = workspace / "secrets"
        tenant_secrets = secret_directory / "tenants" / manifest.tenant_id
        tenant_secrets.mkdir(parents=True, mode=0o700)

        postgres_superuser_password = self._new_secret()
        tenant_postgres_password = self._new_secret()
        neo4j_password = self._new_secret()
        self._write_private(
            secret_directory / "postgres_superuser_password",
            postgres_superuser_password,
        )
        self._write_private(tenant_secrets / "postgres_password", tenant_postgres_password)
        self._write_private(tenant_secrets / "neo4j_password", neo4j_password)
        self._write_private(
            workspace / "initialize-tenant.sql",
            self._tenant_initialization_sql(manifest, tenant_postgres_password),
        )
        self._write_private(
            workspace / "sanitize-control.sql",
            self._control_sanitization_sql(manifest),
        )

        plaintext = self._decrypt_artifacts(
            artifacts,
            ciphertext_paths,
            age_identity_file,
            workspace,
        )
        self._create_resources(resources, target_id)
        self._restore_neo4j_offline(resources, workspace)
        self._start_postgres(resources, workspace, secret_directory)
        self._wait_for_postgres(resources)
        self._restore_postgres(resources, manifest, plaintext)
        self._start_neo4j(resources, tenant_secrets, manifest.neo4j_service_name)
        self._wait_for_neo4j(resources)

        report = self._verifier.verify(
            RestoreVerificationContext(
                target_id=target_id,
                source_tenant_id=manifest.tenant_id,
                network_name=resources.network,
                workspace=workspace,
                secrets_directory=secret_directory,
                control_database_name=self._config.control_database_name,
                control_database_user="postgres",
                postgres_host="postgres",
                postgres_port=self._config.postgres_port,
                embedding_model=self._config.verifier_embedding_model,
                expectations=manifest.expectations,
            )
        )
        if report.passed:
            self._cleanup(resources, workspace)
        completed_at = self._now("Restore completion")
        if completed_at < started_at:
            raise ValueError("Restore completion cannot precede its start")
        duration = (completed_at - started_at).total_seconds()
        outcome: Literal["passed", "failed"] = "passed" if report.passed else "failed"
        return RestoreDrillRecord(
            backup_id=manifest.backup_id,
            target_id=target_id,
            tenant_id=manifest.tenant_id,
            operator_id=operator_id,
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=duration,
            passed_checks=report.passed_checks,
            outcome=outcome,
            within_rto=report.passed and duration <= self._config.rto.total_seconds(),
        )

    def cleanup(self, target_id: str) -> None:
        """Remove one preserved failed-drill target after exact operator confirmation."""

        if _SAFE_TARGET_ID.fullmatch(target_id) is None:
            raise RestoreSafetyError("restore target identifier is invalid")
        workspace = self._workspace_root / target_id
        if (
            workspace.is_symlink()
            or workspace.parent != self._workspace_root
            or not workspace.name.startswith("restore-drill-")
            or not workspace.is_dir()
        ):
            raise RestoreSafetyError("preserved restore workspace does not exist")
        self._cleanup(_RestoreResources.for_target(target_id), workspace)

    def _validate_request(
        self,
        manifest: BackupManifest,
        ciphertext_paths: Mapping[str, Path],
        target_id: str,
        operator_id: str,
        age_identity_file: Path,
    ) -> Mapping[BackupStore, BackupArtifactManifest]:
        if _SAFE_TARGET_ID.fullmatch(target_id) is None or target_id == manifest.tenant_id:
            raise RestoreSafetyError("restore target must be an exact restore-drill namespace")
        if _SAFE_OPERATOR_ID.fullmatch(operator_id) is None:
            raise RestoreSafetyError("restore operator identifier is invalid")
        if _SAFE_TENANT_ID.fullmatch(manifest.tenant_id) is None:
            raise RestoreSafetyError("backup tenant identifier is invalid")
        if not manifest.complete:
            raise BackupValidationError("incomplete backup cannot be restored")
        normalized = manifest.tenant_id.replace("-", "_")
        if (
            manifest.database_name != f"tenant_{normalized}"
            or manifest.database_role != f"tenant_{normalized}_rw"
            or manifest.neo4j_service_name != f"neo4j-{manifest.tenant_id}"
        ):
            raise RestoreSafetyError("backup route metadata is not canonical")
        expected_ids = {artifact.artifact_id for artifact in manifest.artifacts}
        if set(ciphertext_paths) != expected_ids:
            raise BackupValidationError("restore requires exactly the manifest artifacts")
        self._validate_identity(age_identity_file)

        by_store = {artifact.store: artifact for artifact in manifest.artifacts}
        expected_versions = {
            BackupStore.CONTROL_POSTGRES: (
                self._config.postgres_store_version,
                CONTROL_SCHEMA_REVISION,
            ),
            BackupStore.TENANT_POSTGRES: (
                self._config.postgres_store_version,
                TENANT_SCHEMA_REVISION,
            ),
            BackupStore.TENANT_NEO4J: (
                self._config.neo4j_store_version,
                self._config.neo4j_schema_version,
            ),
        }
        for store, artifact in by_store.items():
            store_version, schema_version = expected_versions[store]
            if artifact.store_version != store_version:
                raise RestoreSafetyError(f"{store.value} store version is incompatible")
            if artifact.schema_version != schema_version:
                raise RestoreSafetyError(f"{store.value} schema version is incompatible")
            path = ciphertext_paths[artifact.artifact_id]
            if path.is_symlink():
                raise BackupValidationError("restore ciphertext cannot be a symbolic link")
            verify_ciphertext(path, artifact)
        return by_store

    @staticmethod
    def _validate_identity(path: Path) -> None:
        if path.is_symlink() or not path.is_file():
            raise BackupValidationError("age identity is not a regular file")
        if path.stat().st_mode & 0o077:
            raise BackupValidationError("age identity must not be group/world accessible")
        if path.stat().st_size < 1:
            raise BackupValidationError("age identity is empty")

    def _create_workspace(self, target_id: str) -> Path:
        workspace = self._workspace_root / target_id
        if workspace.parent != self._workspace_root or workspace.exists() or workspace.is_symlink():
            raise RestoreSafetyError("restore workspace is not a new exact target")
        workspace.mkdir(mode=0o700)
        return workspace

    def _new_secret(self) -> str:
        value = self._secret_factory()
        if _SAFE_SECRET.fullmatch(value) is None:
            raise RestoreSafetyError("generated restore credential is invalid")
        return value

    @staticmethod
    def _write_private(path: Path, value: str) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, value.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _decrypt_artifacts(
        self,
        artifacts: Mapping[BackupStore, BackupArtifactManifest],
        ciphertext_paths: Mapping[str, Path],
        identity: Path,
        workspace: Path,
    ) -> Mapping[BackupStore, Path]:
        names = {
            BackupStore.CONTROL_POSTGRES: "control-postgres.dump",
            BackupStore.TENANT_POSTGRES: "tenant-postgres.dump",
            BackupStore.TENANT_NEO4J: "neo4j.dump",
        }
        decrypted: dict[BackupStore, Path] = {}
        adapter = AgeEncryptionCommandAdapter(self._runner)
        for store in BackupStore:
            artifact = artifacts[store]
            destination = workspace / names[store]
            adapter.decrypt(ciphertext_paths[artifact.artifact_id], destination, identity)
            if destination.is_symlink() or not destination.is_file():
                raise BackupValidationError(f"age did not decrypt {store.value}")
            if destination.stat().st_size < 1:
                raise BackupValidationError(f"decrypted {store.value} is empty")
            destination.chmod(0o600)
            decrypted[store] = destination
        return decrypted

    def _create_resources(self, resources: _RestoreResources, target_id: str) -> None:
        label = f"memory.restore.target={target_id}"
        self._run(
            "docker",
            "network",
            "create",
            "--internal",
            "--label",
            label,
            resources.network,
        )
        self._run(
            "docker",
            "volume",
            "create",
            "--label",
            label,
            resources.postgres_volume,
        )
        self._run(
            "docker",
            "volume",
            "create",
            "--label",
            label,
            resources.neo4j_volume,
        )

    def _restore_neo4j_offline(
        self,
        resources: _RestoreResources,
        workspace: Path,
    ) -> None:
        dump = workspace / "neo4j.dump"
        dump.chmod(0o640)
        try:
            self._run(
                "docker",
                "run",
                "--name",
                resources.neo4j_loader,
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
                f"{resources.neo4j_volume}:/data",
                "--volume",
                f"{workspace}:/backups:ro",
                "--entrypoint",
                "/var/lib/neo4j/bin/neo4j-admin",
                self._config.neo4j_image,
                "database",
                "load",
                "neo4j",
                "--from-path=/backups",
                "--overwrite-destination=true",
            )
        finally:
            dump.chmod(0o600)

    def _start_postgres(
        self,
        resources: _RestoreResources,
        workspace: Path,
        secret_directory: Path,
    ) -> None:
        self._run(
            "docker",
            "run",
            "--detach",
            "--name",
            resources.postgres_container,
            "--network",
            resources.network,
            "--network-alias",
            "postgres",
            "--restart",
            "no",
            "--volume",
            f"{resources.postgres_volume}:/var/lib/postgresql/data",
            "--volume",
            f"{workspace}:/restore:ro",
            "--volume",
            f"{secret_directory}:/run/restore-secrets:ro",
            "--env",
            "POSTGRES_PASSWORD_FILE=/run/restore-secrets/postgres_superuser_password",
            "--env",
            "POSTGRES_USER=postgres",
            "--env",
            "POSTGRES_DB=postgres",
            self._config.postgres_image,
        )

    def _wait_for_postgres(self, resources: _RestoreResources) -> None:
        self._wait_until_ready(
            (
                "docker",
                "exec",
                resources.postgres_container,
                "pg_isready",
                "--username=postgres",
                "--dbname=postgres",
            ),
            "PostgreSQL",
        )

    def _restore_postgres(
        self,
        resources: _RestoreResources,
        manifest: BackupManifest,
        plaintext: Mapping[BackupStore, Path],
    ) -> None:
        del plaintext  # validated host paths are exposed at fixed container paths
        self._run(
            "docker",
            "exec",
            resources.postgres_container,
            "createdb",
            "--username=postgres",
            "--template=template0",
            self._config.control_database_name,
        )
        self._run(
            "docker",
            "exec",
            resources.postgres_container,
            "pg_restore",
            "--exit-on-error",
            "--no-owner",
            "--no-acl",
            "--username=postgres",
            f"--dbname={self._config.control_database_name}",
            "/restore/control-postgres.dump",
        )
        self._run(
            "docker",
            "exec",
            resources.postgres_container,
            "psql",
            "--username=postgres",
            "--dbname=postgres",
            "--file=/restore/initialize-tenant.sql",
        )
        self._run(
            "docker",
            "exec",
            resources.postgres_container,
            "pg_restore",
            "--exit-on-error",
            "--no-owner",
            "--no-acl",
            "--username=postgres",
            f"--role={manifest.database_role}",
            f"--dbname={manifest.database_name}",
            "/restore/tenant-postgres.dump",
        )
        self._run(
            "docker",
            "exec",
            resources.postgres_container,
            "psql",
            "--username=postgres",
            f"--dbname={self._config.control_database_name}",
            "--file=/restore/sanitize-control.sql",
        )

    def _start_neo4j(
        self,
        resources: _RestoreResources,
        tenant_secrets: Path,
        service_name: str,
    ) -> None:
        self._run(
            "docker",
            "run",
            "--detach",
            "--name",
            resources.neo4j_container,
            "--network",
            resources.network,
            "--network-alias",
            service_name,
            "--restart",
            "no",
            "--volume",
            f"{resources.neo4j_volume}:/data",
            "--volume",
            f"{tenant_secrets / 'neo4j_password'}:/run/secrets/neo4j_password:ro",
            "--env",
            "NEO4J_server_default__listen__address=0.0.0.0",
            "--env",
            f"NEO4J_server_bolt_advertised__address={service_name}:7687",
            "--entrypoint",
            "/bin/bash",
            self._config.neo4j_image,
            "-ec",
            _NEO4J_START_SCRIPT,
        )

    def _wait_for_neo4j(self, resources: _RestoreResources) -> None:
        self._wait_until_ready(
            (
                "docker",
                "exec",
                resources.neo4j_container,
                "/bin/bash",
                "-ec",
                _NEO4J_READY_SCRIPT,
            ),
            "Neo4j",
        )

    def _wait_until_ready(self, command: Sequence[str], service: str) -> None:
        for attempt in range(self._config.ready_attempts):
            try:
                self._runner.run(command)
            except Exception as exc:  # noqa: BLE001 - readiness adapters vary
                if attempt + 1 == self._config.ready_attempts:
                    message = f"{service} restore target did not become ready"
                    raise RestoreSafetyError(message) from exc
                self._sleeper(self._config.ready_interval_seconds)
            else:
                return

    def _cleanup(self, resources: _RestoreResources, workspace: Path) -> None:
        if (
            workspace.is_symlink()
            or workspace.parent != self._workspace_root
            or not workspace.name.startswith("restore-drill-")
        ):
            raise RestoreSafetyError("refusing to clean a non-exact restore workspace")

        removals: tuple[tuple[Literal["container", "volume", "network"], str], ...] = (
            ("container", resources.verifier_container),
            ("container", resources.neo4j_container),
            ("container", resources.postgres_container),
            ("container", resources.neo4j_loader),
            ("volume", resources.neo4j_volume),
            ("volume", resources.postgres_volume),
            ("network", resources.network),
        )
        failed = False
        for kind, name in removals:
            try:
                self._runner.remove_docker_resource(kind, name)
            except Exception:  # noqa: BLE001 - adapters sanitize command failures
                failed = True
        if failed:
            raise RestoreSafetyError(
                "restore resource cleanup was incomplete; retry the exact target"
            )
        shutil.rmtree(workspace)

    def _run(self, *arguments: str) -> None:
        self._runner.run(arguments)

    def _now(self, label: str) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError(f"{label} clock must be timezone-aware")
        return value

    @staticmethod
    def _tenant_initialization_sql(manifest: BackupManifest, password: str) -> str:
        return (
            "\\set ON_ERROR_STOP on\n"
            f"CREATE ROLE {manifest.database_role} LOGIN PASSWORD '{password}';\n"
            f"CREATE DATABASE {manifest.database_name} OWNER {manifest.database_role} "
            "TEMPLATE template0;\n"
        )

    @staticmethod
    def _control_sanitization_sql(manifest: BackupManifest) -> str:
        tenant = manifest.tenant_id
        neo4j_address = f"neo4j-{tenant}:7687"
        neo4j_secret = f"{tenant}/neo4j_password"
        return f"""\
\\set ON_ERROR_STOP on
BEGIN;
DELETE FROM control.backup_barriers;
UPDATE control.routing SET healthy = false, checked_at = CURRENT_TIMESTAMP,
    updated_at = CURRENT_TIMESTAMP;
UPDATE control.tenants SET active = (tenant_id = '{tenant}'),
    updated_at = CURRENT_TIMESTAMP;
UPDATE control.memberships SET active = false, updated_at = CURRENT_TIMESTAMP
    WHERE tenant_id <> '{tenant}';
UPDATE control.delegations SET active = false, updated_at = CURRENT_TIMESTAMP
    WHERE tenant_id <> '{tenant}';
UPDATE control.channel_bindings SET active = false, updated_at = CURRENT_TIMESTAMP
    WHERE tenant_id <> '{tenant}';
UPDATE control.access_tokens SET revoked_at = COALESCE(revoked_at, CURRENT_TIMESTAMP)
    WHERE tenant_id <> '{tenant}';
UPDATE control.routing
SET neo4j_service_address = '{neo4j_address}',
    neo4j_secret_name = '{neo4j_secret}',
    tenant_database_name = '{manifest.database_name}',
    tenant_database_role = '{manifest.database_role}',
    healthy = true,
    checked_at = CURRENT_TIMESTAMP,
    updated_at = CURRENT_TIMESTAMP
WHERE tenant_id = '{tenant}';
COMMIT;
"""

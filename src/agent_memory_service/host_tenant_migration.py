"""Production composition for fixed active-Tenant schema migrations."""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import psycopg

from agent_memory_service.backup import DEFAULT_RPO, BackupManifest
from agent_memory_service.control import ControlModule, TenantRouteRecord
from agent_memory_service.database_url import (
    psycopg_database_url as _psycopg_database_url,
)
from agent_memory_service.decommission import ProtectionKind
from agent_memory_service.host_decommission import ProtectionEvidenceReceipt
from agent_memory_service.host_provisioning import (
    HostCommandRunner,
    HostComposeRunner,
    HostTenantMigrationRunner,
    HostTenantProvisioningVerifier,
    SubprocessHostCommandRunner,
)
from agent_memory_service.manifest import TenantManifest
from agent_memory_service.pseudonyms import TelemetryPseudonymizer
from agent_memory_service.routing import FileSecretReader
from agent_memory_service.stores.postgres_tenant_migration import (
    PostgresActiveMigrationStateStore,
)
from agent_memory_service.tenant_migration import (
    POSTGRES_TARGET_VERSION,
    ActiveTenantMigrationService,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class FilesystemMigrationRecoveryGate:
    """Require one exact, recent, checksummed backup before schema mutation."""

    def __init__(
        self,
        receipt_root: Path,
        *,
        max_age: timedelta = DEFAULT_RPO,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        root = receipt_root.resolve()
        if not root.is_dir():
            raise ValueError("Migration recovery evidence root must be a directory")
        if max_age <= timedelta(0):
            raise ValueError("Migration recovery evidence max age must be positive")
        self._root = root
        self._max_age = max_age
        self._clock = clock

    def verify(self, route: TenantRouteRecord) -> None:
        now = self._clock()
        if now.tzinfo is None:
            raise RuntimeError("Migration recovery evidence clock must be timezone-aware")
        for receipt_path in self._root.iterdir():
            if self._valid_receipt(receipt_path, route, now):
                return
        raise RuntimeError("No valid Tenant backup exists within the recovery-point RPO")

    def _valid_receipt(
        self,
        receipt_path: Path,
        route: TenantRouteRecord,
        now: datetime,
    ) -> bool:
        if (
            receipt_path.suffix != ".json"
            or receipt_path.is_symlink()
            or not receipt_path.is_file()
            or receipt_path.stat().st_size > 1_000_000
        ):
            return False
        try:
            receipt_bytes = receipt_path.read_bytes()
            receipt = ProtectionEvidenceReceipt.model_validate_json(receipt_bytes)
        except (OSError, ValueError):
            return False
        evidence = receipt.evidence
        if (
            receipt.tenant_id != route.tenant_id
            or receipt_path.name != f"{evidence.artifact_id}.json"
            or evidence.kind is not ProtectionKind.BACKUP
            or not evidence.integrity_verified
            or not evidence.complete
            or evidence.created_at > now
            or evidence.verified_at > now
            or now - evidence.created_at > self._max_age
        ):
            return False
        manifest_path = Path(receipt.artifact_manifest_path)
        if (
            not manifest_path.is_absolute()
            or manifest_path.is_symlink()
            or not manifest_path.is_file()
            or manifest_path.stat().st_size > 1_000_000
        ):
            return False
        try:
            manifest_bytes = manifest_path.read_bytes()
            manifest = BackupManifest.model_validate_json(manifest_bytes)
        except (OSError, ValueError):
            return False
        manifest_checksum = hashlib.sha256(manifest_bytes).hexdigest()
        return (
            manifest_checksum == receipt.artifact_manifest_sha256
            and manifest.backup_id == evidence.artifact_id
            and manifest.tenant_id == route.tenant_id
            and manifest.created_at == evidence.created_at
            and manifest.database_name == route.tenant_database_name
            and manifest.database_role == route.tenant_database_role
            and manifest.neo4j_service_name == route.neo4j_service_address.removesuffix(":7687")
            and manifest.complete
        )


class FixedActiveTenantMigrationRunner:
    """Apply only the code-pinned target, never an operator-selected revision."""

    def __init__(
        self,
        commands: HostCommandRunner,
        neo4j_runner: HostTenantMigrationRunner,
        alembic_config: Path,
        secrets: FileSecretReader,
        *,
        postgres_host: str,
        postgres_port: int,
    ) -> None:
        config = alembic_config.resolve()
        if alembic_config.is_symlink() or not config.is_file():
            raise ValueError("Tenant Alembic configuration must be a regular file")
        self._commands = commands
        self._neo4j_runner = neo4j_runner
        self._alembic_config = config
        self._secrets = secrets
        self._postgres_host = postgres_host
        self._postgres_port = postgres_port

    def migrate_postgres(self, route: TenantRouteRecord) -> None:
        password = self._secrets.read(f"{route.tenant_id}/postgres_password")
        database_url = (
            f"postgresql://{quote(route.tenant_database_role, safe='')}:"
            f"{quote(password, safe='')}@{self._postgres_host}:{self._postgres_port}/"
            f"{quote(route.tenant_database_name, safe='')}"
        )
        self._commands.run(
            (
                sys.executable,
                "-m",
                "alembic",
                "-c",
                str(self._alembic_config),
                "upgrade",
                POSTGRES_TARGET_VERSION,
            ),
            environment={"TENANT_DATABASE_URL": database_url},
        )

    def migrate_neo4j(self, route: TenantRouteRecord) -> None:
        self._neo4j_runner.migrate_neo4j(_manifest(route))


class HostActiveTenantMigrationAdapter:
    """Run only repository-registered migrations and active-route verifications."""

    def __init__(
        self,
        migration_runner: FixedActiveTenantMigrationRunner,
        verifier: HostTenantProvisioningVerifier,
        recovery: FilesystemMigrationRecoveryGate,
        control_database_url: str,
    ) -> None:
        self._migrations = migration_runner
        self._verifier = verifier
        self._recovery = recovery
        self._control_database_url = _psycopg_database_url(control_database_url)

    def verify_recovery_point(self, route: TenantRouteRecord) -> None:
        self._recovery.verify(route)

    def migrate_postgres(self, route: TenantRouteRecord) -> None:
        self._migrations.migrate_postgres(route)

    def migrate_neo4j(self, route: TenantRouteRecord) -> None:
        self._migrations.migrate_neo4j(route)

    def verify_neo4j_health(self, route: TenantRouteRecord) -> None:
        self._verifier.verify_neo4j_health(_manifest(route))

    def verify_routing(self, route: TenantRouteRecord) -> None:
        with psycopg.connect(self._control_database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT t.active, r.neo4j_service_address, r.neo4j_secret_name,
                           r.tenant_database_name, r.tenant_database_role, r.healthy
                    FROM control.tenants AS t
                    JOIN control.routing AS r ON r.tenant_id = t.tenant_id
                    WHERE t.tenant_id = %s
                    """,
                    (route.tenant_id,),
                )
                actual = cursor.fetchone()
        expected = (
            True,
            route.neo4j_service_address,
            route.neo4j_secret_name,
            route.tenant_database_name,
            route.tenant_database_role,
            True,
        )
        if actual != expected:
            raise RuntimeError("Active Tenant route changed during migration")

    def verify_isolation(self, route: TenantRouteRecord) -> None:
        self._verifier.verify_isolation(_manifest(route))


def create_host_active_tenant_migrator(
    *,
    control: ControlModule,
    control_database_url: str,
    postgres_host: str,
    postgres_port: int,
    secrets_root: Path,
    evidence_root: Path,
    repository_root: Path,
    telemetry_pseudonymizer: TelemetryPseudonymizer,
    command_runner: HostCommandRunner | None = None,
) -> ActiveTenantMigrationService:
    root = repository_root.resolve()
    secrets = FileSecretReader(secrets_root.resolve())
    commands = command_runner or SubprocessHostCommandRunner(root)
    compose = HostComposeRunner(
        commands,
        root / "deploy" / "tenant.compose.yaml",
        secrets_root,
        telemetry_pseudonymizer,
    )
    neo4j_migrations = HostTenantMigrationRunner(
        commands,
        compose,
        root / "alembic-tenant.ini",
        secrets,
        postgres_host=postgres_host,
        postgres_port=postgres_port,
    )
    migrations = FixedActiveTenantMigrationRunner(
        commands,
        neo4j_migrations,
        root / "alembic-tenant.ini",
        secrets,
        postgres_host=postgres_host,
        postgres_port=postgres_port,
    )
    verifier = HostTenantProvisioningVerifier(
        control_database_url,
        compose,
        secrets,
        postgres_host=postgres_host,
        postgres_port=postgres_port,
    )
    return ActiveTenantMigrationService(
        control,
        PostgresActiveMigrationStateStore(control_database_url),
        HostActiveTenantMigrationAdapter(
            migrations,
            verifier,
            FilesystemMigrationRecoveryGate(evidence_root),
            control_database_url,
        ),
    )


def _manifest(route: TenantRouteRecord) -> TenantManifest:
    return TenantManifest(
        tenant_id=route.tenant_id,
        name=f"Active migration for {route.tenant_id}",
        database_name=route.tenant_database_name,
        database_role=route.tenant_database_role,
        neo4j_service_name=route.neo4j_service_address.removesuffix(":7687"),
    )

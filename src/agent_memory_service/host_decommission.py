"""Exact-resource host adapters for guarded Tenant decommission operations."""

from __future__ import annotations

import hashlib
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import psycopg
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field

from agent_memory_service.database_url import (
    psycopg_database_url as _psycopg_database_url,
)
from agent_memory_service.decommission import (
    DecommissionConflict,
    DecommissionService,
    ProtectionEvidence,
    TenantResourceIdentity,
)
from agent_memory_service.host_provisioning import (
    HostCommandRunner,
    HostComposeRunner,
    SubprocessHostCommandRunner,
)
from agent_memory_service.manifest import TenantManifest
from agent_memory_service.pseudonyms import TelemetryPseudonymizer
from agent_memory_service.routing import FileSecretReader
from agent_memory_service.schema import TENANT_SCHEMA_REVISION, SchemaRequirement, require_schema
from agent_memory_service.stores.postgres_decommission import (
    PostgresDecommissionStore,
    PostgresTenantAccessAdapter,
)


class ProtectionEvidenceReceipt(BaseModel):
    """Protected local registry entry written by the backup/export workflow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    tenant_id: str
    evidence: ProtectionEvidence
    artifact_manifest_path: str = Field(min_length=1, max_length=1024)
    artifact_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class FilesystemProtectionEvidenceAdapter:
    """Verify confirmation evidence against a protected, content-free receipt."""

    def __init__(self, receipt_root: Path) -> None:
        root = receipt_root.resolve()
        if not root.is_dir():
            raise ValueError("Protection evidence root must be a directory")
        self._root = root

    def verify(self, tenant_id: str, evidence: ProtectionEvidence) -> bool:
        path = (self._root / f"{evidence.artifact_id}.json").resolve()
        if path.parent != self._root or path.is_symlink() or not path.is_file():
            return False
        if path.stat().st_size > 1_000_000:
            return False
        try:
            receipt = ProtectionEvidenceReceipt.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return False
        artifact = Path(receipt.artifact_manifest_path)
        if artifact.is_symlink() or not artifact.is_file():
            return False
        try:
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        except OSError:
            return False
        return (
            receipt.tenant_id == tenant_id
            and receipt.evidence == evidence
            and digest == receipt.artifact_manifest_sha256
        )


class HostTenantReadinessAdapter:
    """Read-only cancellation checks over exact Control Store and secret targets."""

    def __init__(
        self,
        control_database_url: str,
        secrets_root: Path,
        *,
        compose: HostComposeRunner,
        postgres_host: str,
        postgres_port: int,
    ) -> None:
        self._database_url = _psycopg_database_url(control_database_url)
        self._secrets_root = secrets_root.resolve()
        self._secrets = FileSecretReader(self._secrets_root)
        self._compose = compose
        self._postgres_host = postgres_host
        self._postgres_port = postgres_port

    def verify_route(self, resources: TenantResourceIdentity) -> bool:
        row = self._fetch_one(
            """
            SELECT t.active, r.neo4j_service_address, r.neo4j_secret_name,
                   r.tenant_database_name, r.tenant_database_role
            FROM control.tenants AS t
            JOIN control.routing AS r ON r.tenant_id = t.tenant_id
            WHERE t.tenant_id = %s
            """,
            (resources.tenant_id,),
        )
        return row == (
            False,
            f"neo4j-{resources.tenant_id}:7687",
            f"{resources.tenant_id}/neo4j_password",
            resources.database_name,
            resources.database_role,
        )

    def verify_schema(self, resources: TenantResourceIdentity) -> bool:
        require_schema(
            SchemaRequirement(
                database_url=self._tenant_database_url(resources),
                expected_revision=TENANT_SCHEMA_REVISION,
                store_name="suspended Tenant Store",
            )
        )
        self._compose.verify_neo4j_schema(self._manifest(resources))
        return True

    def verify_isolation(self, resources: TenantResourceIdentity) -> bool:
        row = self._fetch_one(
            """
            SELECT count(*)
            FROM control.routing
            WHERE tenant_id <> %s
              AND (
                  tenant_database_name = %s
                  OR tenant_database_role = %s
                  OR neo4j_service_address = %s
                  OR neo4j_secret_name = %s
              )
            """,
            (
                resources.tenant_id,
                resources.database_name,
                resources.database_role,
                f"neo4j-{resources.tenant_id}:7687",
                f"{resources.tenant_id}/neo4j_password",
            ),
        )
        return row == (0,)

    def verify_credentials(self, resources: TenantResourceIdentity) -> bool:
        directory = Path(resources.tenant_secret_ref).resolve()
        if directory != self._secrets_root / resources.tenant_id or not directory.is_dir():
            return False
        directory_details = directory.stat()
        if stat.S_IMODE(directory_details.st_mode) != 0o750:
            return False
        for name in ("neo4j_password", "postgres_password"):
            path = directory / name
            if path.is_symlink() or not path.is_file():
                return False
            details = path.stat()
            if stat.S_IMODE(details.st_mode) != 0o640 or details.st_gid != directory_details.st_gid:
                return False
            if not path.read_text(encoding="utf-8").strip():
                return False
        return True

    def verify_health(self, resources: TenantResourceIdentity) -> bool:
        with psycopg.connect(self._tenant_database_url(resources)) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                if cursor.fetchone() != (1,):
                    return False
        self._compose.verify_neo4j_health(self._manifest(resources))
        return True

    def _tenant_database_url(self, resources: TenantResourceIdentity) -> str:
        from urllib.parse import quote

        password = self._secrets.read(f"{resources.tenant_id}/postgres_password")
        return (
            f"postgresql://{quote(resources.database_role, safe='')}:"
            f"{quote(password, safe='')}@{self._postgres_host}:{self._postgres_port}/"
            f"{quote(resources.database_name, safe='')}"
        )

    @staticmethod
    def _manifest(resources: TenantResourceIdentity) -> TenantManifest:
        return TenantManifest(
            tenant_id=resources.tenant_id,
            name="Suspended Tenant readiness verification",
        )

    def _fetch_one(
        self, statement: str, parameters: tuple[object, ...]
    ) -> tuple[object, ...] | None:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(statement, parameters)
                return cursor.fetchone()


class HostTenantDestructionAdapter:
    """Idempotent host removal of only the immutable Tenant resource identity."""

    def __init__(
        self,
        *,
        command_runner: HostCommandRunner,
        compose_file: Path,
        secrets_root: Path,
        control_database_url: str,
        postgres_admin_url: str,
        telemetry_pseudonymizer: TelemetryPseudonymizer,
    ) -> None:
        compose = compose_file.resolve()
        if compose_file.is_symlink() or not compose.is_file():
            raise ValueError("Tenant Compose file must be a regular file")
        self._commands = command_runner
        self._compose_file = compose
        self._secrets_root = secrets_root.resolve()
        self._control_database_url = _psycopg_database_url(control_database_url)
        self._postgres_admin_url = _psycopg_database_url(postgres_admin_url)
        self._telemetry_pseudonymizer = telemetry_pseudonymizer

    def remove_compose_project(self, resources: TenantResourceIdentity) -> None:
        self._validate_secret_target(resources)
        self._commands.run(
            (*self._compose_arguments(resources), "down", "--remove-orphans"),
            environment=self._compose_environment(resources),
        )

    def remove_neo4j_volume(self, resources: TenantResourceIdentity) -> None:
        self._validate_secret_target(resources)
        self._commands.run(
            (*self._compose_arguments(resources), "down", "--volumes", "--remove-orphans"),
            environment=self._compose_environment(resources),
        )

    def remove_tenant_database_and_role(self, resources: TenantResourceIdentity) -> None:
        with psycopg.connect(self._postgres_admin_url, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT database.datname, pg_get_userbyid(database.datdba),
                           role.rolsuper, role.rolcreatedb, role.rolcreaterole,
                           role.rolreplication, role.rolbypassrls
                    FROM pg_database AS database
                    JOIN pg_roles AS role ON role.oid = database.datdba
                    WHERE database.datname = %s
                    """,
                    (resources.database_name,),
                )
                database = cursor.fetchone()
                if database is not None:
                    if database[1] != resources.database_role or any(
                        bool(value) for value in database[2:]
                    ):
                        raise DecommissionConflict(
                            "Tenant database ownership is not exact and safe"
                        )
                    cursor.execute(
                        """
                        SELECT pg_terminate_backend(pid)
                        FROM pg_stat_activity
                        WHERE datname = %s AND pid <> pg_backend_pid()
                        """,
                        (resources.database_name,),
                    )
                    cursor.execute(
                        sql.SQL("DROP DATABASE {}").format(sql.Identifier(resources.database_name))
                    )
                cursor.execute(
                    """
                    SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls
                    FROM pg_roles WHERE rolname = %s
                    """,
                    (resources.database_role,),
                )
                role = cursor.fetchone()
                if role is not None:
                    if any(bool(value) for value in role):
                        raise DecommissionConflict("Tenant role is privileged; refusing removal")
                    cursor.execute(
                        sql.SQL("DROP ROLE {}").format(sql.Identifier(resources.database_role))
                    )

    def remove_route(self, resources: TenantResourceIdentity) -> None:
        with psycopg.connect(self._control_database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM control.routing
                    WHERE tenant_id = %s
                      AND tenant_database_name = %s
                      AND tenant_database_role = %s
                      AND neo4j_service_address = %s
                      AND neo4j_secret_name = %s
                    RETURNING tenant_id
                    """,
                    (
                        resources.tenant_id,
                        resources.database_name,
                        resources.database_role,
                        f"neo4j-{resources.tenant_id}:7687",
                        f"{resources.tenant_id}/neo4j_password",
                    ),
                )
                if cursor.fetchone() is None:
                    cursor.execute(
                        "SELECT 1 FROM control.routing WHERE tenant_id = %s",
                        (resources.tenant_id,),
                    )
                    if cursor.fetchone() is not None:
                        raise DecommissionConflict("Tenant route identity changed")

    def remove_tenant_secret_files(self, resources: TenantResourceIdentity) -> None:
        directory = self._validate_secret_target(resources)
        if not directory.exists():
            return
        allowed = {"neo4j_password", "postgres_password"}
        entries = list(directory.iterdir())
        if any(entry.name not in allowed or entry.is_symlink() for entry in entries):
            raise DecommissionConflict("Tenant secret directory contains an unexpected entry")
        for entry in entries:
            if not entry.is_file():
                raise DecommissionConflict("Tenant secret target is not a regular file")
            entry.unlink()
        directory.rmdir()

    def purge_tenant_domain_records(self, resources: TenantResourceIdentity) -> None:
        with psycopg.connect(self._control_database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM control.routing WHERE tenant_id = %s",
                    (resources.tenant_id,),
                )
                if cursor.fetchone() is not None:
                    raise DecommissionConflict("Tenant route must be removed before domain records")
                for table in (
                    "channel_bindings",
                    "access_tokens",
                    "delegations",
                    "memberships",
                    "provisioning",
                ):
                    cursor.execute(
                        sql.SQL("DELETE FROM control.{} WHERE tenant_id = %s").format(
                            sql.Identifier(table)
                        ),
                        (resources.tenant_id,),
                    )
                cursor.execute(
                    "DELETE FROM control.tenants WHERE tenant_id = %s RETURNING tenant_id",
                    (resources.tenant_id,),
                )
                # A retry after a committed delete is an idempotent success.

    def _compose_arguments(self, resources: TenantResourceIdentity) -> tuple[str, ...]:
        return (
            "docker",
            "compose",
            "--project-name",
            resources.compose_project,
            "--file",
            str(self._compose_file),
        )

    def _compose_environment(self, resources: TenantResourceIdentity) -> Mapping[str, str]:
        return {
            "TENANT_ID": resources.tenant_id,
            "TENANT_SECRETS_DIR": resources.tenant_secret_ref,
            "TENANT_TELEMETRY_REF": self._telemetry_pseudonymizer.reference(
                "tenant", resources.tenant_id
            ),
        }

    def _validate_secret_target(self, resources: TenantResourceIdentity) -> Path:
        expected = self._secrets_root / resources.tenant_id
        actual = Path(resources.tenant_secret_ref).resolve()
        if actual != expected:
            raise DecommissionConflict("Tenant secret target is outside the configured root")
        return actual


def create_host_decommission_service(
    *,
    control_database_url: str,
    postgres_admin_url: str,
    secrets_root: Path,
    evidence_root: Path,
    repository_root: Path,
    postgres_host: str,
    postgres_port: int,
    telemetry_pseudonymizer: TelemetryPseudonymizer,
    command_runner: HostCommandRunner | None = None,
) -> DecommissionService:
    """Compose production adapters while retaining all destructive safety gates."""

    root = repository_root.resolve()
    commands = command_runner or SubprocessHostCommandRunner(root)
    compose = HostComposeRunner(
        commands,
        root / "deploy" / "tenant.compose.yaml",
        secrets_root,
        telemetry_pseudonymizer,
    )
    return DecommissionService(
        store=PostgresDecommissionStore(control_database_url),
        access=PostgresTenantAccessAdapter(control_database_url),
        protection=FilesystemProtectionEvidenceAdapter(evidence_root),
        reactivation=HostTenantReadinessAdapter(
            control_database_url,
            secrets_root,
            compose=compose,
            postgres_host=postgres_host,
            postgres_port=postgres_port,
        ),
        destruction=HostTenantDestructionAdapter(
            command_runner=commands,
            compose_file=root / "deploy" / "tenant.compose.yaml",
            secrets_root=secrets_root,
            control_database_url=control_database_url,
            postgres_admin_url=postgres_admin_url,
            telemetry_pseudonymizer=telemetry_pseudonymizer,
        ),
    )

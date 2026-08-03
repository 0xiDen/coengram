"""Safe host-side infrastructure Adapters for Tenant provisioning."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import quote

import psycopg
from psycopg import sql

from agent_memory_service.database_url import (
    psycopg_database_url as _psycopg_database_url,
)
from agent_memory_service.manifest import TenantManifest
from agent_memory_service.provisioning import (
    FilesystemSecretWriter,
    ProvisioningConflict,
    TenantProvisioner,
)
from agent_memory_service.pseudonyms import TelemetryPseudonymizer
from agent_memory_service.routing import FileSecretReader
from agent_memory_service.stores.postgres_provisioning import PostgresProvisioningStateStore

_NEO4J_SCHEMA_MIGRATION = """
password="$(tr -d '\r\n' < /run/secrets/neo4j_password)"
exec cypher-shell -a bolt://127.0.0.1:7687 -u neo4j -p "$password" \
  'CREATE CONSTRAINT memory_schema_version_unique IF NOT EXISTS
   FOR (n:MemorySchemaVersion) REQUIRE n.version IS UNIQUE;
   MERGE (:MemorySchemaVersion {version: 1});'
""".strip()

_NEO4J_HEALTH_CHECK = """
password="$(tr -d '\r\n' < /run/secrets/neo4j_password)"
exec cypher-shell -a bolt://127.0.0.1:7687 -u neo4j -p "$password" 'RETURN 1'
""".strip()

_NEO4J_SCHEMA_CHECK = """
password="$(tr -d '\r\n' < /run/secrets/neo4j_password)"
exec cypher-shell -a bolt://127.0.0.1:7687 -u neo4j -p "$password" \
  'MATCH (n:MemorySchemaVersion {version: 1})
   WITH count(n) AS count
   RETURN 1 / CASE WHEN count = 1 THEN 1 ELSE 0 END'
""".strip()


class HostCommandRunner(Protocol):
    def run(
        self,
        arguments: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None: ...


class SubprocessHostCommandRunner:
    """Execute fixed argument vectors from the repository root without a host shell."""

    def __init__(self, repository_root: Path, *, timeout_seconds: int = 300) -> None:
        root = repository_root.resolve()
        if not root.is_dir():
            raise ValueError("Repository root must be a directory")
        if timeout_seconds < 1:
            raise ValueError("Command timeout must be positive")
        self._root = root
        self._timeout_seconds = timeout_seconds

    def run(
        self,
        arguments: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if not arguments or any(not argument for argument in arguments):
            raise ValueError("Host command arguments cannot be empty")
        command_environment = os.environ.copy()
        if environment is not None:
            command_environment.update(environment)
        subprocess.run(
            list(arguments),
            cwd=self._root,
            env=command_environment,
            check=True,
            timeout=self._timeout_seconds,
        )

    def remove_docker_resource(
        self,
        kind: Literal["container", "volume", "network"],
        name: str,
    ) -> None:
        """Remove one exact Docker resource, treating an absent resource as removed."""

        if not name or any(character.isspace() for character in name):
            raise ValueError("Docker resource name is invalid")
        commands = {
            "container": ("docker", "rm", "--force", name),
            "volume": ("docker", "volume", "rm", name),
            "network": ("docker", "network", "rm", name),
        }
        arguments = commands[kind]
        completed = subprocess.run(
            arguments,
            cwd=self._root,
            env=os.environ.copy(),
            check=False,
            capture_output=True,
            text=True,
            timeout=self._timeout_seconds,
        )
        if completed.returncode == 0:
            return
        error = completed.stderr.casefold()
        missing_markers = {
            "container": (f"no such container: {name}".casefold(),),
            "volume": (
                f"no such volume: {name}".casefold(),
                f"get {name}: no such volume".casefold(),
            ),
            "network": (
                f"network {name} not found".casefold(),
                f"no such network: {name}".casefold(),
            ),
        }
        if any(marker in error for marker in missing_markers[kind]):
            return
        raise RuntimeError("Docker resource removal failed")


class HostComposeRunner:
    """Start one derived Tenant Compose project from the operator host."""

    def __init__(
        self,
        command_runner: HostCommandRunner,
        compose_file: Path,
        secrets_root: Path,
        telemetry_pseudonymizer: TelemetryPseudonymizer,
    ) -> None:
        if compose_file.is_symlink():
            raise ValueError("Tenant Compose file must be a regular file")
        compose_path = compose_file.resolve()
        if not compose_path.is_file():
            raise ValueError("Tenant Compose file must be a regular file")
        self._commands = command_runner
        self._compose_file = compose_path
        self._secrets_root = secrets_root.resolve()
        self._telemetry_pseudonymizer = telemetry_pseudonymizer

    def ensure_tenant_running(self, manifest: TenantManifest) -> None:
        self._commands.run(
            (*self._compose_arguments(manifest), "up", "-d", "--wait", "neo4j"),
            environment=self._compose_environment(manifest),
        )

    def execute_neo4j(self, manifest: TenantManifest, script: str) -> None:
        if script not in {
            _NEO4J_SCHEMA_MIGRATION,
            _NEO4J_SCHEMA_CHECK,
            _NEO4J_HEALTH_CHECK,
        }:
            raise ValueError("Only registered Neo4j operator scripts may execute")
        self._commands.run(
            (
                *self._compose_arguments(manifest),
                "exec",
                "-T",
                "neo4j",
                "/bin/bash",
                "-ec",
                script,
            ),
            environment=self._compose_environment(manifest),
        )

    def verify_neo4j_schema(self, manifest: TenantManifest) -> None:
        self.execute_neo4j(manifest, _NEO4J_SCHEMA_CHECK)

    def verify_neo4j_health(self, manifest: TenantManifest) -> None:
        self.execute_neo4j(manifest, _NEO4J_HEALTH_CHECK)

    def _compose_arguments(self, manifest: TenantManifest) -> tuple[str, ...]:
        return (
            "docker",
            "compose",
            "--project-name",
            f"memory-tenant-{manifest.tenant_id}",
            "--file",
            str(self._compose_file),
        )

    def _compose_environment(self, manifest: TenantManifest) -> dict[str, str]:
        return {
            "TENANT_ID": manifest.tenant_id,
            "TENANT_SECRETS_DIR": str(self._secrets_root / manifest.tenant_id),
            "TENANT_TELEMETRY_REF": self._telemetry_pseudonymizer.reference(
                "tenant", manifest.tenant_id
            ),
        }


class PostgresTenantDatabaseProvisioner:
    """Create a least-privilege Tenant role and its one owned database idempotently."""

    def __init__(self, admin_database_url: str, secrets: FileSecretReader) -> None:
        if not admin_database_url.strip():
            raise ValueError("PostgreSQL administrator URL cannot be empty")
        self._admin_database_url = _psycopg_database_url(admin_database_url)
        self._secrets = secrets

    def ensure_database_and_role(
        self,
        manifest: TenantManifest,
        *,
        password_secret_name: str,
    ) -> None:
        password = self._secrets.read(f"{manifest.tenant_id}/{password_secret_name}")
        with psycopg.connect(self._admin_database_url, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication,
                           rolbypassrls,
                           EXISTS (
                               SELECT 1 FROM pg_auth_members WHERE member = pg_roles.oid
                           )
                    FROM pg_roles
                    WHERE rolname = %s
                    """,
                    (manifest.database_role,),
                )
                role = cursor.fetchone()
                if role is None:
                    cursor.execute(
                        sql.SQL(
                            "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                            "NOREPLICATION NOBYPASSRLS"
                        ).format(sql.Identifier(manifest.database_role))
                    )
                elif any(bool(value) for value in role):
                    raise ProvisioningConflict("Existing Tenant database role is privileged")
                _set_role_password(cursor, manifest.database_role, password)

                cursor.execute(
                    """
                    SELECT pg_get_userbyid(datdba)
                    FROM pg_database
                    WHERE datname = %s
                    """,
                    (manifest.database_name,),
                )
                database = cursor.fetchone()
                if database is None:
                    cursor.execute(
                        sql.SQL("CREATE DATABASE {} OWNER {}").format(
                            sql.Identifier(manifest.database_name),
                            sql.Identifier(manifest.database_role),
                        )
                    )
                elif str(database[0]) != manifest.database_role:
                    raise ProvisioningConflict("Existing Tenant database has another owner")
                cursor.execute(
                    sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(
                        sql.Identifier(manifest.database_name)
                    )
                )
                cursor.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        sql.Identifier(manifest.database_name),
                        sql.Identifier(manifest.database_role),
                    )
                )


class HostTenantMigrationRunner:
    """Run pinned Alembic and registered Neo4j migrations from the operator host."""

    def __init__(
        self,
        command_runner: HostCommandRunner,
        compose: HostComposeRunner,
        tenant_alembic_config: Path,
        secrets: FileSecretReader,
        *,
        postgres_host: str,
        postgres_port: int,
    ) -> None:
        if tenant_alembic_config.is_symlink():
            raise ValueError("Tenant Alembic configuration must be a regular file")
        alembic_config = tenant_alembic_config.resolve()
        if not alembic_config.is_file():
            raise ValueError("Tenant Alembic configuration must be a regular file")
        self._commands = command_runner
        self._compose = compose
        self._alembic_config = alembic_config
        self._secrets = secrets
        self._postgres_host = postgres_host
        self._postgres_port = postgres_port

    def migrate_postgres(self, manifest: TenantManifest) -> None:
        self._commands.run(
            (
                sys.executable,
                "-m",
                "alembic",
                "-c",
                str(self._alembic_config),
                "upgrade",
                "head",
            ),
            environment={
                "TENANT_DATABASE_URL": _tenant_database_url(
                    manifest,
                    self._secrets,
                    self._postgres_host,
                    self._postgres_port,
                )
            },
        )

    def migrate_neo4j(self, manifest: TenantManifest) -> None:
        self._compose.execute_neo4j(manifest, _NEO4J_SCHEMA_MIGRATION)


class HostTenantProvisioningVerifier:
    """Verify health, exact fail-closed routing, and PostgreSQL Tenant isolation."""

    def __init__(
        self,
        control_database_url: str,
        compose: HostComposeRunner,
        secrets: FileSecretReader,
        *,
        postgres_host: str,
        postgres_port: int,
    ) -> None:
        self._control_database_url = _psycopg_database_url(control_database_url)
        self._compose = compose
        self._secrets = secrets
        self._postgres_host = postgres_host
        self._postgres_port = postgres_port

    def verify_neo4j_health(self, manifest: TenantManifest) -> None:
        self._compose.execute_neo4j(manifest, _NEO4J_HEALTH_CHECK)

    def verify_routing(self, manifest: TenantManifest) -> None:
        with psycopg.connect(self._control_database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT neo4j_service_address, neo4j_secret_name,
                           tenant_database_name, tenant_database_role, healthy
                    FROM control.routing
                    WHERE tenant_id = %s
                    """,
                    (manifest.tenant_id,),
                )
                route = cursor.fetchone()
        expected = (
            f"{manifest.neo4j_service_name}:7687",
            f"{manifest.tenant_id}/neo4j_password",
            manifest.database_name,
            manifest.database_role,
            False,
        )
        if route != expected:
            raise ProvisioningConflict(
                "Tenant route is missing, redirected, or prematurely healthy"
            )

    def verify_isolation(self, manifest: TenantManifest) -> None:
        tenant_url = _tenant_database_url(
            manifest,
            self._secrets,
            self._postgres_host,
            self._postgres_port,
        )
        with psycopg.connect(tenant_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_database(), current_user")
                identity = cursor.fetchone()
        if identity != (manifest.database_name, manifest.database_role):
            raise ProvisioningConflict(
                "Tenant database route did not preserve its database and role"
            )

        with psycopg.connect(self._control_database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT routing.tenant_database_name,
                           has_database_privilege(
                               %s,
                               routing.tenant_database_name,
                               'CONNECT'
                           )
                    FROM control.routing AS routing
                    JOIN control.tenants AS tenant
                      ON tenant.tenant_id = routing.tenant_id
                    JOIN pg_database AS database
                      ON database.datname = routing.tenant_database_name
                    WHERE routing.tenant_id <> %s
                      AND routing.healthy = true
                      AND tenant.active = true
                    ORDER BY routing.tenant_id
                    """,
                    (manifest.database_role, manifest.tenant_id),
                )
                cross_tenant_access = tuple(cursor.fetchall())
        if any(bool(row[1]) for row in cross_tenant_access):
            raise ProvisioningConflict("Tenant database role can connect across Tenant boundary")


def create_host_tenant_provisioner(
    *,
    control_database_url: str,
    postgres_admin_url: str,
    postgres_host: str,
    postgres_port: int,
    secrets_root: Path,
    tenant_secrets_group_id: int,
    repository_root: Path,
    telemetry_pseudonymizer: TelemetryPseudonymizer,
    command_runner: HostCommandRunner | None = None,
) -> TenantProvisioner:
    """Compose production Adapters without granting Docker access to any container."""
    resolved_repository = repository_root.resolve()
    resolved_secrets = secrets_root.resolve()
    commands = command_runner or SubprocessHostCommandRunner(resolved_repository)
    secret_reader = FileSecretReader(resolved_secrets)
    compose = HostComposeRunner(
        commands,
        resolved_repository / "deploy" / "tenant.compose.yaml",
        resolved_secrets,
        telemetry_pseudonymizer,
    )
    migrations = HostTenantMigrationRunner(
        commands,
        compose,
        resolved_repository / "alembic-tenant.ini",
        secret_reader,
        postgres_host=postgres_host,
        postgres_port=postgres_port,
    )
    verifier = HostTenantProvisioningVerifier(
        control_database_url,
        compose,
        secret_reader,
        postgres_host=postgres_host,
        postgres_port=postgres_port,
    )
    return TenantProvisioner(
        PostgresProvisioningStateStore(control_database_url),
        FilesystemSecretWriter(
            resolved_secrets,
            group_id=tenant_secrets_group_id,
        ),
        compose,
        PostgresTenantDatabaseProvisioner(postgres_admin_url, secret_reader),
        migrations,
        verifier,
    )


def _tenant_database_url(
    manifest: TenantManifest,
    secrets: FileSecretReader,
    host: str,
    port: int,
) -> str:
    password = secrets.read(f"{manifest.tenant_id}/postgres_password")
    return _database_url(host, port, manifest.database_name, manifest.database_role, password)


def _database_url(host: str, port: int, database: str, user: str, password: str) -> str:
    if not host or not 1 <= port <= 65535:
        raise ValueError("PostgreSQL host and port must be valid")
    return (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@"
        f"{host}:{port}/{quote(database, safe='')}"
    )


def _set_role_password(
    cursor: psycopg.Cursor[tuple[object, ...]],
    role: str,
    password: str,
) -> None:
    """Set a role password without placing the credential in a client SQL statement."""
    cursor.execute(
        "SELECT set_config('memory.provisioning_role_password', %s, false)",
        (password,),
    )
    cursor.execute(
        sql.SQL(
            """
            DO $provision$
            BEGIN
                EXECUTE format(
                    'ALTER ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                    'NOREPLICATION NOBYPASSRLS PASSWORD %L',
                    {},
                    current_setting('memory.provisioning_role_password')
                );
            END
            $provision$
            """
        ).format(sql.Literal(role))
    )
    cursor.execute("SELECT set_config('memory.provisioning_role_password', '', false)")

from __future__ import annotations

import io
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

import pytest

from agent_memory_service.auth import TokenService
from agent_memory_service.cli import run_cli
from agent_memory_service.control import ControlModule, InMemoryControlStore
from agent_memory_service.host_provisioning import (
    HostComposeRunner,
    HostTenantMigrationRunner,
    SubprocessHostCommandRunner,
)
from agent_memory_service.manifest import (
    ManifestMembership,
    ManifestPrincipal,
    TenantManifest,
)
from agent_memory_service.models import PrincipalKind
from agent_memory_service.provisioning import (
    InMemoryProvisioningStateStore,
    TenantProvisioner,
)
from agent_memory_service.pseudonyms import TelemetryPseudonymizer
from agent_memory_service.routing import FileSecretReader

_TELEMETRY = TelemetryPseudonymizer(b"0123456789abcdef0123456789abcdef")


class _SecretWriter:
    def ensure_secret(self, tenant_id: str, name: str, value: str, *, mode: int) -> None:
        pass


class _Infrastructure:
    def ensure_tenant_running(self, manifest: TenantManifest) -> None:
        pass

    def ensure_database_and_role(
        self,
        manifest: TenantManifest,
        *,
        password_secret_name: str,
    ) -> None:
        pass

    def migrate_postgres(self, manifest: TenantManifest) -> None:
        pass

    def migrate_neo4j(self, manifest: TenantManifest) -> None:
        pass

    def verify_neo4j_health(self, manifest: TenantManifest) -> None:
        pass

    def verify_routing(self, manifest: TenantManifest) -> None:
        pass

    def verify_isolation(self, manifest: TenantManifest) -> None:
        pass


class _RecordingCommands:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def run(
        self,
        arguments: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.calls.append((tuple(arguments), dict(environment or {})))


def _manifest() -> TenantManifest:
    return TenantManifest(
        tenant_id="tenant-production",
        name="Product Backend",
        principals=(
            ManifestPrincipal(
                principal_id="user-admin",
                name="Admin",
                kind=PrincipalKind.USER,
            ),
        ),
        memberships=(
            ManifestMembership(
                principal_id="user-admin",
                roles=("tenant_administrator", "tenant_member"),
            ),
        ),
    )


def _provisioner() -> tuple[TenantProvisioner, InMemoryProvisioningStateStore]:
    states = InMemoryProvisioningStateStore()
    infrastructure = _Infrastructure()
    return (
        TenantProvisioner(
            states,
            _SecretWriter(),
            infrastructure,
            infrastructure,
            infrastructure,
            infrastructure,
        ),
        states,
    )


def _control() -> ControlModule:
    store = InMemoryControlStore()
    return ControlModule(store, TokenService(store))


def test_memoryctl_plan_is_read_only_and_apply_requires_exact_tenant_confirmation(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    manifest_path = tmp_path / "tenant.json"
    manifest_path.write_text(manifest.export_json(), encoding="utf-8")
    provisioner, states = _provisioner()
    output = io.StringIO()

    assert (
        run_cli(
            ["tenant", "plan", "--manifest", str(manifest_path)],
            _control(),
            output,
            provisioner=provisioner,
        )
        == 0
    )
    plan = json.loads(output.getvalue())
    assert plan["operation"] == "plan"
    assert plan["tenant_id"] == manifest.tenant_id
    assert plan["noop"] is False
    assert len(plan["steps"]) == 10
    assert states.get(manifest.tenant_id) is None

    with pytest.raises(ValueError, match="confirmation"):
        run_cli(
            [
                "tenant",
                "apply",
                "--manifest",
                str(manifest_path),
                "--confirm",
                "another-tenant",
            ],
            _control(),
            provisioner=provisioner,
        )
    assert states.get(manifest.tenant_id) is None

    output = io.StringIO()
    run_cli(
        [
            "tenant",
            "apply",
            "--manifest",
            str(manifest_path),
            "--confirm",
            manifest.tenant_id,
        ],
        _control(),
        output,
        provisioner=provisioner,
    )
    applied = json.loads(output.getvalue())
    assert applied["operation"] == "apply"
    assert applied["plan"]["steps"][0]["step"] == "record_control_state"
    assert applied["result"]["status"] == "active"


def test_host_adapters_use_argument_vectors_and_keep_database_password_out_of_arguments(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    compose_file = repository / "tenant.compose.yaml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    alembic_file = repository / "alembic-tenant.ini"
    alembic_file.write_text("[alembic]\n", encoding="utf-8")
    secrets_root = tmp_path / "secrets"
    tenant_secrets = secrets_root / "tenant-production"
    tenant_secrets.mkdir(parents=True)
    (tenant_secrets / "postgres_password").write_text(
        "tenant-database-password\n",
        encoding="utf-8",
    )
    commands = _RecordingCommands()
    compose = HostComposeRunner(commands, compose_file, secrets_root, _TELEMETRY)
    migrations = HostTenantMigrationRunner(
        commands,
        compose,
        alembic_file,
        FileSecretReader(secrets_root),
        postgres_host="postgres.internal",
        postgres_port=5432,
    )

    compose.ensure_tenant_running(_manifest())
    migrations.migrate_postgres(_manifest())

    compose_arguments, compose_environment = commands.calls[0]
    migration_arguments, migration_environment = commands.calls[1]
    assert compose_arguments[:2] == ("docker", "compose")
    assert compose_arguments[-4:] == ("up", "-d", "--wait", "neo4j")
    assert compose_environment == {
        "TENANT_ID": "tenant-production",
        "TENANT_SECRETS_DIR": str(tenant_secrets),
        "TENANT_TELEMETRY_REF": _TELEMETRY.reference("tenant", "tenant-production"),
    }
    assert migration_arguments[:3] == (sys.executable, "-m", "alembic")
    assert "tenant-database-password" not in " ".join(migration_arguments)
    assert migration_environment["TENANT_DATABASE_URL"].startswith(
        "postgresql://tenant_tenant_production_rw:tenant-database-password@"
    )

    compose.verify_neo4j_schema(_manifest())
    compose.verify_neo4j_health(_manifest())
    schema_arguments = " ".join(commands.calls[2][0])
    health_arguments = " ".join(commands.calls[3][0])
    assert "MemorySchemaVersion" in schema_arguments
    assert "CASE WHEN count = 1" in schema_arguments
    assert "RETURN 1" in health_arguments
    assert "tenant-database-password" not in schema_arguments + health_arguments


def test_host_adapters_reject_symlinked_operator_files(tmp_path: Path) -> None:
    compose_target = tmp_path / "compose.yaml"
    compose_target.write_text("services: {}\n", encoding="utf-8")
    compose_link = tmp_path / "compose-link.yaml"
    compose_link.symlink_to(compose_target)

    with pytest.raises(ValueError, match="regular file"):
        HostComposeRunner(_RecordingCommands(), compose_link, tmp_path / "secrets", _TELEMETRY)


@pytest.mark.parametrize(
    ("kind", "stderr", "expected"),
    (
        (
            "container",
            "Error response from daemon: No such container: exact-target",
            ("docker", "rm", "--force", "exact-target"),
        ),
        (
            "volume",
            "Error response from daemon: No such volume: exact-target",
            ("docker", "volume", "rm", "exact-target"),
        ),
        (
            "volume",
            "Error response from daemon: get exact-target: no such volume",
            ("docker", "volume", "rm", "exact-target"),
        ),
        (
            "network",
            "Error response from daemon: network exact-target not found",
            ("docker", "network", "rm", "exact-target"),
        ),
    ),
)
def test_docker_resource_removal_treats_exact_not_found_as_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: Literal["container", "volume", "network"],
    stderr: str,
    expected: tuple[str, ...],
) -> None:
    calls: list[tuple[str, ...]] = []

    def run(arguments: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(arguments))
        return subprocess.CompletedProcess(
            arguments,
            returncode=1,
            stdout="",
            stderr=stderr,
        )

    monkeypatch.setattr("agent_memory_service.host_provisioning.subprocess.run", run)
    runner = SubprocessHostCommandRunner(tmp_path)

    runner.remove_docker_resource(kind, "exact-target")

    assert calls == [expected]


def test_docker_resource_removal_sanitizes_unexpected_daemon_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(arguments: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            arguments,
            returncode=1,
            stdout="",
            stderr="secret-bearing daemon failure",
        )

    monkeypatch.setattr("agent_memory_service.host_provisioning.subprocess.run", run)
    runner = SubprocessHostCommandRunner(tmp_path)

    with pytest.raises(RuntimeError, match="Docker resource removal failed") as raised:
        runner.remove_docker_resource("volume", "exact-target")

    assert "secret-bearing" not in str(raised.value)

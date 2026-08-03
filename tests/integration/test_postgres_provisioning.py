"""Real Control Store behavior for durable Tenant provisioning transitions."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from agent_memory_service.auth import TokenService
from agent_memory_service.control import ControlModule, ControlNotFound
from agent_memory_service.host_provisioning import (
    HostComposeRunner,
    HostTenantProvisioningVerifier,
    PostgresTenantDatabaseProvisioner,
)
from agent_memory_service.manifest import (
    ManifestMembership,
    ManifestPrincipal,
    TenantManifest,
    TenantPolicies,
)
from agent_memory_service.models import PrincipalKind
from agent_memory_service.provisioning import (
    PROVISIONING_STEPS,
    FilesystemSecretWriter,
    ProvisioningConflict,
    ProvisioningStatus,
    ProvisioningStep,
)
from agent_memory_service.pseudonyms import TelemetryPseudonymizer
from agent_memory_service.routing import FileSecretReader
from agent_memory_service.stores.postgres_control import PostgresControlStore
from agent_memory_service.stores.postgres_provisioning import PostgresProvisioningStateStore

CONTROL_DATABASE_URL = os.environ.get("CONTROL_DATABASE_URL")
POSTGRES_ADMIN_URL = os.environ.get("POSTGRES_ADMIN_URL")
TENANT_POSTGRES_HOST = os.environ.get("TENANT_POSTGRES_HOST", "127.0.0.1")
TENANT_POSTGRES_PORT = int(os.environ.get("TENANT_POSTGRES_PORT", "5432"))
_TELEMETRY = TelemetryPseudonymizer(b"0123456789abcdef0123456789abcdef")

pytestmark = pytest.mark.skipif(
    not CONTROL_DATABASE_URL,
    reason="CONTROL_DATABASE_URL is required for PostgreSQL provisioning integration tests",
)


@pytest.fixture(scope="module", autouse=True)
def migrated_control_store() -> None:
    if not CONTROL_DATABASE_URL:
        return
    from alembic import command
    from alembic.config import Config

    repository_root = Path(__file__).resolve().parents[2]
    command.upgrade(Config(repository_root / "alembic-control.ini"), "head")


def test_manifest_start_resume_and_activation_are_atomic_and_fail_closed() -> None:
    assert CONTROL_DATABASE_URL is not None
    suffix = uuid4().hex[:20]
    tenant_id = f"tenant-{suffix}"
    principal_id = f"user-{suffix}"
    manifest = TenantManifest(
        tenant_id=tenant_id,
        name="Provisioned Backend",
        principals=(
            ManifestPrincipal(
                principal_id=principal_id,
                name="Provisioned Admin",
                kind=PrincipalKind.USER,
            ),
        ),
        memberships=(
            ManifestMembership(
                principal_id=principal_id,
                roles=("tenant_administrator", "tenant_member"),
            ),
        ),
        policies=TenantPolicies(user_token_lifetime_days=7),
    )
    states = PostgresProvisioningStateStore(CONTROL_DATABASE_URL)
    control_store = PostgresControlStore(CONTROL_DATABASE_URL)
    control = ControlModule(control_store, TokenService(control_store))

    started = states.start(manifest)
    assert started.status is ProvisioningStatus.PROVISIONING
    assert started.completed_steps == (ProvisioningStep.RECORD_CONTROL_STATE,)
    assert started.attempt == 1
    assert control_store.get_tenant(tenant_id) is not None
    assert not control_store.get_tenant(tenant_id).active  # type: ignore[union-attr]
    membership = control_store.get_membership(tenant_id, principal_id)
    assert membership is not None and not membership.active
    route = control_store.get_tenant_route(tenant_id)
    assert route is not None and not route.healthy
    with pytest.raises(ControlNotFound, match="Healthy Tenant route"):
        control.resolve_tenant_route(tenant_id)

    failed = states.mark_failed(
        tenant_id,
        ProvisioningStep.WRITE_SECRETS,
        "SimulatedFailure",
    )
    assert failed.status is ProvisioningStatus.FAILED
    assert failed.failed_step is ProvisioningStep.WRITE_SECRETS
    resumed = states.start(manifest)
    assert resumed.status is ProvisioningStatus.PROVISIONING
    assert resumed.attempt == 2

    for step in PROVISIONING_STEPS[1:-1]:
        states.mark_completed(tenant_id, step)
    with pytest.raises(ControlNotFound, match="Healthy Tenant route"):
        control.resolve_tenant_route(tenant_id)
    active = states.activate(tenant_id)

    assert active.status is ProvisioningStatus.ACTIVE
    assert active.completed_steps == PROVISIONING_STEPS
    assert control_store.get_tenant(tenant_id).active  # type: ignore[union-attr]
    assert control_store.get_membership(tenant_id, principal_id).active  # type: ignore[union-attr]
    assert control.resolve_tenant_route(tenant_id).healthy
    credential = control.issue_access_token(tenant_id, principal_id)
    lifetime = credential.expires_at - datetime.now(UTC)
    assert timedelta(days=6) < lifetime <= timedelta(days=7)
    assert states.start(manifest) == active

    with pytest.raises(ProvisioningConflict, match="changed manifest"):
        states.start(manifest.model_copy(update={"name": "Changed after activation"}))


@pytest.mark.skipif(
    not POSTGRES_ADMIN_URL,
    reason="POSTGRES_ADMIN_URL is required for Tenant database isolation integration tests",
)
def test_tenant_database_roles_are_idempotent_and_cannot_cross_connect(
    tmp_path: Path,
) -> None:
    assert CONTROL_DATABASE_URL is not None
    assert POSTGRES_ADMIN_URL is not None
    suffix = uuid4().hex[:12]
    first = TenantManifest(tenant_id=f"tenant-first-{suffix}", name="First Backend")
    second = TenantManifest(tenant_id=f"tenant-second-{suffix}", name="Second Backend")
    states = PostgresProvisioningStateStore(CONTROL_DATABASE_URL)
    states.start(first)
    states.start(second)

    secret_writer = FilesystemSecretWriter(tmp_path)
    for manifest, password in ((first, "first-password"), (second, "second-password")):
        secret_writer.ensure_secret(
            manifest.tenant_id,
            "postgres_password",
            password,
            mode=0o640,
        )
    secrets = FileSecretReader(tmp_path)
    databases = PostgresTenantDatabaseProvisioner(POSTGRES_ADMIN_URL, secrets)
    databases.ensure_database_and_role(first, password_secret_name="postgres_password")
    databases.ensure_database_and_role(first, password_secret_name="postgres_password")
    databases.ensure_database_and_role(second, password_secret_name="postgres_password")

    for step in PROVISIONING_STEPS[1:-1]:
        states.mark_completed(second.tenant_id, step)
    states.activate(second.tenant_id)

    compose_file = tmp_path / "tenant.compose.yaml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    verifier = HostTenantProvisioningVerifier(
        CONTROL_DATABASE_URL,
        HostComposeRunner(_UnusedCommandRunner(), compose_file, tmp_path, _TELEMETRY),
        secrets,
        postgres_host=TENANT_POSTGRES_HOST,
        postgres_port=TENANT_POSTGRES_PORT,
    )
    verifier.verify_routing(first)
    verifier.verify_isolation(first)
    for step in PROVISIONING_STEPS[1:-1]:
        states.mark_completed(first.tenant_id, step)
    states.activate(first.tenant_id)
    verifier.verify_isolation(second)


class _UnusedCommandRunner:
    def run(
        self,
        arguments: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        raise AssertionError("Database isolation verification must not invoke Compose")

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

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
    InMemoryProvisioningStateStore,
    ProvisioningConflict,
    ProvisioningFailed,
    ProvisioningState,
    ProvisioningStatus,
    ProvisioningStep,
    TenantProvisioner,
)


def manifest(*, name: str = "Product A Backend") -> TenantManifest:
    return TenantManifest(
        tenant_id="01jexample00000000000000000",
        name=name,
        principals=(
            ManifestPrincipal(
                principal_id="user-alice",
                name="Alice",
                kind=PrincipalKind.USER,
            ),
            ManifestPrincipal(
                principal_id="agent-synthesis",
                name="Knowledge Synthesis Agent",
                kind=PrincipalKind.AGENT,
            ),
        ),
        memberships=(
            ManifestMembership(
                principal_id="user-alice",
                roles=("tenant_administrator", "knowledge_curator", "tenant_member"),
            ),
            ManifestMembership(
                principal_id="agent-synthesis",
                roles=("tenant_member",),
            ),
        ),
        policies=TenantPolicies(autonomous_agent_token_lifetime_days=21),
    )


class RecordingStateStore(InMemoryProvisioningStateStore):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    def activate(self, tenant_id: str) -> ProvisioningState:
        self.events.append("activate")
        return super().activate(tenant_id)


class FakeSecretWriter:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.values: dict[str, tuple[str, int]] = {}

    def ensure_secret(self, tenant_id: str, name: str, value: str, *, mode: int) -> None:
        self.events.append(f"secret:{name}")
        self.values.setdefault(name, (value, mode))


class FakeInfrastructure:
    def __init__(self, events: list[str], *, fail_once_at: str | None = None) -> None:
        self.events = events
        self.fail_once_at = fail_once_at

    def _record(self, event: str) -> None:
        self.events.append(event)
        if self.fail_once_at == event:
            self.fail_once_at = None
            raise RuntimeError("simulated dependency failure")

    def ensure_tenant_running(self, tenant: TenantManifest) -> None:
        self._record(f"compose:{tenant.neo4j_service_name}")

    def ensure_database_and_role(
        self,
        tenant: TenantManifest,
        *,
        password_secret_name: str,
    ) -> None:
        assert password_secret_name == "postgres_password"
        self._record(f"database:{tenant.database_name}:{tenant.database_role}")

    def migrate_postgres(self, tenant: TenantManifest) -> None:
        self._record(f"migrate_postgres:{tenant.tenant_id}")

    def migrate_neo4j(self, tenant: TenantManifest) -> None:
        self._record(f"migrate_neo4j:{tenant.tenant_id}")

    def verify_neo4j_health(self, tenant: TenantManifest) -> None:
        self._record(f"verify_health:{tenant.tenant_id}")

    def verify_routing(self, tenant: TenantManifest) -> None:
        self._record(f"verify_routing:{tenant.tenant_id}")

    def verify_isolation(self, tenant: TenantManifest) -> None:
        self._record(f"verify_isolation:{tenant.tenant_id}")


def provisioner(
    *, fail_once_at: str | None = None
) -> tuple[
    TenantProvisioner,
    RecordingStateStore,
    FakeSecretWriter,
    FakeInfrastructure,
    list[str],
]:
    events: list[str] = []
    states = RecordingStateStore(events)
    secret_writer = FakeSecretWriter(events)
    infrastructure = FakeInfrastructure(events, fail_once_at=fail_once_at)
    return (
        TenantProvisioner(
            states,
            secret_writer,
            infrastructure,
            infrastructure,
            infrastructure,
            infrastructure,
        ),
        states,
        secret_writer,
        infrastructure,
        events,
    )


def test_manifest_round_trip_is_versioned_canonical_and_secret_free() -> None:
    original = manifest()

    exported = original.export_json()
    restored = TenantManifest.import_json(exported)
    document = json.loads(exported)

    assert restored == original
    assert restored.export_json() == exported
    assert document["version"] == 1
    assert document["database_name"] == "tenant_01jexample00000000000000000"
    assert document["database_role"] == "tenant_01jexample00000000000000000_rw"
    assert document["neo4j_service_name"] == "neo4j-01jexample00000000000000000"
    assert "password" not in exported.casefold()
    assert "access_token" not in exported.casefold()
    assert len(original.fingerprint) == 64


def test_manifest_rejects_secret_fields_unsupported_versions_and_unsafe_names() -> None:
    base = json.loads(manifest().export_json())

    with pytest.raises(ValueError, match="cannot contain secret field"):
        TenantManifest.import_json(json.dumps({**base, "password": "do-not-store-this"}))
    with pytest.raises(ValidationError):
        TenantManifest.model_validate({**base, "version": 2})
    with pytest.raises(ValidationError):
        TenantManifest(tenant_id="../other-tenant", name="unsafe")
    for field_name, unsafe_override in (
        ("database_name", "tenant_somewhere_else"),
        ("database_role", "tenant_somewhere_else_rw"),
        ("neo4j_service_name", "neo4j-somewhere-else"),
    ):
        with pytest.raises(ValidationError, match="derived from the immutable Tenant ID"):
            TenantManifest.model_validate({**base, field_name: unsafe_override})


def test_manifest_validates_principal_membership_links_and_agent_roles() -> None:
    with pytest.raises(ValidationError, match="undeclared Principal"):
        TenantManifest(
            tenant_id="tenant-a",
            name="Product A Backend",
            memberships=(
                ManifestMembership(principal_id="user-unknown", roles=("tenant_member",)),
            ),
        )

    with pytest.raises(ValidationError, match="Agents cannot receive"):
        TenantManifest(
            tenant_id="tenant-a",
            name="Product A Backend",
            principals=(
                ManifestPrincipal(
                    principal_id="agent-admin",
                    name="Unsafe Agent",
                    kind=PrincipalKind.AGENT,
                ),
            ),
            memberships=(
                ManifestMembership(
                    principal_id="agent-admin",
                    roles=("tenant_administrator",),
                ),
            ),
        )


def test_plan_is_complete_and_has_no_side_effects() -> None:
    tenant = manifest()
    service, states, secret_writer, _, events = provisioner()

    plan = service.plan(tenant)

    assert plan.tenant_id == tenant.tenant_id
    assert plan.current_status is ProvisioningStatus.NOT_STARTED
    assert plan.pending_steps == PROVISIONING_STEPS
    assert plan.steps[0].step is ProvisioningStep.RECORD_CONTROL_STATE
    assert "Principals" in plan.steps[0].action
    assert not plan.is_noop
    assert states.get(tenant.tenant_id) is None
    assert not secret_writer.values
    assert not events


def test_apply_runs_every_step_and_activates_only_after_all_checks() -> None:
    tenant = manifest()
    service, states, secret_writer, _, events = provisioner()

    result = service.apply(tenant)

    assert result.status is ProvisioningStatus.ACTIVE
    assert result.completed_steps == PROVISIONING_STEPS
    assert result.database_name == tenant.database_name
    assert result.database_role == tenant.database_role
    assert result.neo4j_service_name == tenant.neo4j_service_name
    assert states.get(tenant.tenant_id) == result
    assert events == [
        "secret:neo4j_password",
        "secret:postgres_password",
        f"compose:{tenant.neo4j_service_name}",
        f"database:{tenant.database_name}:{tenant.database_role}",
        f"migrate_postgres:{tenant.tenant_id}",
        f"migrate_neo4j:{tenant.tenant_id}",
        f"verify_health:{tenant.tenant_id}",
        f"verify_routing:{tenant.tenant_id}",
        f"verify_isolation:{tenant.tenant_id}",
        "activate",
    ]
    assert set(secret_writer.values) == {"neo4j_password", "postgres_password"}
    first, second = (secret_writer.values[name] for name in sorted(secret_writer.values))
    assert first[1] == second[1] == 0o640
    assert first[0] != second[0]
    assert all(len(value) >= 64 for value, _ in secret_writer.values.values())
    assert all(re.fullmatch(r"[A-Za-z0-9_-]+", value) for value, _ in secret_writer.values.values())


def test_generated_service_credentials_cannot_begin_as_cli_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agent_memory_service.provisioning.secrets.token_urlsafe",
        lambda _length: "-looks-like-a-command-option-but-is-long-enough",
    )
    service, _states, secret_writer, _database, _events = provisioner()

    service.apply(manifest())

    assert all(value.startswith("c") for value, _mode in secret_writer.values.values())


def test_failure_is_visible_and_reapply_resumes_after_last_completed_step() -> None:
    tenant = manifest()
    failing_event = f"verify_routing:{tenant.tenant_id}"
    service, states, _, _, events = provisioner(fail_once_at=failing_event)

    with pytest.raises(ProvisioningFailed) as failure:
        service.apply(tenant)

    failed = states.get(tenant.tenant_id)
    assert failure.value.step is ProvisioningStep.VERIFY_ROUTING
    assert failed is not None
    assert failed.status is ProvisioningStatus.FAILED
    assert failed.failed_step is ProvisioningStep.VERIFY_ROUTING
    assert failed.failure_code == "RuntimeError"
    assert ProvisioningStep.VERIFY_NEO4J_HEALTH in failed.completed_steps
    assert ProvisioningStep.VERIFY_ROUTING not in failed.completed_steps
    assert "activate" not in events

    events_before_resume = tuple(events)
    resumed = service.apply(tenant)

    assert resumed.status is ProvisioningStatus.ACTIVE
    assert resumed.attempt == 2
    assert tuple(events[: len(events_before_resume)]) == events_before_resume
    assert events[len(events_before_resume) :] == [
        failing_event,
        f"verify_isolation:{tenant.tenant_id}",
        "activate",
    ]
    assert events.count("secret:neo4j_password") == 1
    assert events.count(f"compose:{tenant.neo4j_service_name}") == 1


def test_successful_reapply_is_a_noop() -> None:
    tenant = manifest()
    service, _, _, _, events = provisioner()
    first = service.apply(tenant)
    event_count = len(events)

    second = service.apply(tenant)
    plan = service.plan(tenant)

    assert second == first
    assert len(events) == event_count
    assert plan.is_noop
    assert plan.current_status is ProvisioningStatus.ACTIVE


def test_changed_manifest_cannot_resume_partial_infrastructure() -> None:
    tenant = manifest()
    service, _, _, _, _ = provisioner(fail_once_at=f"verify_routing:{tenant.tenant_id}")
    with pytest.raises(ProvisioningFailed):
        service.apply(tenant)

    with pytest.raises(ProvisioningConflict, match="changed manifest"):
        service.plan(manifest(name="A renamed Tenant"))


def test_filesystem_secret_writer_creates_group_readable_secret_once(
    tmp_path: Path,
) -> None:
    writer = FilesystemSecretWriter(tmp_path)

    writer.ensure_secret("tenant-a", "neo4j_password", "first-value", mode=0o640)
    writer.ensure_secret("tenant-a", "neo4j_password", "replacement-value", mode=0o640)

    secret_path = tmp_path / "tenant-a" / "neo4j_password"
    assert secret_path.read_text(encoding="utf-8") == "first-value\n"
    assert stat.S_IMODE(secret_path.stat().st_mode) == 0o640
    assert secret_path.stat().st_gid == os.getegid()
    assert stat.S_IMODE(secret_path.parent.stat().st_mode) == 0o750

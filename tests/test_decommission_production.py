from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_memory_service.auth import TokenService
from agent_memory_service.cli import run_cli
from agent_memory_service.control import ControlModule, InMemoryControlStore
from agent_memory_service.decommission import (
    DecommissionService,
    InMemoryDecommissionStore,
    ProtectionEvidence,
    ProtectionKind,
    TenantResourceIdentity,
)
from agent_memory_service.host_decommission import (
    FilesystemProtectionEvidenceAdapter,
    HostTenantDestructionAdapter,
    ProtectionEvidenceReceipt,
)
from agent_memory_service.pseudonyms import TelemetryPseudonymizer
from agent_memory_service.stores.postgres_decommission import _decode_retention_pins

NOW = datetime(2026, 8, 2, 12, tzinfo=UTC)
TENANT_ID = "tenant-product-a"
_TELEMETRY = TelemetryPseudonymizer(b"0123456789abcdef0123456789abcdef")


class Access:
    def suspend_sessions(self, resources: TenantResourceIdentity) -> None:
        pass

    def block_token_issuance(self, resources: TenantResourceIdentity) -> None:
        pass

    def revoke_active_credentials(self, resources: TenantResourceIdentity) -> None:
        pass

    def reactivate_without_issuing_tokens(self, resources: TenantResourceIdentity) -> None:
        pass


class Protection:
    def verify(self, tenant_id: str, evidence: ProtectionEvidence) -> bool:
        return True


class Readiness:
    def verify_route(self, resources: TenantResourceIdentity) -> bool:
        return True

    def verify_schema(self, resources: TenantResourceIdentity) -> bool:
        return True

    def verify_isolation(self, resources: TenantResourceIdentity) -> bool:
        return True

    def verify_credentials(self, resources: TenantResourceIdentity) -> bool:
        return True

    def verify_health(self, resources: TenantResourceIdentity) -> bool:
        return True


class Destruction:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def remove_compose_project(self, resources: TenantResourceIdentity) -> None:
        self.calls.append("compose")

    def remove_neo4j_volume(self, resources: TenantResourceIdentity) -> None:
        self.calls.append("volume")

    def remove_tenant_database_and_role(self, resources: TenantResourceIdentity) -> None:
        self.calls.append("postgres")

    def remove_route(self, resources: TenantResourceIdentity) -> None:
        self.calls.append("route")

    def remove_tenant_secret_files(self, resources: TenantResourceIdentity) -> None:
        self.calls.append("secrets")

    def purge_tenant_domain_records(self, resources: TenantResourceIdentity) -> None:
        self.calls.append("records")


def identity(secret_root: Path) -> TenantResourceIdentity:
    normalized = TENANT_ID.replace("-", "_")
    return TenantResourceIdentity(
        tenant_id=TENANT_ID,
        compose_project=f"memory-tenant-{TENANT_ID}",
        neo4j_volume=f"memory-tenant-{TENANT_ID}-neo4j-data",
        database_name=f"tenant_{normalized}",
        database_role=f"tenant_{normalized}_rw",
        route_id=f"route:{TENANT_ID}",
        tenant_secret_ref=str(secret_root / TENANT_ID),
    )


def evidence() -> ProtectionEvidence:
    return ProtectionEvidence(
        kind=ProtectionKind.BACKUP,
        artifact_id="backup-product-a-001",
        created_at=NOW - timedelta(hours=2),
        verified_at=NOW - timedelta(hours=1),
        verified_by="operator-backup",
        integrity_verified=True,
        complete=True,
    )


def write_json(path: Path, document: object) -> None:
    path.write_text(json.dumps(document), encoding="utf-8")


def test_memoryctl_decommission_uses_versioned_files_and_exact_confirmation(
    tmp_path: Path,
) -> None:
    destruction = Destruction()
    decommission = DecommissionService(
        store=InMemoryDecommissionStore(),
        access=Access(),
        protection=Protection(),
        reactivation=Readiness(),
        destruction=destruction,
    )
    control_store = InMemoryControlStore()
    control = ControlModule(control_store, TokenService(control_store))
    output = io.StringIO()
    request_path = tmp_path / "request.json"
    confirmation_path = tmp_path / "confirmation.json"
    finalization_path = tmp_path / "finalization.json"
    resource = identity(tmp_path / "secrets")
    write_json(
        request_path,
        {
            "version": 1,
            "request_id": "decom-001",
            "resources": resource.model_dump(mode="json"),
            "actor_id": "operator-alice",
            "reason": "team retired",
            "requested_at": NOW.isoformat(),
            "protection_policy": "recent-valid-backup-or-accepted-export",
        },
    )
    write_json(
        confirmation_path,
        {
            "version": 1,
            "request_id": "decom-001",
            "tenant_id": TENANT_ID,
            "actor_id": "operator-bob",
            "evidence": evidence().model_dump(mode="json"),
            "confirmed_at": NOW.isoformat(),
        },
    )
    write_json(
        finalization_path,
        {
            "version": 1,
            "request_id": "decom-001",
            "tenant_id": TENANT_ID,
            "finalized_at": (NOW + timedelta(days=30)).isoformat(),
        },
    )

    run_cli(
        ["decommission", "request", "--input", str(request_path)],
        control,
        output,
        decommissioner=decommission,
    )
    with pytest.raises(ValueError, match="immutable Tenant ID"):
        run_cli(
            [
                "decommission",
                "confirm",
                "--input",
                str(confirmation_path),
                "--confirm-tenant-id",
                "wrong-tenant",
            ],
            control,
            output,
            decommissioner=decommission,
        )
    run_cli(
        [
            "decommission",
            "confirm",
            "--input",
            str(confirmation_path),
            "--confirm-tenant-id",
            TENANT_ID,
        ],
        control,
        output,
        decommissioner=decommission,
    )
    run_cli(
        [
            "decommission",
            "finalize",
            "--input",
            str(finalization_path),
            "--confirm-tenant-id",
            TENANT_ID,
        ],
        control,
        output,
        decommissioner=decommission,
    )

    documents = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [document["operation"] for document in documents] == [
        "request",
        "confirm",
        "finalize",
    ]
    assert documents[0]["state"] == "suspended"
    assert documents[1]["state"] == "grace-period"
    assert documents[2]["status"] == "destroyed"
    assert destruction.calls == ["compose", "volume", "postgres", "route", "secrets", "records"]


def test_operator_json_rejects_unknown_versions_and_fields(tmp_path: Path) -> None:
    path = tmp_path / "request.json"
    resource = identity(tmp_path / "secrets")
    write_json(
        path,
        {
            "version": 2,
            "request_id": "decom-001",
            "resources": resource.model_dump(mode="json"),
            "actor_id": "operator-alice",
            "reason": "team retired",
            "requested_at": NOW.isoformat(),
            "unexpected": "not allowed",
        },
    )
    store = InMemoryControlStore()
    with pytest.raises(ValidationError):
        run_cli(
            ["decommission", "request", "--input", str(path)],
            ControlModule(store, TokenService(store)),
            decommissioner=DecommissionService(
                store=InMemoryDecommissionStore(),
                access=Access(),
                protection=Protection(),
                reactivation=Readiness(),
                destruction=Destruction(),
            ),
        )


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], Mapping[str, str] | None]] = []

    def run(
        self,
        arguments: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.calls.append((tuple(arguments), environment))


def test_host_compose_adapter_uses_only_exact_project_and_tenant_environment(
    tmp_path: Path,
) -> None:
    compose = tmp_path / "tenant.compose.yaml"
    compose.write_text("services: {}\n", encoding="utf-8")
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    resource = identity(secrets)
    runner = RecordingRunner()
    adapter = HostTenantDestructionAdapter(
        command_runner=runner,
        compose_file=compose,
        secrets_root=secrets,
        control_database_url="postgresql://control",
        postgres_admin_url="postgresql://admin",
        telemetry_pseudonymizer=_TELEMETRY,
    )

    adapter.remove_compose_project(resource)
    adapter.remove_neo4j_volume(resource)

    assert runner.calls[0][0] == (
        "docker",
        "compose",
        "--project-name",
        f"memory-tenant-{TENANT_ID}",
        "--file",
        str(compose),
        "down",
        "--remove-orphans",
    )
    assert runner.calls[1][0][-3:] == ("down", "--volumes", "--remove-orphans")
    assert runner.calls[0][1] == {
        "TENANT_ID": TENANT_ID,
        "TENANT_SECRETS_DIR": str(secrets / TENANT_ID),
        "TENANT_TELEMETRY_REF": _TELEMETRY.reference("tenant", TENANT_ID),
    }


def test_protection_adapter_requires_exact_registered_receipt(tmp_path: Path) -> None:
    item = evidence()
    artifact = tmp_path / "manifest.json"
    artifact.write_text('{"complete":true}', encoding="utf-8")
    receipt = ProtectionEvidenceReceipt(
        tenant_id=TENANT_ID,
        evidence=item,
        artifact_manifest_path=str(artifact),
        artifact_manifest_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
    )
    path = tmp_path / f"{item.artifact_id}.json"
    path.write_text(receipt.model_dump_json(), encoding="utf-8")
    verifier = FilesystemProtectionEvidenceAdapter(tmp_path)

    assert verifier.verify(TENANT_ID, item)
    assert not verifier.verify("tenant-other", item)
    artifact.write_text('{"complete":false}', encoding="utf-8")
    assert not verifier.verify(TENANT_ID, item)
    assert not verifier.verify(
        TENANT_ID,
        item.model_copy(update={"integrity_verified": False}),
    )


@pytest.mark.parametrize("state", ("suspended", "grace-period", "finalizing"))
def test_open_decommission_pin_remains_active_at_grace_boundary(state: str) -> None:
    grace_boundary = NOW + timedelta(days=30)

    pins = _decode_retention_pins(
        [("backup-product-a-001", state, grace_boundary)],
        [],
        now=grace_boundary,
    )

    assert pins[0].status == "pinned"
    assert pins[0].reason == f"open-decommission:{state}"
    assert pins[0].protected_until == grace_boundary


def test_post_destruction_pin_expires_only_at_its_30_day_boundary() -> None:
    retention_boundary = NOW + timedelta(days=30)

    before = _decode_retention_pins(
        [],
        [("backup-product-a-001", retention_boundary)],
        now=retention_boundary - timedelta(microseconds=1),
    )
    at_boundary = _decode_retention_pins(
        [],
        [("backup-product-a-001", retention_boundary)],
        now=retention_boundary,
    )

    assert before[0].reason == "post-destruction-recovery"
    assert before[0].protected_until == retention_boundary
    assert at_boundary == ()

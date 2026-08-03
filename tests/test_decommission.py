from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from agent_memory_service.decommission import (
    DecommissionConflict,
    DecommissionNotReady,
    DecommissionOperationFailed,
    DecommissionService,
    DecommissionState,
    DestructionStep,
    InMemoryDecommissionStore,
    ProtectionEvidence,
    ProtectionKind,
    TenantResourceIdentity,
)

NOW = datetime(2026, 8, 2, 12, tzinfo=UTC)
TENANT_A = "tenant-product-a-backend"
TENANT_B = "tenant-product-b-platform"


def resources(tenant_id: str = TENANT_A) -> TenantResourceIdentity:
    normalized = tenant_id.replace("-", "_")
    return TenantResourceIdentity(
        tenant_id=tenant_id,
        compose_project=f"memory-tenant-{tenant_id}",
        neo4j_volume=f"memory-tenant-{tenant_id}-neo4j-data",
        database_name=f"tenant_{normalized}",
        database_role=f"tenant_{normalized}_rw",
        route_id=f"route:{tenant_id}",
        tenant_secret_ref=f"/run/memory-tenants/{tenant_id}",
    )


def backup_evidence(
    *,
    created_at: datetime = NOW - timedelta(hours=2),
    complete: bool = True,
    integrity_verified: bool = True,
) -> ProtectionEvidence:
    return ProtectionEvidence(
        kind=ProtectionKind.BACKUP,
        artifact_id="backup-product-a-20260802",
        created_at=created_at,
        verified_at=NOW - timedelta(hours=1),
        verified_by="operator-backup",
        integrity_verified=integrity_verified,
        complete=complete,
    )


class Access:
    def __init__(self, *, fail_once_at: str | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_once_at = fail_once_at

    def _call(self, operation: str, value: TenantResourceIdentity) -> None:
        self.calls.append((operation, value.tenant_id))
        if self.fail_once_at == operation:
            self.fail_once_at = None
            raise RuntimeError("temporary access failure")

    def suspend_sessions(self, value: TenantResourceIdentity) -> None:
        self._call("suspend-sessions", value)

    def block_token_issuance(self, value: TenantResourceIdentity) -> None:
        self._call("block-token-issuance", value)

    def revoke_active_credentials(self, value: TenantResourceIdentity) -> None:
        self._call("revoke-active-credentials", value)

    def reactivate_without_issuing_tokens(self, value: TenantResourceIdentity) -> None:
        self._call("reactivate-without-tokens", value)


class Protection:
    def __init__(self, *, valid: bool = True) -> None:
        self.valid = valid
        self.calls: list[tuple[str, str]] = []

    def verify(self, tenant_id: str, evidence: ProtectionEvidence) -> bool:
        self.calls.append((tenant_id, evidence.artifact_id))
        return self.valid


class Reactivation:
    def __init__(self, *, fail_check: str | None = None) -> None:
        self.fail_check = fail_check
        self.calls: list[str] = []

    def _check(self, name: str, value: TenantResourceIdentity) -> bool:
        self.calls.append(f"{name}:{value.tenant_id}")
        return name != self.fail_check

    def verify_route(self, value: TenantResourceIdentity) -> bool:
        return self._check("route", value)

    def verify_schema(self, value: TenantResourceIdentity) -> bool:
        return self._check("schema", value)

    def verify_isolation(self, value: TenantResourceIdentity) -> bool:
        return self._check("isolation", value)

    def verify_credentials(self, value: TenantResourceIdentity) -> bool:
        return self._check("credentials", value)

    def verify_health(self, value: TenantResourceIdentity) -> bool:
        return self._check("health", value)


class Destruction:
    def __init__(self, *, fail_once_at: str | None = None) -> None:
        self.fail_once_at = fail_once_at
        self.calls: list[tuple[str, str]] = []

    def _remove(self, name: str, value: TenantResourceIdentity) -> None:
        self.calls.append((name, value.tenant_id))
        if self.fail_once_at == name:
            self.fail_once_at = None
            raise RuntimeError("temporary infrastructure failure")

    def remove_compose_project(self, value: TenantResourceIdentity) -> None:
        self._remove("remove-compose-project", value)

    def remove_neo4j_volume(self, value: TenantResourceIdentity) -> None:
        self._remove("remove-neo4j-volume", value)

    def remove_tenant_database_and_role(self, value: TenantResourceIdentity) -> None:
        self._remove("remove-tenant-database-and-role", value)

    def remove_route(self, value: TenantResourceIdentity) -> None:
        self._remove("remove-route", value)

    def remove_tenant_secret_files(self, value: TenantResourceIdentity) -> None:
        self._remove("remove-tenant-secret-files", value)

    def purge_tenant_domain_records(self, value: TenantResourceIdentity) -> None:
        self._remove("purge-tenant-domain-records", value)


def service(
    *,
    store: InMemoryDecommissionStore | None = None,
    access: Access | None = None,
    protection: Protection | None = None,
    reactivation: Reactivation | None = None,
    destruction: Destruction | None = None,
) -> tuple[
    DecommissionService,
    InMemoryDecommissionStore,
    Access,
    Protection,
    Reactivation,
    Destruction,
]:
    actual_store = store or InMemoryDecommissionStore()
    actual_access = access or Access()
    actual_protection = protection or Protection()
    actual_reactivation = reactivation or Reactivation()
    actual_destruction = destruction or Destruction()
    return (
        DecommissionService(
            store=actual_store,
            access=actual_access,
            protection=actual_protection,
            reactivation=actual_reactivation,
            destruction=actual_destruction,
        ),
        actual_store,
        actual_access,
        actual_protection,
        actual_reactivation,
        actual_destruction,
    )


def request(
    decommission: DecommissionService,
    *,
    tenant_id: str = TENANT_A,
    request_id: str = "decom-product-a-001",
    actor_id: str = "operator-alice",
) -> None:
    decommission.request(
        request_id=request_id,
        resources=resources(tenant_id),
        actor_id=actor_id,
        reason="engineering team retired",
        requested_at=NOW,
    )


def confirm(
    decommission: DecommissionService,
    *,
    tenant_id: str = TENANT_A,
    request_id: str = "decom-product-a-001",
) -> None:
    decommission.confirm(
        request_id=request_id,
        tenant_id=tenant_id,
        actor_id="operator-bob",
        evidence=backup_evidence(),
        confirmed_at=NOW,
    )


def test_request_is_non_destructive_immutable_and_idempotent() -> None:
    decommission, store, access, _, _, destruction = service()
    request(decommission)

    record = store.get_record("decom-product-a-001")
    assert record is not None
    assert record.state is DecommissionState.SUSPENDED
    assert record.resources.tenant_id == TENANT_A
    assert access.calls == [
        ("suspend-sessions", TENANT_A),
        ("block-token-issuance", TENANT_A),
        ("revoke-active-credentials", TENANT_A),
    ]
    assert destruction.calls == []

    request(decommission)
    assert len(access.calls) == 3
    with pytest.raises(DecommissionConflict, match="payload does not match"):
        decommission.request(
            request_id="decom-product-a-001",
            resources=resources(TENANT_A),
            actor_id="operator-alice",
            reason="a different reason",
            requested_at=NOW,
        )


def test_resource_identity_rejects_shared_or_broad_targets() -> None:
    valid = resources().model_dump()
    with pytest.raises(ValidationError, match="database_name must be derived"):
        TenantResourceIdentity(**{**valid, "database_name": "postgres"})
    with pytest.raises(ValidationError, match="exact absolute tenant path"):
        TenantResourceIdentity(**{**valid, "tenant_secret_ref": "/"})
    with pytest.raises(ValidationError, match="compose_project must be derived"):
        TenantResourceIdentity(**{**valid, "compose_project": "coengram-shared"})


def test_failed_suspension_resumes_from_last_persisted_step() -> None:
    access = Access(fail_once_at="block-token-issuance")
    decommission, store, _, _, _, _ = service(access=access)
    with pytest.raises(DecommissionOperationFailed, match="block-token"):
        request(decommission)

    partial = store.get_record("decom-product-a-001")
    assert partial is not None
    assert partial.state is DecommissionState.SUSPENDING
    assert [step.value for step in partial.suspension_steps] == ["suspend-sessions"]

    request(decommission)
    assert access.calls.count(("suspend-sessions", TENANT_A)) == 1
    assert store.get_record("decom-product-a-001").state is DecommissionState.SUSPENDED  # type: ignore[union-attr]


def test_confirmation_requires_exact_tenant_and_a_different_operator() -> None:
    decommission, _, _, _, _, _ = service()
    request(decommission)

    with pytest.raises(DecommissionConflict, match="own request"):
        decommission.confirm(
            request_id="decom-product-a-001",
            tenant_id=TENANT_A,
            actor_id="operator-alice",
            evidence=backup_evidence(),
            confirmed_at=NOW,
        )
    with pytest.raises(DecommissionConflict, match="tenant_id"):
        decommission.confirm(
            request_id="decom-product-a-001",
            tenant_id=TENANT_B,
            actor_id="operator-bob",
            evidence=backup_evidence(),
            confirmed_at=NOW,
        )


@pytest.mark.parametrize(
    "evidence, match",
    [
        (backup_evidence(complete=False), "incomplete"),
        (backup_evidence(integrity_verified=False), "unverified"),
        (backup_evidence(created_at=NOW - timedelta(hours=25)), "older"),
        (
            ProtectionEvidence(
                kind=ProtectionKind.ACCEPTED_EXPORT,
                artifact_id="export-product-a",
                created_at=NOW - timedelta(days=2),
                verified_at=NOW - timedelta(hours=1),
                verified_by="operator-backup",
                integrity_verified=True,
                complete=True,
                export_explicitly_accepted=False,
            ),
            "explicitly accepted",
        ),
    ],
)
def test_confirmation_requires_valid_backup_or_accepted_export(
    evidence: ProtectionEvidence, match: str
) -> None:
    decommission, store, _, _, _, _ = service()
    request(decommission)
    with pytest.raises(DecommissionNotReady, match=match):
        decommission.confirm(
            request_id="decom-product-a-001",
            tenant_id=TENANT_A,
            actor_id="operator-bob",
            evidence=evidence,
            confirmed_at=NOW,
        )
    assert store.get_record("decom-product-a-001").state is DecommissionState.SUSPENDED  # type: ignore[union-attr]


def test_grace_period_is_exactly_30_days_and_early_finalization_is_rejected() -> None:
    decommission, store, _, _, _, destruction = service()
    request(decommission)
    confirm(decommission)
    record = store.get_record("decom-product-a-001")
    assert record is not None
    assert record.grace_ends_at == NOW + timedelta(days=30)

    with pytest.raises(DecommissionNotReady, match="30-day"):
        decommission.finalize(
            request_id="decom-product-a-001",
            tenant_id=TENANT_A,
            finalized_at=NOW + timedelta(days=30) - timedelta(microseconds=1),
        )
    assert destruction.calls == []


def test_cancel_requires_all_checks_and_never_issues_tokens() -> None:
    decommission, store, access, _, reactivation, _ = service()
    request(decommission)
    confirm(decommission)

    cancelled = decommission.cancel(
        request_id="decom-product-a-001",
        tenant_id=TENANT_A,
        actor_id="operator-carol",
        cancelled_at=NOW + timedelta(days=2),
    )
    assert cancelled.state is DecommissionState.CANCELLED
    assert reactivation.calls == [
        f"route:{TENANT_A}",
        f"schema:{TENANT_A}",
        f"isolation:{TENANT_A}",
        f"credentials:{TENANT_A}",
        f"health:{TENANT_A}",
    ]
    assert access.calls[-1] == ("reactivate-without-tokens", TENANT_A)
    assert all("issue" not in operation for operation, _ in access.calls)
    assert store.get_record("decom-product-a-001") == cancelled


def test_failed_cancel_check_keeps_tenant_suspended_in_grace() -> None:
    decommission, store, access, _, _, _ = service(
        reactivation=Reactivation(fail_check="isolation")
    )
    request(decommission)
    confirm(decommission)

    with pytest.raises(DecommissionNotReady, match="verify-isolation"):
        decommission.cancel(
            request_id="decom-product-a-001",
            tenant_id=TENANT_A,
            actor_id="operator-carol",
            cancelled_at=NOW + timedelta(days=2),
        )
    record = store.get_record("decom-product-a-001")
    assert record is not None
    assert record.state is DecommissionState.GRACE_PERIOD
    assert record.last_failed_action == "verify-isolation"
    assert ("reactivate-without-tokens", TENANT_A) not in access.calls


def test_partial_finalization_retries_only_incomplete_exact_tenant_steps() -> None:
    destruction = Destruction(fail_once_at="remove-tenant-database-and-role")
    decommission, store, _, _, _, _ = service(destruction=destruction)
    request(decommission)
    confirm(decommission)

    with pytest.raises(DecommissionOperationFailed, match="database-and-role"):
        decommission.finalize(
            request_id="decom-product-a-001",
            tenant_id=TENANT_A,
            finalized_at=NOW + timedelta(days=30),
        )
    partial = store.get_record("decom-product-a-001")
    assert partial is not None
    assert partial.state is DecommissionState.FINALIZING
    assert partial.destruction_steps == (
        DestructionStep.REMOVE_COMPOSE_PROJECT,
        DestructionStep.REMOVE_NEO4J_VOLUME,
    )

    tombstone = decommission.finalize(
        request_id="decom-product-a-001",
        tenant_id=TENANT_A,
        finalized_at=NOW + timedelta(days=30, minutes=1),
    )
    assert destruction.calls.count(("remove-compose-project", TENANT_A)) == 1
    assert destruction.calls.count(("remove-neo4j-volume", TENANT_A)) == 1
    assert {tenant_id for _, tenant_id in destruction.calls} == {TENANT_A}
    assert store.get_record("decom-product-a-001") is None
    assert "reason" not in tombstone.model_dump()
    assert "resources" not in tombstone.model_dump()
    assert "artifact" not in str(tombstone.model_dump())

    call_count = len(destruction.calls)
    assert (
        decommission.finalize(
            request_id="decom-product-a-001",
            tenant_id=TENANT_A,
            finalized_at=NOW + timedelta(days=31),
        )
        == tombstone
    )
    assert len(destruction.calls) == call_count


def test_finalizing_one_tenant_does_not_change_another_tenant() -> None:
    decommission, store, _, _, _, destruction = service()
    request(decommission)
    confirm(decommission)
    request(
        decommission,
        tenant_id=TENANT_B,
        request_id="decom-product-b-001",
        actor_id="operator-carol",
    )
    tenant_b_before = store.get_record("decom-product-b-001")

    decommission.finalize(
        request_id="decom-product-a-001",
        tenant_id=TENANT_A,
        finalized_at=NOW + timedelta(days=30),
    )

    assert store.get_record("decom-product-b-001") == tenant_b_before
    assert {tenant_id for _, tenant_id in destruction.calls} == {TENANT_A}

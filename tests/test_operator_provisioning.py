from __future__ import annotations

from dataclasses import dataclass, field

from agent_memory_service.control import InMemoryControlStore
from agent_memory_service.manifest import TenantManifest
from agent_memory_service.operator_provisioning import (
    OperatorProvisioningService,
    ProvisioningJobState,
    new_provisioning_job,
)
from agent_memory_service.provisioning import (
    PROVISIONING_STEPS,
    ProvisioningFailed,
    ProvisioningState,
    ProvisioningStatus,
    ProvisioningStep,
)


def test_operator_service_claims_and_completes_queued_provisioning_job() -> None:
    store = InMemoryControlStore()
    manifest = _manifest()
    queued = store.create_provisioning_job(
        new_provisioning_job(
            manifest,
            idempotency_key="create-product-a",
            requested_by_operator_id="operator-alice",
        )
    )
    provisioner = _Provisioner(
        ProvisioningState(
            tenant_id=manifest.tenant_id,
            manifest_fingerprint=manifest.fingerprint,
            database_name=manifest.database_name,
            database_role=manifest.database_role,
            neo4j_service_name=manifest.neo4j_service_name,
            status=ProvisioningStatus.ACTIVE,
            completed_steps=PROVISIONING_STEPS,
            attempt=1,
        )
    )
    service = OperatorProvisioningService(store, provisioner, worker_id="operator-service-1")

    completed = service.run_once()

    assert completed is not None
    assert completed.job_id == queued.job_id
    assert completed.state is ProvisioningJobState.SUCCEEDED
    assert completed.claimed_by == "operator-service-1"
    assert completed.attempt == 1
    assert completed.completed_steps == PROVISIONING_STEPS
    assert provisioner.manifests == [manifest]
    assert service.run_once() is None


def test_operator_service_marks_failed_job_without_recording_exception_text() -> None:
    store = InMemoryControlStore()
    manifest = _manifest()
    queued = store.create_provisioning_job(
        new_provisioning_job(
            manifest,
            idempotency_key="create-product-a",
            requested_by_operator_id="operator-alice",
        )
    )
    service = OperatorProvisioningService(
        store,
        _Provisioner(ProvisioningFailed(ProvisioningStep.WRITE_SECRETS)),
        worker_id="operator-service-1",
    )

    failed = service.run_once()

    assert failed is not None
    assert failed.job_id == queued.job_id
    assert failed.state is ProvisioningJobState.FAILED
    assert failed.failed_step is ProvisioningStep.WRITE_SECRETS
    assert failed.failure_code == "ProvisioningFailed"


def test_operator_service_skips_jobs_with_requested_cancellation() -> None:
    store = InMemoryControlStore()
    manifest = _manifest()
    queued = store.create_provisioning_job(
        new_provisioning_job(
            manifest,
            idempotency_key="create-product-a",
            requested_by_operator_id="operator-alice",
        )
    )
    store.update_provisioning_job_state(
        queued.job_id,
        state=ProvisioningJobState.CANCEL_REQUESTED,
        changed_at=queued.created_at,
    )
    service = OperatorProvisioningService(
        store,
        _Provisioner(AssertionError("canceled job should not run")),
        worker_id="operator-service-1",
    )

    assert service.run_once() is None


def _manifest() -> TenantManifest:
    return TenantManifest(tenant_id="tenant-product-a", name="Product A")


@dataclass
class _Provisioner:
    result: ProvisioningState | Exception
    manifests: list[TenantManifest] = field(default_factory=list, init=False)

    def apply(self, manifest: TenantManifest) -> ProvisioningState:
        self.manifests.append(manifest)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

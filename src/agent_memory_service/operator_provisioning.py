"""Provisioning Job contracts for Operator-requested Tenant provisioning."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from agent_memory_service.provisioning import (
    ProvisioningFailed,
    ProvisioningState,
    ProvisioningStatus,
    ProvisioningStep,
)

if TYPE_CHECKING:
    from agent_memory_service.manifest import TenantManifest


class ProvisioningJobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELED = "canceled"
    CLEANUP_REQUESTED = "cleanup_requested"
    CLEANED_UP = "cleaned_up"


@dataclass(frozen=True, slots=True)
class ProvisioningJobRecord:
    job_id: str
    tenant_id: str
    manifest_fingerprint: str
    manifest: TenantManifest
    idempotency_key: str
    requested_by_operator_id: str
    state: ProvisioningJobState
    created_at: datetime
    updated_at: datetime
    attempt: int = 0
    completed_steps: tuple[ProvisioningStep, ...] = ()
    failed_step: ProvisioningStep | None = None
    failure_code: str | None = None
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    heartbeat_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    cleanup_requested_at: datetime | None = None
    cleanup_completed_at: datetime | None = None


class ProvisioningJobView(BaseModel):
    model_config = ConfigDict(frozen=True)

    job_id: str
    tenant_id: str
    manifest_fingerprint: str
    requested_by_operator_id: str
    state: ProvisioningJobState
    attempt: int
    completed_steps: tuple[ProvisioningStep, ...]
    failed_step: ProvisioningStep | None = None
    failure_code: str | None = None
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    heartbeat_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    cleanup_requested_at: datetime | None = None
    cleanup_completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


def new_provisioning_job(
    manifest: TenantManifest,
    *,
    idempotency_key: str,
    requested_by_operator_id: str,
) -> ProvisioningJobRecord:
    now = datetime.now(UTC)
    return ProvisioningJobRecord(
        job_id=str(uuid4()),
        tenant_id=manifest.tenant_id,
        manifest_fingerprint=manifest.fingerprint,
        manifest=manifest,
        idempotency_key=idempotency_key,
        requested_by_operator_id=requested_by_operator_id,
        state=ProvisioningJobState.QUEUED,
        created_at=now,
        updated_at=now,
    )


def provisioning_job_view(job: ProvisioningJobRecord) -> ProvisioningJobView:
    return ProvisioningJobView(
        job_id=job.job_id,
        tenant_id=job.tenant_id,
        manifest_fingerprint=job.manifest_fingerprint,
        requested_by_operator_id=job.requested_by_operator_id,
        state=job.state,
        attempt=job.attempt,
        completed_steps=job.completed_steps,
        failed_step=job.failed_step,
        failure_code=job.failure_code,
        claimed_by=job.claimed_by,
        claimed_at=job.claimed_at,
        heartbeat_at=job.heartbeat_at,
        cancel_requested_at=job.cancel_requested_at,
        cleanup_requested_at=job.cleanup_requested_at,
        cleanup_completed_at=job.cleanup_completed_at,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


class ProvisioningJobStore(Protocol):
    def claim_next_provisioning_job(
        self,
        *,
        worker_id: str,
        claimed_at: datetime,
    ) -> ProvisioningJobRecord | None: ...

    def complete_provisioning_job(
        self,
        job_id: str,
        *,
        worker_id: str,
        completed_steps: tuple[ProvisioningStep, ...],
        completed_at: datetime,
    ) -> ProvisioningJobRecord: ...

    def fail_provisioning_job(
        self,
        job_id: str,
        *,
        worker_id: str,
        failed_step: ProvisioningStep | None,
        failure_code: str,
        failed_at: datetime,
    ) -> ProvisioningJobRecord: ...


class TenantProvisionerPort(Protocol):
    def apply(self, manifest: TenantManifest) -> ProvisioningState: ...


class OperatorProvisioningService:
    """Claim queued Provisioning Jobs and execute the host-side Tenant provisioner."""

    def __init__(
        self,
        store: ProvisioningJobStore,
        provisioner: TenantProvisionerPort,
        *,
        worker_id: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        normalized_worker_id = worker_id.strip()
        if not normalized_worker_id:
            raise ValueError("Operator Service worker ID cannot be empty")
        self._store = store
        self._provisioner = provisioner
        self._worker_id = normalized_worker_id
        self._clock = clock or (lambda: datetime.now(UTC))

    def run_once(self) -> ProvisioningJobRecord | None:
        job = self._store.claim_next_provisioning_job(
            worker_id=self._worker_id,
            claimed_at=self._clock(),
        )
        if job is None:
            return None
        try:
            state = self._provisioner.apply(job.manifest)
        except ProvisioningFailed as exc:
            return self._store.fail_provisioning_job(
                job.job_id,
                worker_id=self._worker_id,
                failed_step=exc.step,
                failure_code=type(exc).__name__,
                failed_at=self._clock(),
            )
        except Exception as exc:
            return self._store.fail_provisioning_job(
                job.job_id,
                worker_id=self._worker_id,
                failed_step=None,
                failure_code=type(exc).__name__,
                failed_at=self._clock(),
            )
        if state.status is ProvisioningStatus.ACTIVE:
            return self._store.complete_provisioning_job(
                job.job_id,
                worker_id=self._worker_id,
                completed_steps=state.completed_steps,
                completed_at=self._clock(),
            )
        return self._store.fail_provisioning_job(
            job.job_id,
            worker_id=self._worker_id,
            failed_step=state.failed_step,
            failure_code=state.failure_code or state.status.value,
            failed_at=self._clock(),
        )

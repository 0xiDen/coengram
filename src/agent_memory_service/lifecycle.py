"""Private Memory correction and approved-erasure contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from agent_memory_service.models import MemoryKind


class CorrectMemory(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True, extra="forbid")

    memory_id: str
    replacement_content: str = Field(min_length=1, max_length=16_000)
    kind: MemoryKind = MemoryKind.EXPLICIT
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=4_000)
    idempotency_key: str = Field(min_length=1, max_length=255)


class RequestErasure(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True, extra="forbid")

    memory_id: str
    reason: str = Field(min_length=1, max_length=4_000)
    idempotency_key: str = Field(min_length=1, max_length=255)


class ErasureDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class ErasureStatus(StrEnum):
    REQUESTED = "requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    COMPLETED = "completed"
    FAILED = "failed"


class ReviewErasure(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True, extra="forbid")

    request_id: str
    decision: ErasureDecision
    rationale: str = Field(min_length=1, max_length=4_000)
    idempotency_key: str = Field(min_length=1, max_length=255)


class ErasureRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    tenant_id: str
    memory_id: str
    requester_id: str
    owner_principal_id: str
    reason: str | None
    status: ErasureStatus
    created_at: datetime
    reviewed_by: str | None = None
    review_rationale: str | None = None
    completed_at: datetime | None = None


class ErasureRequestView(BaseModel):
    """Content-free request and eventual tombstone exposed to callers."""

    model_config = ConfigDict(frozen=True)

    id: str
    memory_id: str
    requester_id: str
    owner_principal_id: str
    status: ErasureStatus
    created_at: datetime
    reviewed_by: str | None = None
    review_rationale: str | None = None
    completed_at: datetime | None = None


class ErasureTombstone(BaseModel):
    """Content-free proof that an approved Private Memory erasure completed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    memory_id: str
    requester_id: str
    owner_principal_id: str
    decision: Literal[ErasureDecision.APPROVE] = ErasureDecision.APPROVE
    status: Literal[ErasureStatus.COMPLETED] = ErasureStatus.COMPLETED
    created_at: datetime
    reviewed_by: str
    completed_at: datetime


class ErasureStore(Protocol):
    async def request(
        self,
        tenant_id: str,
        requester_id: str,
        owner_principal_id: str,
        command: RequestErasure,
    ) -> ErasureRequest: ...

    async def get_request(self, tenant_id: str, request_id: str) -> ErasureRequest | None: ...

    async def review(
        self,
        tenant_id: str,
        reviewer_id: str,
        command: ReviewErasure,
    ) -> ErasureRequest: ...

    async def next_approved(self, tenant_id: str) -> ErasureRequest | None: ...

    async def mark_completed(self, tenant_id: str, request_id: str) -> ErasureRequest: ...

    async def list_completed(
        self, tenant_id: str, owner_principal_id: str
    ) -> tuple[ErasureTombstone, ...]: ...

    async def import_tombstone(
        self,
        tenant_id: str,
        owner_principal_id: str,
        tombstone: ErasureTombstone,
    ) -> bool: ...


class InMemoryErasureStore:
    def __init__(self) -> None:
        self._requests: dict[str, ErasureRequest] = {}
        self._request_keys: dict[tuple[str, str, str], str] = {}
        self._review_keys: dict[tuple[str, str, str], str] = {}

    async def request(
        self,
        tenant_id: str,
        requester_id: str,
        owner_principal_id: str,
        command: RequestErasure,
    ) -> ErasureRequest:
        key = (tenant_id, requester_id, command.idempotency_key)
        if request_id := self._request_keys.get(key):
            return self._requests[request_id]
        request = ErasureRequest(
            id=str(uuid4()),
            tenant_id=tenant_id,
            memory_id=command.memory_id,
            requester_id=requester_id,
            owner_principal_id=owner_principal_id,
            reason=command.reason,
            status=ErasureStatus.REQUESTED,
            created_at=datetime.now(UTC),
        )
        self._requests[request.id] = request
        self._request_keys[key] = request.id
        return request

    async def get_request(self, tenant_id: str, request_id: str) -> ErasureRequest | None:
        request = self._requests.get(request_id)
        if request is None or request.tenant_id != tenant_id:
            return None
        return request

    async def review(
        self,
        tenant_id: str,
        reviewer_id: str,
        command: ReviewErasure,
    ) -> ErasureRequest:
        key = (tenant_id, reviewer_id, command.idempotency_key)
        if request_id := self._review_keys.get(key):
            return self._requests[request_id]
        request = await self.get_request(tenant_id, command.request_id)
        if request is None:
            raise LookupError("Erasure Request not found")
        if request.status is not ErasureStatus.REQUESTED:
            raise ValueError("Erasure Request is not awaiting review")
        reviewed = request.model_copy(
            update={
                "status": (
                    ErasureStatus.APPROVED
                    if command.decision is ErasureDecision.APPROVE
                    else ErasureStatus.REJECTED
                ),
                "reviewed_by": reviewer_id,
                "review_rationale": command.rationale,
            }
        )
        self._requests[request.id] = reviewed
        self._review_keys[key] = request.id
        return reviewed

    async def next_approved(self, tenant_id: str) -> ErasureRequest | None:
        return next(
            (
                request
                for request in self._requests.values()
                if request.tenant_id == tenant_id and request.status is ErasureStatus.APPROVED
            ),
            None,
        )

    async def mark_completed(self, tenant_id: str, request_id: str) -> ErasureRequest:
        request = await self.get_request(tenant_id, request_id)
        if request is None:
            raise LookupError("Erasure Request not found")
        completed = request.model_copy(
            update={
                "reason": None,
                "status": ErasureStatus.COMPLETED,
                "review_rationale": None,
                "completed_at": datetime.now(UTC),
            }
        )
        self._requests[request.id] = completed
        return completed

    async def list_completed(
        self, tenant_id: str, owner_principal_id: str
    ) -> tuple[ErasureTombstone, ...]:
        return tuple(
            erasure_tombstone(request)
            for request in sorted(
                self._requests.values(),
                key=lambda request: (request.created_at, request.id),
            )
            if request.tenant_id == tenant_id
            and request.owner_principal_id == owner_principal_id
            and request.status is ErasureStatus.COMPLETED
        )

    async def import_tombstone(
        self,
        tenant_id: str,
        owner_principal_id: str,
        tombstone: ErasureTombstone,
    ) -> bool:
        existing = self._requests.get(tombstone.id)
        rebound = tombstone.model_copy(update={"owner_principal_id": owner_principal_id})
        if existing is not None:
            if existing.tenant_id != tenant_id or erasure_tombstone(existing) != rebound:
                raise ValueError("Erasure Tombstone identifier already has different data")
            return False
        self._requests[tombstone.id] = ErasureRequest(
            id=tombstone.id,
            tenant_id=tenant_id,
            memory_id=tombstone.memory_id,
            requester_id=tombstone.requester_id,
            owner_principal_id=owner_principal_id,
            reason=None,
            status=ErasureStatus.COMPLETED,
            created_at=tombstone.created_at,
            reviewed_by=tombstone.reviewed_by,
            review_rationale=None,
            completed_at=tombstone.completed_at,
        )
        return True


def erasure_view(request: ErasureRequest) -> ErasureRequestView:
    return ErasureRequestView(
        id=request.id,
        memory_id=request.memory_id,
        requester_id=request.requester_id,
        owner_principal_id=request.owner_principal_id,
        status=request.status,
        created_at=request.created_at,
        reviewed_by=request.reviewed_by,
        review_rationale=request.review_rationale or None,
        completed_at=request.completed_at,
    )


def erasure_tombstone(request: ErasureRequest) -> ErasureTombstone:
    if (
        request.status is not ErasureStatus.COMPLETED
        or request.reviewed_by is None
        or request.completed_at is None
    ):
        raise ValueError("Erasure Request is not a completed approved erasure")
    return ErasureTombstone(
        id=request.id,
        memory_id=request.memory_id,
        requester_id=request.requester_id,
        owner_principal_id=request.owner_principal_id,
        created_at=request.created_at,
        reviewed_by=request.reviewed_by,
        completed_at=request.completed_at,
    )

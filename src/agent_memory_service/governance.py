"""Knowledge Candidate, review, and publication contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class CandidateStatus(StrEnum):
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"


class ReviewDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class ProposeKnowledge(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True, extra="forbid")

    claim: str = Field(min_length=1, max_length=16_000)
    source_memory_ids: tuple[str, ...] = Field(min_length=1, max_length=100)
    duplicate_memory_ids: tuple[str, ...] = Field(default=(), max_length=100)
    conflicting_memory_ids: tuple[str, ...] = Field(default=(), max_length=100)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    idempotency_key: str = Field(min_length=1, max_length=255)


class ReviewKnowledge(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True, extra="forbid")

    candidate_id: str
    decision: ReviewDecision
    rationale: str = Field(min_length=1, max_length=4_000)
    idempotency_key: str = Field(min_length=1, max_length=255)


class KnowledgeCandidate(BaseModel):
    """Internal candidate state; source identifiers never cross the public view."""

    model_config = ConfigDict(frozen=True)

    id: str
    tenant_id: str
    claim: str
    confidence: float
    proposer_id: str
    source_memory_ids: tuple[str, ...]
    duplicate_memory_ids: tuple[str, ...] = ()
    conflicting_memory_ids: tuple[str, ...] = ()
    status: CandidateStatus
    created_at: datetime
    reviewed_by: str | None = None
    review_rationale: str | None = None


class KnowledgeCandidateView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    claim: str
    confidence: float
    proposer_id: str
    source_count: int
    duplicate_memory_ids: tuple[str, ...] = ()
    conflicting_memory_ids: tuple[str, ...] = ()
    status: CandidateStatus
    created_at: datetime
    reviewed_by: str | None = None
    review_rationale: str | None = None


class PublicationEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    tenant_id: str
    candidate_id: str
    created_at: datetime


class GovernanceStore(Protocol):
    async def propose(
        self,
        tenant_id: str,
        proposer_id: str,
        command: ProposeKnowledge,
    ) -> KnowledgeCandidate: ...

    async def get_candidate(
        self, tenant_id: str, candidate_id: str
    ) -> KnowledgeCandidate | None: ...

    async def list_candidates(self, tenant_id: str) -> tuple[KnowledgeCandidate, ...]: ...

    async def review(
        self,
        tenant_id: str,
        reviewer_id: str,
        command: ReviewKnowledge,
    ) -> KnowledgeCandidate: ...

    async def next_publication(self, tenant_id: str) -> PublicationEvent | None: ...

    async def mark_published(self, tenant_id: str, candidate_id: str) -> KnowledgeCandidate: ...

    async def import_candidate(
        self,
        tenant_id: str,
        proposer_id: str,
        candidate_id: str,
        claim: str,
        confidence: float,
    ) -> KnowledgeCandidate: ...


class InMemoryGovernanceStore:
    """State-machine Adapter used by public contract tests."""

    def __init__(self) -> None:
        self._candidates: dict[str, KnowledgeCandidate] = {}
        self._proposal_keys: dict[tuple[str, str, str], str] = {}
        self._review_keys: dict[tuple[str, str, str], str] = {}
        self._outbox: list[PublicationEvent] = []

    async def propose(
        self,
        tenant_id: str,
        proposer_id: str,
        command: ProposeKnowledge,
    ) -> KnowledgeCandidate:
        key = (tenant_id, proposer_id, command.idempotency_key)
        if candidate_id := self._proposal_keys.get(key):
            return self._candidates[candidate_id]
        candidate = KnowledgeCandidate(
            id=str(uuid4()),
            tenant_id=tenant_id,
            claim=command.claim,
            confidence=command.confidence,
            proposer_id=proposer_id,
            source_memory_ids=command.source_memory_ids,
            duplicate_memory_ids=command.duplicate_memory_ids,
            conflicting_memory_ids=command.conflicting_memory_ids,
            status=CandidateStatus.SUBMITTED,
            created_at=datetime.now(UTC),
        )
        self._candidates[candidate.id] = candidate
        self._proposal_keys[key] = candidate.id
        return candidate

    async def get_candidate(self, tenant_id: str, candidate_id: str) -> KnowledgeCandidate | None:
        candidate = self._candidates.get(candidate_id)
        if candidate is None or candidate.tenant_id != tenant_id:
            return None
        return candidate

    async def list_candidates(self, tenant_id: str) -> tuple[KnowledgeCandidate, ...]:
        return tuple(
            sorted(
                (
                    candidate
                    for candidate in self._candidates.values()
                    if candidate.tenant_id == tenant_id
                ),
                key=lambda candidate: (candidate.created_at, candidate.id),
            )
        )

    async def review(
        self,
        tenant_id: str,
        reviewer_id: str,
        command: ReviewKnowledge,
    ) -> KnowledgeCandidate:
        key = (tenant_id, reviewer_id, command.idempotency_key)
        if candidate_id := self._review_keys.get(key):
            return self._candidates[candidate_id]
        candidate = await self.get_candidate(tenant_id, command.candidate_id)
        if candidate is None:
            raise LookupError("Knowledge Candidate not found")
        if candidate.status is not CandidateStatus.SUBMITTED:
            raise ValueError("Knowledge Candidate is not awaiting review")
        status = (
            CandidateStatus.PUBLISHING
            if command.decision is ReviewDecision.APPROVE
            else CandidateStatus.REJECTED
        )
        reviewed = candidate.model_copy(
            update={
                "status": status,
                "reviewed_by": reviewer_id,
                "review_rationale": command.rationale,
            }
        )
        self._candidates[candidate.id] = reviewed
        self._review_keys[key] = candidate.id
        if status is CandidateStatus.PUBLISHING:
            self._outbox.append(
                PublicationEvent(
                    id=str(uuid4()),
                    tenant_id=tenant_id,
                    candidate_id=candidate.id,
                    created_at=datetime.now(UTC),
                )
            )
        return reviewed

    async def next_publication(self, tenant_id: str) -> PublicationEvent | None:
        return next((event for event in self._outbox if event.tenant_id == tenant_id), None)

    async def mark_published(self, tenant_id: str, candidate_id: str) -> KnowledgeCandidate:
        candidate = await self.get_candidate(tenant_id, candidate_id)
        if candidate is None:
            raise LookupError("Knowledge Candidate not found")
        published = candidate.model_copy(update={"status": CandidateStatus.PUBLISHED})
        self._candidates[candidate_id] = published
        self._outbox = [event for event in self._outbox if event.candidate_id != candidate_id]
        return published

    async def import_candidate(
        self,
        tenant_id: str,
        proposer_id: str,
        candidate_id: str,
        claim: str,
        confidence: float,
    ) -> KnowledgeCandidate:
        existing = await self.get_candidate(tenant_id, candidate_id)
        if existing is not None:
            return existing
        candidate = KnowledgeCandidate(
            id=candidate_id,
            tenant_id=tenant_id,
            claim=claim,
            confidence=confidence,
            proposer_id=proposer_id,
            source_memory_ids=(),
            duplicate_memory_ids=(),
            conflicting_memory_ids=(),
            status=CandidateStatus.SUBMITTED,
            created_at=datetime.now(UTC),
        )
        self._candidates[candidate.id] = candidate
        return candidate


def candidate_view(candidate: KnowledgeCandidate) -> KnowledgeCandidateView:
    return KnowledgeCandidateView(
        id=candidate.id,
        claim=candidate.claim,
        confidence=candidate.confidence,
        proposer_id=candidate.proposer_id,
        source_count=len(candidate.source_memory_ids),
        duplicate_memory_ids=candidate.duplicate_memory_ids,
        conflicting_memory_ids=candidate.conflicting_memory_ids,
        status=candidate.status,
        created_at=candidate.created_at,
        reviewed_by=candidate.reviewed_by,
        review_rationale=candidate.review_rationale,
    )

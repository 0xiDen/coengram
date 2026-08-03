"""Public domain types for authenticated tenant memory operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class PrincipalKind(StrEnum):
    USER = "user"
    AGENT = "agent"


class MemoryScope(StrEnum):
    PRIVATE = "private"
    TENANT_KNOWLEDGE = "tenant_knowledge"


class MemoryState(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ERASURE_PENDING = "erasure_pending"
    ERASED = "erased"


class MutationState(StrEnum):
    ACCEPTED = "accepted"
    APPLIED = "applied"
    FAILED = "failed"


class MemoryKind(StrEnum):
    EXPLICIT = "explicit"
    PREFERENCE = "preference"
    CONSTRAINT = "constraint"
    OUTCOME = "outcome"


@dataclass(frozen=True, slots=True)
class TenantSession:
    """Server-derived authorization context for exactly one Tenant."""

    tenant_id: str
    actor_id: str
    actor_kind: PrincipalKind
    roles: frozenset[str]
    subject_user_id: str | None = None
    delegation_id: str | None = None
    token_id: str | None = None

    def __post_init__(self) -> None:
        if self.subject_user_id is not None and self.actor_kind is not PrincipalKind.AGENT:
            raise ValueError("Only an Agent session can have a Subject User")
        if (self.subject_user_id is None) != (self.delegation_id is None):
            raise ValueError("Subject User and Delegation must be bound together")


class RetainMemory(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True, extra="forbid")

    content: str = Field(min_length=1, max_length=16_000)
    kind: MemoryKind = MemoryKind.EXPLICIT
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    idempotency_key: str = Field(min_length=1, max_length=255)


class RecallQuery(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True, extra="forbid")

    query: str = Field(min_length=1, max_length=4_000)
    limit: int = Field(default=10, ge=1, le=100)


class Provenance(BaseModel):
    model_config = ConfigDict(frozen=True)

    actor_id: str
    source: str


class MemoryItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    owner_principal_id: str | None
    scope: MemoryScope
    content: str
    kind: MemoryKind
    confidence: float
    provenance: Provenance
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    state: MemoryState = MemoryState.ACTIVE
    supersedes_id: str | None = None


class MemoryMutationReceipt(BaseModel):
    """Versioned public contract for an authoritative memory mutation."""

    model_config = ConfigDict(frozen=True)

    version: int = 1
    code: str = "memory_mutation_accepted"
    operation_id: str
    state: MutationState
    item: MemoryItem
    id: str


class PrivateMemoryInspection(BaseModel):
    """Authoritative Private Memory view, including content-free erasure state."""

    model_config = ConfigDict(frozen=True)

    id: str
    owner_principal_id: str
    state: MemoryState
    operation_id: str | None = None
    mutation_state: MutationState
    content: str | None = None
    kind: MemoryKind | None = None
    confidence: float | None = None
    created_at: datetime
    supersedes_id: str | None = None


class RecallResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: tuple[MemoryItem, ...]

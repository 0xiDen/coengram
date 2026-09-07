"""Structured content-safe Operator Audit Events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from agent_memory_service.operator_auth import OperatorSession


class OperatorAuditEventView(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str
    operator_id: str
    roles: tuple[str, ...]
    action: str
    target_type: str
    target_ids: dict[str, str]
    outcome: str
    request_ref: str | None = None
    before_metadata: dict[str, str] = Field(default_factory=dict)
    after_metadata: dict[str, str] = Field(default_factory=dict)
    created_at: datetime


@dataclass(frozen=True, slots=True)
class OperatorAuditEvent:
    event_id: str
    operator_id: str
    roles: frozenset[str]
    action: str
    target_type: str
    target_ids: dict[str, str]
    outcome: str
    created_at: datetime
    request_ref: str | None = None
    before_metadata: dict[str, str] | None = None
    after_metadata: dict[str, str] | None = None


def create_operator_audit_event(
    session: OperatorSession,
    *,
    action: str,
    target_type: str,
    target_ids: dict[str, str],
    outcome: str,
    request_ref: str | None = None,
    before_metadata: dict[str, str] | None = None,
    after_metadata: dict[str, str] | None = None,
) -> OperatorAuditEvent:
    return OperatorAuditEvent(
        event_id=str(uuid4()),
        operator_id=session.operator_id,
        roles=session.roles,
        action=action,
        target_type=target_type,
        target_ids=target_ids,
        outcome=outcome,
        request_ref=request_ref,
        before_metadata=before_metadata,
        after_metadata=after_metadata,
        created_at=datetime.now(UTC),
    )


def operator_audit_event_view(event: OperatorAuditEvent) -> OperatorAuditEventView:
    return OperatorAuditEventView(
        event_id=event.event_id,
        operator_id=event.operator_id,
        roles=tuple(sorted(event.roles)),
        action=event.action,
        target_type=event.target_type,
        target_ids=event.target_ids,
        outcome=event.outcome,
        request_ref=event.request_ref,
        before_metadata=event.before_metadata or {},
        after_metadata=event.after_metadata or {},
        created_at=event.created_at,
    )

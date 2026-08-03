"""PostgreSQL Adapter for Tenant governance, erasure, and transactional outbox state."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, overload
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from agent_memory_service.database_barrier import acquire_backup_shared_lock_async
from agent_memory_service.database_url import (
    psycopg_database_url as _psycopg_database_url,
)
from agent_memory_service.durable_memory import (
    ImportAcceptance,
    PrivateMemoryCommand,
    PrivateMemoryCommandState,
    PrivateMemoryCommandType,
)
from agent_memory_service.governance import (
    CandidateStatus,
    KnowledgeCandidate,
    ProposeKnowledge,
    PublicationEvent,
    ReviewDecision,
    ReviewKnowledge,
)
from agent_memory_service.lifecycle import (
    CorrectMemory,
    ErasureDecision,
    ErasureRequest,
    ErasureStatus,
    ErasureTombstone,
    RequestErasure,
    ReviewErasure,
    erasure_tombstone,
)
from agent_memory_service.models import (
    MemoryItem,
    MemoryScope,
    MemoryState,
    MutationState,
    PrivateMemoryInspection,
    Provenance,
    RetainMemory,
)
from agent_memory_service.outbox import OutboxRecord

_CANDIDATE_COLUMNS = """
    candidate_id,
    tenant_id,
    claim,
    confidence,
    proposer_id,
    source_memory_ids,
    duplicate_memory_ids,
    conflicting_memory_ids,
    status,
    created_at,
    reviewed_by,
    review_rationale
"""

_ERASURE_COLUMNS = """
    request_id,
    tenant_id,
    memory_id,
    requester_id,
    owner_principal_id,
    reason,
    status,
    created_at,
    reviewed_by,
    review_rationale,
    completed_at
"""

_MEMORY_COMMAND_COLUMNS = """
    command_id,
    tenant_id,
    actor_id,
    owner_principal_id,
    command_type,
    idempotency_key,
    target_memory_id,
    result_memory_id,
    result_item,
    erasure_request_id,
    state,
    created_at
"""


class PostgresGovernanceStore:
    """One Tenant Operations Store implementing governance and erasure contracts."""

    def __init__(
        self,
        database_url: str,
    ) -> None:
        if not database_url.strip():
            raise ValueError("Tenant database URL cannot be empty")
        self._database_url = _psycopg_database_url(database_url)

    async def accept_retain(
        self,
        tenant_id: str,
        actor_id: str,
        owner_principal_id: str,
        command: RetainMemory,
    ) -> MemoryItem:
        item = MemoryItem(
            id=str(uuid4()),
            owner_principal_id=owner_principal_id,
            scope=MemoryScope.PRIVATE,
            content=command.content,
            kind=command.kind,
            confidence=command.confidence,
            provenance=Provenance(actor_id=actor_id, source="explicit"),
            created_at=datetime.now(UTC),
        )
        async with await psycopg.AsyncConnection.connect(self._database_url) as connection:
            async with connection.cursor() as cursor:
                await acquire_backup_shared_lock_async(cursor)
                existing = await _existing_memory_command(
                    cursor,
                    tenant_id,
                    actor_id,
                    PrivateMemoryCommandType.RETAIN,
                    command.idempotency_key,
                )
                if existing is not None:
                    return _required_result_item(existing)
                await _insert_memory_command(
                    cursor,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    owner_principal_id=owner_principal_id,
                    command_type=PrivateMemoryCommandType.RETAIN,
                    idempotency_key=command.idempotency_key,
                    target_memory_id=None,
                    result_item=item,
                    payload=command.model_dump(mode="json"),
                )
        return item

    async def accept_correction(
        self,
        tenant_id: str,
        actor_id: str,
        owner_principal_id: str,
        command: CorrectMemory,
    ) -> MemoryItem:
        async with await psycopg.AsyncConnection.connect(self._database_url) as connection:
            async with connection.cursor() as cursor:
                await acquire_backup_shared_lock_async(cursor)
                existing = await _existing_memory_command(
                    cursor,
                    tenant_id,
                    actor_id,
                    PrivateMemoryCommandType.CORRECT,
                    command.idempotency_key,
                )
                if existing is not None:
                    return _required_result_item(existing)
                await cursor.execute(
                    """
                    SELECT owner_principal_id, state, item
                    FROM memory.private_memory_items
                    WHERE tenant_id = %s AND memory_id = %s
                    FOR UPDATE
                    """,
                    (tenant_id, command.memory_id),
                )
                original_row = await cursor.fetchone()
                if (
                    original_row is None
                    or str(original_row[0]) != owner_principal_id
                    or str(original_row[1]) != MemoryState.ACTIVE.value
                ):
                    raise LookupError("Owned active Memory Item not found")
                original = MemoryItem.model_validate(original_row[2])
                corrected = MemoryItem(
                    id=str(uuid4()),
                    owner_principal_id=owner_principal_id,
                    scope=MemoryScope.PRIVATE,
                    content=command.replacement_content,
                    kind=command.kind,
                    confidence=command.confidence,
                    provenance=Provenance(actor_id=actor_id, source="correction"),
                    created_at=datetime.now(UTC),
                    supersedes_id=command.memory_id,
                )
                await _insert_memory_command(
                    cursor,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    owner_principal_id=owner_principal_id,
                    command_type=PrivateMemoryCommandType.CORRECT,
                    idempotency_key=command.idempotency_key,
                    target_memory_id=command.memory_id,
                    result_item=corrected,
                    payload=command.model_dump(mode="json"),
                )
                await cursor.execute(
                    """
                    UPDATE memory.private_memory_items
                    SET state = 'superseded', item = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND memory_id = %s
                    """,
                    (
                        Jsonb(
                            original.model_copy(
                                update={"state": MemoryState.SUPERSEDED}
                            ).model_dump(mode="json")
                        ),
                        tenant_id,
                        command.memory_id,
                    ),
                )
        return corrected

    async def accept_import(
        self,
        tenant_id: str,
        actor_id: str,
        owner_principal_id: str,
        item: MemoryItem,
    ) -> ImportAcceptance:
        imported = item.model_copy(
            update={
                "owner_principal_id": owner_principal_id,
                "scope": MemoryScope.PRIVATE,
            }
        )
        async with await psycopg.AsyncConnection.connect(self._database_url) as connection:
            async with connection.cursor() as cursor:
                await acquire_backup_shared_lock_async(cursor)
                await _idempotency_lock(cursor, "memory-id", tenant_id, "global", imported.id)
                await cursor.execute(
                    """
                    SELECT owner_principal_id, state, item
                    FROM memory.private_memory_items
                    WHERE tenant_id = %s AND memory_id = %s
                    FOR UPDATE
                    """,
                    (tenant_id, imported.id),
                )
                existing_item = await cursor.fetchone()
                if existing_item is not None:
                    if str(existing_item[0]) != owner_principal_id:
                        raise ValueError("Memory Item identifier belongs to another Principal")
                    if existing_item[2] is None:
                        raise ValueError("An erased Memory Item identifier cannot be restored")
                    return ImportAcceptance(
                        item=MemoryItem.model_validate(existing_item[2]),
                        accepted=False,
                    )
                await _insert_memory_command(
                    cursor,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    owner_principal_id=owner_principal_id,
                    command_type=PrivateMemoryCommandType.IMPORT,
                    idempotency_key=imported.id,
                    target_memory_id=imported.supersedes_id,
                    result_item=imported,
                    payload={"archive_memory_id": imported.id},
                )
                if imported.supersedes_id is not None:
                    await cursor.execute(
                        """
                        UPDATE memory.private_memory_items
                        SET state = 'superseded',
                            item = jsonb_set(item, '{state}', '"superseded"'::jsonb),
                            updated_at = CURRENT_TIMESTAMP
                        WHERE tenant_id = %s AND memory_id = %s AND state = 'active'
                        """,
                        (tenant_id, imported.supersedes_id),
                    )
        return ImportAcceptance(item=imported, accepted=True)

    async def get_memory_command(
        self,
        tenant_id: str,
        command_id: str,
    ) -> PrivateMemoryCommand | None:
        row = await self._fetch_one(
            f"""
            SELECT {_MEMORY_COMMAND_COLUMNS}
            FROM memory.private_memory_commands
            WHERE tenant_id = %s AND command_id = %s
            """,
            (tenant_id, command_id),
        )
        return None if row is None else _decode_memory_command(row)

    async def get_memory_command_by_result(
        self,
        tenant_id: str,
        result_memory_id: str,
    ) -> PrivateMemoryCommand | None:
        row = await self._fetch_one(
            f"""
            SELECT {_MEMORY_COMMAND_COLUMNS}
            FROM memory.private_memory_commands
            WHERE tenant_id = %s AND result_memory_id = %s
            """,
            (tenant_id, result_memory_id),
        )
        return None if row is None else _decode_memory_command(row)

    async def mark_memory_command_applied(
        self,
        tenant_id: str,
        command_id: str,
    ) -> PrivateMemoryCommand:
        async with await psycopg.AsyncConnection.connect(self._database_url) as connection:
            async with connection.cursor() as cursor:
                await acquire_backup_shared_lock_async(cursor)
                await cursor.execute(
                    f"""
                    SELECT {_MEMORY_COMMAND_COLUMNS}
                    FROM memory.private_memory_commands
                    WHERE tenant_id = %s AND command_id = %s
                    FOR UPDATE
                    """,
                    (tenant_id, command_id),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise LookupError("Private Memory Command not found")
                current = _decode_memory_command(row)
                if current.state is PrivateMemoryCommandState.APPLIED:
                    return current
                if current.state is not PrivateMemoryCommandState.ACCEPTED:
                    raise ValueError("Private Memory Command is not awaiting projection")
                if current.command_type is PrivateMemoryCommandType.ERASE:
                    assert current.target_memory_id is not None
                    await _complete_erasure_command(cursor, current)
                await cursor.execute(
                    f"""
                    UPDATE memory.private_memory_commands
                    SET state = 'applied', applied_at = CURRENT_TIMESTAMP, last_error_code = NULL
                    WHERE tenant_id = %s AND command_id = %s
                    RETURNING {_MEMORY_COMMAND_COLUMNS}
                    """,
                    (tenant_id, command_id),
                )
                applied_row = _required_row(await cursor.fetchone())
                await cursor.execute(
                    """
                    UPDATE memory.outbox
                    SET processed_at = CURRENT_TIMESTAMP, locked_at = NULL, lock_id = NULL
                    WHERE tenant_id = %s
                      AND aggregate_id = %s
                      AND event_type = 'memory.command.accepted'
                    """,
                    (tenant_id, command_id),
                )
                await _write_audit(
                    cursor,
                    tenant_id=tenant_id,
                    actor_id="memory-projection-worker",
                    event_type=f"memory.{current.command_type.value}.applied",
                    target_type="private_memory_command",
                    target_id=command_id,
                    outcome="applied",
                )
        return _decode_memory_command(applied_row)

    async def list_private_memory_state(
        self,
        tenant_id: str,
        owner_principal_id: str,
    ) -> tuple[PrivateMemoryInspection, ...]:
        rows = await self._fetch_all(
            """
            SELECT i.memory_id, i.owner_principal_id, i.state, i.item,
                   i.source_command_id, i.created_at, c.state
            FROM memory.private_memory_items AS i
            JOIN memory.private_memory_commands AS c
              ON c.command_id = i.source_command_id
            WHERE i.tenant_id = %s AND i.owner_principal_id = %s
            ORDER BY i.created_at, i.memory_id
            """,
            (tenant_id, owner_principal_id),
        )
        inspections: list[PrivateMemoryInspection] = []
        for row in rows:
            item = None if row[3] is None else MemoryItem.model_validate(row[3])
            inspections.append(
                PrivateMemoryInspection(
                    id=str(row[0]),
                    owner_principal_id=str(row[1]),
                    state=MemoryState(str(row[2])),
                    operation_id=str(row[4]),
                    mutation_state=MutationState(str(row[6])),
                    content=None if item is None else item.content,
                    kind=None if item is None else item.kind,
                    confidence=None if item is None else item.confidence,
                    created_at=_datetime(row[5], "Private Memory creation time"),
                    supersedes_id=None if item is None else item.supersedes_id,
                )
            )
        return tuple(inspections)

    async def list_private_memory_items(
        self,
        tenant_id: str,
        owner_principal_id: str,
    ) -> tuple[MemoryItem, ...]:
        rows = await self._fetch_all(
            """
            SELECT item
            FROM memory.private_memory_items
            WHERE tenant_id = %s
              AND owner_principal_id = %s
              AND item IS NOT NULL
            ORDER BY created_at, memory_id
            """,
            (tenant_id, owner_principal_id),
        )
        return tuple(MemoryItem.model_validate(row[0]) for row in rows)

    async def propose(
        self,
        tenant_id: str,
        proposer_id: str,
        command: ProposeKnowledge,
    ) -> KnowledgeCandidate:
        candidate_id = str(uuid4())
        async with await psycopg.AsyncConnection.connect(self._database_url) as connection:
            async with connection.cursor() as cursor:
                await acquire_backup_shared_lock_async(cursor)
                await cursor.execute(
                    f"""
                    INSERT INTO memory.knowledge_candidates (
                        candidate_id,
                        tenant_id,
                        claim,
                        confidence,
                        proposer_id,
                        source_memory_ids,
                        duplicate_memory_ids,
                        conflicting_memory_ids,
                        status,
                        proposal_idempotency_key
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'submitted', %s)
                    ON CONFLICT (tenant_id, proposer_id, proposal_idempotency_key)
                    DO UPDATE SET proposal_idempotency_key = EXCLUDED.proposal_idempotency_key
                    RETURNING {_CANDIDATE_COLUMNS}, (xmax = 0) AS inserted
                    """,
                    (
                        candidate_id,
                        tenant_id,
                        command.claim,
                        command.confidence,
                        proposer_id,
                        Jsonb(list(command.source_memory_ids)),
                        Jsonb(list(command.duplicate_memory_ids)),
                        Jsonb(list(command.conflicting_memory_ids)),
                        command.idempotency_key,
                    ),
                )
                row = _required_row(await cursor.fetchone())
                if bool(row[12]):
                    await _write_audit(
                        cursor,
                        tenant_id=tenant_id,
                        actor_id=proposer_id,
                        event_type="knowledge.proposed",
                        target_type="knowledge_candidate",
                        target_id=str(row[0]),
                        outcome="submitted",
                    )
        return _decode_candidate(row)

    async def get_candidate(
        self,
        tenant_id: str,
        candidate_id: str,
    ) -> KnowledgeCandidate | None:
        row = await self._fetch_one(
            f"""
            SELECT {_CANDIDATE_COLUMNS}
            FROM memory.knowledge_candidates
            WHERE tenant_id = %s AND candidate_id = %s
            """,
            (tenant_id, candidate_id),
        )
        return None if row is None else _decode_candidate(row)

    async def list_candidates(self, tenant_id: str) -> tuple[KnowledgeCandidate, ...]:
        rows = await self._fetch_all(
            f"""
            SELECT {_CANDIDATE_COLUMNS}
            FROM memory.knowledge_candidates
            WHERE tenant_id = %s
            ORDER BY created_at, candidate_id
            """,
            (tenant_id,),
        )
        return tuple(_decode_candidate(row) for row in rows)

    @overload
    async def review(
        self,
        tenant_id: str,
        reviewer_id: str,
        command: ReviewKnowledge,
    ) -> KnowledgeCandidate: ...

    @overload
    async def review(
        self,
        tenant_id: str,
        reviewer_id: str,
        command: ReviewErasure,
    ) -> ErasureRequest: ...

    async def review(
        self,
        tenant_id: str,
        reviewer_id: str,
        command: ReviewKnowledge | ReviewErasure,
    ) -> KnowledgeCandidate | ErasureRequest:
        if isinstance(command, ReviewErasure):
            return await self._review_erasure(tenant_id, reviewer_id, command)
        return await self._review_knowledge(tenant_id, reviewer_id, command)

    async def _review_knowledge(
        self,
        tenant_id: str,
        reviewer_id: str,
        command: ReviewKnowledge,
    ) -> KnowledgeCandidate:
        async with await psycopg.AsyncConnection.connect(self._database_url) as connection:
            async with connection.cursor() as cursor:
                await acquire_backup_shared_lock_async(cursor)
                await _idempotency_lock(
                    cursor,
                    "knowledge-review",
                    tenant_id,
                    reviewer_id,
                    command.idempotency_key,
                )
                await cursor.execute(
                    f"""
                    SELECT c.{_qualified_candidate_columns("c")}
                    FROM memory.knowledge_reviews AS r
                    JOIN memory.knowledge_candidates AS c
                      ON c.candidate_id = r.candidate_id
                    WHERE r.tenant_id = %s
                      AND r.reviewer_id = %s
                      AND r.idempotency_key = %s
                    """,
                    (tenant_id, reviewer_id, command.idempotency_key),
                )
                existing = await cursor.fetchone()
                if existing is not None:
                    return _decode_candidate(existing)

                await cursor.execute(
                    f"""
                    SELECT {_CANDIDATE_COLUMNS}
                    FROM memory.knowledge_candidates
                    WHERE tenant_id = %s AND candidate_id = %s
                    FOR UPDATE
                    """,
                    (tenant_id, command.candidate_id),
                )
                candidate_row = await cursor.fetchone()
                if candidate_row is None:
                    raise LookupError("Knowledge Candidate not found")
                candidate = _decode_candidate(candidate_row)
                if candidate.status is not CandidateStatus.SUBMITTED:
                    raise ValueError("Knowledge Candidate is not awaiting review")

                status = (
                    CandidateStatus.PUBLISHING
                    if command.decision is ReviewDecision.APPROVE
                    else CandidateStatus.REJECTED
                )
                await cursor.execute(
                    """
                    INSERT INTO memory.knowledge_reviews (
                        review_id,
                        tenant_id,
                        candidate_id,
                        reviewer_id,
                        decision,
                        rationale,
                        idempotency_key
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        str(uuid4()),
                        tenant_id,
                        candidate.id,
                        reviewer_id,
                        command.decision.value,
                        command.rationale,
                        command.idempotency_key,
                    ),
                )
                await cursor.execute(
                    f"""
                    UPDATE memory.knowledge_candidates
                    SET status = %s,
                        reviewed_by = %s,
                        review_rationale = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE candidate_id = %s
                    RETURNING {_CANDIDATE_COLUMNS}
                    """,
                    (status.value, reviewer_id, command.rationale, candidate.id),
                )
                reviewed_row = _required_row(await cursor.fetchone())
                await _write_audit(
                    cursor,
                    tenant_id=tenant_id,
                    actor_id=reviewer_id,
                    event_type="knowledge.reviewed",
                    target_type="knowledge_candidate",
                    target_id=candidate.id,
                    outcome=status.value,
                )
                if status is CandidateStatus.PUBLISHING:
                    event_id = str(uuid4())
                    await cursor.execute(
                        """
                        INSERT INTO memory.outbox (
                            event_id,
                            tenant_id,
                            aggregate_type,
                            aggregate_id,
                            event_type,
                            payload
                        )
                        VALUES (
                            %s,
                            %s,
                            'knowledge_candidate',
                            %s,
                            'knowledge.approved',
                            %s
                        )
                        """,
                        (
                            event_id,
                            tenant_id,
                            candidate.id,
                            Jsonb(
                                {
                                    "candidate_id": candidate.id,
                                    "tenant_id": tenant_id,
                                    "version": 1,
                                }
                            ),
                        ),
                    )
        return _decode_candidate(reviewed_row)

    async def next_publication(self, tenant_id: str) -> PublicationEvent | None:
        row = await self._fetch_one(
            """
            SELECT event_id, tenant_id, aggregate_id, created_at
            FROM memory.outbox
            WHERE tenant_id = %s
              AND event_type = 'knowledge.approved'
              AND processed_at IS NULL
            ORDER BY created_at, event_id
            LIMIT 1
            """,
            (tenant_id,),
        )
        if row is None:
            return None
        return PublicationEvent(
            id=str(row[0]),
            tenant_id=str(row[1]),
            candidate_id=str(row[2]),
            created_at=_datetime(row[3], "publication creation time"),
        )

    async def get_publication(
        self,
        event_id: str,
        tenant_id: str,
        candidate_id: str,
    ) -> PublicationEvent | None:
        row = await self._fetch_one(
            """
            SELECT event_id, tenant_id, aggregate_id, created_at
            FROM memory.outbox
            WHERE event_id = %s
              AND tenant_id = %s
              AND aggregate_id = %s
              AND event_type = 'knowledge.approved'
            """,
            (event_id, tenant_id, candidate_id),
        )
        if row is None:
            return None
        return PublicationEvent(
            id=str(row[0]),
            tenant_id=str(row[1]),
            candidate_id=str(row[2]),
            created_at=_datetime(row[3], "publication creation time"),
        )

    async def mark_published(self, tenant_id: str, candidate_id: str) -> KnowledgeCandidate:
        async with await psycopg.AsyncConnection.connect(self._database_url) as connection:
            async with connection.cursor() as cursor:
                await acquire_backup_shared_lock_async(cursor)
                await cursor.execute(
                    f"""
                    UPDATE memory.knowledge_candidates
                    SET status = 'published', updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s
                      AND candidate_id = %s
                      AND status = 'publishing'
                    RETURNING {_CANDIDATE_COLUMNS}
                    """,
                    (tenant_id, candidate_id),
                )
                row = await cursor.fetchone()
                if row is None:
                    await cursor.execute(
                        f"""
                        SELECT {_CANDIDATE_COLUMNS}
                        FROM memory.knowledge_candidates
                        WHERE tenant_id = %s AND candidate_id = %s
                        """,
                        (tenant_id, candidate_id),
                    )
                    current_row = await cursor.fetchone()
                    if current_row is None:
                        raise LookupError("Knowledge Candidate not found")
                    current = _decode_candidate(current_row)
                    if current.status is CandidateStatus.PUBLISHED:
                        return current
                    raise ValueError("Knowledge Candidate is not awaiting publication")
                await cursor.execute(
                    """
                    UPDATE memory.outbox
                    SET processed_at = CURRENT_TIMESTAMP,
                        locked_at = NULL,
                        lock_id = NULL
                    WHERE tenant_id = %s
                      AND aggregate_id = %s
                      AND event_type = 'knowledge.approved'
                    """,
                    (tenant_id, candidate_id),
                )
                await _write_audit(
                    cursor,
                    tenant_id=tenant_id,
                    actor_id="publication-worker",
                    event_type="knowledge.published",
                    target_type="knowledge_candidate",
                    target_id=candidate_id,
                    outcome="published",
                )
        return _decode_candidate(row)

    async def import_candidate(
        self,
        tenant_id: str,
        proposer_id: str,
        candidate_id: str,
        claim: str,
        confidence: float,
    ) -> KnowledgeCandidate:
        async with await psycopg.AsyncConnection.connect(self._database_url) as connection:
            async with connection.cursor() as cursor:
                await acquire_backup_shared_lock_async(cursor)
                await cursor.execute(
                    f"""
                    INSERT INTO memory.knowledge_candidates (
                        candidate_id,
                        tenant_id,
                        claim,
                        confidence,
                        proposer_id,
                        source_memory_ids,
                        status
                    )
                    VALUES (%s, %s, %s, %s, %s, '[]'::jsonb, 'submitted')
                    ON CONFLICT (candidate_id) DO UPDATE
                    SET candidate_id = EXCLUDED.candidate_id
                    RETURNING {_CANDIDATE_COLUMNS}, (xmax = 0) AS inserted
                    """,
                    (candidate_id, tenant_id, claim, confidence, proposer_id),
                )
                row = _required_row(await cursor.fetchone())
                candidate = _decode_candidate(row)
                if candidate.tenant_id != tenant_id:
                    raise ValueError("Knowledge Candidate identifier belongs to another Tenant")
                if bool(row[12]):
                    await _write_audit(
                        cursor,
                        tenant_id=tenant_id,
                        actor_id=proposer_id,
                        event_type="knowledge.imported",
                        target_type="knowledge_candidate",
                        target_id=candidate_id,
                        outcome="submitted",
                    )
        return candidate

    async def request(
        self,
        tenant_id: str,
        requester_id: str,
        owner_principal_id: str,
        command: RequestErasure,
    ) -> ErasureRequest:
        request_id = str(uuid4())
        async with await psycopg.AsyncConnection.connect(self._database_url) as connection:
            async with connection.cursor() as cursor:
                await acquire_backup_shared_lock_async(cursor)
                await cursor.execute(
                    f"""
                    INSERT INTO memory.erasure_requests (
                        request_id,
                        tenant_id,
                        memory_id,
                        requester_id,
                        owner_principal_id,
                        reason,
                        status,
                        request_idempotency_key
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, 'requested', %s)
                    ON CONFLICT (tenant_id, requester_id, request_idempotency_key)
                    DO UPDATE
                    SET request_idempotency_key = EXCLUDED.request_idempotency_key
                    RETURNING {_ERASURE_COLUMNS}, (xmax = 0) AS inserted
                    """,
                    (
                        request_id,
                        tenant_id,
                        command.memory_id,
                        requester_id,
                        owner_principal_id,
                        command.reason,
                        command.idempotency_key,
                    ),
                )
                row = _required_row(await cursor.fetchone())
                if bool(row[11]):
                    await _write_audit(
                        cursor,
                        tenant_id=tenant_id,
                        actor_id=requester_id,
                        event_type="erasure.requested",
                        target_type="erasure_request",
                        target_id=str(row[0]),
                        outcome="requested",
                    )
        return _decode_erasure(row)

    async def get_request(self, tenant_id: str, request_id: str) -> ErasureRequest | None:
        row = await self._fetch_one(
            f"""
            SELECT {_ERASURE_COLUMNS}
            FROM memory.erasure_requests
            WHERE tenant_id = %s AND request_id = %s
            """,
            (tenant_id, request_id),
        )
        return None if row is None else _decode_erasure(row)

    async def _review_erasure(
        self,
        tenant_id: str,
        reviewer_id: str,
        command: ReviewErasure,
    ) -> ErasureRequest:
        async with await psycopg.AsyncConnection.connect(self._database_url) as connection:
            async with connection.cursor() as cursor:
                await acquire_backup_shared_lock_async(cursor)
                await _idempotency_lock(
                    cursor,
                    "erasure-review",
                    tenant_id,
                    reviewer_id,
                    command.idempotency_key,
                )
                await cursor.execute(
                    f"""
                    SELECT r.{_qualified_erasure_columns("r")}
                    FROM memory.erasure_reviews AS v
                    JOIN memory.erasure_requests AS r ON r.request_id = v.request_id
                    WHERE v.tenant_id = %s
                      AND v.reviewer_id = %s
                      AND v.idempotency_key = %s
                    """,
                    (tenant_id, reviewer_id, command.idempotency_key),
                )
                existing = await cursor.fetchone()
                if existing is not None:
                    return _decode_erasure(existing)

                await cursor.execute(
                    f"""
                    SELECT {_ERASURE_COLUMNS}
                    FROM memory.erasure_requests
                    WHERE tenant_id = %s AND request_id = %s
                    FOR UPDATE
                    """,
                    (tenant_id, command.request_id),
                )
                request_row = await cursor.fetchone()
                if request_row is None:
                    raise LookupError("Erasure Request not found")
                request = _decode_erasure(request_row)
                if request.status is not ErasureStatus.REQUESTED:
                    raise ValueError("Erasure Request is not awaiting review")
                status = (
                    ErasureStatus.APPROVED
                    if command.decision is ErasureDecision.APPROVE
                    else ErasureStatus.REJECTED
                )
                await cursor.execute(
                    """
                    INSERT INTO memory.erasure_reviews (
                        review_id,
                        tenant_id,
                        request_id,
                        reviewer_id,
                        decision,
                        rationale,
                        idempotency_key
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        str(uuid4()),
                        tenant_id,
                        request.id,
                        reviewer_id,
                        command.decision.value,
                        command.rationale,
                        command.idempotency_key,
                    ),
                )
                await cursor.execute(
                    f"""
                    UPDATE memory.erasure_requests
                    SET status = %s,
                        reviewed_by = %s,
                        review_rationale = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE request_id = %s
                    RETURNING {_ERASURE_COLUMNS}
                    """,
                    (status.value, reviewer_id, command.rationale, request.id),
                )
                reviewed_row = _required_row(await cursor.fetchone())
                await _write_audit(
                    cursor,
                    tenant_id=tenant_id,
                    actor_id=reviewer_id,
                    event_type="erasure.reviewed",
                    target_type="erasure_request",
                    target_id=request.id,
                    outcome=status.value,
                )
                if status is ErasureStatus.APPROVED:
                    await cursor.execute(
                        """
                        SELECT owner_principal_id, state
                        FROM memory.private_memory_items
                        WHERE tenant_id = %s AND memory_id = %s
                        FOR UPDATE
                        """,
                        (tenant_id, request.memory_id),
                    )
                    item_row = await cursor.fetchone()
                    # A missing row is a pre-ledger record and remains on the
                    # compatibility polling path. New writes always enqueue here.
                    if item_row is not None:
                        if str(item_row[0]) != request.owner_principal_id:
                            raise ValueError("Erasure target belongs to another Principal")
                        if str(item_row[1]) == "erased":
                            raise ValueError("Erasure target is already erased")
                        await _insert_erasure_memory_command(
                            cursor,
                            tenant_id=tenant_id,
                            actor_id=reviewer_id,
                            request=request,
                        )
        return _decode_erasure(reviewed_row)

    async def next_approved(self, tenant_id: str) -> ErasureRequest | None:
        row = await self._fetch_one(
            f"""
            SELECT {_ERASURE_COLUMNS}
            FROM memory.erasure_requests AS r
            WHERE r.tenant_id = %s AND r.status = 'approved'
              AND NOT EXISTS (
                  SELECT 1
                  FROM memory.private_memory_commands AS c
                  WHERE c.erasure_request_id = r.request_id
              )
            ORDER BY created_at, request_id
            LIMIT 1
            """,
            (tenant_id,),
        )
        return None if row is None else _decode_erasure(row)

    async def mark_completed(self, tenant_id: str, request_id: str) -> ErasureRequest:
        async with await psycopg.AsyncConnection.connect(self._database_url) as connection:
            async with connection.cursor() as cursor:
                await acquire_backup_shared_lock_async(cursor)
                await cursor.execute(
                    f"""
                    UPDATE memory.erasure_requests
                    SET status = 'completed',
                        reason = NULL,
                        review_rationale = NULL,
                        completed_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND request_id = %s AND status = 'approved'
                    RETURNING {_ERASURE_COLUMNS}
                    """,
                    (tenant_id, request_id),
                )
                row = await cursor.fetchone()
                if row is None:
                    await cursor.execute(
                        f"""
                        SELECT {_ERASURE_COLUMNS}
                        FROM memory.erasure_requests
                        WHERE tenant_id = %s AND request_id = %s
                        """,
                        (tenant_id, request_id),
                    )
                    current_row = await cursor.fetchone()
                    if current_row is None:
                        raise LookupError("Erasure Request not found")
                    current = _decode_erasure(current_row)
                    if current.status is ErasureStatus.COMPLETED:
                        await cursor.execute(
                            """
                            UPDATE memory.erasure_requests
                            SET reason = NULL,
                                review_rationale = NULL,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE tenant_id = %s AND request_id = %s
                            """,
                            (tenant_id, request_id),
                        )
                        await cursor.execute(
                            """
                            UPDATE memory.erasure_reviews
                            SET rationale = NULL
                            WHERE tenant_id = %s AND request_id = %s
                            """,
                            (tenant_id, request_id),
                        )
                        return current.model_copy(update={"reason": None, "review_rationale": None})
                    raise ValueError("Erasure Request is not approved")
                await cursor.execute(
                    """
                    UPDATE memory.erasure_reviews
                    SET rationale = NULL
                    WHERE tenant_id = %s AND request_id = %s
                    """,
                    (tenant_id, request_id),
                )
                await _write_audit(
                    cursor,
                    tenant_id=tenant_id,
                    actor_id="erasure-worker",
                    event_type="erasure.completed",
                    target_type="erasure_request",
                    target_id=request_id,
                    outcome="completed",
                )
        return _decode_erasure(row)

    async def list_completed(
        self, tenant_id: str, owner_principal_id: str
    ) -> tuple[ErasureTombstone, ...]:
        rows = await self._fetch_all(
            f"""
            SELECT {_ERASURE_COLUMNS}
            FROM memory.erasure_requests
            WHERE tenant_id = %s
              AND owner_principal_id = %s
              AND status = 'completed'
            ORDER BY created_at, request_id
            """,
            (tenant_id, owner_principal_id),
        )
        return tuple(erasure_tombstone(_decode_erasure(row)) for row in rows)

    async def import_tombstone(
        self,
        tenant_id: str,
        owner_principal_id: str,
        tombstone: ErasureTombstone,
    ) -> bool:
        rebound = tombstone.model_copy(update={"owner_principal_id": owner_principal_id})
        async with await psycopg.AsyncConnection.connect(self._database_url) as connection:
            async with connection.cursor() as cursor:
                await acquire_backup_shared_lock_async(cursor)
                await _idempotency_lock(
                    cursor,
                    "erasure-tombstone",
                    tenant_id,
                    "global",
                    tombstone.id,
                )
                await cursor.execute(
                    f"""
                    SELECT {_ERASURE_COLUMNS}
                    FROM memory.erasure_requests
                    WHERE request_id = %s
                    FOR UPDATE
                    """,
                    (tombstone.id,),
                )
                existing_row = await cursor.fetchone()
                if existing_row is not None:
                    existing = _decode_erasure(existing_row)
                    if existing.tenant_id != tenant_id or erasure_tombstone(existing) != rebound:
                        raise ValueError("Erasure Tombstone identifier already has different data")
                    return False
                await cursor.execute(
                    """
                    SELECT state
                    FROM memory.private_memory_items
                    WHERE tenant_id = %s AND memory_id = %s
                    FOR UPDATE
                    """,
                    (tenant_id, tombstone.memory_id),
                )
                memory_row = await cursor.fetchone()
                if memory_row is not None and str(memory_row[0]) != "erased":
                    raise ValueError("Erasure Tombstone conflicts with an active Memory Item")
                archive_key = hashlib.sha256(tombstone.id.encode("utf-8")).hexdigest()
                await cursor.execute(
                    """
                    INSERT INTO memory.erasure_requests (
                        request_id,
                        tenant_id,
                        memory_id,
                        requester_id,
                        owner_principal_id,
                        reason,
                        status,
                        request_idempotency_key,
                        created_at,
                        reviewed_by,
                        review_rationale,
                        completed_at,
                        updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, NULL, 'completed', %s, %s, %s, NULL, %s, %s)
                    """,
                    (
                        tombstone.id,
                        tenant_id,
                        tombstone.memory_id,
                        tombstone.requester_id,
                        owner_principal_id,
                        f"archive:{archive_key}",
                        tombstone.created_at,
                        tombstone.reviewed_by,
                        tombstone.completed_at,
                        tombstone.completed_at,
                    ),
                )
                await cursor.execute(
                    """
                    INSERT INTO memory.erasure_reviews (
                        review_id,
                        tenant_id,
                        request_id,
                        reviewer_id,
                        decision,
                        rationale,
                        idempotency_key,
                        created_at
                    )
                    VALUES (%s, %s, %s, %s, 'approve', NULL, %s, %s)
                    """,
                    (
                        str(uuid4()),
                        tenant_id,
                        tombstone.id,
                        tombstone.reviewed_by,
                        f"archive:{archive_key}",
                        tombstone.completed_at,
                    ),
                )
                await _write_audit(
                    cursor,
                    tenant_id=tenant_id,
                    actor_id=owner_principal_id,
                    event_type="erasure.tombstone_imported",
                    target_type="erasure_request",
                    target_id=tombstone.id,
                    outcome="completed",
                )
        return True

    async def claim_outbox(self, *, lease_seconds: int = 60) -> OutboxRecord | None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        lock_id = str(uuid4())
        row = await self._fetch_one_mutating(
            """
            WITH next_event AS (
                SELECT event_id
                FROM memory.outbox
                WHERE dispatched_at IS NULL
                  AND processed_at IS NULL
                  AND available_at <= CURRENT_TIMESTAMP
                  AND (
                      locked_at IS NULL
                      OR locked_at < CURRENT_TIMESTAMP - make_interval(secs => %s)
                  )
                ORDER BY created_at, event_id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE memory.outbox AS o
            SET locked_at = CURRENT_TIMESTAMP, lock_id = %s
            FROM next_event
            WHERE o.event_id = next_event.event_id
            RETURNING
                o.event_id,
                o.tenant_id,
                o.aggregate_type,
                o.aggregate_id,
                o.event_type,
                o.created_at,
                o.lock_id
            """,
            (lease_seconds, lock_id),
        )
        if row is None:
            return None
        return OutboxRecord(
            event_id=str(row[0]),
            tenant_id=str(row[1]),
            aggregate_type=str(row[2]),
            aggregate_id=str(row[3]),
            event_type=str(row[4]),
            created_at=_datetime(row[5], "outbox creation time"),
            lock_id=str(row[6]),
        )

    async def mark_outbox_dispatched(self, event_id: str, lock_id: str) -> None:
        changed = await self._execute_returning(
            """
            UPDATE memory.outbox
            SET dispatched_at = CURRENT_TIMESTAMP,
                locked_at = NULL,
                lock_id = NULL,
                last_error_code = NULL
            WHERE event_id = %s AND lock_id = %s AND dispatched_at IS NULL
            RETURNING event_id
            """,
            (event_id, lock_id),
        )
        if not changed:
            raise LookupError("Claimed outbox event not found")

    async def release_outbox(self, event_id: str, lock_id: str, error_code: str) -> None:
        changed = await self._execute_returning(
            """
            UPDATE memory.outbox
            SET locked_at = NULL,
                lock_id = NULL,
                attempt_count = attempt_count + 1,
                last_error_code = %s,
                available_at = CURRENT_TIMESTAMP + interval '5 seconds'
            WHERE event_id = %s AND lock_id = %s AND dispatched_at IS NULL
            RETURNING event_id
            """,
            (error_code[:128], event_id, lock_id),
        )
        if not changed:
            raise LookupError("Claimed outbox event not found")

    async def schedule_outbox_redelivery(
        self,
        tenant_id: str,
        event_id: str,
        operator_id: str,
    ) -> None:
        """Reopen one unprocessed event and audit the exact operator action atomically."""
        if not operator_id.strip():
            raise ValueError("Operator ID cannot be empty")
        async with await psycopg.AsyncConnection.connect(self._database_url) as connection:
            async with connection.cursor() as cursor:
                await acquire_backup_shared_lock_async(cursor)
                await cursor.execute(
                    """
                    UPDATE memory.outbox
                    SET dispatched_at = NULL,
                        locked_at = NULL,
                        lock_id = NULL,
                        available_at = CURRENT_TIMESTAMP,
                        attempt_count = attempt_count + 1,
                        last_error_code = 'OperatorRedrive'
                    WHERE event_id = %s
                      AND tenant_id = %s
                      AND processed_at IS NULL
                    RETURNING aggregate_type, aggregate_id
                    """,
                    (event_id, tenant_id),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise LookupError("Authoritative unprocessed outbox event not found")
                await _write_audit(
                    cursor,
                    tenant_id=tenant_id,
                    actor_id=operator_id,
                    event_type="outbox.redrive_scheduled",
                    target_type=str(row[0]),
                    target_id=str(row[1]),
                    outcome="scheduled",
                )

    async def _fetch_one(
        self,
        statement: str,
        parameters: Sequence[object],
    ) -> tuple[Any, ...] | None:
        async with await psycopg.AsyncConnection.connect(self._database_url) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(statement, parameters)
                return await cursor.fetchone()

    async def _fetch_all(
        self,
        statement: str,
        parameters: Sequence[object],
    ) -> list[tuple[Any, ...]]:
        async with await psycopg.AsyncConnection.connect(self._database_url) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(statement, parameters)
                return list(await cursor.fetchall())

    async def _fetch_one_mutating(
        self,
        statement: str,
        parameters: Sequence[object],
    ) -> tuple[Any, ...] | None:
        async with await psycopg.AsyncConnection.connect(self._database_url) as connection:
            async with connection.cursor() as cursor:
                await acquire_backup_shared_lock_async(cursor)
                await cursor.execute(statement, parameters)
                return await cursor.fetchone()

    async def _execute_returning(
        self,
        statement: str,
        parameters: Sequence[object],
    ) -> bool:
        async with await psycopg.AsyncConnection.connect(self._database_url) as connection:
            async with connection.cursor() as cursor:
                await acquire_backup_shared_lock_async(cursor)
                await cursor.execute(statement, parameters)
                return await cursor.fetchone() is not None


async def _existing_memory_command(
    cursor: psycopg.AsyncCursor[tuple[Any, ...]],
    tenant_id: str,
    actor_id: str,
    command_type: PrivateMemoryCommandType,
    idempotency_key: str,
) -> PrivateMemoryCommand | None:
    await _idempotency_lock(
        cursor,
        f"memory-{command_type.value}",
        tenant_id,
        actor_id,
        idempotency_key,
    )
    await cursor.execute(
        f"""
        SELECT {_MEMORY_COMMAND_COLUMNS}
        FROM memory.private_memory_commands
        WHERE tenant_id = %s
          AND actor_id = %s
          AND command_type = %s
          AND idempotency_key = %s
        """,
        (tenant_id, actor_id, command_type.value, idempotency_key),
    )
    row = await cursor.fetchone()
    return None if row is None else _decode_memory_command(row)


async def _insert_memory_command(
    cursor: psycopg.AsyncCursor[tuple[Any, ...]],
    *,
    tenant_id: str,
    actor_id: str,
    owner_principal_id: str,
    command_type: PrivateMemoryCommandType,
    idempotency_key: str,
    target_memory_id: str | None,
    result_item: MemoryItem,
    payload: dict[str, object],
) -> str:
    command_id = str(uuid4())
    event_id = str(uuid4())
    result_json = result_item.model_dump(mode="json")
    await cursor.execute(
        """
        INSERT INTO memory.private_memory_commands (
            command_id,
            tenant_id,
            actor_id,
            owner_principal_id,
            command_type,
            idempotency_key,
            target_memory_id,
            result_memory_id,
            payload,
            result_item
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            command_id,
            tenant_id,
            actor_id,
            owner_principal_id,
            command_type.value,
            idempotency_key,
            target_memory_id,
            result_item.id,
            Jsonb(payload),
            Jsonb(result_json),
        ),
    )
    await cursor.execute(
        """
        INSERT INTO memory.private_memory_items (
            memory_id,
            tenant_id,
            owner_principal_id,
            state,
            item,
            source_command_id
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            result_item.id,
            tenant_id,
            owner_principal_id,
            result_item.state.value,
            Jsonb(result_json),
            command_id,
        ),
    )
    await cursor.execute(
        """
        INSERT INTO memory.outbox (
            event_id,
            tenant_id,
            aggregate_type,
            aggregate_id,
            event_type,
            payload
        )
        VALUES (%s, %s, 'private_memory_command', %s, 'memory.command.accepted', %s)
        """,
        (
            event_id,
            tenant_id,
            command_id,
            Jsonb(
                {
                    "command_id": command_id,
                    "tenant_id": tenant_id,
                    "version": 1,
                }
            ),
        ),
    )
    await _write_audit(
        cursor,
        tenant_id=tenant_id,
        actor_id=actor_id,
        event_type=f"memory.{command_type.value}.accepted",
        target_type="private_memory_command",
        target_id=command_id,
        outcome="accepted",
    )
    return command_id


async def _complete_erasure_command(
    cursor: psycopg.AsyncCursor[tuple[Any, ...]],
    command: PrivateMemoryCommand,
) -> None:
    assert command.target_memory_id is not None
    await cursor.execute(
        """
        UPDATE memory.private_memory_items
        SET state = 'erased', item = NULL, updated_at = CURRENT_TIMESTAMP
        WHERE tenant_id = %s
          AND owner_principal_id = %s
          AND memory_id = %s
        """,
        (command.tenant_id, command.owner_principal_id, command.target_memory_id),
    )
    await cursor.execute(
        """
        UPDATE memory.private_memory_commands
        SET payload = '{}'::jsonb,
            result_item = NULL,
            redacted_at = CURRENT_TIMESTAMP
        WHERE tenant_id = %s AND result_memory_id = %s
        """,
        (command.tenant_id, command.target_memory_id),
    )
    if command.erasure_request_id is not None:
        await cursor.execute(
            """
            UPDATE memory.erasure_requests
            SET status = 'completed',
                reason = NULL,
                review_rationale = NULL,
                completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP),
                updated_at = CURRENT_TIMESTAMP
            WHERE tenant_id = %s
              AND request_id = %s
              AND status IN ('approved', 'completed')
            """,
            (command.tenant_id, command.erasure_request_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("Erasure Request is not approved")
        await cursor.execute(
            """
            UPDATE memory.erasure_reviews
            SET rationale = NULL
            WHERE tenant_id = %s AND request_id = %s
            """,
            (command.tenant_id, command.erasure_request_id),
        )
        await _write_audit(
            cursor,
            tenant_id=command.tenant_id,
            actor_id="memory-projection-worker",
            event_type="erasure.completed",
            target_type="erasure_request",
            target_id=command.erasure_request_id,
            outcome="completed",
        )


async def _insert_erasure_memory_command(
    cursor: psycopg.AsyncCursor[tuple[Any, ...]],
    *,
    tenant_id: str,
    actor_id: str,
    request: ErasureRequest,
) -> None:
    command_id = str(uuid4())
    await cursor.execute(
        """
        INSERT INTO memory.private_memory_commands (
            command_id,
            tenant_id,
            actor_id,
            owner_principal_id,
            command_type,
            idempotency_key,
            target_memory_id,
            erasure_request_id,
            payload
        )
        VALUES (%s, %s, %s, %s, 'erase', %s, %s, %s, %s)
        """,
        (
            command_id,
            tenant_id,
            actor_id,
            request.owner_principal_id,
            request.id,
            request.memory_id,
            request.id,
            Jsonb({"erasure_request_id": request.id}),
        ),
    )
    await cursor.execute(
        """
        UPDATE memory.private_memory_items
        SET state = 'erasure_pending', updated_at = CURRENT_TIMESTAMP
        WHERE tenant_id = %s AND memory_id = %s
        """,
        (tenant_id, request.memory_id),
    )
    await cursor.execute(
        """
        INSERT INTO memory.outbox (
            event_id,
            tenant_id,
            aggregate_type,
            aggregate_id,
            event_type,
            payload
        )
        VALUES (%s, %s, 'private_memory_command', %s, 'memory.command.accepted', %s)
        """,
        (
            str(uuid4()),
            tenant_id,
            command_id,
            Jsonb({"command_id": command_id, "tenant_id": tenant_id, "version": 1}),
        ),
    )
    await _write_audit(
        cursor,
        tenant_id=tenant_id,
        actor_id=actor_id,
        event_type="memory.erase.accepted",
        target_type="private_memory_command",
        target_id=command_id,
        outcome="accepted",
    )


async def _idempotency_lock(
    cursor: psycopg.AsyncCursor[tuple[Any, ...]],
    operation: str,
    tenant_id: str,
    actor_id: str,
    idempotency_key: str,
) -> None:
    await cursor.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"{operation}:{tenant_id}:{actor_id}:{idempotency_key}",),
    )


async def _write_audit(
    cursor: psycopg.AsyncCursor[tuple[Any, ...]],
    *,
    tenant_id: str,
    actor_id: str,
    event_type: str,
    target_type: str,
    target_id: str,
    outcome: str,
) -> None:
    await cursor.execute(
        """
        INSERT INTO memory.governance_audit (
            audit_id,
            tenant_id,
            actor_id,
            event_type,
            target_type,
            target_id,
            outcome,
            metadata
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            str(uuid4()),
            tenant_id,
            actor_id,
            event_type,
            target_type,
            target_id,
            outcome,
            Jsonb({}),
        ),
    )


def _qualified_candidate_columns(alias: str) -> str:
    return f", {alias}.".join(column.strip() for column in _CANDIDATE_COLUMNS.split(","))


def _qualified_erasure_columns(alias: str) -> str:
    return f", {alias}.".join(column.strip() for column in _ERASURE_COLUMNS.split(","))


def _decode_candidate(row: tuple[Any, ...]) -> KnowledgeCandidate:
    source_ids = _string_tuple(row[5], "source Memory identifiers")
    duplicate_ids = _string_tuple(row[6], "duplicate Tenant Memory identifiers")
    conflicting_ids = _string_tuple(row[7], "conflicting Tenant Memory identifiers")
    return KnowledgeCandidate(
        id=str(row[0]),
        tenant_id=str(row[1]),
        claim=str(row[2]),
        confidence=float(row[3]),
        proposer_id=str(row[4]),
        source_memory_ids=source_ids,
        duplicate_memory_ids=duplicate_ids,
        conflicting_memory_ids=conflicting_ids,
        status=CandidateStatus(str(row[8])),
        created_at=_datetime(row[9], "candidate creation time"),
        reviewed_by=None if row[10] is None else str(row[10]),
        review_rationale=None if row[11] is None else str(row[11]),
    )


def _decode_erasure(row: tuple[Any, ...]) -> ErasureRequest:
    completed_at = row[10]
    if completed_at is not None:
        completed_at = _datetime(completed_at, "erasure completion time")
    return ErasureRequest(
        id=str(row[0]),
        tenant_id=str(row[1]),
        memory_id=str(row[2]),
        requester_id=str(row[3]),
        owner_principal_id=str(row[4]),
        reason=None if row[5] is None else str(row[5]),
        status=ErasureStatus(str(row[6])),
        created_at=_datetime(row[7], "erasure creation time"),
        reviewed_by=None if row[8] is None else str(row[8]),
        review_rationale=None if row[9] is None else str(row[9]),
        completed_at=completed_at,
    )


def _decode_memory_command(row: tuple[Any, ...]) -> PrivateMemoryCommand:
    result_item = None if row[8] is None else MemoryItem.model_validate(row[8])
    return PrivateMemoryCommand(
        id=str(row[0]),
        tenant_id=str(row[1]),
        actor_id=str(row[2]),
        owner_principal_id=str(row[3]),
        command_type=PrivateMemoryCommandType(str(row[4])),
        idempotency_key=str(row[5]),
        target_memory_id=None if row[6] is None else str(row[6]),
        result_item=result_item,
        erasure_request_id=None if row[9] is None else str(row[9]),
        state=PrivateMemoryCommandState(str(row[10])),
        created_at=_datetime(row[11], "Private Memory Command creation time"),
    )


def _required_result_item(command: PrivateMemoryCommand) -> MemoryItem:
    if command.result_item is None:
        raise ValueError("Private Memory Command result has been erased")
    return command.result_item


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Tenant Operations Store contains invalid {label}")
    return tuple(value)


def _datetime(value: object, label: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"Tenant Operations Store contains invalid {label}")
    return value


def _required_row(row: tuple[Any, ...] | None) -> tuple[Any, ...]:
    if row is None:
        raise RuntimeError("Tenant Operations Store did not return the persisted record")
    return row

"""Neo4j Agent Memory Adapter for one physically isolated Tenant graph."""

from __future__ import annotations

from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import uuid4

from neo4j.exceptions import (
    AuthError,
    ConnectionAcquisitionTimeoutError,
    DatabaseError,
    ServiceUnavailable,
    SessionExpired,
    TransientError,
)
from neo4j_agent_memory.core.exceptions import ConnectionError as Neo4jConnectionError

from agent_memory_service.lifecycle import CorrectMemory
from agent_memory_service.models import (
    MemoryItem,
    MemoryKind,
    MemoryScope,
    MemoryState,
    Provenance,
    RecallQuery,
    RetainMemory,
)
from agent_memory_service.stores.memory import TenantMemoryUnavailable

_SCHEMA_VERSION = 1
_TENANT_KNOWLEDGE_SESSION = "tenant-knowledge"
_TENANT_MEMORY_CAPACITY_BASELINE = 100_000
_DELETE_OWNED_MESSAGE = """
MATCH (conversation:Conversation {session_id: $session_id})
      -[:HAS_MESSAGE]->(message:Message {id: $message_id})
WITH DISTINCT message
DETACH DELETE message
RETURN count(message) > 0 AS deleted
""".strip()

_DEPENDENCY_FAILURES = (
    Neo4jConnectionError,
    AuthError,
    ConnectionAcquisitionTimeoutError,
    DatabaseError,
    ServiceUnavailable,
    SessionExpired,
    TransientError,
    TimeoutError,
)


class _Message(Protocol):
    id: object
    content: str
    metadata: dict[str, Any]
    created_at: datetime


class _Conversation(Protocol):
    messages: list[_Message]


class _GraphClient(Protocol):
    async def execute_write(
        self,
        query: str,
        parameters: dict[str, object],
    ) -> list[dict[str, object]]: ...


class _ShortTerm(Protocol):
    @property
    def client(self) -> _GraphClient: ...

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        **kwargs: Any,
    ) -> _Message: ...

    async def get_conversation(self, session_id: str, **kwargs: Any) -> _Conversation: ...

    async def search_messages(
        self,
        query: str,
        *,
        session_id: str | None = None,
        limit: int = 10,
        **kwargs: Any,
    ) -> list[_Message]: ...


class Neo4jTenantMemoryStore:
    """Persist platform Memory Items through neo4j-agent-memory's public Bolt API.

    One instance is bound to one Tenant graph by construction. Caller-provided values
    can choose a private owner inside that graph, but can never select a graph or URI.
    """

    def __init__(self, client: Any) -> None:
        # The upstream MemoryClient is not structurally typed; keep that looseness at
        # this Adapter boundary and use the narrow protocol everywhere below it.
        self._short_term = cast(_ShortTerm, client.short_term)

    async def retain_private(
        self,
        owner_principal_id: str,
        command: RetainMemory,
        *,
        actor_id: str | None = None,
    ) -> MemoryItem:
        messages = await self._messages(_private_session(owner_principal_id))
        existing = _find_by_key(messages, "retain_idempotency_key", command.idempotency_key)
        if existing is not None:
            return _to_item(existing, _superseded_ids(messages))
        message = await _dependency_operation(
            self._short_term.add_message(
                _private_session(owner_principal_id),
                "system",
                command.content,
                extract_entities=False,
                extract_relations=False,
                generate_embedding=True,
                extraction_mode="skip",
                user_identifier=owner_principal_id,
                metadata={
                    **_item_metadata(
                        platform_id=str(uuid4()),
                        owner_principal_id=owner_principal_id,
                        scope=MemoryScope.PRIVATE,
                        kind=command.kind,
                        confidence=command.confidence,
                        actor_id=actor_id or owner_principal_id,
                        source="explicit",
                    ),
                    "retain_idempotency_key": command.idempotency_key,
                },
            )
        )
        return _to_item(message, set())

    async def recall(
        self,
        private_owner_ids: tuple[str, ...],
        query: RecallQuery,
    ) -> tuple[MemoryItem, ...]:
        sessions = tuple(_private_session(owner) for owner in private_owner_ids) + (
            _TENANT_KNOWLEDGE_SESSION,
        )
        all_messages = [
            message for session in sessions for message in await self._messages(session)
        ]
        superseded = _superseded_ids(all_messages)
        found: dict[str, MemoryItem] = {}
        for session in sessions:
            results = await _dependency_operation(
                self._short_term.search_messages(
                    query.query,
                    session_id=session,
                    limit=query.limit,
                    threshold=0.0,
                )
            )
            for message in results:
                # neo4j-agent-memory's vector search has historically returned
                # results outside the requested session in some configurations.
                # Treat its filter as an optimisation, never as an authorization
                # boundary, and fail closed on our own immutable scope metadata.
                if not _belongs_to_session(message, session):
                    continue
                item = _to_item(message, superseded)
                if item.state is MemoryState.ACTIVE:
                    found[item.id] = item
        return tuple(found.values())[: query.limit]

    async def get_visible_private_items(
        self,
        private_owner_ids: tuple[str, ...],
        item_ids: tuple[str, ...],
    ) -> tuple[MemoryItem, ...]:
        requested = set(item_ids)
        items = [item for owner in private_owner_ids for item in await self.list_private(owner)]
        return tuple(
            item for item in items if item.id in requested and item.state is MemoryState.ACTIVE
        )

    async def publish_tenant_knowledge(
        self,
        candidate_id: str,
        claim: str,
        confidence: float,
        proposer_id: str,
    ) -> MemoryItem:
        messages = await self._messages(_TENANT_KNOWLEDGE_SESSION)
        existing = _find_by_key(messages, "candidate_id", candidate_id)
        if existing is not None:
            return _to_item(existing, _superseded_ids(messages))
        message = await _dependency_operation(
            self._short_term.add_message(
                _TENANT_KNOWLEDGE_SESSION,
                "system",
                claim,
                extract_entities=False,
                extract_relations=False,
                generate_embedding=True,
                extraction_mode="skip",
                user_identifier=_TENANT_KNOWLEDGE_SESSION,
                metadata={
                    **_item_metadata(
                        platform_id=str(uuid4()),
                        owner_principal_id=None,
                        scope=MemoryScope.TENANT_KNOWLEDGE,
                        kind=MemoryKind.EXPLICIT,
                        confidence=confidence,
                        actor_id=proposer_id,
                        source=f"candidate:{candidate_id}",
                    ),
                    "candidate_id": candidate_id,
                },
            )
        )
        return _to_item(message, set())

    async def correct_private(
        self,
        owner_principal_id: str,
        command: CorrectMemory,
        *,
        actor_id: str | None = None,
    ) -> MemoryItem:
        messages = await self._messages(_private_session(owner_principal_id))
        existing = _find_by_key(messages, "correction_idempotency_key", command.idempotency_key)
        if existing is not None:
            return _to_item(existing, _superseded_ids(messages))
        superseded = _superseded_ids(messages)
        original = next(
            (
                message
                for message in messages
                if str(message.metadata.get("platform_id")) == command.memory_id
                and command.memory_id not in superseded
            ),
            None,
        )
        if original is None:
            raise LookupError("Owned active Memory Item not found")
        message = await _dependency_operation(
            self._short_term.add_message(
                _private_session(owner_principal_id),
                "system",
                command.replacement_content,
                extract_entities=False,
                extract_relations=False,
                generate_embedding=True,
                extraction_mode="skip",
                user_identifier=owner_principal_id,
                metadata={
                    **_item_metadata(
                        platform_id=str(uuid4()),
                        owner_principal_id=owner_principal_id,
                        scope=MemoryScope.PRIVATE,
                        kind=command.kind,
                        confidence=command.confidence,
                        actor_id=actor_id or owner_principal_id,
                        source="correction",
                        supersedes_id=command.memory_id,
                    ),
                    "correction_idempotency_key": command.idempotency_key,
                    "correction_reason": command.reason,
                },
            )
        )
        return _to_item(message, superseded)

    async def erase_private(self, owner_principal_id: str, memory_id: str) -> bool:
        messages = await self._messages(_private_session(owner_principal_id))
        message = next(
            (
                candidate
                for candidate in messages
                if str(candidate.metadata.get("platform_id")) == memory_id
            ),
            None,
        )
        if message is None:
            return False
        results = await _dependency_operation(
            self._short_term.client.execute_write(
                _DELETE_OWNED_MESSAGE,
                {
                    "session_id": _private_session(owner_principal_id),
                    "message_id": str(message.id),
                },
            )
        )
        return bool(results and results[0].get("deleted"))

    async def list_private(self, owner_principal_id: str) -> tuple[MemoryItem, ...]:
        messages = await self._messages(_private_session(owner_principal_id))
        superseded = _superseded_ids(messages)
        return tuple(_to_item(message, superseded) for message in messages)

    async def list_tenant_knowledge(self) -> tuple[MemoryItem, ...]:
        messages = await self._messages(_TENANT_KNOWLEDGE_SESSION)
        superseded = _superseded_ids(messages)
        return tuple(_to_item(message, superseded) for message in messages)

    async def import_private(self, owner_principal_id: str, item: MemoryItem) -> bool:
        return await self._apply_private_item(
            owner_principal_id,
            item,
            provenance_source=f"archive:{item.provenance.source}",
        )

    async def apply_private_item(self, owner_principal_id: str, item: MemoryItem) -> bool:
        return await self._apply_private_item(
            owner_principal_id,
            item,
            provenance_source=item.provenance.source,
        )

    async def _apply_private_item(
        self,
        owner_principal_id: str,
        item: MemoryItem,
        *,
        provenance_source: str,
    ) -> bool:
        messages = await self._messages(_private_session(owner_principal_id))
        if any(str(message.metadata.get("platform_id")) == item.id for message in messages):
            return False
        await _dependency_operation(
            self._short_term.add_message(
                _private_session(owner_principal_id),
                "system",
                item.content,
                extract_entities=False,
                extract_relations=False,
                generate_embedding=True,
                extraction_mode="skip",
                user_identifier=owner_principal_id,
                metadata=_item_metadata(
                    platform_id=item.id,
                    owner_principal_id=owner_principal_id,
                    scope=MemoryScope.PRIVATE,
                    kind=item.kind,
                    confidence=item.confidence,
                    actor_id=item.provenance.actor_id,
                    source=provenance_source,
                    supersedes_id=item.supersedes_id,
                    created_at=item.created_at,
                ),
            ),
        )
        return True

    async def _messages(self, session_id: str) -> list[_Message]:
        conversation = await _dependency_operation(
            self._short_term.get_conversation(
                session_id,
                limit=_TENANT_MEMORY_CAPACITY_BASELINE,
            )
        )
        return list(conversation.messages)


async def _dependency_operation[ResultT](operation: Awaitable[ResultT]) -> ResultT:
    try:
        return await operation
    except _DEPENDENCY_FAILURES as exc:
        raise TenantMemoryUnavailable("Tenant Memory Store is unavailable") from exc


def _private_session(owner_principal_id: str) -> str:
    return f"private:{owner_principal_id}"


def _belongs_to_session(message: _Message, session_id: str) -> bool:
    metadata = message.metadata
    if session_id == _TENANT_KNOWLEDGE_SESSION:
        return (
            metadata.get("scope") == MemoryScope.TENANT_KNOWLEDGE.value
            and metadata.get("owner_principal_id") is None
        )
    if not session_id.startswith("private:"):
        return False
    owner_principal_id = session_id.removeprefix("private:")
    return (
        metadata.get("scope") == MemoryScope.PRIVATE.value
        and metadata.get("owner_principal_id") == owner_principal_id
    )


def _item_metadata(
    *,
    platform_id: str,
    owner_principal_id: str | None,
    scope: MemoryScope,
    kind: MemoryKind,
    confidence: float,
    actor_id: str,
    source: str,
    supersedes_id: str | None = None,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "platform_schema_version": _SCHEMA_VERSION,
        "platform_id": platform_id,
        "owner_principal_id": owner_principal_id,
        "scope": scope.value,
        "kind": kind.value,
        "confidence": confidence,
        "provenance_actor_id": actor_id,
        "provenance_source": source,
        "supersedes_id": supersedes_id,
        "platform_created_at": (created_at or datetime.now(UTC)).isoformat(),
    }


def _find_by_key(messages: list[_Message], key: str, value: str) -> _Message | None:
    return next((message for message in messages if message.metadata.get(key) == value), None)


def _superseded_ids(messages: list[_Message]) -> set[str]:
    return {
        value
        for message in messages
        if isinstance((value := message.metadata.get("supersedes_id")), str)
    }


def _to_item(message: _Message, superseded_ids: set[str]) -> MemoryItem:
    metadata = message.metadata
    if metadata.get("platform_schema_version") != _SCHEMA_VERSION:
        raise ValueError("Tenant Memory Store contains an unsupported item")
    platform_id = str(metadata["platform_id"])
    return MemoryItem(
        id=platform_id,
        owner_principal_id=(
            None
            if metadata.get("owner_principal_id") is None
            else str(metadata["owner_principal_id"])
        ),
        scope=MemoryScope(str(metadata["scope"])),
        content=message.content,
        kind=MemoryKind(str(metadata["kind"])),
        confidence=float(metadata["confidence"]),
        provenance=Provenance(
            actor_id=str(metadata["provenance_actor_id"]),
            source=str(metadata["provenance_source"]),
        ),
        created_at=datetime.fromisoformat(str(metadata["platform_created_at"])),
        state=(MemoryState.SUPERSEDED if platform_id in superseded_ids else MemoryState.ACTIVE),
        supersedes_id=(
            None if metadata.get("supersedes_id") is None else str(metadata["supersedes_id"])
        ),
    )

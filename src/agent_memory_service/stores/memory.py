"""Tenant Memory Store port and deterministic in-memory Adapter."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol
from uuid import uuid4

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


class TenantMemoryUnavailable(RuntimeError):
    """The requested Tenant has no healthy, provisioned Memory Store."""


class TenantMemoryStore(Protocol):
    async def retain_private(
        self,
        owner_principal_id: str,
        command: RetainMemory,
        *,
        actor_id: str | None = None,
    ) -> MemoryItem:
        """Retain an idempotent item in one Principal's private scope."""

    async def recall(
        self,
        private_owner_ids: tuple[str, ...],
        query: RecallQuery,
    ) -> tuple[MemoryItem, ...]:
        """Recall active items visible to the supplied private scopes."""

    async def get_visible_private_items(
        self,
        private_owner_ids: tuple[str, ...],
        item_ids: tuple[str, ...],
    ) -> tuple[MemoryItem, ...]: ...

    async def publish_tenant_knowledge(
        self,
        candidate_id: str,
        claim: str,
        confidence: float,
        proposer_id: str,
    ) -> MemoryItem: ...

    async def correct_private(
        self,
        owner_principal_id: str,
        command: CorrectMemory,
        *,
        actor_id: str | None = None,
    ) -> MemoryItem: ...

    async def erase_private(self, owner_principal_id: str, memory_id: str) -> bool: ...

    async def list_private(self, owner_principal_id: str) -> tuple[MemoryItem, ...]: ...

    async def list_tenant_knowledge(self) -> tuple[MemoryItem, ...]: ...

    async def import_private(self, owner_principal_id: str, item: MemoryItem) -> bool: ...

    async def apply_private_item(self, owner_principal_id: str, item: MemoryItem) -> bool:
        """Idempotently apply an authoritative item without rewriting provenance."""
        ...


class TenantMemoryRouter(Protocol):
    def for_tenant(self, tenant_id: str) -> TenantMemoryStore:
        """Resolve exactly one Tenant Memory Store or fail closed."""


class InMemoryTenantMemoryStore:
    """Behavioral test Adapter; production storage is supplied separately."""

    def __init__(self) -> None:
        self._items: list[MemoryItem] = []
        self._idempotency: dict[tuple[str, str], MemoryItem] = {}
        self._published_candidates: dict[str, MemoryItem] = {}
        self._correction_keys: dict[tuple[str, str], MemoryItem] = {}

    async def retain_private(
        self,
        owner_principal_id: str,
        command: RetainMemory,
        *,
        actor_id: str | None = None,
    ) -> MemoryItem:
        key = (owner_principal_id, command.idempotency_key)
        if existing := self._idempotency.get(key):
            return existing
        item = MemoryItem(
            id=str(uuid4()),
            owner_principal_id=owner_principal_id,
            scope=MemoryScope.PRIVATE,
            content=command.content,
            kind=command.kind,
            confidence=command.confidence,
            provenance=Provenance(actor_id=actor_id or owner_principal_id, source="explicit"),
        )
        self._items.append(item)
        self._idempotency[key] = item
        return item

    async def recall(
        self,
        private_owner_ids: tuple[str, ...],
        query: RecallQuery,
    ) -> tuple[MemoryItem, ...]:
        visible = (
            item
            for item in self._items
            if item.state is MemoryState.ACTIVE
            and (
                item.scope is MemoryScope.TENANT_KNOWLEDGE
                or item.owner_principal_id in private_owner_ids
            )
        )
        ranked = sorted(
            visible,
            key=lambda item: self._score(item, query.query),
            reverse=True,
        )
        return tuple(item for item in ranked if self._score(item, query.query) > 0)[: query.limit]

    async def get_visible_private_items(
        self,
        private_owner_ids: tuple[str, ...],
        item_ids: tuple[str, ...],
    ) -> tuple[MemoryItem, ...]:
        requested = set(item_ids)
        return tuple(
            item
            for item in self._items
            if item.id in requested
            and item.scope is MemoryScope.PRIVATE
            and item.state is MemoryState.ACTIVE
            and item.owner_principal_id in private_owner_ids
        )

    async def publish_tenant_knowledge(
        self,
        candidate_id: str,
        claim: str,
        confidence: float,
        proposer_id: str,
    ) -> MemoryItem:
        if existing := self._published_candidates.get(candidate_id):
            return existing
        item = MemoryItem(
            id=str(uuid4()),
            owner_principal_id=None,
            scope=MemoryScope.TENANT_KNOWLEDGE,
            content=claim,
            kind=MemoryKind.EXPLICIT,
            confidence=confidence,
            provenance=Provenance(actor_id=proposer_id, source=f"candidate:{candidate_id}"),
        )
        self._items.append(item)
        self._published_candidates[candidate_id] = item
        return item

    async def correct_private(
        self,
        owner_principal_id: str,
        command: CorrectMemory,
        *,
        actor_id: str | None = None,
    ) -> MemoryItem:
        key = (owner_principal_id, command.idempotency_key)
        if existing := self._correction_keys.get(key):
            return existing
        original_index = next(
            (
                index
                for index, item in enumerate(self._items)
                if item.id == command.memory_id
                and item.owner_principal_id == owner_principal_id
                and item.scope is MemoryScope.PRIVATE
                and item.state is MemoryState.ACTIVE
            ),
            None,
        )
        if original_index is None:
            raise LookupError("Owned active Memory Item not found")
        original = self._items[original_index]
        self._items[original_index] = original.model_copy(update={"state": MemoryState.SUPERSEDED})
        corrected = MemoryItem(
            id=str(uuid4()),
            owner_principal_id=owner_principal_id,
            scope=MemoryScope.PRIVATE,
            content=command.replacement_content,
            kind=command.kind,
            confidence=command.confidence,
            provenance=Provenance(actor_id=actor_id or owner_principal_id, source="correction"),
            supersedes_id=original.id,
        )
        self._items.append(corrected)
        self._correction_keys[key] = corrected
        return corrected

    async def erase_private(self, owner_principal_id: str, memory_id: str) -> bool:
        before = len(self._items)
        self._items = [
            item
            for item in self._items
            if not (item.id == memory_id and item.owner_principal_id == owner_principal_id)
        ]
        return len(self._items) < before

    async def list_private(self, owner_principal_id: str) -> tuple[MemoryItem, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._items
                    if item.scope is MemoryScope.PRIVATE
                    and item.owner_principal_id == owner_principal_id
                ),
                key=lambda item: item.id,
            )
        )

    async def list_tenant_knowledge(self) -> tuple[MemoryItem, ...]:
        return tuple(
            sorted(
                (item for item in self._items if item.scope is MemoryScope.TENANT_KNOWLEDGE),
                key=lambda item: item.id,
            )
        )

    async def import_private(self, owner_principal_id: str, item: MemoryItem) -> bool:
        return await self.apply_private_item(owner_principal_id, item)

    async def apply_private_item(self, owner_principal_id: str, item: MemoryItem) -> bool:
        if any(existing.id == item.id for existing in self._items):
            return False
        if item.supersedes_id is not None:
            self._items = [
                (
                    existing.model_copy(update={"state": MemoryState.SUPERSEDED})
                    if existing.id == item.supersedes_id
                    and existing.owner_principal_id == owner_principal_id
                    else existing
                )
                for existing in self._items
            ]
        imported = item.model_copy(
            update={
                "owner_principal_id": owner_principal_id,
                "scope": MemoryScope.PRIVATE,
            }
        )
        self._items.append(imported)
        return True

    @staticmethod
    def _score(item: MemoryItem, query: str) -> int:
        query_terms = _terms(query)
        return len(query_terms.intersection(_terms(item.content)))


def _terms(value: str) -> set[str]:
    return {term.strip(".,:;!?()[]{}\"'").casefold() for term in value.split() if term}


class InMemoryTenantMemoryRouter:
    def __init__(self, tenant_ids: Iterable[str] = ()) -> None:
        self._stores = {tenant_id: InMemoryTenantMemoryStore() for tenant_id in tenant_ids}

    def for_tenant(self, tenant_id: str) -> InMemoryTenantMemoryStore:
        try:
            return self._stores[tenant_id]
        except KeyError as exc:
            raise TenantMemoryUnavailable("Tenant Memory Store is not available") from exc

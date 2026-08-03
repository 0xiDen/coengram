"""Authorization-aware Memory Module Interface."""

from __future__ import annotations

from typing import Protocol

from agent_memory_service.archive import (
    ArchiveImportResult,
    ArchiveScope,
    decode_archive,
    encode_archive,
    order_correction_history,
)
from agent_memory_service.durable_memory import PrivateMemoryCommandStore
from agent_memory_service.governance import (
    GovernanceStore,
    KnowledgeCandidateView,
    ProposeKnowledge,
    ReviewKnowledge,
    candidate_view,
)
from agent_memory_service.lifecycle import (
    CorrectMemory,
    ErasureRequestView,
    ErasureStore,
    ErasureTombstone,
    RequestErasure,
    ReviewErasure,
    erasure_view,
)
from agent_memory_service.models import (
    MemoryItem,
    MemoryMutationReceipt,
    MemoryScope,
    MemoryState,
    MutationState,
    PrincipalKind,
    PrivateMemoryInspection,
    RecallQuery,
    RecallResult,
    RetainMemory,
    TenantSession,
)
from agent_memory_service.stores.memory import TenantMemoryRouter


class SelfApprovalPolicy(Protocol):
    def permits_self_approval(self, tenant_id: str, principal_id: str) -> bool: ...


class MemoryModule:
    """Deep Module that owns scope selection independently of storage and transport."""

    def __init__(
        self,
        router: TenantMemoryRouter,
        governance: GovernanceStore | None = None,
        *,
        erasures: ErasureStore | None = None,
        self_approval: SelfApprovalPolicy | None = None,
        commands: PrivateMemoryCommandStore | None = None,
    ) -> None:
        self._router = router
        self._governance = governance
        self._erasures = erasures
        self._self_approval = self_approval
        self._commands = commands

    async def retain(self, session: TenantSession, command: RetainMemory) -> MemoryItem:
        owner_id = session.subject_user_id or session.actor_id
        if self._commands is not None:
            return await self._commands.accept_retain(
                session.tenant_id,
                session.actor_id,
                owner_id,
                command,
            )
        store = self._router.for_tenant(session.tenant_id)
        return await store.retain_private(owner_id, command, actor_id=session.actor_id)

    async def retain_result(
        self,
        session: TenantSession,
        command: RetainMemory,
    ) -> MemoryMutationReceipt:
        item = await self.retain(session, command)
        return await self._mutation_receipt(session.tenant_id, item)

    async def retain_agent_private(
        self,
        session: TenantSession,
        command: RetainMemory,
    ) -> MemoryItem:
        """Retain an explicit operating lesson in the Agent Actor's own scope."""

        if session.actor_kind is not PrincipalKind.AGENT:
            raise PermissionError("Agent Private Memory requires an Agent Actor")
        if self._commands is not None:
            return await self._commands.accept_retain(
                session.tenant_id,
                session.actor_id,
                session.actor_id,
                command,
            )
        store = self._router.for_tenant(session.tenant_id)
        return await store.retain_private(session.actor_id, command, actor_id=session.actor_id)

    async def recall(self, session: TenantSession, query: RecallQuery) -> RecallResult:
        store = self._router.for_tenant(session.tenant_id)
        owners = [session.actor_id]
        if session.actor_kind is PrincipalKind.AGENT and session.subject_user_id is not None:
            owners.append(session.subject_user_id)
        return RecallResult(items=await store.recall(tuple(owners), query))

    async def inspect_private(
        self,
        session: TenantSession,
    ) -> tuple[PrivateMemoryInspection, ...]:
        """List only the authenticated Principal's owned Private Memory."""
        if self._commands is not None:
            return await self._commands.list_private_memory_state(
                session.tenant_id,
                session.actor_id,
            )
        items = await self._router.for_tenant(session.tenant_id).list_private(session.actor_id)
        return tuple(
            PrivateMemoryInspection(
                id=item.id,
                owner_principal_id=session.actor_id,
                state=item.state,
                mutation_state=MutationState.APPLIED,
                content=item.content,
                kind=item.kind,
                confidence=item.confidence,
                created_at=item.created_at,
                supersedes_id=item.supersedes_id,
            )
            for item in items
        )

    async def propose_knowledge(
        self,
        session: TenantSession,
        command: ProposeKnowledge,
    ) -> KnowledgeCandidateView:
        governance = self._require_governance()
        store = self._router.for_tenant(session.tenant_id)
        owners = self._private_owners(session)
        sources = await store.get_visible_private_items(owners, command.source_memory_ids)
        if {item.id for item in sources} != set(command.source_memory_ids):
            raise PermissionError("One or more source Memory Items are not visible")
        return candidate_view(
            await governance.propose(session.tenant_id, session.actor_id, command)
        )

    async def review_knowledge(
        self,
        session: TenantSession,
        command: ReviewKnowledge,
    ) -> KnowledgeCandidateView:
        governance = self._require_governance()
        if session.actor_kind is not PrincipalKind.USER or not (
            {"knowledge_curator", "tenant_administrator"} & session.roles
        ):
            raise PermissionError("Knowledge review requires a human Knowledge Curator")
        candidate = await governance.get_candidate(session.tenant_id, command.candidate_id)
        if candidate is None:
            raise LookupError("Knowledge Candidate not found")
        if candidate.proposer_id == session.actor_id and not (
            "tenant_administrator" in session.roles
            and self._self_approval is not None
            and self._self_approval.permits_self_approval(
                session.tenant_id,
                session.actor_id,
            )
        ):
            raise PermissionError("Self-approval requires a single-human Tenant Administrator")
        return candidate_view(await governance.review(session.tenant_id, session.actor_id, command))

    async def list_knowledge_candidates(
        self,
        session: TenantSession,
    ) -> tuple[KnowledgeCandidateView, ...]:
        """Expose privacy-safe review material to human curators only."""

        if session.actor_kind is not PrincipalKind.USER or not (
            {"knowledge_curator", "tenant_administrator"} & session.roles
        ):
            raise PermissionError("Candidate inspection requires a human Knowledge Curator")
        governance = self._require_governance()
        return tuple(
            candidate_view(candidate)
            for candidate in await governance.list_candidates(session.tenant_id)
        )

    async def publish_next(self, tenant_id: str) -> KnowledgeCandidateView | None:
        governance = self._require_governance()
        event = await governance.next_publication(tenant_id)
        if event is None:
            return None
        candidate = await governance.get_candidate(tenant_id, event.candidate_id)
        if candidate is None:
            raise LookupError("Knowledge Candidate not found")
        store = self._router.for_tenant(tenant_id)
        await store.publish_tenant_knowledge(
            candidate.id,
            candidate.claim,
            candidate.confidence,
            candidate.proposer_id,
        )
        return candidate_view(await governance.mark_published(tenant_id, candidate.id))

    async def correct(self, session: TenantSession, command: CorrectMemory) -> MemoryItem:
        owner_id = session.subject_user_id or session.actor_id
        if self._commands is not None:
            return await self._commands.accept_correction(
                session.tenant_id,
                session.actor_id,
                owner_id,
                command,
            )
        store = self._router.for_tenant(session.tenant_id)
        return await store.correct_private(owner_id, command, actor_id=session.actor_id)

    async def correct_result(
        self,
        session: TenantSession,
        command: CorrectMemory,
    ) -> MemoryMutationReceipt:
        item = await self.correct(session, command)
        return await self._mutation_receipt(session.tenant_id, item)

    async def _mutation_receipt(
        self,
        tenant_id: str,
        item: MemoryItem,
    ) -> MemoryMutationReceipt:
        operation_id = item.id
        state = MutationState.APPLIED
        if self._commands is not None:
            command = await self._commands.get_memory_command_by_result(tenant_id, item.id)
            if command is None:
                raise RuntimeError("Committed Private Memory operation cannot be inspected")
            operation_id = command.id
            state = MutationState(command.state.value)
        return MemoryMutationReceipt(
            code=f"memory_mutation_{state.value}",
            operation_id=operation_id,
            state=state,
            item=item,
            id=item.id,
        )

    async def request_erasure(
        self,
        session: TenantSession,
        command: RequestErasure,
    ) -> ErasureRequestView:
        erasures = self._require_erasures()
        store = self._router.for_tenant(session.tenant_id)
        owner_id = session.subject_user_id or session.actor_id
        visible = await store.get_visible_private_items((owner_id,), (command.memory_id,))
        if not visible:
            raise LookupError("Owned active Memory Item not found")
        return erasure_view(
            await erasures.request(
                session.tenant_id,
                session.actor_id,
                owner_id,
                command,
            )
        )

    async def review_erasure(
        self,
        session: TenantSession,
        command: ReviewErasure,
    ) -> ErasureRequestView:
        erasures = self._require_erasures()
        if (
            session.actor_kind is not PrincipalKind.USER
            or "tenant_administrator" not in session.roles
        ):
            raise PermissionError("Erasure review requires a human Tenant Administrator")
        request = await erasures.get_request(session.tenant_id, command.request_id)
        if request is None:
            raise LookupError("Erasure Request not found")
        if (
            request.requester_id == session.actor_id
            or request.owner_principal_id == session.actor_id
        ):
            raise PermissionError("Erasure requires a separate Tenant Administrator")
        return erasure_view(await erasures.review(session.tenant_id, session.actor_id, command))

    async def erase_next(self, tenant_id: str) -> ErasureRequestView | None:
        erasures = self._require_erasures()
        request = await erasures.next_approved(tenant_id)
        if request is None:
            return None
        store = self._router.for_tenant(tenant_id)
        erased = await store.erase_private(request.owner_principal_id, request.memory_id)
        if not erased:
            raise LookupError("Owned Memory Item not found for approved erasure")
        return erasure_view(await erasures.mark_completed(tenant_id, request.id))

    @staticmethod
    def _private_owners(session: TenantSession) -> tuple[str, ...]:
        owners = [session.actor_id]
        if session.actor_kind is PrincipalKind.AGENT and session.subject_user_id is not None:
            owners.append(session.subject_user_id)
        return tuple(owners)

    def _require_governance(self) -> GovernanceStore:
        if self._governance is None:
            raise RuntimeError("Tenant governance is not available")
        return self._governance

    def _require_erasures(self) -> ErasureStore:
        if self._erasures is None:
            raise RuntimeError("Erasure governance is not available")
        return self._erasures

    async def export_private(self, session: TenantSession) -> bytes:
        if self._commands is None:
            items = await self._router.for_tenant(session.tenant_id).list_private(session.actor_id)
        else:
            items = await self._commands.list_private_memory_items(
                session.tenant_id,
                session.actor_id,
            )
        tombstones = (
            ()
            if self._erasures is None
            else await self._erasures.list_completed(session.tenant_id, session.actor_id)
        )
        return encode_archive(
            ArchiveScope.PRIVATE,
            [item.model_dump(mode="json") for item in items],
            erasure_records=[tombstone.model_dump(mode="json") for tombstone in tombstones],
        )

    async def import_private(
        self,
        session: TenantSession,
        archive: bytes,
    ) -> ArchiveImportResult:
        decoded = decode_archive(archive, ArchiveScope.PRIVATE)
        if decoded.governance_candidates:
            raise ValueError("Private Memory Archive contains Tenant governance records")
        if decoded.erasure_tombstones and self._erasures is None:
            raise RuntimeError("Erasure governance is not available")
        records = order_correction_history(decoded.memory_items)
        _validate_private_archive(records, decoded.erasure_tombstones)
        if self._commands is None:
            existing = await self._router.for_tenant(session.tenant_id).list_private(
                session.actor_id
            )
        else:
            existing = await self._commands.list_private_memory_items(
                session.tenant_id,
                session.actor_id,
            )
        existing_items = {item.id: item for item in existing}
        tombstone_memory_ids = {tombstone.memory_id for tombstone in decoded.erasure_tombstones}
        if tombstone_memory_ids.intersection(existing_items):
            raise ValueError("Erasure Tombstone conflicts with an active Memory Item")
        for item in records:
            persisted_item = existing_items.get(item.id)
            if persisted_item is not None and not _same_archived_memory(persisted_item, item):
                raise ValueError("Memory Item identifier already has different data")
        if self._erasures is not None:
            existing_tombstones = {
                tombstone.id: tombstone
                for tombstone in await self._erasures.list_completed(
                    session.tenant_id,
                    session.actor_id,
                )
            }
            existing_tombstones_by_memory = {
                tombstone.memory_id: tombstone for tombstone in existing_tombstones.values()
            }
            for tombstone in decoded.erasure_tombstones:
                existing_tombstone = existing_tombstones.get(tombstone.id)
                rebound = tombstone.model_copy(update={"owner_principal_id": session.actor_id})
                if existing_tombstone is not None and existing_tombstone != rebound:
                    raise ValueError("Erasure Tombstone identifier already has different data")
                same_memory = existing_tombstones_by_memory.get(tombstone.memory_id)
                if same_memory is not None and same_memory.id != tombstone.id:
                    raise ValueError("Memory Item already has a different Erasure Tombstone")
        imported = 0
        imported_tombstones = 0
        for tombstone in decoded.erasure_tombstones:
            assert self._erasures is not None
            imported_tombstones += int(
                await self._erasures.import_tombstone(
                    session.tenant_id,
                    session.actor_id,
                    tombstone,
                )
            )
        for item in records:
            if self._commands is not None:
                acceptance = await self._commands.accept_import(
                    session.tenant_id,
                    session.actor_id,
                    session.actor_id,
                    item,
                )
                imported += int(acceptance.accepted)
            else:
                store = self._router.for_tenant(session.tenant_id)
                imported += int(await store.import_private(session.actor_id, item))
        total = len(records) + len(decoded.erasure_tombstones)
        accepted = imported + imported_tombstones
        return ArchiveImportResult(
            imported=accepted,
            skipped=total - accepted,
            erasure_tombstones=imported_tombstones,
        )

    async def export_tenant(self, session: TenantSession) -> bytes:
        if (
            session.actor_kind is not PrincipalKind.USER
            or "tenant_administrator" not in session.roles
        ):
            raise PermissionError("Tenant export requires a human Tenant Administrator")
        store = self._router.for_tenant(session.tenant_id)
        items = await store.list_tenant_knowledge()
        governance = self._require_governance()
        candidates = await governance.list_candidates(session.tenant_id)
        return encode_archive(
            ArchiveScope.TENANT,
            [item.model_dump(mode="json") for item in items],
            governance_records=[
                candidate_view(candidate).model_dump(mode="json") for candidate in candidates
            ],
        )

    async def import_tenant(
        self,
        session: TenantSession,
        archive: bytes,
    ) -> ArchiveImportResult:
        if (
            session.actor_kind is not PrincipalKind.USER
            or "tenant_administrator" not in session.roles
        ):
            raise PermissionError("Tenant import requires a human Tenant Administrator")
        governance = self._require_governance()
        decoded = decode_archive(archive, ArchiveScope.TENANT)
        if decoded.erasure_tombstones:
            raise ValueError("Tenant Memory Archive contains Private Memory erasure records")
        records = decoded.memory_items
        if any(item.scope is not MemoryScope.TENANT_KNOWLEDGE for item in records):
            raise ValueError("Tenant Memory Archive contains a private item")
        candidates = []
        skipped_existing = 0
        for item in records:
            existing = await governance.get_candidate(session.tenant_id, item.id)
            if existing is None:
                continue
            if existing.claim != item.content or existing.confidence != item.confidence:
                raise ValueError("Knowledge Candidate identifier already has different data")
            candidates.append(candidate_view(existing))
            skipped_existing += 1
        existing_ids = {candidate.id for candidate in candidates}
        for item in records:
            if item.id in existing_ids:
                continue
            candidate = await governance.import_candidate(
                session.tenant_id,
                session.actor_id,
                item.id,
                item.content,
                item.confidence,
            )
            candidates.append(candidate_view(candidate))
        return ArchiveImportResult(
            imported=len(candidates) - skipped_existing,
            skipped=len(decoded.governance_candidates) + skipped_existing,
            candidates=tuple(candidates),
            governance_records=len(decoded.governance_candidates),
        )


def _validate_private_archive(
    items: tuple[MemoryItem, ...],
    tombstones: tuple[ErasureTombstone, ...],
) -> None:
    if any(item.scope is not MemoryScope.PRIVATE for item in items):
        raise ValueError("Private Memory Archive contains a non-private item")
    by_id = {item.id: item for item in items}
    erased_ids = {tombstone.memory_id for tombstone in tombstones}
    corrected_parent_ids = [item.supersedes_id for item in items if item.supersedes_id is not None]
    if len(corrected_parent_ids) != len(set(corrected_parent_ids)):
        raise ValueError("Memory Archive correction history branches from one ancestor")
    for item in items:
        if item.supersedes_id is None:
            continue
        if item.supersedes_id == item.id:
            raise ValueError("Memory Archive correction cannot supersede itself")
        parent = by_id.get(item.supersedes_id)
        if parent is None and item.supersedes_id not in erased_ids:
            raise ValueError("Memory Archive correction history is incomplete")
        if parent is not None and parent.state is not MemoryState.SUPERSEDED:
            raise ValueError("Memory Archive correction ancestor is not superseded")


def _same_archived_memory(existing: MemoryItem, archived: MemoryItem) -> bool:
    """Compare stable memory facts while allowing destination ownership/provenance wrapping."""

    return (
        existing.id == archived.id
        and existing.content == archived.content
        and existing.kind is archived.kind
        and existing.confidence == archived.confidence
        and existing.state is archived.state
        and existing.supersedes_id == archived.supersedes_id
        and existing.provenance.actor_id == archived.provenance.actor_id
        and existing.created_at == archived.created_at
    )

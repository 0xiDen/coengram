"""Application adapters for the native knowledge-synthesis Pack."""

from __future__ import annotations

from agent_memory_service.agents.knowledge_synthesis_pack import (
    KnowledgeSynthesisDraft,
    KnowledgeSynthesisInput,
    KnowledgeSynthesisMemoryPort,
    KnowledgeSynthesisResult,
    execute_knowledge_synthesis_pack,
)
from agent_memory_service.agents.runtime import (
    AgentCapability,
    AgentRunContext,
)
from agent_memory_service.governance import KnowledgeCandidateView, ProposeKnowledge
from agent_memory_service.memory import MemoryModule
from agent_memory_service.models import MemoryItem, PrincipalKind, TenantSession
from agent_memory_service.stores.memory import TenantMemoryRouter

__all__ = [
    "KnowledgeSynthesisCapability",
    "KnowledgeSynthesisDraft",
    "KnowledgeSynthesisInput",
    "KnowledgeSynthesisMemoryPort",
    "KnowledgeSynthesisResult",
    "MemoryModuleKnowledgeSynthesisPort",
]


class MemoryModuleKnowledgeSynthesisPort:
    """Adapt MemoryModule without exposing recall-all or governance review."""

    def __init__(self, memory: MemoryModule, router: TenantMemoryRouter) -> None:
        self._memory = memory
        self._router = router

    async def read_selected_private(
        self,
        session: TenantSession,
        source_memory_ids: tuple[str, ...],
    ) -> tuple[MemoryItem, ...]:
        store = self._router.for_tenant(session.tenant_id)
        owners = [session.actor_id]
        if session.actor_kind is PrincipalKind.AGENT and session.subject_user_id is not None:
            owners.append(session.subject_user_id)
        items = await store.get_visible_private_items(tuple(owners), source_memory_ids)
        by_id = {item.id: item for item in items}
        if set(by_id) != set(source_memory_ids):
            raise PermissionError("One or more source Memory Items are not visible")
        # Preserve the explicit caller order for deterministic prompts and replay.
        return tuple(by_id[item_id] for item_id in source_memory_ids)

    async def read_tenant_knowledge(
        self,
        session: TenantSession,
    ) -> tuple[MemoryItem, ...]:
        return await self._router.for_tenant(session.tenant_id).list_tenant_knowledge()

    async def propose(
        self,
        session: TenantSession,
        command: ProposeKnowledge,
    ) -> KnowledgeCandidateView:
        return await self._memory.propose_knowledge(session, command)


class KnowledgeSynthesisCapability(AgentCapability[KnowledgeSynthesisInput]):
    name = "knowledge_synthesis"
    input_model = KnowledgeSynthesisInput

    def __init__(self, memory: KnowledgeSynthesisMemoryPort) -> None:
        self._memory = memory

    async def execute(
        self,
        context: AgentRunContext,
        command: KnowledgeSynthesisInput,
    ) -> KnowledgeSynthesisResult:
        return await execute_knowledge_synthesis_pack(context, command, self._memory)

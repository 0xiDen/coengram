"""Fail-closed server-derived routes to isolated Tenant stores."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TypeVar, overload
from urllib.parse import quote

from neo4j.exceptions import ServiceUnavailable
from neo4j_agent_memory import MemoryClient, MemorySettings, Neo4jConfig
from neo4j_agent_memory.config.settings import (
    EmbeddingConfig,
    EmbeddingProvider,
    ExtractionConfig,
    ExtractorType,
    MemoryConfig,
)
from neo4j_agent_memory.core.exceptions import ConnectionError as Neo4jConnectionError
from pydantic import SecretStr

from agent_memory_service.agents.persistence import (
    AgentRunClaim,
    AgentRunRepository,
    AgentRunSnapshot,
    PostgresAgentRunRepository,
)
from agent_memory_service.control import ControlModule, ControlNotFound, TenantRouteRecord
from agent_memory_service.durable_memory import (
    ImportAcceptance,
    PrivateMemoryCommand,
)
from agent_memory_service.governance import (
    GovernanceStore,
    KnowledgeCandidate,
    ProposeKnowledge,
    PublicationEvent,
    ReviewKnowledge,
)
from agent_memory_service.lifecycle import (
    CorrectMemory,
    ErasureRequest,
    ErasureTombstone,
    RequestErasure,
    ReviewErasure,
)
from agent_memory_service.models import (
    MemoryItem,
    PrincipalKind,
    PrivateMemoryInspection,
    RecallQuery,
    RetainMemory,
    TenantSession,
)
from agent_memory_service.stores.memory import TenantMemoryStore, TenantMemoryUnavailable
from agent_memory_service.stores.neo4j_memory import Neo4jTenantMemoryStore
from agent_memory_service.stores.postgres_governance import PostgresGovernanceStore

ResultT = TypeVar("ResultT")


class FileSecretReader:
    """Read a non-empty regular file beneath one configured secrets directory."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def read(self, relative_name: str) -> str:
        relative = Path(relative_name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Secret path must remain inside the configured directory")
        candidate = self._root.joinpath(relative)
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError("Secret path must identify a regular file")
        try:
            candidate.resolve().relative_to(self._root)
        except ValueError as exc:
            raise ValueError("Secret path must remain inside the configured directory") from exc
        value = candidate.read_text(encoding="utf-8").strip()
        if not value:
            raise ValueError("Secret file is empty")
        return value


class RoutedTenantMemoryRouter:
    """Cache immutable, lazy Bolt clients selected only by healthy Control routes."""

    def __init__(
        self,
        control: ControlModule,
        secrets: FileSecretReader,
        *,
        embedding_model: str,
        embedding_dimensions: int = 384,
    ) -> None:
        if not embedding_model.strip() or embedding_dimensions < 1:
            raise ValueError("Embedding model configuration is invalid")
        self._control = control
        self._secrets = secrets
        self._embedding_model = embedding_model
        self._embedding_dimensions = embedding_dimensions
        self._stores: dict[str, _LazyNeo4jTenantMemoryStore] = {}

    def for_tenant(self, tenant_id: str) -> TenantMemoryStore:
        try:
            route = self._control.resolve_tenant_route(tenant_id)
        except ControlNotFound as exc:
            raise TenantMemoryUnavailable("Tenant Memory Store is not available") from exc
        existing = self._stores.get(tenant_id)
        if existing is not None:
            if existing.route != route:
                raise TenantMemoryUnavailable("Tenant route changed; restart is required")
            return existing
        try:
            password = self._secrets.read(route.neo4j_secret_name)
        except ValueError as exc:
            raise TenantMemoryUnavailable("Tenant Memory credentials are not available") from exc
        store = _LazyNeo4jTenantMemoryStore(
            route,
            MemorySettings(
                neo4j=Neo4jConfig(
                    uri=f"bolt://{route.neo4j_service_address}",
                    username="neo4j",
                    password=SecretStr(password),
                    database="neo4j",
                ),
                embedding=EmbeddingConfig(
                    provider=EmbeddingProvider.SENTENCE_TRANSFORMERS,
                    model=self._embedding_model,
                    dimensions=self._embedding_dimensions,
                    device="cpu",
                ),
                llm=None,
                memory=MemoryConfig(multi_tenant=True),
                extraction=ExtractionConfig(
                    extractor_type=ExtractorType.NONE,
                    enable_llm_fallback=False,
                ),
            ),
        )
        self._stores[tenant_id] = store
        return store

    async def close(self) -> None:
        await asyncio.gather(*(store.close() for store in self._stores.values()))


class _LazyNeo4jTenantMemoryStore:
    def __init__(self, route: TenantRouteRecord, settings: MemorySettings) -> None:
        self.route = route
        self._settings = settings
        self._lock = asyncio.Lock()
        self._client: MemoryClient | None = None
        self._store: Neo4jTenantMemoryStore | None = None

    async def _connected(self) -> Neo4jTenantMemoryStore:
        if self._store is not None:
            return self._store
        async with self._lock:
            if self._store is not None:
                return self._store
            client = MemoryClient(self._settings)
            try:
                await client.connect()
            except (Neo4jConnectionError, ServiceUnavailable) as exc:
                raise TenantMemoryUnavailable("Tenant Memory Store is unavailable") from exc
            self._client = client
            self._store = Neo4jTenantMemoryStore(client)
            return self._store

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()

    async def retain_private(
        self,
        owner_principal_id: str,
        command: RetainMemory,
        *,
        actor_id: str | None = None,
    ) -> MemoryItem:
        return await (await self._connected()).retain_private(
            owner_principal_id, command, actor_id=actor_id
        )

    async def recall(
        self,
        private_owner_ids: tuple[str, ...],
        query: RecallQuery,
    ) -> tuple[MemoryItem, ...]:
        return await (await self._connected()).recall(private_owner_ids, query)

    async def get_visible_private_items(
        self,
        private_owner_ids: tuple[str, ...],
        item_ids: tuple[str, ...],
    ) -> tuple[MemoryItem, ...]:
        return await (await self._connected()).get_visible_private_items(
            private_owner_ids, item_ids
        )

    async def publish_tenant_knowledge(
        self,
        candidate_id: str,
        claim: str,
        confidence: float,
        proposer_id: str,
    ) -> MemoryItem:
        return await (await self._connected()).publish_tenant_knowledge(
            candidate_id, claim, confidence, proposer_id
        )

    async def correct_private(
        self,
        owner_principal_id: str,
        command: CorrectMemory,
        *,
        actor_id: str | None = None,
    ) -> MemoryItem:
        return await (await self._connected()).correct_private(
            owner_principal_id, command, actor_id=actor_id
        )

    async def erase_private(self, owner_principal_id: str, memory_id: str) -> bool:
        return await (await self._connected()).erase_private(owner_principal_id, memory_id)

    async def list_private(self, owner_principal_id: str) -> tuple[MemoryItem, ...]:
        return await (await self._connected()).list_private(owner_principal_id)

    async def list_tenant_knowledge(self) -> tuple[MemoryItem, ...]:
        return await (await self._connected()).list_tenant_knowledge()

    async def import_private(self, owner_principal_id: str, item: MemoryItem) -> bool:
        return await (await self._connected()).import_private(owner_principal_id, item)

    async def apply_private_item(self, owner_principal_id: str, item: MemoryItem) -> bool:
        return await (await self._connected()).apply_private_item(owner_principal_id, item)


class RoutedGovernanceStore(GovernanceStore):
    """Route governance and erasure to the Tenant's separate PostgreSQL database."""

    def __init__(
        self,
        control: ControlModule,
        secrets: FileSecretReader,
        *,
        postgres_host: str,
        postgres_port: int = 5432,
    ) -> None:
        self._control = control
        self._secrets = secrets
        self._host = postgres_host
        self._port = postgres_port
        self._stores: dict[str, PostgresGovernanceStore] = {}
        self._database_urls: dict[str, str] = {}

    def for_tenant(self, tenant_id: str) -> PostgresGovernanceStore:
        database_url = self.database_url_for_tenant(tenant_id)
        if existing := self._stores.get(tenant_id):
            if self._database_urls[tenant_id] != database_url:
                raise RuntimeError("Tenant database route changed; restart is required")
            return existing
        store = PostgresGovernanceStore(database_url)
        self._stores[tenant_id] = store
        self._database_urls[tenant_id] = database_url
        return store

    def database_url_for_tenant(self, tenant_id: str) -> str:
        route = self._control.resolve_tenant_route(tenant_id)
        password = self._secrets.read(f"{tenant_id}/postgres_password")
        return (
            f"postgresql://{quote(route.tenant_database_role, safe='')}:"
            f"{quote(password, safe='')}@{self._host}:{self._port}/"
            f"{quote(route.tenant_database_name, safe='')}"
        )

    async def propose(
        self, tenant_id: str, proposer_id: str, command: ProposeKnowledge
    ) -> KnowledgeCandidate:
        return await self.for_tenant(tenant_id).propose(tenant_id, proposer_id, command)

    async def get_candidate(self, tenant_id: str, candidate_id: str) -> KnowledgeCandidate | None:
        return await self.for_tenant(tenant_id).get_candidate(tenant_id, candidate_id)

    async def list_candidates(self, tenant_id: str) -> tuple[KnowledgeCandidate, ...]:
        return await self.for_tenant(tenant_id).list_candidates(tenant_id)

    @overload
    async def review(
        self, tenant_id: str, reviewer_id: str, command: ReviewKnowledge
    ) -> KnowledgeCandidate: ...

    @overload
    async def review(
        self, tenant_id: str, reviewer_id: str, command: ReviewErasure
    ) -> ErasureRequest: ...

    async def review(
        self,
        tenant_id: str,
        reviewer_id: str,
        command: ReviewKnowledge | ReviewErasure,
    ) -> KnowledgeCandidate | ErasureRequest:
        return await self.for_tenant(tenant_id).review(tenant_id, reviewer_id, command)

    async def next_publication(self, tenant_id: str) -> PublicationEvent | None:
        return await self.for_tenant(tenant_id).next_publication(tenant_id)

    async def mark_published(self, tenant_id: str, candidate_id: str) -> KnowledgeCandidate:
        return await self.for_tenant(tenant_id).mark_published(tenant_id, candidate_id)

    async def import_candidate(
        self,
        tenant_id: str,
        proposer_id: str,
        candidate_id: str,
        claim: str,
        confidence: float,
    ) -> KnowledgeCandidate:
        return await self.for_tenant(tenant_id).import_candidate(
            tenant_id, proposer_id, candidate_id, claim, confidence
        )

    async def request(
        self,
        tenant_id: str,
        requester_id: str,
        owner_principal_id: str,
        command: RequestErasure,
    ) -> ErasureRequest:
        return await self.for_tenant(tenant_id).request(
            tenant_id, requester_id, owner_principal_id, command
        )

    async def get_request(self, tenant_id: str, request_id: str) -> ErasureRequest | None:
        return await self.for_tenant(tenant_id).get_request(tenant_id, request_id)

    async def next_approved(self, tenant_id: str) -> ErasureRequest | None:
        return await self.for_tenant(tenant_id).next_approved(tenant_id)

    async def mark_completed(self, tenant_id: str, request_id: str) -> ErasureRequest:
        return await self.for_tenant(tenant_id).mark_completed(tenant_id, request_id)

    async def list_completed(
        self, tenant_id: str, owner_principal_id: str
    ) -> tuple[ErasureTombstone, ...]:
        return await self.for_tenant(tenant_id).list_completed(tenant_id, owner_principal_id)

    async def import_tombstone(
        self,
        tenant_id: str,
        owner_principal_id: str,
        tombstone: ErasureTombstone,
    ) -> bool:
        return await self.for_tenant(tenant_id).import_tombstone(
            tenant_id,
            owner_principal_id,
            tombstone,
        )

    async def accept_retain(
        self,
        tenant_id: str,
        actor_id: str,
        owner_principal_id: str,
        command: RetainMemory,
    ) -> MemoryItem:
        return await self.for_tenant(tenant_id).accept_retain(
            tenant_id, actor_id, owner_principal_id, command
        )

    async def accept_correction(
        self,
        tenant_id: str,
        actor_id: str,
        owner_principal_id: str,
        command: CorrectMemory,
    ) -> MemoryItem:
        return await self.for_tenant(tenant_id).accept_correction(
            tenant_id, actor_id, owner_principal_id, command
        )

    async def accept_import(
        self,
        tenant_id: str,
        actor_id: str,
        owner_principal_id: str,
        item: MemoryItem,
    ) -> ImportAcceptance:
        return await self.for_tenant(tenant_id).accept_import(
            tenant_id, actor_id, owner_principal_id, item
        )

    async def get_memory_command(
        self,
        tenant_id: str,
        command_id: str,
    ) -> PrivateMemoryCommand | None:
        return await self.for_tenant(tenant_id).get_memory_command(tenant_id, command_id)

    async def get_memory_command_by_result(
        self,
        tenant_id: str,
        result_memory_id: str,
    ) -> PrivateMemoryCommand | None:
        return await self.for_tenant(tenant_id).get_memory_command_by_result(
            tenant_id,
            result_memory_id,
        )

    async def mark_memory_command_applied(
        self,
        tenant_id: str,
        command_id: str,
    ) -> PrivateMemoryCommand:
        return await self.for_tenant(tenant_id).mark_memory_command_applied(tenant_id, command_id)

    async def list_private_memory_state(
        self,
        tenant_id: str,
        owner_principal_id: str,
    ) -> tuple[PrivateMemoryInspection, ...]:
        return await self.for_tenant(tenant_id).list_private_memory_state(
            tenant_id,
            owner_principal_id,
        )

    async def list_private_memory_items(
        self,
        tenant_id: str,
        owner_principal_id: str,
    ) -> tuple[MemoryItem, ...]:
        return await self.for_tenant(tenant_id).list_private_memory_items(
            tenant_id,
            owner_principal_id,
        )


class RoutedAgentRunRepository(AgentRunRepository):
    """Persist Agent Runs in the same exact Tenant Operations Store route."""

    def __init__(self, governance: RoutedGovernanceStore) -> None:
        self._governance = governance
        self._stores: dict[str, PostgresAgentRunRepository] = {}
        self._database_urls: dict[str, str] = {}

    def for_tenant(self, tenant_id: str) -> PostgresAgentRunRepository:
        database_url = self._governance.database_url_for_tenant(tenant_id)
        if existing := self._stores.get(tenant_id):
            if self._database_urls[tenant_id] != database_url:
                raise RuntimeError("Tenant database route changed; restart is required")
            return existing
        store = PostgresAgentRunRepository(database_url)
        self._stores[tenant_id] = store
        self._database_urls[tenant_id] = database_url
        return store

    def save(
        self,
        snapshot: AgentRunSnapshot,
        *,
        claim: AgentRunClaim | None = None,
    ) -> None:
        self.for_tenant(snapshot.tenant_id).save(snapshot, claim=claim)

    def create_or_get(self, snapshot: AgentRunSnapshot) -> AgentRunSnapshot:
        return self.for_tenant(snapshot.tenant_id).create_or_get(snapshot)

    def get(self, tenant_id: str, run_id: str) -> AgentRunSnapshot | None:
        return self.for_tenant(tenant_id).get(tenant_id, run_id)

    def request_cancellation(
        self,
        session: TenantSession,
        run_id: str,
    ) -> AgentRunSnapshot | None:
        return self.for_tenant(session.tenant_id).request_cancellation(session, run_id)

    def get_by_idempotency(
        self,
        tenant_id: str,
        actor_id: str,
        idempotency_key: str,
        *,
        actor_kind: PrincipalKind,
        subject_user_id: str | None = None,
        delegation_id: str | None = None,
    ) -> AgentRunSnapshot | None:
        return self.for_tenant(tenant_id).get_by_idempotency(
            tenant_id,
            actor_id,
            idempotency_key,
            actor_kind=actor_kind,
            subject_user_id=subject_user_id,
            delegation_id=delegation_id,
        )

    def claim_run(
        self,
        tenant_id: str,
        run_id: str,
        *,
        lease_seconds: int,
    ) -> AgentRunClaim | None:
        return self.for_tenant(tenant_id).claim_run(
            tenant_id,
            run_id,
            lease_seconds=lease_seconds,
        )

    def claim_runnable(
        self,
        tenant_id: str,
        *,
        lease_seconds: int,
    ) -> AgentRunClaim | None:
        return self.for_tenant(tenant_id).claim_runnable(
            tenant_id,
            lease_seconds=lease_seconds,
        )

    def list_runnable(self, tenant_id: str, *, limit: int = 100) -> tuple[str, ...]:
        return self.for_tenant(tenant_id).list_runnable(tenant_id, limit=limit)

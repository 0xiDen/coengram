"""Real PostgreSQL persistence contract for Agent Runs and ActiveGraph events."""

from __future__ import annotations

import asyncio
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from activegraph import Runtime as ActiveGraphRuntime  # type: ignore[import-untyped]
from activegraph.store.postgres import PostgresEventStore  # type: ignore[import-untyped]
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict

from agent_memory_service.agents import (
    AgentCapability,
    AgentInvocation,
    AgentRunClaim,
    AgentRunContext,
    AgentRunLeaseLost,
    AgentRunSnapshot,
    AgentRunState,
    AgentRuntimeModule,
    KnowledgeSynthesisCapability,
    ManagedActiveGraphStoreFactory,
    MemoryModuleKnowledgeSynthesisPort,
    PostgresAgentRunRepository,
    RecordedCompletion,
    RecordedProvider,
)
from agent_memory_service.governance import InMemoryGovernanceStore
from agent_memory_service.memory import MemoryModule
from agent_memory_service.models import PrincipalKind, RetainMemory, TenantSession
from agent_memory_service.stores.memory import InMemoryTenantMemoryRouter

TENANT_DATABASE_URL = os.environ.get("TENANT_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TENANT_DATABASE_URL,
    reason="TENANT_DATABASE_URL is required for Agent persistence integration tests",
)


class _PostgresReadRaceRepository(PostgresAgentRunRepository):
    """Hold concurrent legacy reads open so the database must arbitrate creation."""

    def __init__(self, database_url: str, parties: int) -> None:
        super().__init__(database_url)
        self._reads = threading.Barrier(parties)

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
        snapshot = super().get_by_idempotency(
            tenant_id,
            actor_id,
            idempotency_key,
            actor_kind=actor_kind,
            subject_user_id=subject_user_id,
            delegation_id=delegation_id,
        )
        self._reads.wait(timeout=10)
        return snapshot


class _ConcurrentPostgresEventStore(PostgresEventStore):  # type: ignore[misc]
    def __init__(self, database_url: str, run_id: str, appends: threading.Barrier) -> None:
        super().__init__(database_url, run_id)
        self._appends = appends

    def append(self, event: Any) -> None:
        self._appends.wait(timeout=10)
        super().append(event)


class _FailOncePostgresEventStore(PostgresEventStore):  # type: ignore[misc]
    def __init__(
        self,
        database_url: str,
        run_id: str,
        failure_state: list[bool],
        failure_lock: threading.Lock,
    ) -> None:
        super().__init__(database_url, run_id)
        self._failure_state = failure_state
        self._failure_lock = failure_lock

    def append(self, event: Any) -> None:
        with self._failure_lock:
            if self._failure_state[0]:
                self._failure_state[0] = False
                raise RuntimeError("simulated ActiveGraph interruption")
        super().append(event)


class _EchoInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str


class _EchoOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    answer: str


class _EchoCapability(AgentCapability[_EchoInput]):
    name = "echo"
    input_model = _EchoInput

    async def execute(
        self,
        context: AgentRunContext,
        command: _EchoInput,
    ) -> _EchoOutput:
        return context.complete(
            system="Return a typed echo.",
            user=command.text,
            output_schema=_EchoOutput,
        )


def _knowledge_capability(tenant_id: str) -> KnowledgeSynthesisCapability:
    router = InMemoryTenantMemoryRouter([tenant_id])
    memory = MemoryModule(router, InMemoryGovernanceStore())
    return KnowledgeSynthesisCapability(MemoryModuleKnowledgeSynthesisPort(memory, router))


def _session(tenant_id: str, actor_id: str = "user-alice") -> TenantSession:
    return TenantSession(
        tenant_id=tenant_id,
        actor_id=actor_id,
        actor_kind=PrincipalKind.USER,
        roles=frozenset({"tenant_member"}),
    )


def _invocation(idempotency_key: str) -> AgentInvocation:
    return AgentInvocation(
        capability="knowledge_synthesis",
        input={"request": "Distill retries", "source_memory_ids": ["source-memory"]},
        idempotency_key=idempotency_key,
    )


def _echo_invocation(idempotency_key: str) -> AgentInvocation:
    return AgentInvocation(
        capability="echo",
        input={"text": "execute once"},
        idempotency_key=idempotency_key,
    )


def _echo_provider() -> RecordedProvider:
    return RecordedProvider(
        [
            RecordedCompletion(
                parsed={"answer": "done"},
                raw_text='{"answer":"done"}',
                cost_usd=Decimal("0.001"),
            )
        ]
    )


@pytest.fixture(scope="module", autouse=True)
def migrated_tenant_store() -> None:
    if not TENANT_DATABASE_URL:
        return
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[2]
    command.upgrade(Config(root / "alembic-tenant.ini"), "head")


@pytest.mark.asyncio
async def test_postgres_agent_runs_are_scoped_to_delegated_subject_context() -> None:
    assert TENANT_DATABASE_URL is not None
    tenant_id = f"tenant-{uuid4().hex}"
    repository = PostgresAgentRunRepository(TENANT_DATABASE_URL)
    runtime = AgentRuntimeModule(
        capabilities=(_EchoCapability(),),
        provider=RecordedProvider([]),
        repository=repository,
    )
    sessions = tuple(
        TenantSession(
            tenant_id=tenant_id,
            actor_id="agent-shared",
            actor_kind=PrincipalKind.AGENT,
            roles=frozenset({"tenant_member"}),
            subject_user_id=subject_user_id,
            delegation_id=delegation_id,
        )
        for subject_user_id, delegation_id in (
            ("user-alice", "delegation-alice"),
            ("user-bob", "delegation-bob"),
        )
    )
    invocation = _echo_invocation("same-delegated-key")

    alice_run = await runtime.start_agent_run(sessions[0], invocation)
    bob_run = await runtime.start_agent_run(sessions[1], invocation)

    assert alice_run.id != bob_run.id
    assert runtime.status(sessions[0], alice_run.id).id == alice_run.id
    with pytest.raises(LookupError, match="Agent Run not found"):
        runtime.status(sessions[1], alice_run.id)
    with pytest.raises(LookupError, match="Agent Run not found"):
        runtime.cancel(sessions[1], alice_run.id)
    for session, run in zip(sessions, (alice_run, bob_run), strict=True):
        persisted = repository.get_by_idempotency(
            tenant_id,
            session.actor_id,
            invocation.idempotency_key,
            actor_kind=session.actor_kind,
            subject_user_id=session.subject_user_id,
            delegation_id=session.delegation_id,
        )
        assert persisted is not None
        assert persisted.run_id == run.id


@pytest.mark.asyncio
async def test_postgres_concurrent_workers_claim_and_execute_one_run_once() -> None:
    assert TENANT_DATABASE_URL is not None
    tenant_id = f"tenant-{uuid4().hex}"
    repository = PostgresAgentRunRepository(TENANT_DATABASE_URL)
    gateway = AgentRuntimeModule(
        capabilities=(_EchoCapability(),),
        provider=RecordedProvider([]),
        repository=repository,
    )
    started = await gateway.start_agent_run(
        _session(tenant_id),
        _echo_invocation("postgres-worker-claim-once"),
    )
    contenders = threading.Barrier(2)

    def claim(_index: int) -> AgentRunClaim | None:
        contenders.wait(timeout=10)
        return PostgresAgentRunRepository(TENANT_DATABASE_URL).claim_runnable(
            tenant_id,
            lease_seconds=60,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = tuple(executor.map(claim, range(2)))

    claimed = tuple(item for item in claims if item is not None)
    providers = (_echo_provider(), _echo_provider())
    workers = tuple(
        AgentRuntimeModule(
            capabilities=(_EchoCapability(),),
            provider=provider,
            repository=PostgresAgentRunRepository(TENANT_DATABASE_URL),
        )
        for provider in providers
    )

    assert len(claimed) == 1
    completed = await workers[0].run_claimed(claimed[0])
    assert completed.id == started.id
    assert completed.state is AgentRunState.COMPLETED
    assert sum(len(provider.calls) for provider in providers) == 1


@pytest.mark.asyncio
async def test_postgres_expired_worker_lease_recovers_and_fences_stale_owner() -> None:
    assert TENANT_DATABASE_URL is not None
    tenant_id = f"tenant-{uuid4().hex}"
    repository = PostgresAgentRunRepository(TENANT_DATABASE_URL)
    gateway = AgentRuntimeModule(
        capabilities=(_EchoCapability(),),
        provider=RecordedProvider([]),
        repository=repository,
    )
    started = await gateway.start_agent_run(
        _session(tenant_id),
        _echo_invocation("postgres-expired-worker-lease"),
    )
    stale_claim = repository.claim_runnable(tenant_id, lease_seconds=1)
    assert stale_claim is not None

    await asyncio.sleep(1.1)
    recovered_claim = repository.claim_runnable(tenant_id, lease_seconds=60)
    assert recovered_claim is not None
    assert recovered_claim.run_id == started.id
    assert recovered_claim.lease_version == stale_claim.lease_version + 1

    stale_running = stale_claim.snapshot.model_copy(update={"state": AgentRunState.RUNNING})
    with pytest.raises(AgentRunLeaseLost, match="lease is no longer owned"):
        repository.save(stale_running, claim=stale_claim)

    provider = _echo_provider()
    worker = AgentRuntimeModule(
        capabilities=(_EchoCapability(),),
        provider=provider,
        repository=repository,
    )
    completed = await worker.run_claimed(recovered_claim)

    assert completed.state is AgentRunState.COMPLETED
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_worker_runtime_resumes_postgres_run_and_rejects_native_divergence() -> None:
    assert TENANT_DATABASE_URL is not None
    tenant_id = f"tenant-{uuid4().hex}"
    session = TenantSession(
        tenant_id=tenant_id,
        actor_id="user-alice",
        actor_kind=PrincipalKind.USER,
        roles=frozenset({"tenant_member"}),
    )
    router = InMemoryTenantMemoryRouter([tenant_id])
    memory = MemoryModule(router, InMemoryGovernanceStore())
    source = await memory.retain(
        session,
        RetainMemory(content="Use bounded jittered retries.", idempotency_key="agent-pg-source"),
    )
    repository = PostgresAgentRunRepository(TENANT_DATABASE_URL)
    capability = KnowledgeSynthesisCapability(MemoryModuleKnowledgeSynthesisPort(memory, router))
    gateway = AgentRuntimeModule(
        capabilities=(capability,),
        provider=RecordedProvider([]),
        repository=repository,
    )
    started = await gateway.start_agent_run(
        session,
        AgentInvocation(
            capability="knowledge_synthesis",
            input={"request": "Distill retries", "source_memory_ids": [source.id]},
            idempotency_key="agent-pg-run",
        ),
    )
    worker = AgentRuntimeModule(
        capabilities=(capability,),
        provider=RecordedProvider(
            [
                RecordedCompletion(
                    parsed={
                        "claim": "Use bounded jittered retries.",
                        "confidence": 0.95,
                        "duplicate_memory_ids": [],
                        "conflicting_memory_ids": [],
                    },
                    cost_usd=Decimal("0.001"),
                )
            ]
        ),
        repository=repository,
        activegraph_store_factory=lambda _session, run_id: PostgresEventStore(
            TENANT_DATABASE_URL, run_id
        ),
    )

    completed = await worker.run_pending(tenant_id, started.id)
    native_store = PostgresEventStore(TENANT_DATABASE_URL, started.id)
    native_events = list(native_store.iter_events())
    application_events = [
        event for event in native_events if "agent_memory_run_event" in event.payload
    ]
    loaded = ActiveGraphRuntime.load(TENANT_DATABASE_URL, run_id=started.id)
    observer = AgentRuntimeModule(
        capabilities=(capability,),
        provider=RecordedProvider([]),
        repository=repository,
        activegraph_store_factory=lambda _session, run_id: PostgresEventStore(
            TENANT_DATABASE_URL, run_id
        ),
    )

    assert completed.state is AgentRunState.COMPLETED
    assert observer.status(session, started.id) == completed
    assert observer.replay(session, started.id) == completed
    assert [event.type for event in application_events] == [
        event.type for event in completed.events
    ]
    assert application_events[-1].type == "agent.run.completed"
    assert "knowledge_synthesis.requested" in {event.type for event in native_events}
    assert loaded.graph.events == native_events
    assert loaded.graph.replayed_ids == frozenset(event.id for event in native_events)
    assert native_store.get_run() is not None
    native_store.close()
    assert loaded.graph.store is not None
    loaded.graph.store.close()

    with psycopg.connect(TENANT_DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE memory.agent_runs
                SET state = 'queued',
                    snapshot = snapshot || %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE tenant_id = %s AND run_id = %s
                """,
                (
                    Jsonb(
                        {
                            "state": "queued",
                            "model_calls": 0,
                            "tool_calls": 0,
                            "cost_usd": "0",
                            "events": [],
                            "output": None,
                            "failure": None,
                            "resumable": False,
                        }
                    ),
                    tenant_id,
                    started.id,
                ),
            )
    divergent_observer = AgentRuntimeModule(
        capabilities=(capability,),
        provider=RecordedProvider([]),
        repository=repository,
        activegraph_store_factory=lambda _session, run_id: PostgresEventStore(
            TENANT_DATABASE_URL, run_id
        ),
    )
    with pytest.raises(ValueError, match="diverges"):
        divergent_observer.status(session, started.id)
    with pytest.raises(ValueError, match="diverges"):
        divergent_observer.replay(session, started.id)


def test_concurrent_postgres_invocations_create_and_project_one_actor_scoped_run() -> None:
    assert TENANT_DATABASE_URL is not None
    tenant_id = f"tenant-{uuid4().hex}"
    session = _session(tenant_id)
    invocation = _invocation("agent-pg-concurrent")
    repository = _PostgresReadRaceRepository(TENANT_DATABASE_URL, parties=2)
    appends = threading.Barrier(2)
    stores: list[_ConcurrentPostgresEventStore] = []
    stores_lock = threading.Lock()

    def store_factory(
        _session: TenantSession,
        run_id: str,
    ) -> _ConcurrentPostgresEventStore:
        store = _ConcurrentPostgresEventStore(TENANT_DATABASE_URL, run_id, appends)
        with stores_lock:
            stores.append(store)
        return store

    def start(_index: int) -> str:
        runtime = AgentRuntimeModule(
            capabilities=(_knowledge_capability(tenant_id),),
            provider=RecordedProvider([]),
            repository=repository,
            activegraph_store_factory=store_factory,
        )
        return asyncio.run(runtime.start_agent_run(session, invocation)).id

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            run_ids = tuple(executor.map(start, range(2)))

        assert run_ids[0] == run_ids[1]
        assert repository.list_runnable(tenant_id) == (run_ids[0],)
        native_store = PostgresEventStore(TENANT_DATABASE_URL, run_ids[0])
        try:
            application_events = [
                event
                for event in native_store.iter_events()
                if "agent_memory_run_event" in event.payload
            ]
            assert [event.type for event in application_events] == ["agent.run.queued"]
        finally:
            native_store.close()

        bob = _session(tenant_id, actor_id="user-bob")
        bob_runtime = AgentRuntimeModule(
            capabilities=(_knowledge_capability(tenant_id),),
            provider=RecordedProvider([]),
            repository=repository,
        )
        bob_run = asyncio.run(bob_runtime.start_agent_run(bob, invocation))
        assert bob_run.id != run_ids[0]
    finally:
        for store in stores:
            store.close()


@pytest.mark.asyncio
async def test_postgres_projection_interruption_leaves_a_retryable_queued_run() -> None:
    assert TENANT_DATABASE_URL is not None
    tenant_id = f"tenant-{uuid4().hex}"
    session = _session(tenant_id)
    invocation = _invocation("agent-pg-projection-interruption")
    repository = PostgresAgentRunRepository(TENANT_DATABASE_URL)
    failure_state = [True]
    failure_lock = threading.Lock()
    failed_stores: list[_FailOncePostgresEventStore] = []

    def failing_store_factory(
        _session: TenantSession,
        run_id: str,
    ) -> _FailOncePostgresEventStore:
        store = _FailOncePostgresEventStore(
            TENANT_DATABASE_URL,
            run_id,
            failure_state,
            failure_lock,
        )
        failed_stores.append(store)
        return store

    gateway = AgentRuntimeModule(
        capabilities=(_knowledge_capability(tenant_id),),
        provider=RecordedProvider([]),
        repository=repository,
        activegraph_store_factory=failing_store_factory,
    )
    try:
        with pytest.raises(RuntimeError, match="simulated ActiveGraph interruption"):
            await gateway.start_agent_run(session, invocation)

        persisted = repository.get_by_idempotency(
            tenant_id,
            session.actor_id,
            invocation.idempotency_key,
            actor_kind=session.actor_kind,
            subject_user_id=session.subject_user_id,
            delegation_id=session.delegation_id,
        )
        assert persisted is not None
        assert persisted.state is AgentRunState.QUEUED
        assert repository.list_runnable(tenant_id) == (persisted.run_id,)

        retry = AgentRuntimeModule(
            capabilities=(_knowledge_capability(tenant_id),),
            provider=RecordedProvider([]),
            repository=repository,
            activegraph_store_factory=lambda _session, run_id: PostgresEventStore(
                TENANT_DATABASE_URL,
                run_id,
            ),
        )
        retried = await retry.start_agent_run(session, invocation)
        native_store = PostgresEventStore(TENANT_DATABASE_URL, persisted.run_id)
        try:
            application_events = [
                event
                for event in native_store.iter_events()
                if "agent_memory_run_event" in event.payload
            ]
        finally:
            native_store.close()

        assert retried.id == persisted.run_id
        assert [event.type for event in application_events] == ["agent.run.queued"]
    finally:
        for store in failed_stores:
            store.close()


@pytest.mark.asyncio
async def test_managed_activegraph_pool_bounds_repeated_run_connections() -> None:
    assert TENANT_DATABASE_URL is not None
    tenant_id = f"tenant-{uuid4().hex}"
    application_name = f"coengram-pool-{uuid4().hex}"
    separator = "&" if "?" in TENANT_DATABASE_URL else "?"
    pool_database_url = f"{TENANT_DATABASE_URL}{separator}application_name={application_name}"

    def database_url_for_tenant(selected_tenant: str) -> str:
        if selected_tenant != tenant_id:
            raise LookupError("unexpected Tenant")
        return pool_database_url

    stores = ManagedActiveGraphStoreFactory(
        database_url_for_tenant,
        max_size=2,
    )
    completions = [
        RecordedCompletion(
            parsed={"answer": "done"},
            raw_text='{"answer":"done"}',
            cost_usd=Decimal("0.001"),
        )
        for _ in range(25)
    ]
    runtime = AgentRuntimeModule(
        capabilities=(_EchoCapability(),),
        provider=RecordedProvider(completions),
        repository=PostgresAgentRunRepository(TENANT_DATABASE_URL),
        activegraph_store_factory=stores,
    )
    session = _session(tenant_id)

    try:
        for index in range(25):
            started = await runtime.start_agent_run(
                session,
                _echo_invocation(f"managed-pool-{index}"),
            )
            completed = await runtime.run_until_terminal(session, started.id)
            assert completed.state is AgentRunState.COMPLETED

        with psycopg.connect(TENANT_DATABASE_URL) as connection:
            row = connection.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE application_name = %s",
                (application_name,),
            ).fetchone()
        assert row is not None
        assert 0 < int(row[0]) <= 2
    finally:
        stores.close()

    with psycopg.connect(TENANT_DATABASE_URL) as connection:
        row = connection.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE application_name = %s",
            (application_name,),
        ).fetchone()
    assert row is not None
    assert int(row[0]) == 0
    with pytest.raises(RuntimeError, match="closed"):
        stores(session, "after-close")

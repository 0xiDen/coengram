from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from activegraph.store.memory import InMemoryEventStore  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict

from agent_memory_service.agents import (
    AgentBudget,
    AgentCapability,
    AgentInvocation,
    AgentModelSettings,
    AgentRunClaim,
    AgentRunContext,
    AgentRunLeaseLost,
    AgentRunSnapshot,
    AgentRunState,
    AgentRuntimeModule,
    InMemoryAgentRunRepository,
    LangChainAnthropicProvider,
    RecordedCompletion,
    RecordedProvider,
)
from agent_memory_service.models import PrincipalKind, TenantSession


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


class _GreedyCapability(AgentCapability[_EchoInput]):
    name = "greedy"
    input_model = _EchoInput

    async def execute(
        self,
        context: AgentRunContext,
        command: _EchoInput,
    ) -> _EchoOutput:
        result: _EchoOutput | None = None
        for _ in range(4):
            result = context.complete(
                system="Return a typed echo.",
                user=command.text,
                output_schema=_EchoOutput,
            )
        assert result is not None
        return result


class _BlockingCapability(AgentCapability[_EchoInput]):
    name = "blocking"
    input_model = _EchoInput

    def __init__(self, entered: asyncio.Event, release: asyncio.Event) -> None:
        self._entered = entered
        self._release = release

    async def execute(
        self,
        context: AgentRunContext,
        command: _EchoInput,
    ) -> _EchoOutput:
        async def wait_for_host() -> str:
            self._entered.set()
            await self._release.wait()
            return "released"

        await context.tool("blocking_read", wait_for_host)
        return context.complete(
            system="Return a typed echo.",
            user=command.text,
            output_schema=_EchoOutput,
        )


class _RawBlockingCapability(AgentCapability[_EchoInput]):
    """Model uninstrumented cooperative work returning after cancellation races."""

    name = "raw_blocking"
    input_model = _EchoInput

    def __init__(self, entered: asyncio.Event, release: asyncio.Event) -> None:
        self._entered = entered
        self._release = release

    async def execute(
        self,
        context: AgentRunContext,
        command: _EchoInput,
    ) -> _EchoOutput:
        del context
        self._entered.set()
        await self._release.wait()
        return _EchoOutput(answer=command.text)


class _RawFailingBlockingCapability(_RawBlockingCapability):
    name = "raw_failing_blocking"

    async def execute(
        self,
        context: AgentRunContext,
        command: _EchoInput,
    ) -> _EchoOutput:
        await super().execute(context, command)
        raise RuntimeError("failure after cancellation")


class _ToolGreedyCapability(AgentCapability[_EchoInput]):
    name = "tool_greedy"
    input_model = _EchoInput

    async def execute(
        self,
        context: AgentRunContext,
        command: _EchoInput,
    ) -> _EchoOutput:
        async def read() -> str:
            return command.text

        await context.tool("read_one", read)
        await context.tool("read_two", read)
        return _EchoOutput(answer="unreachable")


class _FailingCapability(AgentCapability[_EchoInput]):
    name = "failing"
    input_model = _EchoInput

    async def execute(
        self,
        context: AgentRunContext,
        command: _EchoInput,
    ) -> _EchoOutput:
        del context, command
        raise RuntimeError("recorded failure")


class _AdvancingCapability(AgentCapability[_EchoInput]):
    name = "advancing"
    input_model = _EchoInput

    def __init__(self, advance: Callable[[], None]) -> None:
        self._advance = advance

    async def execute(
        self,
        context: AgentRunContext,
        command: _EchoInput,
    ) -> _EchoOutput:
        async def advance_time() -> str:
            self._advance()
            return command.text

        await context.tool("advance_time", advance_time)
        return _EchoOutput(answer="unreachable")


def _session(
    tenant_id: str = "tenant-a",
    *,
    actor_id: str = "user-1",
) -> TenantSession:
    return TenantSession(
        tenant_id=tenant_id,
        actor_id=actor_id,
        actor_kind=PrincipalKind.USER,
        roles=frozenset({"member"}),
    )


def _provider(count: int = 1) -> RecordedProvider:
    return RecordedProvider(
        [
            RecordedCompletion(
                parsed={"answer": "done"},
                raw_text='{"answer":"done"}',
                input_tokens=10,
                output_tokens=2,
                cost_usd=Decimal("0.001"),
            )
            for _ in range(count)
        ]
    )


class _ReadRaceRepository(InMemoryAgentRunRepository):
    """Force the former read-before-create implementation through its race window."""

    def __init__(self) -> None:
        super().__init__()
        self._reads = threading.Barrier(2)

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
        self._reads.wait(timeout=5)
        return snapshot


class _FailOnceEventStore(InMemoryEventStore):  # type: ignore[misc]
    def __init__(self, run_id: str) -> None:
        super().__init__(run_id)
        self.failed = False

    def append(self, event: Any) -> None:
        if not self.failed:
            self.failed = True
            raise RuntimeError("simulated ActiveGraph interruption")
        super().append(event)


class _ConcurrentProjectionStore(InMemoryEventStore):  # type: ignore[misc]
    def __init__(self, run_id: str) -> None:
        super().__init__(run_id)
        self._appends = threading.Barrier(2)

    def append(self, event: Any) -> None:
        self._appends.wait(timeout=5)
        super().append(event)


@pytest.mark.asyncio
async def test_public_run_status_and_replay_are_tenant_scoped_and_idempotent() -> None:
    provider = _provider()
    runtime = AgentRuntimeModule(capabilities=(_EchoCapability(),), provider=provider)
    invocation = AgentInvocation(
        capability="echo",
        input={"text": "hello"},
        idempotency_key="invoke-1",
    )

    started = await runtime.start_agent_run(_session(), invocation)
    duplicate = await runtime.start_agent_run(_session(), invocation)
    completed = await runtime.run_until_terminal(_session(), started.id)

    assert duplicate.id == started.id
    assert started.state is AgentRunState.QUEUED
    assert completed.state is AgentRunState.COMPLETED
    assert completed.output == {"answer": "done"}
    assert completed.budget == AgentBudget()
    assert completed.settings == AgentModelSettings(
        provider="recorded",
        adapter="recorded",
    )
    assert completed.usage.model_calls == 1
    assert runtime.status(_session(), completed.id) == completed
    assert runtime.replay(_session(), completed.id) == completed
    assert len(provider.calls) == 1

    with pytest.raises(LookupError):
        runtime.status(_session("tenant-b"), completed.id)


@pytest.mark.asyncio
async def test_repeated_terminal_runs_do_not_accumulate_runtime_records() -> None:
    stores: dict[str, InMemoryEventStore] = {}
    runtime = AgentRuntimeModule(
        capabilities=(_EchoCapability(),),
        provider=_provider(count=50),
        activegraph_store_factory=lambda _session, run_id: stores.setdefault(
            run_id,
            InMemoryEventStore(run_id),
        ),
    )

    for index in range(50):
        started = await runtime.start_agent_run(
            _session(),
            AgentInvocation(
                capability="echo",
                input={"text": f"run-{index}"},
                idempotency_key=f"bounded-runtime-{index}",
            ),
        )
        completed = await runtime.run_until_terminal(_session(), started.id)
        assert completed.state is AgentRunState.COMPLETED
        assert runtime._runs == {}  # noqa: SLF001 - lifecycle regression boundary


def test_concurrent_same_actor_invocations_create_one_run() -> None:
    repository = _ReadRaceRepository()
    invocation = AgentInvocation(
        capability="echo",
        input={"text": "concurrent"},
        idempotency_key="invoke-concurrently",
    )
    runtimes = tuple(
        AgentRuntimeModule(
            capabilities=(_EchoCapability(),),
            provider=_provider(),
            repository=repository,
        )
        for _ in range(2)
    )

    def start(runtime: AgentRuntimeModule) -> str:
        return asyncio.run(runtime.start_agent_run(_session(), invocation)).id

    with ThreadPoolExecutor(max_workers=2) as executor:
        run_ids = tuple(executor.map(start, runtimes))

    assert run_ids[0] == run_ids[1]
    assert repository.list_runnable("tenant-a") == (run_ids[0],)


def test_concurrent_same_actor_invocations_project_one_native_queued_event() -> None:
    repository = InMemoryAgentRunRepository()
    stores: dict[str, _ConcurrentProjectionStore] = {}
    stores_lock = threading.Lock()
    invocation = AgentInvocation(
        capability="echo",
        input={"text": "concurrent projection"},
        idempotency_key="project-concurrently",
    )

    def store_factory(_session: TenantSession, run_id: str) -> _ConcurrentProjectionStore:
        with stores_lock:
            return stores.setdefault(run_id, _ConcurrentProjectionStore(run_id))

    runtimes = tuple(
        AgentRuntimeModule(
            capabilities=(_EchoCapability(),),
            provider=_provider(),
            repository=repository,
            activegraph_store_factory=store_factory,
        )
        for _ in range(2)
    )

    def start(runtime: AgentRuntimeModule) -> str:
        return asyncio.run(runtime.start_agent_run(_session(), invocation)).id

    with ThreadPoolExecutor(max_workers=2) as executor:
        run_ids = tuple(executor.map(start, runtimes))

    assert run_ids[0] == run_ids[1]
    assert [event.type for event in stores[run_ids[0]].iter_events()] == ["agent.run.queued"]


@pytest.mark.asyncio
async def test_projection_interruption_leaves_a_retryable_queued_run() -> None:
    repository = InMemoryAgentRunRepository()
    stores: dict[str, _FailOnceEventStore] = {}

    def store_factory(_session: TenantSession, run_id: str) -> _FailOnceEventStore:
        return stores.setdefault(run_id, _FailOnceEventStore(run_id))

    invocation = AgentInvocation(
        capability="echo",
        input={"text": "recoverable"},
        idempotency_key="projection-interruption",
    )
    runtime = AgentRuntimeModule(
        capabilities=(_EchoCapability(),),
        provider=_provider(),
        repository=repository,
        activegraph_store_factory=store_factory,
    )

    with pytest.raises(RuntimeError, match="simulated ActiveGraph interruption"):
        await runtime.start_agent_run(_session(), invocation)

    persisted = repository.get_by_idempotency(
        "tenant-a",
        "user-1",
        invocation.idempotency_key,
        actor_kind=PrincipalKind.USER,
    )
    assert persisted is not None
    assert persisted.state is AgentRunState.QUEUED
    assert [event.type for event in persisted.events] == ["agent.run.queued"]

    retried = await runtime.start_agent_run(_session(), invocation)

    assert retried.id == persisted.run_id
    assert [event.type for event in stores[retried.id].iter_events()] == ["agent.run.queued"]


@pytest.mark.asyncio
async def test_run_observation_and_control_are_scoped_to_the_originating_actor() -> None:
    provider = _provider()
    runtime = AgentRuntimeModule(capabilities=(_EchoCapability(),), provider=provider)
    started = await runtime.start_agent_run(
        _session(actor_id="user-alice"),
        AgentInvocation(
            capability="echo",
            input={"text": "private run"},
            idempotency_key="alice-private-run",
        ),
    )
    bob = _session(actor_id="user-bob")

    with pytest.raises(LookupError, match="Agent Run not found"):
        runtime.status(bob, started.id)
    with pytest.raises(LookupError, match="Agent Run not found"):
        runtime.replay(bob, started.id)
    with pytest.raises(LookupError, match="Agent Run not found"):
        runtime.cancel(bob, started.id)
    with pytest.raises(LookupError, match="Agent Run not found"):
        await runtime.run_quantum(bob, started.id)

    completed = await runtime.run_pending("tenant-a", started.id)

    assert completed.actor_id == "user-alice"
    assert completed.state is AgentRunState.COMPLETED
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_separate_worker_runtime_resumes_durable_queued_run() -> None:
    repository = InMemoryAgentRunRepository()
    gateway = AgentRuntimeModule(
        capabilities=(_EchoCapability(),),
        provider=_provider(),
        repository=repository,
    )
    started = await gateway.start_agent_run(
        _session(),
        AgentInvocation(
            capability="echo",
            input={"text": "durable"},
            idempotency_key="durable-run-1",
        ),
    )
    worker_provider = _provider()
    worker = AgentRuntimeModule(
        capabilities=(_EchoCapability(),),
        provider=worker_provider,
        repository=repository,
    )

    completed = await worker.run_pending("tenant-a", started.id)
    observer = AgentRuntimeModule(
        capabilities=(_EchoCapability(),),
        provider=_provider(),
        repository=repository,
    )

    assert completed.state is AgentRunState.COMPLETED
    assert observer.status(_session(), started.id) == completed
    assert len(worker_provider.calls) == 1


@pytest.mark.asyncio
async def test_concurrent_workers_claim_and_execute_one_run_once() -> None:
    repository = InMemoryAgentRunRepository()
    gateway = AgentRuntimeModule(
        capabilities=(_EchoCapability(),),
        provider=_provider(),
        repository=repository,
    )
    started = await gateway.start_agent_run(
        _session(),
        AgentInvocation(
            capability="echo",
            input={"text": "claim once"},
            idempotency_key="worker-claim-once",
        ),
    )
    contenders = threading.Barrier(2)

    def claim(_index: int) -> AgentRunClaim | None:
        contenders.wait(timeout=5)
        return repository.claim_runnable("tenant-a", lease_seconds=60)

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = tuple(executor.map(claim, range(2)))

    claimed = tuple(item for item in claims if item is not None)
    provider = _provider()
    worker = AgentRuntimeModule(
        capabilities=(_EchoCapability(),),
        provider=provider,
        repository=repository,
    )

    assert len(claimed) == 1
    completed = await worker.run_claimed(claimed[0])
    assert completed.id == started.id
    assert completed.state is AgentRunState.COMPLETED
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_expired_worker_lease_is_recovered_and_stale_owner_is_fenced() -> None:
    now = [datetime(2026, 8, 3, 12, tzinfo=UTC)]
    repository = InMemoryAgentRunRepository(clock=lambda: now[0])
    stores: dict[str, InMemoryEventStore] = {}

    def store_factory(_session: TenantSession, run_id: str) -> InMemoryEventStore:
        return stores.setdefault(run_id, InMemoryEventStore(run_id))

    gateway = AgentRuntimeModule(
        capabilities=(_EchoCapability(),),
        provider=_provider(),
        repository=repository,
        activegraph_store_factory=store_factory,
    )
    started = await gateway.start_agent_run(
        _session(),
        AgentInvocation(
            capability="echo",
            input={"text": "recover"},
            idempotency_key="expired-worker-lease",
        ),
    )
    stale_claim = repository.claim_runnable("tenant-a", lease_seconds=5)
    assert stale_claim is not None

    now[0] += timedelta(seconds=6)
    recovered_claim = repository.claim_runnable("tenant-a", lease_seconds=5)
    assert recovered_claim is not None
    assert recovered_claim.run_id == started.id
    assert recovered_claim.lease_version == stale_claim.lease_version + 1

    stale_running = stale_claim.snapshot.model_copy(update={"state": AgentRunState.RUNNING})
    with pytest.raises(AgentRunLeaseLost, match="lease is no longer owned"):
        repository.save(stale_running, claim=stale_claim)

    events_before_stale_execution = tuple(stores[started.id].iter_events())
    stale_worker = AgentRuntimeModule(
        capabilities=(_EchoCapability(),),
        provider=_provider(),
        repository=repository,
        activegraph_store_factory=store_factory,
    )
    with pytest.raises(AgentRunLeaseLost, match="lease is no longer owned"):
        await stale_worker.run_claimed(stale_claim)
    assert tuple(stores[started.id].iter_events()) == events_before_stale_execution

    provider = _provider()
    worker = AgentRuntimeModule(
        capabilities=(_EchoCapability(),),
        provider=provider,
        repository=repository,
        activegraph_store_factory=store_factory,
    )
    completed = await worker.run_claimed(recovered_claim)

    assert completed.state is AgentRunState.COMPLETED
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_cancellation_appends_to_latest_claimed_snapshot_without_stream_divergence() -> None:
    repository = InMemoryAgentRunRepository()
    stores: dict[str, InMemoryEventStore] = {}
    entered = asyncio.Event()
    release = asyncio.Event()

    def store_factory(_session: TenantSession, run_id: str) -> InMemoryEventStore:
        return stores.setdefault(run_id, InMemoryEventStore(run_id))

    gateway = AgentRuntimeModule(
        capabilities=(_BlockingCapability(entered, release),),
        provider=_provider(),
        repository=repository,
        activegraph_store_factory=store_factory,
    )
    invocation = AgentInvocation(
        capability="blocking",
        input={"text": "cancel raced run"},
        idempotency_key="cancel-raced-run",
    )
    started = await gateway.start_agent_run(_session(), invocation)
    claim = repository.claim_runnable("tenant-a", lease_seconds=60)
    assert claim is not None

    cancelling_runtime = AgentRuntimeModule(
        capabilities=(_BlockingCapability(entered, release),),
        provider=_provider(),
        repository=repository,
        activegraph_store_factory=store_factory,
    )
    provider = _provider()
    worker = AgentRuntimeModule(
        capabilities=(_BlockingCapability(entered, release),),
        provider=provider,
        repository=repository,
        activegraph_store_factory=store_factory,
    )
    worker_run = asyncio.create_task(worker.run_claimed(claim))
    await asyncio.wait_for(entered.wait(), timeout=5)
    requested = cancelling_runtime.cancel(_session(), started.id)
    release.set()
    completed = await asyncio.wait_for(worker_run, timeout=5)

    assert requested.state is AgentRunState.CANCEL_REQUESTED
    assert completed.state is AgentRunState.CANCELLED
    assert provider.calls == ()
    assert [event.type for event in stores[started.id].iter_events()] == [
        "agent.run.queued",
        "agent.run.started",
        "agent.tool.requested",
        "agent.run.cancellation_requested",
        "agent.run.cancelled",
    ]
    persisted = repository.get("tenant-a", started.id)
    assert persisted is not None
    assert persisted.state is AgentRunState.CANCELLED


@pytest.mark.asyncio
async def test_cancellation_reloads_latest_snapshot_after_uninstrumented_work_returns() -> None:
    repository = InMemoryAgentRunRepository()
    stores: dict[str, InMemoryEventStore] = {}
    entered = asyncio.Event()
    release = asyncio.Event()
    capability = _RawBlockingCapability(entered, release)

    def store_factory(_session: TenantSession, run_id: str) -> InMemoryEventStore:
        return stores.setdefault(run_id, InMemoryEventStore(run_id))

    gateway = AgentRuntimeModule(
        capabilities=(capability,),
        provider=_provider(),
        repository=repository,
        activegraph_store_factory=store_factory,
    )
    started = await gateway.start_agent_run(
        _session(),
        AgentInvocation(
            capability="raw_blocking",
            input={"text": "cancel after return"},
            idempotency_key="cancel-after-uninstrumented-return",
        ),
    )
    claim = repository.claim_runnable("tenant-a", lease_seconds=60)
    assert claim is not None
    worker = AgentRuntimeModule(
        capabilities=(capability,),
        provider=_provider(),
        repository=repository,
        activegraph_store_factory=store_factory,
    )

    worker_run = asyncio.create_task(worker.run_claimed(claim))
    await asyncio.wait_for(entered.wait(), timeout=5)
    requested = gateway.cancel(_session(), started.id)
    release.set()
    completed = await asyncio.wait_for(worker_run, timeout=5)

    assert requested.state is AgentRunState.CANCEL_REQUESTED
    assert completed.state is AgentRunState.CANCELLED
    assert [event.type for event in stores[started.id].iter_events()] == [
        "agent.run.queued",
        "agent.run.started",
        "agent.run.cancellation_requested",
        "agent.run.cancelled",
    ]
    assert gateway.replay(_session(), started.id).state is AgentRunState.CANCELLED


@pytest.mark.asyncio
async def test_cancellation_wins_when_uninstrumented_work_raises_after_request() -> None:
    repository = InMemoryAgentRunRepository()
    stores: dict[str, InMemoryEventStore] = {}
    entered = asyncio.Event()
    release = asyncio.Event()
    capability = _RawFailingBlockingCapability(entered, release)

    def store_factory(_session: TenantSession, run_id: str) -> InMemoryEventStore:
        return stores.setdefault(run_id, InMemoryEventStore(run_id))

    gateway = AgentRuntimeModule(
        capabilities=(capability,),
        provider=_provider(),
        repository=repository,
        activegraph_store_factory=store_factory,
    )
    started = await gateway.start_agent_run(
        _session(),
        AgentInvocation(
            capability="raw_failing_blocking",
            input={"text": "fail after cancellation"},
            idempotency_key="fail-after-cancellation",
        ),
    )
    claim = repository.claim_runnable("tenant-a", lease_seconds=60)
    assert claim is not None
    worker = AgentRuntimeModule(
        capabilities=(capability,),
        provider=_provider(),
        repository=repository,
        activegraph_store_factory=store_factory,
    )

    worker_run = asyncio.create_task(worker.run_claimed(claim))
    await asyncio.wait_for(entered.wait(), timeout=5)
    gateway.cancel(_session(), started.id)
    release.set()
    completed = await asyncio.wait_for(worker_run, timeout=5)

    assert completed.state is AgentRunState.CANCELLED
    assert [event.type for event in stores[started.id].iter_events()] == [
        "agent.run.queued",
        "agent.run.started",
        "agent.run.cancellation_requested",
        "agent.run.cancelled",
    ]
    persisted = repository.get("tenant-a", started.id)
    assert persisted is not None
    assert persisted.state is AgentRunState.CANCELLED


@pytest.mark.asyncio
async def test_native_activegraph_suffix_beyond_snapshot_fails_closed() -> None:
    repository = InMemoryAgentRunRepository()
    stores: dict[str, InMemoryEventStore] = {}

    def store_factory(_session: TenantSession, run_id: str) -> InMemoryEventStore:
        return stores.setdefault(run_id, InMemoryEventStore(run_id))

    runtime = AgentRuntimeModule(
        capabilities=(_EchoCapability(),),
        provider=_provider(),
        repository=repository,
        activegraph_store_factory=store_factory,
    )
    started = await runtime.start_agent_run(
        _session(),
        AgentInvocation(
            capability="echo",
            input={"text": "native"},
            idempotency_key="native-source-1",
        ),
    )
    await runtime.run_until_terminal(_session(), started.id)

    native_events = list(stores[started.id].iter_events())
    assert [event.type for event in native_events] == [
        "agent.run.queued",
        "agent.run.started",
        "agent.model.requested",
        "agent.model.completed",
        "agent.run.completed",
    ]
    snapshot = repository.get("tenant-a", started.id)
    assert snapshot is not None
    repository._runs[("tenant-a", started.id)] = snapshot.model_copy(  # noqa: SLF001
        update={
            "state": AgentRunState.QUEUED,
            "model_calls": 0,
            "cost_usd": Decimal("0"),
            "events": (),
            "output": None,
        }
    )
    observer = AgentRuntimeModule(
        capabilities=(_EchoCapability(),),
        provider=_provider(),
        repository=repository,
        activegraph_store_factory=store_factory,
    )

    with pytest.raises(ValueError, match="diverges"):
        observer.status(_session(), started.id)
    with pytest.raises(ValueError, match="diverges"):
        observer.replay(_session(), started.id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capability", "expected_state", "terminal_type"),
    (
        (_GreedyCapability(), AgentRunState.BUDGET_EXHAUSTED, "agent.run.budget_exhausted"),
        (_FailingCapability(), AgentRunState.FAILED, "agent.run.failed"),
    ),
)
async def test_native_activegraph_terminal_suffix_beyond_snapshot_fails_closed(
    capability: AgentCapability[_EchoInput],
    expected_state: AgentRunState,
    terminal_type: str,
) -> None:
    repository = InMemoryAgentRunRepository()
    stores: dict[str, InMemoryEventStore] = {}

    def store_factory(_session: TenantSession, run_id: str) -> InMemoryEventStore:
        return stores.setdefault(run_id, InMemoryEventStore(run_id))

    runtime = AgentRuntimeModule(
        capabilities=(capability,),
        provider=_provider(count=4),
        repository=repository,
        activegraph_store_factory=store_factory,
    )
    started = await runtime.start_agent_run(
        _session(),
        AgentInvocation(
            capability=capability.name,
            input={"text": "terminal"},
            idempotency_key=f"native-{expected_state.value}",
        ),
    )
    terminal = await runtime.run_until_terminal(_session(), started.id)
    snapshot = repository.get("tenant-a", started.id)
    assert snapshot is not None
    repository._runs[("tenant-a", started.id)] = snapshot.model_copy(  # noqa: SLF001
        update={
            "state": AgentRunState.QUEUED,
            "model_calls": 0,
            "tool_calls": 0,
            "cost_usd": Decimal("0"),
            "events": (),
            "output": None,
            "failure": None,
            "resumable": False,
        }
    )
    observer = AgentRuntimeModule(
        capabilities=(capability,),
        provider=_provider(),
        repository=repository,
        activegraph_store_factory=store_factory,
    )

    assert terminal.state is expected_state
    assert terminal.events[-1].type == terminal_type
    with pytest.raises(ValueError, match="diverges"):
        observer.status(_session(), started.id)
    with pytest.raises(ValueError, match="diverges"):
        observer.replay(_session(), started.id)


@pytest.mark.asyncio
async def test_start_accepts_only_registered_capabilities_and_their_typed_inputs() -> None:
    runtime = AgentRuntimeModule(capabilities=(_EchoCapability(),), provider=_provider())

    with pytest.raises(ValueError, match="not registered"):
        await runtime.start_agent_run(
            _session(),
            AgentInvocation(
                capability="caller_supplied_pack",
                input={},
                idempotency_key="unknown-capability",
            ),
        )
    with pytest.raises(ValueError):
        await runtime.start_agent_run(
            _session(),
            AgentInvocation(
                capability="echo",
                input={"unexpected": "shape"},
                idempotency_key="invalid-shape",
            ),
        )


@pytest.mark.asyncio
async def test_cancellation_is_durable_and_prevents_provider_work() -> None:
    provider = _provider()
    runtime = AgentRuntimeModule(capabilities=(_EchoCapability(),), provider=provider)
    started = await runtime.start_agent_run(
        _session(),
        AgentInvocation(
            capability="echo",
            input={"text": "hello"},
            idempotency_key="cancel-1",
        ),
    )

    requested = runtime.cancel(_session(), started.id)
    cancelled = await runtime.run_until_terminal(_session(), started.id)

    assert requested.state is AgentRunState.CANCEL_REQUESTED
    assert cancelled.state is AgentRunState.CANCELLED
    assert runtime.replay(_session(), started.id) == cancelled
    assert provider.calls == ()


@pytest.mark.asyncio
async def test_host_cancellation_stops_work_at_the_next_cooperative_boundary() -> None:
    provider = _provider()
    entered = asyncio.Event()
    release = asyncio.Event()
    runtime = AgentRuntimeModule(
        capabilities=(_BlockingCapability(entered, release),),
        provider=provider,
    )
    started = await runtime.start_agent_run(
        _session(),
        AgentInvocation(
            capability="blocking",
            input={"text": "hello"},
            idempotency_key="in-flight-cancel-1",
        ),
    )

    quantum = asyncio.create_task(runtime.run_quantum(_session(), started.id))
    await entered.wait()
    runtime.cancel(_session(), started.id)
    release.set()
    cancelled = await quantum

    assert cancelled.state is AgentRunState.CANCELLED
    assert cancelled.events[-1].type == "agent.run.cancelled"
    assert provider.calls == ()


@pytest.mark.asyncio
async def test_gateway_cancellation_reaches_an_in_flight_worker_runtime() -> None:
    repository = InMemoryAgentRunRepository()
    entered = asyncio.Event()
    release = asyncio.Event()
    capability = _BlockingCapability(entered, release)
    gateway = AgentRuntimeModule(
        capabilities=(capability,),
        provider=_provider(),
        repository=repository,
    )
    worker_provider = _provider()
    worker = AgentRuntimeModule(
        capabilities=(capability,),
        provider=worker_provider,
        repository=repository,
    )
    started = await gateway.start_agent_run(
        _session(),
        AgentInvocation(
            capability="blocking",
            input={"text": "hello"},
            idempotency_key="cross-runtime-cancel",
        ),
    )

    quantum = asyncio.create_task(worker.run_pending("tenant-a", started.id))
    await entered.wait()
    requested = gateway.cancel(_session(), started.id)
    release.set()
    cancelled = await quantum

    assert requested.state is AgentRunState.CANCEL_REQUESTED
    assert cancelled.state is AgentRunState.CANCELLED
    assert repository.get("tenant-a", started.id) is not None
    assert worker_provider.calls == ()


@pytest.mark.asyncio
async def test_default_model_call_budget_records_resumable_exhaustion() -> None:
    provider = _provider(count=4)
    runtime = AgentRuntimeModule(capabilities=(_GreedyCapability(),), provider=provider)
    started = await runtime.start_agent_run(
        _session(),
        AgentInvocation(
            capability="greedy",
            input={"text": "hello"},
            idempotency_key="budget-1",
        ),
    )

    exhausted = await runtime.run_until_terminal(_session(), started.id)

    assert exhausted.state is AgentRunState.BUDGET_EXHAUSTED
    assert exhausted.resumable is True
    assert exhausted.usage.model_calls == 3
    assert len(provider.calls) == 3
    assert exhausted.events[-1].type == "agent.run.budget_exhausted"
    assert exhausted.events[-1].data["dimension"] == "model_calls"


@pytest.mark.asyncio
async def test_tool_call_budget_is_enforced_before_the_second_effect() -> None:
    runtime = AgentRuntimeModule(
        capabilities=(_ToolGreedyCapability(),),
        provider=_provider(),
        budget=AgentBudget(max_tool_calls=1),
    )
    started = await runtime.start_agent_run(
        _session(),
        AgentInvocation(
            capability="tool_greedy",
            input={"text": "hello"},
            idempotency_key="tool-budget-1",
        ),
    )

    exhausted = await runtime.run_until_terminal(_session(), started.id)

    assert exhausted.state is AgentRunState.BUDGET_EXHAUSTED
    assert exhausted.usage.tool_calls == 1
    assert exhausted.events[-1].data["dimension"] == "tool_calls"
    assert runtime.replay(_session(), started.id) == exhausted


@pytest.mark.asyncio
async def test_event_budget_still_records_the_terminal_budget_event() -> None:
    provider = _provider()
    runtime = AgentRuntimeModule(
        capabilities=(_EchoCapability(),),
        provider=provider,
        budget=AgentBudget(max_events=1),
    )
    started = await runtime.start_agent_run(
        _session(),
        AgentInvocation(
            capability="echo",
            input={"text": "hello"},
            idempotency_key="event-budget-1",
        ),
    )

    exhausted = await runtime.run_until_terminal(_session(), started.id)

    assert exhausted.state is AgentRunState.BUDGET_EXHAUSTED
    assert exhausted.events[-1].data["dimension"] == "events"
    assert provider.calls == ()
    assert runtime.replay(_session(), started.id) == exhausted


@pytest.mark.asyncio
async def test_cost_budget_accounts_for_the_charge_that_exhausted_it() -> None:
    provider = RecordedProvider(
        [
            RecordedCompletion(
                parsed={"answer": "expensive"},
                cost_usd=Decimal("0.30"),
            )
        ]
    )
    runtime = AgentRuntimeModule(capabilities=(_EchoCapability(),), provider=provider)
    started = await runtime.start_agent_run(
        _session(),
        AgentInvocation(
            capability="echo",
            input={"text": "hello"},
            idempotency_key="cost-budget-1",
        ),
    )

    exhausted = await runtime.run_until_terminal(_session(), started.id)

    assert exhausted.state is AgentRunState.BUDGET_EXHAUSTED
    assert exhausted.usage.cost_usd == Decimal("0.30")
    assert exhausted.events[-1].data["dimension"] == "cost_usd"
    assert runtime.replay(_session(), started.id) == exhausted


@pytest.mark.asyncio
async def test_wall_clock_budget_uses_an_injected_monotonic_clock() -> None:
    now = 0.0

    def monotonic() -> float:
        return now

    def advance() -> None:
        nonlocal now
        now = 121.0

    runtime = AgentRuntimeModule(
        capabilities=(_AdvancingCapability(advance),),
        provider=_provider(),
        monotonic=monotonic,
    )
    started = await runtime.start_agent_run(
        _session(),
        AgentInvocation(
            capability="advancing",
            input={"text": "hello"},
            idempotency_key="time-budget-1",
        ),
    )

    exhausted = await runtime.run_until_terminal(_session(), started.id)

    assert exhausted.state is AgentRunState.BUDGET_EXHAUSTED
    assert exhausted.events[-1].data["dimension"] == "seconds"
    assert runtime.replay(_session(), started.id) == exhausted


def test_native_activegraph_pack_loads_without_global_registry_changes() -> None:
    activegraph = pytest.importorskip("activegraph")
    pytest.importorskip("activegraph.packs")
    from activegraph.llm import LLMProvider  # type: ignore[import-untyped]

    from agent_memory_service.agents.knowledge_synthesis_pack import (
        KNOWLEDGE_SYNTHESIS_PACK,
    )

    assert KNOWLEDGE_SYNTHESIS_PACK is not None
    assert isinstance(_provider(), LLMProvider)
    assert isinstance(LangChainAnthropicProvider(), LLMProvider)
    before = tuple(activegraph.get_registry())
    graph = activegraph.Graph()
    runtime = activegraph.Runtime(graph)

    loaded = runtime.load_pack(KNOWLEDGE_SYNTHESIS_PACK, settings={})
    graph.add_object(
        "knowledge_synthesis_invocation",
        data={
            "run_id": "run-native-1",
            "request": "Distill selected facts",
            "source_memory_ids": ["memory-1"],
        },
    )
    quantum = runtime.run_quantum()

    assert loaded is not None
    assert callable(getattr(KNOWLEDGE_SYNTHESIS_PACK, "execute", None))
    assert quantum.idle is True
    assert tuple(activegraph.get_registry()) == before
    assert [pack.name for pack in runtime.loaded_packs()] == ["knowledge_synthesis"]
    assert "knowledge_synthesis.requested" in {
        event.type for event in runtime.status().recent_events
    }


def test_langchain_anthropic_adapter_uses_fake_model_without_network() -> None:
    messages = pytest.importorskip("langchain_core.messages")
    activegraph_llm = pytest.importorskip("activegraph.llm")
    captured: dict[str, object] = {}

    class _FakeChat:
        def invoke(self, invocation_messages: list[object]) -> object:
            captured["messages"] = invocation_messages
            return messages.AIMessage(
                content='{"answer":"from fake"}',
                usage_metadata={
                    "input_tokens": 12,
                    "output_tokens": 2,
                    "total_tokens": 14,
                },
            )

    def factory(**kwargs: object) -> _FakeChat:
        captured["kwargs"] = kwargs
        return _FakeChat()

    provider = LangChainAnthropicProvider(model_factory=factory)
    response = provider.complete(
        system="Return a typed echo.",
        messages=[activegraph_llm.LLMMessage(role="user", content="hello")],
        model="claude-sonnet-5",
        max_tokens=4096,
        temperature=1.0,
        top_p=1.0,
        output_schema=_EchoOutput,
        timeout_seconds=60.0,
    )

    assert response.parsed == _EchoOutput(answer="from fake")
    assert response.model == "claude-sonnet-5"
    assert response.cost_usd == Decimal("0.000066")
    assert captured["kwargs"] == {
        "model": "claude-sonnet-5",
        "max_tokens": 4096,
        "timeout": 60.0,
    }

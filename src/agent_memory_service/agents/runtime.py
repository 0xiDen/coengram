"""Application-owned, tenant-scoped Agent Runtime Module."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, TypeVar
from uuid import uuid4

from pydantic import BaseModel

from agent_memory_service.agents.models import (
    AgentBudget,
    AgentBudgetUsage,
    AgentInvocation,
    AgentModelSettings,
    AgentRunEvent,
    AgentRunState,
    AgentRunView,
)
from agent_memory_service.agents.persistence import (
    AgentRunCancellationRequested,
    AgentRunClaim,
    AgentRunLeaseLost,
    AgentRunRepository,
    AgentRunSnapshot,
    InMemoryAgentRunRepository,
)
from agent_memory_service.agents.providers import AgentModelProvider
from agent_memory_service.models import TenantSession
from agent_memory_service.telemetry import ContentSafeTracer

ModelT = TypeVar("ModelT", bound=BaseModel)
ResultT = TypeVar("ResultT")

_NATIVE_EVENT_PAYLOAD_KEY = "agent_memory_run_event"
_NATIVE_EVENT_SCHEMA_VERSION = 1


class AgentBudgetExceeded(RuntimeError):
    def __init__(self, dimension: str) -> None:
        super().__init__(f"Agent budget exhausted: {dimension}")
        self.dimension = dimension


class _AgentCancellationRequested(RuntimeError):
    pass


class AgentCapability[InputT: BaseModel](ABC):
    """A registered, typed capability. Callers cannot supply arbitrary Packs."""

    name: str
    input_model: type[InputT]

    @abstractmethod
    async def execute(self, context: AgentRunContext, command: InputT) -> Any:
        """Perform one cooperative unit using only context-owned provider/tools."""


@dataclass(slots=True)
class _RunRecord:
    id: str
    session: TenantSession
    invocation: AgentInvocation
    command: BaseModel
    settings: AgentModelSettings
    budget: AgentBudget
    state: AgentRunState = AgentRunState.QUEUED
    model_calls: int = 0
    tool_calls: int = 0
    cost_usd: Decimal = Decimal("0")
    events: list[AgentRunEvent] = field(default_factory=list)
    output: dict[str, Any] | None = None
    failure: str | None = None
    resumable: bool = False
    started_monotonic: float | None = None
    monotonic: Callable[[], float] = time.monotonic
    backend: _CooperativeBackend | None = None
    persist: Callable[[_RunRecord], None] | None = None
    refresh_cancellation: Callable[[_RunRecord], bool] | None = None
    event_store: Any | None = None

    def usage(self) -> AgentBudgetUsage:
        return AgentBudgetUsage(
            model_calls=self.model_calls,
            tool_calls=self.tool_calls,
            events=len(self.events),
            cost_usd=self.cost_usd,
        )


class _CooperativeBackend(ABC):
    @abstractmethod
    def run_quantum(self) -> None: ...


class _NoOpBackend(_CooperativeBackend):
    def run_quantum(self) -> None:
        return None


class AgentRunContext:
    """Budget-accounting seam used by capabilities for model and tool work."""

    def __init__(
        self,
        record: _RunRecord,
        provider: AgentModelProvider,
        tracer: ContentSafeTracer | None,
    ) -> None:
        self._record = record
        self._provider = provider
        self._tracer = tracer

    @property
    def session(self) -> TenantSession:
        return self._record.session

    @property
    def run_id(self) -> str:
        return self._record.id

    @property
    def settings(self) -> AgentModelSettings:
        return self._record.settings

    def complete(
        self,
        *,
        system: str,
        user: str,
        output_schema: type[ModelT],
    ) -> ModelT:
        self._check_time()
        if self._record.model_calls >= self._record.budget.max_model_calls:
            raise AgentBudgetExceeded("model_calls")
        self._record.model_calls += 1
        _append_event(
            self._record,
            "agent.model.requested",
            {"call": self._record.model_calls, "model": self.settings.model},
        )
        with self._span(
            "langchain.completion",
            self._run_attributes({"gen_ai.request.model": self.settings.model}),
        ):
            response = self._provider.complete(
                system=system,
                messages=[_user_message(user)],
                model=self.settings.model,
                max_tokens=self.settings.max_tokens,
                temperature=self.settings.temperature,
                top_p=self.settings.top_p,
                output_schema=output_schema,
                timeout_seconds=self.settings.timeout_seconds,
                tools=None,
                structured_output_mode=self.settings.structured_output_mode,
            )
        self._record.cost_usd += Decimal(response.cost_usd)
        if self._record.cost_usd > self._record.budget.max_cost_usd:
            raise AgentBudgetExceeded("cost_usd")
        self._check_time()
        parsed = response.parsed
        if parsed is None:
            raise ValueError("Model provider returned no structured output")
        if not isinstance(parsed, output_schema):
            parsed = output_schema.model_validate(parsed)
        _append_event(
            self._record,
            "agent.model.completed",
            {
                "call": self._record.model_calls,
                "model": response.model,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "cost_usd": str(response.cost_usd),
            },
        )
        return parsed

    async def tool(
        self,
        name: str,
        operation: Callable[[], Awaitable[ResultT]],
    ) -> ResultT:
        self._check_time()
        if self._record.tool_calls >= self._record.budget.max_tool_calls:
            raise AgentBudgetExceeded("tool_calls")
        self._record.tool_calls += 1
        _append_event(
            self._record,
            "agent.tool.requested",
            {"call": self._record.tool_calls, "tool": name},
        )
        with self._span(
            "activegraph.tool",
            self._run_attributes({"agent.tool.name": name}),
        ):
            result = await operation()
        self._check_time()
        _append_event(
            self._record,
            "agent.tool.completed",
            {"call": self._record.tool_calls, "tool": name},
        )
        return result

    def emit(self, event_type: str, data: dict[str, Any]) -> None:
        self._check_time()
        _append_event(self._record, event_type, data)

    def _check_time(self) -> None:
        refresh = self._record.refresh_cancellation
        if self._record.state is AgentRunState.CANCEL_REQUESTED or (
            refresh is not None and refresh(self._record)
        ):
            raise _AgentCancellationRequested
        started = self._record.started_monotonic
        if (
            started is not None
            and self._record.monotonic() - started >= self._record.budget.max_seconds
        ):
            raise AgentBudgetExceeded("seconds")

    def _span(
        self,
        name: str,
        attributes: dict[str, str | int | float | bool],
    ) -> Any:
        if self._tracer is None:
            from contextlib import nullcontext

            return nullcontext()
        return self._tracer.span(name, attributes)

    def _run_attributes(
        self,
        attributes: dict[str, str | int | float | bool],
    ) -> dict[str, str | int | float | bool]:
        if self._tracer is None:
            return attributes
        return {
            **attributes,
            "memory.run_ref": self._tracer.reference("run", self._record.id),
        }


class AgentRuntimeModule:
    """One construction interface hiding provider, Pack and event wiring."""

    def __init__(
        self,
        *,
        capabilities: Iterable[AgentCapability[Any]],
        provider: AgentModelProvider,
        settings: AgentModelSettings | None = None,
        budget: AgentBudget | None = None,
        monotonic: Callable[[], float] | None = None,
        repository: AgentRunRepository | None = None,
        activegraph_store_factory: Callable[[TenantSession, str], Any] | None = None,
        tracer: ContentSafeTracer | None = None,
    ) -> None:
        capability_map = {capability.name: capability for capability in capabilities}
        if not capability_map:
            raise ValueError("At least one Agent capability must be registered")
        self._capabilities = capability_map
        self._provider = provider
        configured_settings = settings or AgentModelSettings()
        self._settings = configured_settings.model_copy(
            update={
                "provider": str(getattr(provider, "provider_id", type(provider).__name__)),
                "adapter": str(getattr(provider, "adapter_id", type(provider).__name__)),
            }
        )
        self._budget = budget or AgentBudget()
        self._monotonic = monotonic or time.monotonic
        self._repository = repository or InMemoryAgentRunRepository()
        self._activegraph_store_factory = activegraph_store_factory
        self._tracer = tracer
        self._runs: dict[tuple[str, str], _RunRecord] = {}
        self._claims: dict[tuple[str, str], AgentRunClaim] = {}

    async def start_agent_run(
        self,
        session: TenantSession,
        invocation: AgentInvocation,
    ) -> AgentRunView:
        try:
            capability = self._capabilities[invocation.capability]
        except KeyError as exc:
            raise ValueError("Agent capability is not registered") from exc
        command = capability.input_model.model_validate(invocation.input)
        run_id = str(uuid4())
        record = _RunRecord(
            id=run_id,
            session=session,
            invocation=invocation,
            command=command,
            settings=self._settings,
            budget=self._budget,
            monotonic=self._monotonic,
        )
        record.persist = self._persist
        record.refresh_cancellation = self._refresh_cancellation
        record.events.append(
            AgentRunEvent(
                sequence=1,
                type="agent.run.queued",
                data={
                    "capability": invocation.capability,
                    "provider": record.settings.provider,
                    "adapter": record.settings.adapter,
                    "model": record.settings.model,
                    "settings": record.settings.model_dump(mode="json"),
                    "budget": record.budget.model_dump(mode="json"),
                },
            )
        )
        accepted = self._repository.create_or_get(_snapshot(record))
        if accepted.run_id != run_id:
            _require_run_context(session, accepted.session())
            duplicate = self._record_from_snapshot(accepted)
            try:
                return _view(duplicate)
            finally:
                _close_event_store(duplicate)
        record.event_store = self._event_store(session, run_id)
        try:
            if record.event_store is not None:
                _seed_native_events(record, record.events)
                _refresh_native_projection(record)
            return _view(record)
        finally:
            _close_event_store(record)

    def status(self, session: TenantSession, run_id: str) -> AgentRunView:
        record = self._record(session, run_id)
        try:
            return _view(record)
        finally:
            if not self._is_locally_executing(session.tenant_id, run_id, record):
                _close_event_store(record)

    def replay(self, session: TenantSession, run_id: str) -> AgentRunView:
        """Reconstruct the public state without executing provider or tool work."""
        record = self._record(session, run_id)
        try:
            return _replay_view(record)
        finally:
            if not self._is_locally_executing(session.tenant_id, run_id, record):
                _close_event_store(record)

    def cancel(self, session: TenantSession, run_id: str) -> AgentRunView:
        snapshot = self._repository.request_cancellation(session, run_id)
        if snapshot is None:
            raise LookupError("Agent Run not found")
        record = self._record_from_snapshot(snapshot)
        try:
            return _view(record)
        finally:
            _close_event_store(record)

    async def run_quantum(self, session: TenantSession, run_id: str) -> AgentRunView:
        key = (session.tenant_id, run_id)
        record = self._record(session, run_id)
        try:
            self._refresh_cancellation(record)
        except Exception:
            self._release_record(key, record)
            raise
        if record.state.terminal:
            try:
                return _view(record)
            finally:
                self._release_record(key, record)
        self._runs[key] = record
        if record.state is AgentRunState.CANCEL_REQUESTED:
            record.state = AgentRunState.CANCELLED
            record.resumable = True
            _append_event(
                record,
                "agent.run.cancelled",
                {"usage": record.usage().model_dump(mode="json")},
                force=True,
            )
            try:
                return _view(record)
            finally:
                self._release_record(key, record)
        if record.started_monotonic is None:
            record.started_monotonic = record.monotonic()
        record.state = AgentRunState.RUNNING
        try:
            _append_event(record, "agent.run.started", {})
            if record.backend is None:
                record.backend = _native_backend_or_noop(
                    record,
                    self._provider,
                )
            record.backend.run_quantum()
            capability = self._capabilities[record.invocation.capability]
            result = await capability.execute(
                AgentRunContext(record, self._provider, self._tracer),
                record.command,
            )
            if isinstance(result, BaseModel):
                record.output = result.model_dump(mode="json")
            elif isinstance(result, dict):
                record.output = result
            else:
                raise TypeError("Agent capability output must be a Pydantic model or dict")
            record.state = AgentRunState.COMPLETED
            _append_event(
                record,
                "agent.run.completed",
                {
                    "output": record.output,
                    "usage": record.usage().model_dump(mode="json"),
                },
            )
        except _AgentCancellationRequested:
            self._complete_cancellation(record)
        except AgentBudgetExceeded as exc:
            record.state = AgentRunState.BUDGET_EXHAUSTED
            record.resumable = True
            try:
                _append_event(
                    record,
                    "agent.run.budget_exhausted",
                    {
                        "dimension": exc.dimension,
                        "usage": record.usage().model_dump(mode="json"),
                        "output": record.output,
                    },
                    force=True,
                )
            except AgentRunCancellationRequested:
                self._complete_cancellation(record)
        except AgentRunCancellationRequested:
            self._complete_cancellation(record)
        except AgentRunLeaseLost:
            self._release_record(key, record)
            raise
        except Exception as exc:
            record.state = AgentRunState.FAILED
            record.failure = type(exc).__name__
            record.resumable = True
            try:
                _append_event(
                    record,
                    "agent.run.failed",
                    {
                        "error_type": type(exc).__name__,
                        "usage": record.usage().model_dump(mode="json"),
                    },
                    force=True,
                )
            except AgentRunCancellationRequested:
                self._complete_cancellation(record)
        try:
            return _view(record)
        finally:
            if record.state.terminal:
                self._release_record(key, record)

    def _complete_cancellation(self, record: _RunRecord) -> None:
        self._refresh_cancellation(record)
        record.state = AgentRunState.CANCELLED
        record.resumable = True
        _append_event(
            record,
            "agent.run.cancelled",
            {"usage": record.usage().model_dump(mode="json")},
            force=True,
        )

    async def run_until_terminal(
        self,
        session: TenantSession,
        run_id: str,
    ) -> AgentRunView:
        span = (
            self._tracer.span(
                "activegraph.run",
                {
                    "memory.tenant_ref": self._tracer.reference("tenant", session.tenant_id),
                    "memory.run_ref": self._tracer.reference("run", run_id),
                },
            )
            if self._tracer is not None
            else None
        )
        if span is None:
            return await self._run_until_terminal(session, run_id)
        with span:
            return await self._run_until_terminal(session, run_id)

    async def _run_until_terminal(
        self,
        session: TenantSession,
        run_id: str,
    ) -> AgentRunView:
        view = self.status(session, run_id)
        while not view.state.terminal:
            view = await self.run_quantum(session, run_id)
        return view

    async def run_pending(self, tenant_id: str, run_id: str) -> AgentRunView:
        claim = self._repository.claim_run(tenant_id, run_id, lease_seconds=300)
        if claim is not None:
            return await self.run_claimed(claim)
        snapshot = self._repository.get(tenant_id, run_id)
        if snapshot is None:
            raise LookupError("Agent Run not found")
        record = self._record_from_snapshot(snapshot)
        try:
            return _view(record)
        finally:
            _close_event_store(record)

    async def run_claimed(self, claim: AgentRunClaim) -> AgentRunView:
        """Execute only while the exact repository-issued lease remains owned."""

        key = (claim.tenant_id, claim.run_id)
        record = self._record_from_snapshot(claim.snapshot, claim=claim)
        self._runs[key] = record
        self._claims[key] = claim
        try:
            return await self.run_until_terminal(record.session, record.id)
        finally:
            self._claims.pop(key, None)
            self._release_record(key, record)

    def _record(self, session: TenantSession, run_id: str) -> _RunRecord:
        key = (session.tenant_id, run_id)
        cached = self._runs.get(key)
        if cached is not None and (cached.state is AgentRunState.RUNNING or key in self._claims):
            _require_run_context(session, cached.session)
            _refresh_native_projection(cached)
            return cached
        snapshot = self._repository.get(session.tenant_id, run_id)
        if snapshot is None:
            raise LookupError("Agent Run not found")
        _require_run_context(session, snapshot.session())
        record = self._record_from_snapshot(snapshot)
        return record

    def _is_locally_executing(
        self,
        tenant_id: str,
        run_id: str,
        record: _RunRecord,
    ) -> bool:
        key = (tenant_id, run_id)
        return self._runs.get(key) is record and (key in self._claims or record.backend is not None)

    def _release_record(self, key: tuple[str, str], record: _RunRecord) -> None:
        if self._runs.get(key) is record:
            self._runs.pop(key, None)
        _close_event_store(record)

    def _persist(self, record: _RunRecord) -> None:
        self._repository.save(_snapshot(record))

    def _persist_claimed(self, record: _RunRecord, claim: AgentRunClaim) -> None:
        self._repository.save(_snapshot(record), claim=claim)

    def _refresh_cancellation(self, record: _RunRecord) -> bool:
        snapshot = self._repository.get(record.session.tenant_id, record.id)
        if snapshot is not None and snapshot.state is AgentRunState.CANCEL_REQUESTED:
            if record.event_store is not None:
                _reconcile_native_projection(record, snapshot.events)
            else:
                record.state = AgentRunState.CANCEL_REQUESTED
                record.model_calls = max(record.model_calls, snapshot.model_calls)
                record.tool_calls = max(record.tool_calls, snapshot.tool_calls)
                record.cost_usd = max(record.cost_usd, snapshot.cost_usd)
                record.events = list(snapshot.events)
                record.output = snapshot.output
                record.failure = snapshot.failure
                record.resumable = snapshot.resumable
            return True
        if record.event_store is not None:
            _refresh_native_projection(record)
        return record.state is AgentRunState.CANCEL_REQUESTED

    def _event_store(self, session: TenantSession, run_id: str) -> Any | None:
        if self._activegraph_store_factory is None:
            return None
        store = self._activegraph_store_factory(session, run_id)
        if getattr(store, "run_id", run_id) != run_id:
            raise ValueError("ActiveGraph EventStore is scoped to the wrong Agent Run")
        if not callable(getattr(store, "append", None)) or not callable(
            getattr(store, "iter_events", None)
        ):
            raise TypeError("ActiveGraph EventStore does not implement the required protocol")
        return store

    def _record_from_snapshot(
        self,
        snapshot: AgentRunSnapshot,
        *,
        claim: AgentRunClaim | None = None,
    ) -> _RunRecord:
        try:
            capability = self._capabilities[snapshot.invocation.capability]
        except KeyError as exc:
            raise ValueError("Persisted Agent capability is not registered") from exc
        record = _RunRecord(
            id=snapshot.run_id,
            session=snapshot.session(),
            invocation=snapshot.invocation,
            command=capability.input_model.model_validate(snapshot.command),
            settings=snapshot.settings,
            budget=snapshot.budget,
            state=snapshot.state,
            model_calls=snapshot.model_calls,
            tool_calls=snapshot.tool_calls,
            cost_usd=snapshot.cost_usd,
            events=list(snapshot.events),
            output=snapshot.output,
            failure=snapshot.failure,
            resumable=snapshot.resumable,
            monotonic=self._monotonic,
            persist=(
                self._persist
                if claim is None
                else lambda current: self._persist_claimed(current, claim)
            ),
            refresh_cancellation=self._refresh_cancellation,
        )
        record.event_store = self._event_store(record.session, record.id)
        if record.event_store is not None:
            native_events = _native_agent_events(record.event_store)
            if not native_events and not snapshot.events:
                raise ValueError("ActiveGraph Agent Run event stream is missing")
            _reconcile_native_projection(record, snapshot.events)
        return record


def _append_event(
    record: _RunRecord,
    event_type: str,
    data: dict[str, Any],
    *,
    force: bool = False,
) -> None:
    if not force and len(record.events) >= record.budget.max_events:
        raise AgentBudgetExceeded("events")
    event = AgentRunEvent(
        sequence=len(record.events) + 1,
        type=event_type,
        data=data,
    )
    if record.event_store is None:
        record.events.append(event)
    else:
        # The fenced Tenant snapshot must commit before ActiveGraph receives the
        # transition. A stale worker therefore fails its lease CAS without writing
        # to either durable representation. Projection repair below is idempotent.
        record.events.append(event)
    if record.persist is not None:
        try:
            record.persist(record)
        except Exception:
            if record.event_store is not None and record.events[-1] == event:
                record.events.pop()
            raise
    if record.event_store is not None:
        _reconcile_native_projection(record, record.events)


def _close_event_store(record: _RunRecord) -> None:
    store = record.event_store
    record.event_store = None
    record.backend = None
    close = getattr(store, "close", None)
    if callable(close):
        close()


def _append_native_event(
    record: _RunRecord,
    event: AgentRunEvent,
    *,
    event_id: str | None = None,
    idempotent: bool = False,
) -> None:
    from activegraph.core.event import Event  # type: ignore[import-untyped]
    from activegraph.store.serde import validate_event  # type: ignore[import-untyped]

    assert record.event_store is not None
    if event.type == "agent.run.queued":
        upsert_run = getattr(record.event_store, "upsert_run", None)
        if callable(upsert_run):
            upsert_run(
                created_at=event.created_at.isoformat(),
                goal=record.invocation.capability,
                frame_id=f"frame-{record.id}",
            )
    native_event = Event(
        id=event_id or f"memory_agent_{uuid4().hex}",
        type=event.type,
        payload={
            _NATIVE_EVENT_PAYLOAD_KEY: {
                "version": _NATIVE_EVENT_SCHEMA_VERSION,
                "data": event.data,
                "created_at": event.created_at.isoformat(),
            }
        },
        actor=record.session.actor_id,
        frame_id=f"frame-{record.id}",
        timestamp=event.created_at.isoformat(),
    )
    validate_event(native_event)
    if not idempotent:
        record.event_store.append(native_event)
        return
    existing = _native_event_by_id(record.event_store, native_event.id)
    if existing is not None:
        _require_same_native_event(existing, native_event)
        return
    try:
        record.event_store.append(native_event)
    except Exception:
        existing = _native_event_by_id(record.event_store, native_event.id)
        if existing is None:
            raise
        _require_same_native_event(existing, native_event)


def _native_event_by_id(store: Any, event_id: str) -> Any | None:
    get_event = getattr(store, "get_event", None)
    if callable(get_event):
        return get_event(event_id)
    return next((event for event in store.iter_events() if event.id == event_id), None)


def _require_same_native_event(existing: Any, expected: Any) -> None:
    if existing != expected:
        raise ValueError("ActiveGraph Agent Run projection event identifier collision")


def _seed_native_events(
    record: _RunRecord,
    events: Iterable[AgentRunEvent],
) -> None:
    for event in events:
        _append_native_event(
            record,
            event,
            event_id=f"memory_agent_projection_{event.sequence:06d}",
            idempotent=True,
        )


def _reconcile_native_projection(
    record: _RunRecord,
    expected_events: Iterable[AgentRunEvent],
) -> None:
    """Repair an interrupted ActiveGraph suffix from the fenced Tenant snapshot."""
    assert record.event_store is not None
    expected = tuple(expected_events)
    native = _native_agent_events(record.event_store)
    shared_length = min(len(native), len(expected))
    if native[:shared_length] != expected[:shared_length]:
        raise ValueError("ActiveGraph Agent Run event stream diverges from its snapshot")
    if len(native) > len(expected):
        raise ValueError("ActiveGraph Agent Run event stream diverges from its snapshot")
    if len(native) < len(expected):
        _seed_native_events(record, expected[len(native) :])
    _refresh_native_projection(record)


def _native_agent_events(store: Any) -> tuple[AgentRunEvent, ...]:
    projected: list[AgentRunEvent] = []
    for native_event in store.iter_events():
        payload = getattr(native_event, "payload", None)
        if not isinstance(payload, dict) or _NATIVE_EVENT_PAYLOAD_KEY not in payload:
            continue
        envelope = payload[_NATIVE_EVENT_PAYLOAD_KEY]
        if not isinstance(envelope, dict):
            raise ValueError("ActiveGraph Agent Run event envelope is invalid")
        if envelope.get("version") != _NATIVE_EVENT_SCHEMA_VERSION:
            raise ValueError("ActiveGraph Agent Run event schema version is unsupported")
        data = envelope.get("data")
        if not isinstance(data, dict):
            raise ValueError("ActiveGraph Agent Run event data is invalid")
        created_at_value = envelope.get("created_at")
        if not isinstance(created_at_value, str):
            raise ValueError("ActiveGraph Agent Run event timestamp is invalid")
        try:
            created_at = datetime.fromisoformat(created_at_value)
        except ValueError as exc:
            raise ValueError("ActiveGraph Agent Run event timestamp is invalid") from exc
        if created_at.tzinfo is None:
            raise ValueError("ActiveGraph Agent Run event timestamp must be timezone-aware")
        projected.append(
            AgentRunEvent(
                sequence=len(projected) + 1,
                type=str(native_event.type),
                data=data,
                created_at=created_at,
            )
        )
    return tuple(projected)


def _refresh_native_projection(record: _RunRecord) -> None:
    if record.event_store is None:
        return
    events = _native_agent_events(record.event_store)
    if not events:
        return
    record.events = list(events)
    view = _replay_view(record)
    record.settings = view.settings
    record.budget = view.budget
    record.state = view.state
    record.model_calls = view.usage.model_calls
    record.tool_calls = view.usage.tool_calls
    record.cost_usd = view.usage.cost_usd
    record.output = view.output
    record.failure = view.failure
    record.resumable = view.resumable


def _snapshot(record: _RunRecord) -> AgentRunSnapshot:
    command = record.command.model_dump(mode="json")
    return AgentRunSnapshot(
        run_id=record.id,
        tenant_id=record.session.tenant_id,
        actor_id=record.session.actor_id,
        actor_kind=record.session.actor_kind,
        roles=tuple(sorted(record.session.roles)),
        subject_user_id=record.session.subject_user_id,
        delegation_id=record.session.delegation_id,
        invocation=record.invocation,
        command=command,
        settings=record.settings,
        budget=record.budget,
        state=record.state,
        model_calls=record.model_calls,
        tool_calls=record.tool_calls,
        cost_usd=record.cost_usd,
        events=tuple(record.events),
        output=record.output,
        failure=record.failure,
        resumable=record.resumable,
    )


def _view(record: _RunRecord) -> AgentRunView:
    return AgentRunView(
        id=record.id,
        tenant_id=record.session.tenant_id,
        actor_id=record.session.actor_id,
        capability=record.invocation.capability,
        state=record.state,
        settings=record.settings,
        budget=record.budget,
        usage=record.usage(),
        events=tuple(record.events),
        output=record.output,
        failure=record.failure,
        resumable=record.resumable,
    )


def _replay_view(record: _RunRecord) -> AgentRunView:
    """Fold durable events into the same public projection without side effects."""
    state = AgentRunState.QUEUED
    settings = record.settings
    budget = record.budget
    output: dict[str, Any] | None = None
    failure: str | None = None
    resumable = False
    model_calls = 0
    tool_calls = 0
    cost_usd = Decimal("0")
    terminal_usage: dict[str, Any] | None = None

    for event in record.events:
        if event.type == "agent.run.queued":
            settings = AgentModelSettings.model_validate(event.data["settings"])
            budget = AgentBudget.model_validate(event.data["budget"])
        elif event.type == "agent.run.started":
            state = AgentRunState.RUNNING
        elif event.type == "agent.run.cancellation_requested":
            state = AgentRunState.CANCEL_REQUESTED
        elif event.type == "agent.model.requested":
            model_calls += 1
        elif event.type == "agent.model.completed":
            cost_usd += Decimal(str(event.data["cost_usd"]))
        elif event.type == "agent.tool.requested":
            tool_calls += 1
        elif event.type == "agent.run.completed":
            state = AgentRunState.COMPLETED
            output = _dict_or_none(event.data.get("output"))
            terminal_usage = _dict_or_none(event.data.get("usage"))
        elif event.type == "agent.run.cancelled":
            state = AgentRunState.CANCELLED
            resumable = True
            terminal_usage = _dict_or_none(event.data.get("usage"))
        elif event.type == "agent.run.budget_exhausted":
            state = AgentRunState.BUDGET_EXHAUSTED
            resumable = True
            output = _dict_or_none(event.data.get("output"))
            terminal_usage = _dict_or_none(event.data.get("usage"))
        elif event.type == "agent.run.failed":
            state = AgentRunState.FAILED
            resumable = True
            failure = str(event.data["error_type"])
            terminal_usage = _dict_or_none(event.data.get("usage"))

    if terminal_usage is not None:
        model_calls = int(terminal_usage["model_calls"])
        tool_calls = int(terminal_usage["tool_calls"])
        cost_usd = Decimal(str(terminal_usage["cost_usd"]))
    return AgentRunView(
        id=record.id,
        tenant_id=record.session.tenant_id,
        actor_id=record.session.actor_id,
        capability=record.invocation.capability,
        state=state,
        settings=settings,
        budget=budget,
        usage=AgentBudgetUsage(
            model_calls=model_calls,
            tool_calls=tool_calls,
            events=len(record.events),
            cost_usd=cost_usd,
        ),
        events=tuple(record.events),
        output=output,
        failure=failure,
        resumable=resumable,
    )


def _dict_or_none(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _require_run_context(session: TenantSession, owner: TenantSession) -> None:
    """Fail closed without revealing another Principal's run exists."""
    context = (
        session.tenant_id,
        session.actor_id,
        session.actor_kind,
        session.subject_user_id,
        session.delegation_id,
    )
    owner_context = (
        owner.tenant_id,
        owner.actor_id,
        owner.actor_kind,
        owner.subject_user_id,
        owner.delegation_id,
    )
    if context != owner_context:
        raise LookupError("Agent Run not found")


def _user_message(content: str) -> Any:
    try:
        from activegraph.llm import LLMMessage  # type: ignore[import-untyped]
    except ImportError:
        return {"role": "user", "content": content}
    return LLMMessage(role="user", content=content)


def _native_backend_or_noop(
    record: _RunRecord,
    provider: AgentModelProvider,
) -> _CooperativeBackend:
    if record.invocation.capability != "knowledge_synthesis":
        return _NoOpBackend()
    try:
        return _ActiveGraphBackend(record, provider)
    except ImportError:
        return _NoOpBackend()


class _ActiveGraphBackend(_CooperativeBackend):
    """Native host that loads the same executable Pack used by the capability."""

    def __init__(
        self,
        record: _RunRecord,
        provider: AgentModelProvider,
    ) -> None:
        from activegraph import Frame, Graph, Runtime  # type: ignore[import-untyped]
        from activegraph.store import replay_into  # type: ignore[import-untyped]

        from agent_memory_service.agents.knowledge_synthesis_pack import (
            KNOWLEDGE_SYNTHESIS_PACK,
        )

        if KNOWLEDGE_SYNTHESIS_PACK is None:
            raise ImportError("ActiveGraph Pack is unavailable")
        graph = Graph(run_id=record.id)
        if record.event_store is not None:
            native_events = list(record.event_store.iter_events())
            replay_into(graph, native_events)
            graph.ids.reseed_from_events(native_events)
        frame = Frame(
            goal="Distill selected private memories into a reviewable Tenant claim",
            id=f"frame-{record.id}",
            permissions=["memory:read:selected", "knowledge:propose"],
        )
        self._runtime = Runtime(
            graph,
            frame=frame,
            budget=record.budget.as_activegraph(),
            llm_provider=provider,
            store=record.event_store,
        )
        self._runtime.load_pack(
            KNOWLEDGE_SYNTHESIS_PACK,
            settings=record.settings.model_dump(mode="python"),
        )
        if not graph.objects(type="knowledge_synthesis_invocation"):
            graph.add_object(
                "knowledge_synthesis_invocation",
                data={
                    "run_id": record.id,
                    "request": getattr(record.command, "request", ""),
                    "source_memory_ids": list(getattr(record.command, "source_memory_ids", ())),
                },
                actor=record.session.actor_id,
                frame_id=frame.id,
            )

    def run_quantum(self) -> None:
        self._runtime.run_quantum(max_queue_events=25, max_seconds=0.25)

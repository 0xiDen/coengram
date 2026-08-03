"""Durable outbox relay, publication projection, and approved-erasure worker."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import SecretStr

from agent_memory_service.agents import (
    AgentRunClaim,
    AgentRunRepository,
    AgentRuntimeModule,
    KnowledgeSynthesisCapability,
    LangChainAnthropicProvider,
    ManagedActiveGraphStoreFactory,
    MemoryModuleKnowledgeSynthesisPort,
)
from agent_memory_service.auth import TokenService
from agent_memory_service.control import ControlModule, ControlNotFound
from agent_memory_service.durable_memory import (
    PrivateMemoryCommand,
    PrivateMemoryCommandType,
)
from agent_memory_service.governance import KnowledgeCandidate
from agent_memory_service.memory import MemoryModule
from agent_memory_service.outbox import (
    GraphProjector,
    OutboxRecord,
    OutboxRelay,
    OutboxSource,
    PrivateMemoryProjector,
    PublicationWorker,
    connect_outbox,
    declare_outbox_topology,
)
from agent_memory_service.platform import PlatformConfig
from agent_memory_service.routing import (
    FileSecretReader,
    RoutedAgentRunRepository,
    RoutedGovernanceStore,
    RoutedTenantMemoryRouter,
)
from agent_memory_service.schema import (
    CONTROL_SCHEMA_REVISION,
    TENANT_SCHEMA_REVISION,
    SchemaRequirement,
    require_schema,
)
from agent_memory_service.stores.memory import TenantMemoryRouter
from agent_memory_service.stores.postgres_control import PostgresControlStore
from agent_memory_service.telemetry import (
    ContentSafeTracer,
    SafeTelemetry,
    start_metrics_server,
)

_LOGGER = logging.getLogger("memory.worker")
_AGENT_RUN_LEASE_SECONDS = 300
_AGENT_RUN_BATCH_SIZE = 100


class MemoryGraphProjector(GraphProjector):
    """Idempotently publish an approved candidate to its routed Tenant graph."""

    def __init__(self, memory: TenantMemoryRouter) -> None:
        self._memory = memory

    async def project(self, candidate: KnowledgeCandidate) -> None:
        await self._memory.for_tenant(candidate.tenant_id).publish_tenant_knowledge(
            candidate.id,
            candidate.claim,
            candidate.confidence,
            candidate.proposer_id,
        )


class DurableMemoryGraphProjector(PrivateMemoryProjector):
    """Apply exact preassigned Memory Items; duplicate delivery is successful."""

    def __init__(self, memory: TenantMemoryRouter) -> None:
        self._memory = memory

    async def project_memory(self, command: PrivateMemoryCommand) -> None:
        store = self._memory.for_tenant(command.tenant_id)
        if command.command_type is PrivateMemoryCommandType.ERASE:
            assert command.target_memory_id is not None
            # Missing means a previous attempt already performed the destructive
            # graph mutation and crashed before committing PostgreSQL completion.
            await store.erase_private(command.owner_principal_id, command.target_memory_id)
            return
        if command.result_item is None:
            raise ValueError("Private Memory Command has no projection result")
        await store.apply_private_item(command.owner_principal_id, command.result_item)


def reauthorize_agent_claim(
    control: ControlModule,
    runs: AgentRunRepository,
    claim: AgentRunClaim,
) -> AgentRunClaim:
    """Refresh delayed authority or durably request cancellation after revocation."""
    persisted_session = claim.snapshot.session()
    try:
        current_session = control.reauthorize_session(persisted_session)
    except ControlNotFound:
        if runs.request_cancellation(persisted_session, claim.run_id) is None:
            raise LookupError("Agent Run not found") from None
        return claim
    refreshed_snapshot = claim.snapshot.model_copy(
        update={"roles": tuple(sorted(current_session.roles))}
    )
    return claim.model_copy(update={"snapshot": refreshed_snapshot})


class RoutedOutboxSource(OutboxSource):
    """Fairly scan healthy Tenant databases while retaining exact claim ownership."""

    def __init__(
        self,
        control: ControlModule,
        governance: RoutedGovernanceStore,
        telemetry: SafeTelemetry | None = None,
    ) -> None:
        self._control = control
        self._governance = governance
        self._claims: dict[tuple[str, str], str] = {}
        self._offset = 0
        self._telemetry = telemetry

    async def claim_outbox(self, *, lease_seconds: int = 60) -> OutboxRecord | None:
        routes = self._control.list_tenant_routes()
        if not routes:
            return None
        ordered = routes[self._offset :] + routes[: self._offset]
        self._offset = (self._offset + 1) % len(routes)
        for route in ordered:
            record = await self._governance.for_tenant(route.tenant_id).claim_outbox(
                lease_seconds=lease_seconds
            )
            if record is not None:
                if record.tenant_id != route.tenant_id:
                    await self._governance.for_tenant(route.tenant_id).release_outbox(
                        record.event_id,
                        record.lock_id,
                        "TenantRouteMismatch",
                    )
                    raise ValueError("Outbox event Tenant does not match its database route")
                self._claims[(record.event_id, record.lock_id)] = route.tenant_id
                if self._telemetry is not None:
                    self._telemetry.observe_outbox(
                        "claimed",
                        lag_seconds=(datetime.now(UTC) - record.created_at).total_seconds(),
                    )
                return record
        return None

    async def mark_outbox_dispatched(self, event_id: str, lock_id: str) -> None:
        tenant_id = self._claim_tenant(event_id, lock_id)
        await self._governance.for_tenant(tenant_id).mark_outbox_dispatched(event_id, lock_id)
        self._claims.pop((event_id, lock_id), None)

    async def release_outbox(self, event_id: str, lock_id: str, error_code: str) -> None:
        tenant_id = self._claim_tenant(event_id, lock_id)
        await self._governance.for_tenant(tenant_id).release_outbox(event_id, lock_id, error_code)
        self._claims.pop((event_id, lock_id), None)

    def _claim_tenant(self, event_id: str, lock_id: str) -> str:
        try:
            return self._claims[(event_id, lock_id)]
        except KeyError as exc:
            raise LookupError("Outbox claim route not found") from exc


async def serve() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = PlatformConfig.from_env()
    tracing = ContentSafeTracer(
        "memory-worker",
        config.otel_endpoint,
        pseudonymizer=config.telemetry_pseudonymizer,
    )
    telemetry = SafeTelemetry(
        pseudonymizer=config.telemetry_pseudonymizer,
        tracer=tracing,
    )
    control_store = PostgresControlStore(config.control_database_url)
    control = ControlModule(control_store, TokenService(control_store))
    secrets = FileSecretReader(config.tenant_credentials_dir)
    memory_router = RoutedTenantMemoryRouter(
        control,
        secrets,
        embedding_model=config.embedding_model,
        embedding_dimensions=config.embedding_dimensions,
    )
    governance = RoutedGovernanceStore(
        control,
        secrets,
        postgres_host=config.tenant_postgres_host,
        postgres_port=config.tenant_postgres_port,
    )
    try:
        if config.verify_schema_on_startup:
            require_schema(
                SchemaRequirement(
                    database_url=config.control_database_url,
                    expected_revision=CONTROL_SCHEMA_REVISION,
                    store_name="Control Store",
                )
            )
            for route in control.list_tenant_routes():
                require_schema(
                    SchemaRequirement(
                        database_url=governance.database_url_for_tenant(route.tenant_id),
                        expected_revision=TENANT_SCHEMA_REVISION,
                        store_name=f"Tenant Store {route.tenant_id}",
                    )
                )
    except Exception:
        await memory_router.close()
        tracing.shutdown()
        raise
    telemetry.set_dependency_health("control_postgres", True)
    telemetry.set_dependency_health("tenant_postgres", True)
    memory = MemoryModule(
        memory_router,
        governance,
        erasures=governance,
        self_approval=control,
        commands=governance,
    )
    agent_runs = RoutedAgentRunRepository(governance)
    activegraph_stores = ManagedActiveGraphStoreFactory(
        governance.database_url_for_tenant,
    )
    agents = AgentRuntimeModule(
        capabilities=(
            KnowledgeSynthesisCapability(MemoryModuleKnowledgeSynthesisPort(memory, memory_router)),
        ),
        provider=LangChainAnthropicProvider(_anthropic_factory(config.anthropic_api_key)),
        repository=agent_runs,
        activegraph_store_factory=activegraph_stores,
        tracer=tracing,
    )
    connection = None
    metrics_server = None
    try:
        connection = await connect_outbox(_amqp_url())
        topology = await declare_outbox_topology(
            connection,
            exchange_name=os.getenv("MEMORY_AMQP_EXCHANGE", "memory.events.v1"),
            queue_name=os.getenv("MEMORY_AMQP_GRAPH_QUEUE", "memory.graph.apply.v1"),
            dead_letter_exchange_name=os.getenv(
                "MEMORY_AMQP_DEAD_LETTER_EXCHANGE",
                "memory.dead-letter.v1",
            ),
            dead_letter_queue_name=os.getenv(
                "MEMORY_AMQP_DEAD_LETTER_QUEUE",
                "memory.graph.dead-letter.v1",
            ),
        )
        telemetry.set_dependency_health("rabbitmq", True)
        relay = OutboxRelay(RoutedOutboxSource(control, governance, telemetry), topology.exchange)
        publication = PublicationWorker(
            governance,
            MemoryGraphProjector(memory_router),
            DurableMemoryGraphProjector(memory_router),
            telemetry,
        )
        await publication.start(topology.queue)
        metrics_server = await start_metrics_server(
            telemetry,
            host="0.0.0.0",
            port=_metrics_port(),
        )
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for handled_signal in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(handled_signal, stop.set)
        _log("worker.started")
        telemetry.observe_worker("started")
        _observe_unused_token_signals(control_store, telemetry)
        next_token_signal_at = asyncio.get_running_loop().time() + 300.0
        while not stop.is_set():
            try:
                dispatched = await relay.relay_once(limit=100)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                dispatched = 0
                telemetry.observe_outbox("failed")
                telemetry.set_dependency_health("rabbitmq", False)
                _log("outbox.relay_failed", error_type=type(exc).__name__)
            if dispatched:
                telemetry.observe_outbox("dispatched", count=dispatched)
                telemetry.set_dependency_health("rabbitmq", True)
                _log("outbox.dispatched", count=dispatched)
            if asyncio.get_running_loop().time() >= next_token_signal_at:
                _observe_unused_token_signals(control_store, telemetry)
                next_token_signal_at = asyncio.get_running_loop().time() + 300.0
            for route in control.list_tenant_routes():
                try:
                    for _ in range(_AGENT_RUN_BATCH_SIZE):
                        claim = agent_runs.claim_runnable(
                            route.tenant_id,
                            lease_seconds=_AGENT_RUN_LEASE_SECONDS,
                        )
                        if claim is None:
                            break
                        claim = reauthorize_agent_claim(control, agent_runs, claim)
                        async with telemetry.operation("synthesis", claim.snapshot.session()):
                            run = await agents.run_claimed(claim)
                        budget_dimension = _budget_dimension(run)
                        telemetry.observe_agent_run(
                            run.id,
                            run.state.value,
                            model_calls=run.usage.model_calls,
                            tool_calls=run.usage.tool_calls,
                            events=run.usage.events,
                            cost_usd=float(run.usage.cost_usd),
                            budget_dimension=budget_dimension,
                        )
                        if run.state.value == "completed":
                            telemetry.set_dependency_health("anthropic", True)
                        elif run.state.value == "failed":
                            telemetry.set_dependency_health("anthropic", False)
                        _log(
                            "agent_run.progressed",
                            tenant_ref=telemetry.reference("tenant", route.tenant_id),
                            run_ref=telemetry.reference("run", claim.run_id),
                            state=run.state.value,
                        )
                    erased = await memory.erase_next(route.tenant_id)
                    if erased is not None:
                        _log(
                            "erasure.completed",
                            tenant_ref=telemetry.reference("tenant", route.tenant_id),
                            request_ref=telemetry.reference("erasure_request", erased.id),
                        )
                    telemetry.observe_worker("cycle_success")
                    telemetry.observe_route("resolved")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    telemetry.observe_worker("cycle_failed")
                    telemetry.observe_route("error")
                    _log(
                        "worker.tenant_cycle_failed",
                        tenant_ref=telemetry.reference("tenant", route.tenant_id),
                        error_type=type(exc).__name__,
                    )
            try:
                await asyncio.wait_for(stop.wait(), timeout=1.0)
            except TimeoutError:
                pass
    finally:
        _log("worker.stopped")
        telemetry.observe_worker("stopped")
        try:
            if metrics_server is not None:
                metrics_server.close()
                await metrics_server.wait_closed()
        finally:
            try:
                if connection is not None:
                    await connection.close()
            finally:
                try:
                    activegraph_stores.close()
                finally:
                    await memory_router.close()
                    tracing.shutdown()


def _amqp_url() -> str:
    password_file = os.getenv("MEMORY_AMQP_PASSWORD_FILE", "").strip()
    if not password_file:
        raise RuntimeError("MEMORY_AMQP_PASSWORD_FILE is required")
    password_path = Path(password_file)
    if password_path.is_symlink() or not password_path.is_file():
        raise RuntimeError("RabbitMQ password secret is not a regular file")
    password = password_path.read_text(encoding="utf-8").strip()
    if not password:
        raise RuntimeError("RabbitMQ password secret is empty")
    user = os.getenv("MEMORY_AMQP_USER", "memory")
    host = os.getenv("MEMORY_AMQP_HOST", "rabbitmq")
    port = os.getenv("MEMORY_AMQP_PORT", "5672")
    vhost = os.getenv("MEMORY_AMQP_VHOST", "memory")
    if not vhost or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for character in vhost
    ):
        raise RuntimeError("MEMORY_AMQP_VHOST must use a safe private-vhost name")
    return (
        f"amqp://{quote(user, safe='')}:{quote(password, safe='')}@"
        f"{host}:{port}/{quote(vhost, safe='')}"
    )


def _anthropic_factory(api_key: str | None) -> Any:
    if api_key is None:
        return None

    def factory(**kwargs: Any) -> Any:
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(api_key=SecretStr(api_key), **kwargs)

    return factory


def _metrics_port() -> int:
    try:
        port = int(os.getenv("MEMORY_WORKER_METRICS_PORT", "9090"))
    except ValueError as exc:
        raise RuntimeError("MEMORY_WORKER_METRICS_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("MEMORY_WORKER_METRICS_PORT must be between 1 and 65535")
    return port


def _log(event: str, **fields: object) -> None:
    allowed_events = {
        "worker.started",
        "worker.stopped",
        "outbox.relay_failed",
        "outbox.dispatched",
        "agent_run.progressed",
        "erasure.completed",
        "worker.tenant_cycle_failed",
    }
    safe_fields: dict[str, object] = {}
    count = fields.get("count")
    if isinstance(count, int) and count >= 0:
        safe_fields["count"] = count
    error_type = fields.get("error_type")
    if isinstance(error_type, str) and error_type.isidentifier() and len(error_type) <= 128:
        safe_fields["error_type"] = error_type
    state = fields.get("state")
    if state in {
        "queued",
        "running",
        "cancel_requested",
        "cancelled",
        "completed",
        "budget_exhausted",
        "failed",
    }:
        safe_fields["state"] = state
    for key in ("tenant_ref", "run_ref", "request_ref"):
        value = fields.get(key)
        if (
            isinstance(value, str)
            and value.startswith("o_")
            and len(value) == 18
            and all(character in "0123456789abcdef" for character in value[2:])
        ):
            safe_fields[key] = value
    _LOGGER.info(
        json.dumps(
            {"event": event if event in allowed_events else "worker.event", **safe_fields},
            sort_keys=True,
        )
    )


def _observe_unused_token_signals(
    control_store: PostgresControlStore,
    telemetry: SafeTelemetry,
) -> None:
    checked_at = datetime.now(UTC)
    try:
        never_used, inactive = control_store.token_usage_signals(
            checked_at=checked_at,
            inactive_before=checked_at - timedelta(days=30),
        )
    except Exception:
        telemetry.set_dependency_health("control_postgres", False)
        return
    telemetry.set_dependency_health("control_postgres", True)
    telemetry.set_unused_token_signal("never_used", never_used)
    telemetry.set_unused_token_signal("inactive_30d", inactive)


def _budget_dimension(run: object) -> str | None:
    from agent_memory_service.agents import AgentRunView

    if not isinstance(run, AgentRunView) or run.state.value != "budget_exhausted":
        return None
    if not run.events:
        return None
    dimension = run.events[-1].data.get("dimension")
    if dimension in {"model_calls", "tool_calls", "events", "seconds", "cost_usd"}:
        return str(dimension)
    return None


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()

"""Durable RabbitMQ delivery for committed Tenant outbox events."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import aio_pika
from aio_pika.abc import (
    AbstractChannel,
    AbstractExchange,
    AbstractIncomingMessage,
    AbstractQueue,
    AbstractRobustConnection,
)

from agent_memory_service.durable_memory import (
    PrivateMemoryCommand,
    PrivateMemoryCommandState,
)
from agent_memory_service.governance import (
    CandidateStatus,
    KnowledgeCandidate,
    PublicationEvent,
)
from agent_memory_service.telemetry import SafeTelemetry

PUBLICATION_ROUTING_KEY = "knowledge.publish"
_LOGGER = logging.getLogger("memory.worker.publication")
_SAFE_TOPOLOGY_NAME = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    event_id: str
    tenant_id: str
    aggregate_id: str
    event_type: str
    created_at: datetime
    lock_id: str
    aggregate_type: str = "knowledge_candidate"

    def message(self) -> aio_pika.Message:
        envelope = {
            "version": 1,
            "event_id": self.event_id,
            "tenant_id": self.tenant_id,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "event_type": self.event_type,
        }
        if self.event_type == "knowledge.approved":
            envelope["candidate_id"] = self.aggregate_id
        return aio_pika.Message(
            body=json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8"),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=self.event_id,
            timestamp=self.created_at,
            headers={"schema_version": 1},
        )


@dataclass(frozen=True, slots=True)
class PublicationEnvelope:
    event_id: str
    tenant_id: str
    aggregate_type: str
    aggregate_id: str
    event_type: str

    @property
    def candidate_id(self) -> str:
        if self.event_type != "knowledge.approved":
            raise ValueError("Envelope does not identify a Knowledge Candidate")
        return self.aggregate_id

    @classmethod
    def decode(cls, body: bytes) -> PublicationEnvelope:
        try:
            value = json.loads(body)
            if not isinstance(value, dict) or value.get("version") != 1:
                raise ValueError("Unsupported publication envelope")
            fields = ("event_id", "tenant_id", "event_type")
            if not all(isinstance(value.get(field), str) for field in fields):
                raise ValueError("Invalid publication envelope")
            event_type = value["event_type"]
            if event_type not in {"knowledge.approved", "memory.command.accepted"}:
                raise ValueError("Unsupported publication event type")
            aggregate_id = value.get("aggregate_id", value.get("candidate_id"))
            if not isinstance(aggregate_id, str):
                raise ValueError("Invalid publication envelope")
            aggregate_type = value.get(
                "aggregate_type",
                "knowledge_candidate" if event_type == "knowledge.approved" else None,
            )
            if not isinstance(aggregate_type, str):
                raise ValueError("Invalid publication envelope")
            expected_aggregate = (
                "knowledge_candidate"
                if event_type == "knowledge.approved"
                else "private_memory_command"
            )
            if aggregate_type != expected_aggregate:
                raise ValueError("Publication envelope aggregate does not match its event type")
            return cls(
                event_id=value["event_id"],
                tenant_id=value["tenant_id"],
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                event_type=event_type,
            )
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
            raise ValueError("Invalid publication envelope") from exc


class OutboxSource(Protocol):
    async def claim_outbox(self, *, lease_seconds: int = 60) -> OutboxRecord | None: ...

    async def mark_outbox_dispatched(self, event_id: str, lock_id: str) -> None: ...

    async def release_outbox(self, event_id: str, lock_id: str, error_code: str) -> None: ...


class GraphProjector(Protocol):
    """Idempotently materialize one approved candidate in its routed graph."""

    async def project(self, candidate: KnowledgeCandidate) -> None: ...


class PrivateMemoryProjector(Protocol):
    """Idempotently materialize one accepted command in its exact Tenant graph."""

    async def project_memory(self, command: PrivateMemoryCommand) -> None: ...


class PublicationState(Protocol):
    async def get_publication(
        self,
        event_id: str,
        tenant_id: str,
        candidate_id: str,
    ) -> PublicationEvent | None: ...

    async def get_candidate(
        self,
        tenant_id: str,
        candidate_id: str,
    ) -> KnowledgeCandidate | None: ...

    async def mark_published(
        self,
        tenant_id: str,
        candidate_id: str,
    ) -> KnowledgeCandidate: ...

    async def get_memory_command(
        self,
        tenant_id: str,
        command_id: str,
    ) -> PrivateMemoryCommand | None: ...

    async def mark_memory_command_applied(
        self,
        tenant_id: str,
        command_id: str,
    ) -> PrivateMemoryCommand: ...

    async def schedule_outbox_redelivery(
        self,
        tenant_id: str,
        event_id: str,
        operator_id: str,
    ) -> None: ...


class TenantPublicationRouter(Protocol):
    def for_tenant(self, tenant_id: str) -> PublicationState:
        """Resolve exactly one Tenant Operations Store or fail closed."""


class SingleTenantPublicationRouter:
    """Explicit fail-closed route for a worker assigned to one Tenant database."""

    def __init__(self, tenant_id: str, state: PublicationState) -> None:
        self._tenant_id = tenant_id
        self._state = state

    def for_tenant(self, tenant_id: str) -> PublicationState:
        if tenant_id != self._tenant_id:
            raise LookupError("Tenant Operations Store route not found")
        return self._state


@dataclass(frozen=True, slots=True)
class OutboxTopology:
    channel: AbstractChannel
    exchange: AbstractExchange
    queue: AbstractQueue
    dead_letter_exchange: AbstractExchange
    dead_letter_queue: AbstractQueue


@dataclass(frozen=True, slots=True)
class DeadLetterRedriveResult:
    tenant_id: str
    event_id: str
    event_type: str
    state: str = "scheduled"


async def connect_outbox(amqp_url: str) -> AbstractRobustConnection:
    """Open a reconnecting AMQP connection without declaring global state implicitly."""
    return await aio_pika.connect_robust(amqp_url)


async def declare_outbox_topology(
    connection: AbstractRobustConnection,
    *,
    namespace: str = "memory",
    retry_limit: int = 5,
    exchange_name: str | None = None,
    queue_name: str | None = None,
    dead_letter_exchange_name: str | None = None,
    dead_letter_queue_name: str | None = None,
) -> OutboxTopology:
    """Declare durable quorum delivery and dead-letter topology idempotently."""
    if retry_limit < 1:
        raise ValueError("retry_limit must be positive")
    channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
    selected_exchange = _topology_name(exchange_name, f"{namespace}.outbox")
    selected_queue = _topology_name(queue_name, f"{namespace}.publication")
    selected_dead_letter_exchange = _topology_name(
        dead_letter_exchange_name,
        f"{namespace}.outbox.dlx",
    )
    selected_dead_letter_queue = _topology_name(
        dead_letter_queue_name,
        f"{namespace}.publication.dead",
    )

    exchange = await channel.declare_exchange(
        selected_exchange,
        aio_pika.ExchangeType.DIRECT,
        durable=True,
    )
    dead_letter_exchange = await channel.declare_exchange(
        selected_dead_letter_exchange,
        aio_pika.ExchangeType.DIRECT,
        durable=True,
    )
    queue = await channel.declare_queue(
        selected_queue,
        durable=True,
        arguments={
            "x-queue-type": "quorum",
            "x-delivery-limit": retry_limit,
            "x-dead-letter-exchange": selected_dead_letter_exchange,
        },
    )
    dead_letter_queue = await channel.declare_queue(
        selected_dead_letter_queue,
        durable=True,
        arguments={"x-queue-type": "quorum"},
    )
    await queue.bind(exchange, routing_key=PUBLICATION_ROUTING_KEY)
    await dead_letter_queue.bind(
        dead_letter_exchange,
        routing_key=PUBLICATION_ROUTING_KEY,
    )
    await channel.set_qos(prefetch_count=1)
    return OutboxTopology(
        channel=channel,
        exchange=exchange,
        queue=queue,
        dead_letter_exchange=dead_letter_exchange,
        dead_letter_queue=dead_letter_queue,
    )


def _topology_name(configured: str | None, fallback: str) -> str:
    selected = fallback if configured is None else configured
    if _SAFE_TOPOLOGY_NAME.fullmatch(selected) is None:
        raise ValueError("AMQP topology name is invalid")
    return selected


class OutboxRelay:
    """Relay committed PostgreSQL outbox records with publisher confirmations."""

    def __init__(self, source: OutboxSource, exchange: AbstractExchange) -> None:
        self._source = source
        self._exchange = exchange

    async def relay_once(self, *, limit: int = 100) -> int:
        if limit < 1:
            raise ValueError("limit must be positive")
        dispatched = 0
        while dispatched < limit:
            record = await self._source.claim_outbox()
            if record is None:
                break
            try:
                await self._exchange.publish(
                    record.message(),
                    routing_key=PUBLICATION_ROUTING_KEY,
                    mandatory=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._source.release_outbox(
                    record.event_id,
                    record.lock_id,
                    type(exc).__name__,
                )
                raise
            await self._source.mark_outbox_dispatched(record.event_id, record.lock_id)
            dispatched += 1
        return dispatched


class DeadLetterRedriver:
    """Reopen one exact dead letter from PostgreSQL truth and remove its broker copy."""

    def __init__(self, routing: TenantPublicationRouter) -> None:
        self._routing = routing

    async def redrive_exact(
        self,
        queue: AbstractQueue,
        *,
        tenant_id: str,
        event_id: str,
        operator_id: str,
        max_scan: int = 1_000,
    ) -> DeadLetterRedriveResult:
        if not tenant_id or not event_id or not operator_id:
            raise ValueError("Tenant, event, and operator identifiers are required")
        if not 1 <= max_scan <= 10_000:
            raise ValueError("Dead-letter scan limit must be between 1 and 10000")
        held: list[AbstractIncomingMessage] = []
        try:
            for _ in range(max_scan):
                message = await queue.get(fail=False)
                if message is None:
                    break
                try:
                    envelope = PublicationEnvelope.decode(message.body)
                except ValueError:
                    held.append(message)
                    continue
                if envelope.tenant_id != tenant_id or envelope.event_id != event_id:
                    held.append(message)
                    continue
                try:
                    await self._routing.for_tenant(tenant_id).schedule_outbox_redelivery(
                        tenant_id,
                        event_id,
                        operator_id,
                    )
                    await message.ack()
                except BaseException:
                    await message.reject(requeue=True)
                    raise
                return DeadLetterRedriveResult(
                    tenant_id=tenant_id,
                    event_id=event_id,
                    event_type=envelope.event_type,
                )
        finally:
            for message in held:
                await message.reject(requeue=True)
        raise LookupError("Exact dead-letter event was not found within the scan limit")


class PublicationWorker:
    """Project deliveries idempotently and acknowledge only committed success."""

    def __init__(
        self,
        routing: TenantPublicationRouter,
        projector: GraphProjector,
        memory_projector: PrivateMemoryProjector | None = None,
        telemetry: SafeTelemetry | None = None,
    ) -> None:
        self._routing = routing
        self._projector = projector
        self._memory_projector = memory_projector
        self._telemetry = telemetry

    async def start(self, queue: AbstractQueue) -> str:
        return await queue.consume(self.handle)

    async def handle(self, message: AbstractIncomingMessage) -> None:
        try:
            envelope = PublicationEnvelope.decode(message.body)
            governance = self._routing.for_tenant(envelope.tenant_id)
            if envelope.event_type == "memory.command.accepted":
                await self._handle_memory(governance, envelope)
                await message.ack()
                return
            publication = await governance.get_publication(
                envelope.event_id,
                envelope.tenant_id,
                envelope.candidate_id,
            )
            if publication is None:
                raise LookupError("Publication event not found")
            candidate = await governance.get_candidate(
                envelope.tenant_id,
                envelope.candidate_id,
            )
            if candidate is None:
                raise LookupError("Knowledge Candidate not found")
            if candidate.status is CandidateStatus.PUBLISHED:
                await message.ack()
                return
            if candidate.status is not CandidateStatus.PUBLISHING:
                raise ValueError("Knowledge Candidate is not awaiting publication")
            await self._projector.project(candidate)
            await governance.mark_published(envelope.tenant_id, envelope.candidate_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._telemetry is not None:
                self._telemetry.observe_outbox("projection_retry")
            _LOGGER.warning(
                json.dumps(
                    {
                        "event": "publication.retry_scheduled",
                        "error_type": type(exc).__name__,
                        "message_ref": (
                            self._telemetry.reference(
                                "message", str(message.message_id or "unknown")
                            )
                            if self._telemetry is not None
                            else "unavailable"
                        ),
                    },
                    sort_keys=True,
                )
            )
            await message.reject(requeue=True)
            return
        await message.ack()

    async def _handle_memory(
        self,
        state: PublicationState,
        envelope: PublicationEnvelope,
    ) -> None:
        if self._memory_projector is None:
            raise RuntimeError("Private Memory projection is not configured")
        command = await state.get_memory_command(
            envelope.tenant_id,
            envelope.aggregate_id,
        )
        if command is None:
            raise LookupError("Private Memory Command not found")
        if command.state is PrivateMemoryCommandState.APPLIED:
            return
        if command.state is not PrivateMemoryCommandState.ACCEPTED:
            raise ValueError("Private Memory Command is not awaiting projection")
        await self._memory_projector.project_memory(command)
        await state.mark_memory_command_applied(envelope.tenant_id, command.id)

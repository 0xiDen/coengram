"""Host-only exact dead-letter recovery over the authoritative Tenant outbox."""

from __future__ import annotations

import asyncio

from agent_memory_service.outbox import (
    DeadLetterRedriver,
    DeadLetterRedriveResult,
    connect_outbox,
    declare_outbox_topology,
)
from agent_memory_service.routing import RoutedGovernanceStore


class HostDeadLetterRedriveService:
    """Synchronous operator seam; Docker/RabbitMQ access never reaches the gateway."""

    def __init__(
        self,
        governance: RoutedGovernanceStore,
        *,
        amqp_url: str,
        exchange_name: str,
        queue_name: str,
        dead_letter_exchange_name: str,
        dead_letter_queue_name: str,
    ) -> None:
        if not amqp_url.strip():
            raise ValueError("AMQP URL cannot be empty")
        self._governance = governance
        self._amqp_url = amqp_url
        self._topology_names = (
            exchange_name,
            queue_name,
            dead_letter_exchange_name,
            dead_letter_queue_name,
        )

    def redrive(
        self,
        *,
        tenant_id: str,
        event_id: str,
        operator_id: str,
    ) -> DeadLetterRedriveResult:
        return asyncio.run(
            self._redrive(
                tenant_id=tenant_id,
                event_id=event_id,
                operator_id=operator_id,
            )
        )

    async def _redrive(
        self,
        *,
        tenant_id: str,
        event_id: str,
        operator_id: str,
    ) -> DeadLetterRedriveResult:
        connection = await connect_outbox(self._amqp_url)
        try:
            topology = await declare_outbox_topology(
                connection,
                exchange_name=self._topology_names[0],
                queue_name=self._topology_names[1],
                dead_letter_exchange_name=self._topology_names[2],
                dead_letter_queue_name=self._topology_names[3],
            )
            return await DeadLetterRedriver(self._governance).redrive_exact(
                topology.dead_letter_queue,
                tenant_id=tenant_id,
                event_id=event_id,
                operator_id=operator_id,
            )
        finally:
            await connection.close()

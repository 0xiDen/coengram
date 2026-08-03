from __future__ import annotations

import io
import json
from collections.abc import Sequence

import pytest

from agent_memory_service.auth import TokenService
from agent_memory_service.cli import run_cli
from agent_memory_service.control import ControlModule, InMemoryControlStore
from agent_memory_service.outbox import (
    DeadLetterRedriver,
    DeadLetterRedriveResult,
)


class _Message:
    def __init__(self, tenant_id: str, event_id: str) -> None:
        self.body = json.dumps(
            {
                "version": 1,
                "tenant_id": tenant_id,
                "event_id": event_id,
                "aggregate_type": "knowledge_candidate",
                "aggregate_id": f"candidate-{event_id}",
                "event_type": "knowledge.approved",
            }
        ).encode()
        self.acked = False
        self.requeued = False

    async def ack(self) -> None:
        self.acked = True

    async def reject(self, *, requeue: bool) -> None:
        self.requeued = requeue


class _Queue:
    def __init__(self, messages: Sequence[_Message]) -> None:
        self._messages = list(messages)

    async def get(self, *, fail: bool) -> _Message | None:
        assert fail is False
        return self._messages.pop(0) if self._messages else None


class _State:
    def __init__(self) -> None:
        self.scheduled: list[tuple[str, str, str]] = []

    async def schedule_outbox_redelivery(
        self,
        tenant_id: str,
        event_id: str,
        operator_id: str,
    ) -> None:
        self.scheduled.append((tenant_id, event_id, operator_id))


class _Router:
    def __init__(self, state: _State) -> None:
        self.state = state

    def for_tenant(self, tenant_id: str) -> _State:
        assert tenant_id == "tenant-a"
        return self.state


@pytest.mark.asyncio
async def test_exact_dead_letter_reopens_postgres_and_requeues_unmatched_messages() -> None:
    other = _Message("tenant-b", "event-other")
    target = _Message("tenant-a", "event-target")
    state = _State()

    result = await DeadLetterRedriver(_Router(state)).redrive_exact(  # type: ignore[arg-type]
        _Queue((other, target)),  # type: ignore[arg-type]
        tenant_id="tenant-a",
        event_id="event-target",
        operator_id="operator-jane",
    )

    assert result == DeadLetterRedriveResult(
        tenant_id="tenant-a",
        event_id="event-target",
        event_type="knowledge.approved",
    )
    assert state.scheduled == [("tenant-a", "event-target", "operator-jane")]
    assert target.acked is True
    assert other.requeued is True


@pytest.mark.asyncio
async def test_missing_exact_dead_letter_requeues_every_scanned_message() -> None:
    other = _Message("tenant-b", "event-other")

    with pytest.raises(LookupError, match="Exact dead-letter"):
        await DeadLetterRedriver(_Router(_State())).redrive_exact(  # type: ignore[arg-type]
            _Queue((other,)),  # type: ignore[arg-type]
            tenant_id="tenant-a",
            event_id="event-target",
            operator_id="operator-jane",
        )

    assert other.requeued is True


class _Operator:
    def redrive(
        self,
        *,
        tenant_id: str,
        event_id: str,
        operator_id: str,
    ) -> DeadLetterRedriveResult:
        assert (tenant_id, event_id, operator_id) == (
            "tenant-a",
            "event-target",
            "operator-jane",
        )
        return DeadLetterRedriveResult(
            tenant_id=tenant_id,
            event_id=event_id,
            event_type="knowledge.approved",
        )


def test_operator_cli_requires_exact_tenant_confirmation_and_reports_no_content() -> None:
    control = ControlModule(InMemoryControlStore(), TokenService(InMemoryControlStore()))
    output = io.StringIO()

    run_cli(
        [
            "outbox",
            "redrive",
            "--tenant-id",
            "tenant-a",
            "--event-id",
            "event-target",
            "--operator-id",
            "operator-jane",
            "--confirm",
            "tenant-a",
        ],
        control,
        output,
        dead_letters=_Operator(),
    )

    assert json.loads(output.getvalue()) == {
        "event_id": "event-target",
        "event_type": "knowledge.approved",
        "operation": "redrive",
        "state": "scheduled",
        "tenant_id": "tenant-a",
    }

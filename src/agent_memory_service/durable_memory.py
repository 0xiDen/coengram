"""Authoritative private-memory commands projected asynchronously to a Tenant graph."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from agent_memory_service.lifecycle import CorrectMemory
from agent_memory_service.models import MemoryItem, PrivateMemoryInspection, RetainMemory


class PrivateMemoryCommandType(StrEnum):
    RETAIN = "retain"
    CORRECT = "correct"
    IMPORT = "import"
    ERASE = "erase"


class PrivateMemoryCommandState(StrEnum):
    ACCEPTED = "accepted"
    APPLIED = "applied"
    FAILED = "failed"


class PrivateMemoryCommand(BaseModel):
    """Content-bearing command loaded only from the exact Tenant Store route."""

    model_config = ConfigDict(frozen=True)

    id: str
    tenant_id: str
    actor_id: str
    owner_principal_id: str
    command_type: PrivateMemoryCommandType
    idempotency_key: str
    target_memory_id: str | None
    result_item: MemoryItem | None
    erasure_request_id: str | None
    state: PrivateMemoryCommandState
    created_at: datetime


class ImportAcceptance(BaseModel):
    model_config = ConfigDict(frozen=True)

    item: MemoryItem
    accepted: bool


class PrivateMemoryCommandStore(Protocol):
    """Accept commands durably before any eventually consistent graph mutation."""

    async def accept_retain(
        self,
        tenant_id: str,
        actor_id: str,
        owner_principal_id: str,
        command: RetainMemory,
    ) -> MemoryItem: ...

    async def accept_correction(
        self,
        tenant_id: str,
        actor_id: str,
        owner_principal_id: str,
        command: CorrectMemory,
    ) -> MemoryItem: ...

    async def accept_import(
        self,
        tenant_id: str,
        actor_id: str,
        owner_principal_id: str,
        item: MemoryItem,
    ) -> ImportAcceptance: ...

    async def get_memory_command(
        self,
        tenant_id: str,
        command_id: str,
    ) -> PrivateMemoryCommand | None: ...

    async def get_memory_command_by_result(
        self,
        tenant_id: str,
        result_memory_id: str,
    ) -> PrivateMemoryCommand | None: ...

    async def mark_memory_command_applied(
        self,
        tenant_id: str,
        command_id: str,
    ) -> PrivateMemoryCommand: ...

    async def list_private_memory_state(
        self,
        tenant_id: str,
        owner_principal_id: str,
    ) -> tuple[PrivateMemoryInspection, ...]: ...

    async def list_private_memory_items(
        self,
        tenant_id: str,
        owner_principal_id: str,
    ) -> tuple[MemoryItem, ...]: ...

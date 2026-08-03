"""Stable application contracts for invoking and observing agents."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AgentRunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in {
            self.CANCELLED,
            self.COMPLETED,
            self.BUDGET_EXHAUSTED,
            self.FAILED,
        }


class AgentBudget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_model_calls: int = Field(default=3, ge=1)
    max_tool_calls: int = Field(default=10, ge=1)
    max_events: int = Field(default=100, ge=1)
    max_seconds: float = Field(default=120.0, gt=0)
    max_cost_usd: Decimal = Field(default=Decimal("0.25"), gt=0)

    def as_activegraph(self) -> dict[str, Any]:
        return {
            "max_llm_calls": self.max_model_calls,
            "max_tool_calls": self.max_tool_calls,
            "max_events": self.max_events,
            "max_seconds": self.max_seconds,
            "max_cost_usd": self.max_cost_usd,
        }


class AgentBudgetUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model_calls: int = 0
    tool_calls: int = 0
    events: int = 0
    cost_usd: Decimal = Decimal("0")


class AgentModelSettings(BaseModel):
    """Material provider settings persisted with every run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = "anthropic"
    adapter: str = "langchain-anthropic"
    model: Literal["claude-sonnet-5"] = "claude-sonnet-5"
    max_tokens: int = Field(default=4096, ge=1)
    temperature: float = Field(default=1.0, ge=1.0, le=1.0)
    top_p: float = Field(default=1.0, ge=1.0, le=1.0)
    timeout_seconds: float = Field(default=60.0, gt=0)
    structured_output_mode: Literal["prompt"] = "prompt"


class AgentInvocation(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        str_strip_whitespace=True,
        extra="forbid",
    )

    capability: str = Field(min_length=1, max_length=128)
    input: dict[str, Any]
    idempotency_key: str = Field(min_length=1, max_length=255)
    reply_context: dict[str, str] | None = None


class AgentRunEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence: int = Field(ge=1)
    type: str
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AgentRunView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    tenant_id: str
    actor_id: str
    capability: str
    state: AgentRunState
    settings: AgentModelSettings
    budget: AgentBudget
    usage: AgentBudgetUsage
    events: tuple[AgentRunEvent, ...]
    output: dict[str, Any] | None = None
    failure: str | None = None
    resumable: bool = False

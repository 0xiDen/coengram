"""Versioned native ActiveGraph Pack and executable synthesis workflow.

The Pack owns the use-case ordering.  The application host supplies only the
deep Agent Runtime API (budgeted model calls, tools and durable events) and the
narrow Memory port.  Keeping the workflow here prevents a nominal Pack from
being loaded while an unrelated host capability performs the real work.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_memory_service.governance import KnowledgeCandidateView, ProposeKnowledge
from agent_memory_service.models import MemoryItem, TenantSession

if TYPE_CHECKING:
    from agent_memory_service.agents.runtime import AgentRunContext


class KnowledgeSynthesisInput(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        str_strip_whitespace=True,
        extra="forbid",
    )

    request: str = Field(min_length=1, max_length=4_000)
    source_memory_ids: tuple[str, ...] = Field(min_length=1, max_length=100)

    @field_validator("source_memory_ids")
    @classmethod
    def _validate_source_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("Selected Private Memory references must be unique")
        return value


class KnowledgeSynthesisDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    claim: str = Field(min_length=1, max_length=16_000)
    confidence: float = Field(ge=0.0, le=1.0)
    duplicate_memory_ids: tuple[str, ...] = ()
    conflicting_memory_ids: tuple[str, ...] = ()


class KnowledgeSynthesisResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate: KnowledgeCandidateView
    duplicate_memory_ids: tuple[str, ...] = ()
    conflicting_memory_ids: tuple[str, ...] = ()


class KnowledgeSynthesisMemoryPort(Protocol):
    """Only the reads/effect the Pack is allowed to perform."""

    async def read_selected_private(
        self,
        session: TenantSession,
        source_memory_ids: tuple[str, ...],
    ) -> tuple[MemoryItem, ...]: ...

    async def read_tenant_knowledge(
        self,
        session: TenantSession,
    ) -> tuple[MemoryItem, ...]: ...

    async def propose(
        self,
        session: TenantSession,
        command: ProposeKnowledge,
    ) -> KnowledgeCandidateView: ...


class KnowledgeSynthesisInvocationObject(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    request: str = Field(min_length=1, max_length=4_000)
    source_memory_ids: tuple[str, ...] = Field(min_length=1, max_length=100)


class KnowledgeSynthesisOutputObject(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    claim: str = Field(min_length=1, max_length=16_000)
    confidence: float = Field(ge=0.0, le=1.0)
    duplicate_memory_ids: tuple[str, ...] = ()
    conflicting_memory_ids: tuple[str, ...] = ()


class KnowledgeSynthesisPackSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = "anthropic"
    adapter: str = "langchain-anthropic"
    model: Literal["claude-sonnet-5"] = "claude-sonnet-5"
    max_tokens: int = 4096
    temperature: float = Field(default=1.0, ge=1.0, le=1.0)
    top_p: float = Field(default=1.0, ge=1.0, le=1.0)
    timeout_seconds: float = 60.0
    structured_output_mode: Literal["prompt"] = "prompt"


class KnowledgeSynthesisPackWorkflow:
    """Executable workflow shipped with the native Pack.

    Every external operation goes through ``AgentRunContext`` so ActiveGraph's
    Tenant event stream remains authoritative and the shared runtime enforces
    model/tool/event/time/cost budgets.  Events contain identifiers and counts
    only; Private Memory and model content never enter operational telemetry.
    """

    name = "knowledge_synthesis"
    version = "1.1.0"

    async def execute(
        self,
        context: AgentRunContext,
        command: KnowledgeSynthesisInput,
        memory: KnowledgeSynthesisMemoryPort,
    ) -> KnowledgeSynthesisResult:
        selected = await context.tool(
            "knowledge_synthesis.read_selected_private",
            lambda: memory.read_selected_private(
                context.session,
                command.source_memory_ids,
            ),
        )
        context.emit(
            "knowledge_synthesis.private_memory_loaded",
            {"source_count": len(selected)},
        )

        tenant_knowledge = await context.tool(
            "knowledge_synthesis.read_tenant_knowledge",
            lambda: memory.read_tenant_knowledge(context.session),
        )
        context.emit(
            "knowledge_synthesis.tenant_knowledge_recalled",
            {"knowledge_count": len(tenant_knowledge)},
        )

        draft = context.complete(
            system=(
                "Distill only the explicitly selected private memories into one safe "
                "Tenant Knowledge claim. Compare it with published Tenant Knowledge, "
                "return exact duplicate/conflict Tenant Knowledge IDs, and never "
                "approve, reject, publish, or administer the resulting proposal."
            ),
            user=_synthesis_prompt(command, selected, tenant_knowledge),
            output_schema=KnowledgeSynthesisDraft,
        )
        known_tenant_ids = {item.id for item in tenant_knowledge}
        findings = set(draft.duplicate_memory_ids) | set(draft.conflicting_memory_ids)
        if not findings.issubset(known_tenant_ids):
            raise ValueError("Model returned an unknown Tenant Knowledge reference")
        context.emit(
            "knowledge_synthesis.findings_detected",
            {
                "duplicate_count": len(draft.duplicate_memory_ids),
                "conflict_count": len(draft.conflicting_memory_ids),
            },
        )

        candidate = await context.tool(
            "knowledge_synthesis.submit_candidate",
            lambda: memory.propose(
                context.session,
                ProposeKnowledge(
                    claim=draft.claim,
                    source_memory_ids=command.source_memory_ids,
                    duplicate_memory_ids=draft.duplicate_memory_ids,
                    conflicting_memory_ids=draft.conflicting_memory_ids,
                    confidence=draft.confidence,
                    idempotency_key=f"agent-run:{context.run_id}",
                ),
            ),
        )
        context.emit(
            "knowledge_synthesis.candidate_submitted",
            {
                "candidate_id": candidate.id,
                "duplicate_count": len(draft.duplicate_memory_ids),
                "conflict_count": len(draft.conflicting_memory_ids),
            },
        )
        return KnowledgeSynthesisResult(
            candidate=candidate,
            duplicate_memory_ids=draft.duplicate_memory_ids,
            conflicting_memory_ids=draft.conflicting_memory_ids,
        )


KNOWLEDGE_SYNTHESIS_WORKFLOW = KnowledgeSynthesisPackWorkflow()


def _synthesis_prompt(
    command: KnowledgeSynthesisInput,
    selected: tuple[MemoryItem, ...],
    tenant_knowledge: tuple[MemoryItem, ...],
) -> str:
    payload = {
        "request": command.request,
        "selected_private_memories": [
            {"id": item.id, "content": item.content, "confidence": item.confidence}
            for item in selected
        ],
        "published_tenant_knowledge": [
            {"id": item.id, "content": item.content, "confidence": item.confidence}
            for item in tenant_knowledge
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


KNOWLEDGE_SYNTHESIS_PACK: Any | None

try:
    from activegraph.packs import (  # type: ignore[import-untyped]
        ObjectType,
        Pack,
        PackPolicy,
        PackPrompt,
        behavior,
    )
except ImportError:
    KNOWLEDGE_SYNTHESIS_PACK = None
else:

    class ExecutableKnowledgeSynthesisPack(Pack):  # type: ignore[misc]
        """A native Pack whose executable method owns its application workflow."""

        async def execute(
            self,
            context: AgentRunContext,
            command: KnowledgeSynthesisInput,
            memory: KnowledgeSynthesisMemoryPort,
        ) -> KnowledgeSynthesisResult:
            return await KNOWLEDGE_SYNTHESIS_WORKFLOW.execute(context, command, memory)

    @behavior(  # type: ignore[misc]
        name="record_knowledge_synthesis_request",
        on=["object.created"],
        where={"object.type": "knowledge_synthesis_invocation"},
    )
    def _record_request(event: Any, graph: Any, ctx: Any) -> None:
        del ctx
        invocation = event.payload["object"]["data"]
        graph.emit(
            "knowledge_synthesis.requested",
            {
                "run_id": invocation["run_id"],
                "source_count": len(invocation["source_memory_ids"]),
            },
        )

    KNOWLEDGE_SYNTHESIS_PACK = ExecutableKnowledgeSynthesisPack(
        name="knowledge_synthesis",
        version=KnowledgeSynthesisPackWorkflow.version,
        description=(
            "Execute explicitly scoped Private Memory loading, Tenant Knowledge recall, "
            "duplicate/conflict detection, model synthesis and candidate submission "
            "through the shared Agent Runtime."
        ),
        object_types=(
            ObjectType(
                name="knowledge_synthesis_invocation",
                schema=KnowledgeSynthesisInvocationObject,
                description="Typed, explicitly scoped invocation.",
            ),
            ObjectType(
                name="knowledge_synthesis_output",
                schema=KnowledgeSynthesisOutputObject,
                description="Distilled proposal and duplicate/conflict findings.",
            ),
        ),
        behaviors=(_record_request,),
        tools=(),
        policies=(
            PackPolicy(
                name="proposal_only",
                requires_approval=("knowledge_synthesis_output",),
            ),
        ),
        prompts=(
            PackPrompt.from_body(
                name="distill_tenant_knowledge",
                version="1.1.0",
                body=(
                    "Distill only the explicitly selected private memories into one "
                    "safe, reviewable engineering claim. Compare it with published "
                    "Tenant Knowledge and report duplicate and conflict references. "
                    "Never approve or publish the claim."
                ),
            ),
        ),
        settings_schema=KnowledgeSynthesisPackSettings,
    )


async def execute_knowledge_synthesis_pack(
    context: AgentRunContext,
    command: KnowledgeSynthesisInput,
    memory: KnowledgeSynthesisMemoryPort,
) -> KnowledgeSynthesisResult:
    """Invoke the exact native Pack instance loaded by the Agent Runtime."""
    if KNOWLEDGE_SYNTHESIS_PACK is None:
        raise RuntimeError("The native ActiveGraph knowledge_synthesis Pack is unavailable")
    result = await KNOWLEDGE_SYNTHESIS_PACK.execute(context, command, memory)
    return KnowledgeSynthesisResult.model_validate(result)

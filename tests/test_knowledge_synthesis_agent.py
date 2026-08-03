from __future__ import annotations

from decimal import Decimal

import pytest
from activegraph.store.memory import InMemoryEventStore  # type: ignore[import-untyped]

from agent_memory_service.agents import (
    AgentInvocation,
    AgentRunState,
    AgentRuntimeModule,
    KnowledgeSynthesisCapability,
    MemoryModuleKnowledgeSynthesisPort,
    RecordedCompletion,
    RecordedProvider,
)
from agent_memory_service.governance import (
    CandidateStatus,
    InMemoryGovernanceStore,
)
from agent_memory_service.memory import MemoryModule
from agent_memory_service.models import PrincipalKind, RetainMemory, TenantSession
from agent_memory_service.stores.memory import InMemoryTenantMemoryRouter

TENANT_ID = "tenant-a"


def _session(actor_id: str = "user-1") -> TenantSession:
    return TenantSession(
        tenant_id=TENANT_ID,
        actor_id=actor_id,
        actor_kind=PrincipalKind.USER,
        roles=frozenset({"member"}),
    )


@pytest.mark.asyncio
async def test_synthesis_reads_only_selected_memory_and_submits_without_approval() -> None:
    router = InMemoryTenantMemoryRouter([TENANT_ID])
    governance = InMemoryGovernanceStore()
    memory = MemoryModule(router, governance)
    selected = await memory.retain(
        _session(),
        RetainMemory(
            content="Retries use exponential backoff with jitter.",
            idempotency_key="selected",
        ),
    )
    unselected = await memory.retain(
        _session(),
        RetainMemory(
            content="Secret personal preference that must not be scanned.",
            idempotency_key="unselected",
        ),
    )
    existing = await router.for_tenant(TENANT_ID).publish_tenant_knowledge(
        "published-1",
        "Services retry transient failures with bounded exponential backoff.",
        0.9,
        "curator-1",
    )
    conflicting = await router.for_tenant(TENANT_ID).publish_tenant_knowledge(
        "published-2",
        "Retry all failures forever without jitter.",
        0.8,
        "curator-1",
    )
    provider = RecordedProvider(
        [
            RecordedCompletion(
                parsed={
                    "claim": "Use bounded exponential backoff with jitter for transient failures.",
                    "confidence": 0.92,
                    "duplicate_memory_ids": [existing.id],
                    "conflicting_memory_ids": [conflicting.id],
                },
                input_tokens=100,
                output_tokens=30,
                cost_usd=Decimal("0.00075"),
            )
        ]
    )
    native_stores: dict[str, InMemoryEventStore] = {}

    def native_store(_session: TenantSession, run_id: str) -> InMemoryEventStore:
        return native_stores.setdefault(run_id, InMemoryEventStore(run_id))

    runtime = AgentRuntimeModule(
        capabilities=(
            KnowledgeSynthesisCapability(MemoryModuleKnowledgeSynthesisPort(memory, router)),
        ),
        provider=provider,
        activegraph_store_factory=native_store,
    )
    started = await runtime.start_agent_run(
        _session(),
        AgentInvocation(
            capability="knowledge_synthesis",
            input={
                "request": "Distill our retry convention",
                "source_memory_ids": [selected.id],
            },
            idempotency_key="synthesis-1",
        ),
    )

    completed = await runtime.run_until_terminal(_session(), started.id)

    assert completed.state is AgentRunState.COMPLETED
    assert completed.output is not None
    candidate_output = completed.output["candidate"]
    assert candidate_output["status"] == CandidateStatus.SUBMITTED
    assert candidate_output["reviewed_by"] is None
    assert candidate_output["source_count"] == 1
    assert candidate_output["duplicate_memory_ids"] == [existing.id]
    assert candidate_output["conflicting_memory_ids"] == [conflicting.id]
    assert completed.output["duplicate_memory_ids"] == [existing.id]
    assert completed.output["conflicting_memory_ids"] == [conflicting.id]
    assert completed.usage.model_calls == 1
    assert completed.usage.tool_calls == 3
    step_events = [
        event.type for event in completed.events if event.type.startswith("knowledge_synthesis.")
    ]
    assert step_events == [
        "knowledge_synthesis.private_memory_loaded",
        "knowledge_synthesis.tenant_knowledge_recalled",
        "knowledge_synthesis.findings_detected",
        "knowledge_synthesis.candidate_submitted",
    ]
    assert [
        event.data["tool"] for event in completed.events if event.type == "agent.tool.requested"
    ] == [
        "knowledge_synthesis.read_selected_private",
        "knowledge_synthesis.read_tenant_knowledge",
        "knowledge_synthesis.submit_candidate",
    ]
    persisted = list(native_stores[started.id].iter_events())
    assert [event.type for event in persisted if "agent_memory_run_event" in event.payload] == [
        event.type for event in completed.events
    ]
    assert "knowledge_synthesis.requested" in {event.type for event in persisted}

    provider_call = provider.calls[0]
    prompt = getattr(provider_call.messages[0], "content", str(provider_call.messages[0]))
    assert selected.content in prompt
    assert existing.content in prompt
    assert conflicting.content in prompt
    assert unselected.content not in prompt
    assert provider_call.model == "claude-sonnet-5"
    assert provider_call.temperature == 1.0
    assert provider_call.top_p == 1.0

    stored = await governance.get_candidate(TENANT_ID, candidate_output["id"])
    assert stored is not None
    assert stored.source_memory_ids == (selected.id,)
    assert stored.status is CandidateStatus.SUBMITTED
    assert stored.reviewed_by is None


@pytest.mark.asyncio
async def test_synthesis_fails_closed_for_another_users_private_reference() -> None:
    router = InMemoryTenantMemoryRouter([TENANT_ID])
    governance = InMemoryGovernanceStore()
    memory = MemoryModule(router, governance)
    private_item = await memory.retain(
        _session("user-2"),
        RetainMemory(content="User two private note", idempotency_key="user-2-note"),
    )
    provider = RecordedProvider(
        [
            RecordedCompletion(
                parsed={
                    "claim": "must not be reached",
                    "confidence": 1.0,
                    "duplicate_memory_ids": [],
                    "conflicting_memory_ids": [],
                }
            )
        ]
    )
    runtime = AgentRuntimeModule(
        capabilities=(
            KnowledgeSynthesisCapability(MemoryModuleKnowledgeSynthesisPort(memory, router)),
        ),
        provider=provider,
    )
    started = await runtime.start_agent_run(
        _session("user-1"),
        AgentInvocation(
            capability="knowledge_synthesis",
            input={
                "request": "Try to read another user",
                "source_memory_ids": [private_item.id],
            },
            idempotency_key="forbidden-1",
        ),
    )

    failed = await runtime.run_until_terminal(_session("user-1"), started.id)

    assert failed.state is AgentRunState.FAILED
    assert failed.failure == "PermissionError"
    assert provider.calls == ()


@pytest.mark.asyncio
async def test_provider_failure_is_recorded_without_submitting_a_candidate() -> None:
    router = InMemoryTenantMemoryRouter([TENANT_ID])
    governance = InMemoryGovernanceStore()
    memory = MemoryModule(router, governance)
    source = await memory.retain(
        _session(),
        RetainMemory(content="A selected note", idempotency_key="note"),
    )
    provider = RecordedProvider([])
    runtime = AgentRuntimeModule(
        capabilities=(
            KnowledgeSynthesisCapability(MemoryModuleKnowledgeSynthesisPort(memory, router)),
        ),
        provider=provider,
    )
    started = await runtime.start_agent_run(
        _session(),
        AgentInvocation(
            capability="knowledge_synthesis",
            input={"request": "Distill", "source_memory_ids": [source.id]},
            idempotency_key="provider-fails",
        ),
    )

    failed = await runtime.run_until_terminal(_session(), started.id)

    assert failed.state is AgentRunState.FAILED
    assert failed.failure == "AgentProviderError"
    assert failed.events[-1].type == "agent.run.failed"

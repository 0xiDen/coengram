from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_memory_service.agents import AgentInvocation
from agent_memory_service.auth import TokenService
from agent_memory_service.control import ControlModule, InMemoryControlStore
from agent_memory_service.http import create_http_app
from agent_memory_service.memory import MemoryModule
from agent_memory_service.models import RecallQuery, TenantSession
from agent_memory_service.pseudonyms import TelemetryPseudonymizer
from agent_memory_service.stores.memory import InMemoryTenantMemoryRouter
from agent_memory_service.telegram import (
    HttpTelegramSender,
    RecordingTelegramSender,
    TelegramAdapter,
    TelegramDeliveryError,
    TelegramReply,
)
from agent_memory_service.telemetry import SafeTelemetry


class RecordingAgentRunCommands:
    def __init__(self) -> None:
        self.starts: list[tuple[TenantSession, AgentInvocation]] = []

    async def start_text(
        self,
        session: TenantSession,
        invocation: AgentInvocation,
    ) -> str:
        self.starts.append((session, invocation))
        return "Agent Run run-telegram-1: queued."

    async def status_text(self, session: TenantSession, run_id: str) -> str:
        del session
        return f"Agent Run {run_id}: queued."

    async def cancel_text(self, session: TenantSession, run_id: str) -> str:
        del session
        return f"Agent Run {run_id}: cancel_requested."


def _telegram(
    telemetry: SafeTelemetry | None = None,
    *,
    agent_runs: RecordingAgentRunCommands | None = None,
) -> tuple[TelegramAdapter, MemoryModule, ControlModule, RecordingTelegramSender]:
    store = InMemoryControlStore()
    control = ControlModule(store, TokenService(store))
    control.create_tenant("tenant-a", "Product A Backend")
    control.create_principal("user-alice", "Alice", "user")
    control.create_principal("agent-telegram", "Telegram Memory Agent", "agent")
    control.grant_membership("tenant-a", "user-alice", "tenant_member")
    control.grant_membership("tenant-a", "agent-telegram", "tenant_member")
    delegation = control.create_delegation(
        "delegation-telegram-alice",
        tenant_id="tenant-a",
        agent_id="agent-telegram",
        subject_user_id="user-alice",
    )
    control.create_channel_binding(
        "binding-telegram-alice",
        channel="telegram",
        external_id="123456789",
        delegation_id=delegation.delegation_id,
    )
    memory = MemoryModule(InMemoryTenantMemoryRouter(["tenant-a"]))
    sender = RecordingTelegramSender()
    adapter = TelegramAdapter(
        control,
        memory,
        sender,
        webhook_secret="webhook-secret",
        agent_runs=agent_runs,
        telemetry=telemetry,
    )
    return adapter, memory, control, sender


def _update(text: str, *, chat_type: str = "private", update_id: int = 100) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 10,
            "from": {"id": 123456789, "username": "untrusted-name"},
            "chat": {"id": 123456789, "type": chat_type},
            "text": text,
        },
    }


@pytest.mark.asyncio
async def test_telegram_remember_uses_numeric_binding_and_subject_user_ownership() -> None:
    telegram, memory, control, sender = _telegram()

    reply = await telegram.handle_update(
        "webhook-secret", _update("/remember Prefer short alerts.")
    )
    session = control.resolve_channel_session("telegram", "123456789")
    recalled = await memory.recall(session, RecallQuery(query="short alerts"))

    assert reply.chat_id == 123456789
    assert sender.messages[-1] == reply
    assert recalled.items[0].owner_principal_id == "user-alice"
    assert "remembered" in reply.text.casefold()


@pytest.mark.asyncio
async def test_telegram_rejects_bad_webhook_secret_and_group_messages() -> None:
    telegram, memory, control, sender = _telegram()

    with pytest.raises(PermissionError, match="webhook"):
        await telegram.handle_update("wrong", _update("/remember Attack content."))
    with pytest.raises(PermissionError, match="direct messages"):
        await telegram.handle_update(
            "webhook-secret",
            _update("/remember Group content.", chat_type="group", update_id=101),
        )

    session = control.resolve_channel_session("telegram", "123456789")
    assert (await memory.recall(session, RecallQuery(query="content"))).items == ()
    assert sender.messages == []


@pytest.mark.asyncio
async def test_ordinary_telegram_text_returns_guidance_without_mutating_memory() -> None:
    telegram, memory, control, sender = _telegram()

    reply = await telegram.handle_update("webhook-secret", _update("Please remember this"))
    session = control.resolve_channel_session("telegram", "123456789")

    assert "/remember" in reply.text
    assert (await memory.recall(session, RecallQuery(query="remember"))).items == ()
    assert len(sender.messages) == 1


@pytest.mark.asyncio
async def test_telegram_synthesis_starts_agent_with_minimal_durable_reply_context() -> None:
    agent_runs = RecordingAgentRunCommands()
    telegram, _memory, _control, sender = _telegram(agent_runs=agent_runs)

    reply = await telegram.handle_update(
        "webhook-secret",
        _update(
            "/synthesize memory-a,memory-b | Distill the deployment lesson.",
            update_id=314,
        ),
    )

    assert reply == TelegramReply(
        chat_id=123456789,
        text="Agent Run run-telegram-1: queued.",
    )
    assert sender.messages[-1] == reply
    assert len(agent_runs.starts) == 1
    session, invocation = agent_runs.starts[0]
    assert session.tenant_id == "tenant-a"
    assert session.actor_id == "agent-telegram"
    assert session.subject_user_id == "user-alice"
    assert invocation == AgentInvocation(
        capability="knowledge_synthesis",
        input={
            "request": "Distill the deployment lesson.",
            "source_memory_ids": ["memory-a", "memory-b"],
        },
        idempotency_key="telegram:314:synthesize",
        reply_context={
            "channel": "telegram",
            "destination": "123456789",
        },
    )
    assert set(invocation.reply_context or ()) == {"channel", "destination"}


@pytest.mark.asyncio
async def test_malformed_telegram_synthesis_returns_help_without_starting_run() -> None:
    agent_runs = RecordingAgentRunCommands()
    telegram, _memory, _control, _sender = _telegram(agent_runs=agent_runs)

    reply = await telegram.handle_update(
        "webhook-secret",
        _update("/synthesize memory-a |", update_id=315),
    )

    assert "/synthesize" in reply.text
    assert agent_runs.starts == []


@pytest.mark.asyncio
async def test_telegram_metrics_contain_only_command_class_and_outcome() -> None:
    telemetry = SafeTelemetry(
        pseudonymizer=TelemetryPseudonymizer(b"0123456789abcdef0123456789abcdef")
    )
    telegram, _memory, _control, _sender = _telegram(telemetry)
    message = "/remember Secret Telegram message content."

    await telegram.handle_update("webhook-secret", _update(message))
    with pytest.raises(PermissionError):
        await telegram.handle_update("wrong-secret", _update(message, update_id=101))

    metrics = telemetry.render_metrics()
    assert 'outcome="accepted",command="remember"' in metrics
    assert 'outcome="rejected",command="help"' in metrics
    assert message not in metrics
    assert "123456789" not in metrics
    assert "wrong-secret" not in metrics


def test_http_webhook_requires_telegram_secret_header() -> None:
    telegram, _memory, _control, sender = _telegram()
    client = TestClient(
        create_http_app(
            _memory,
            TokenService(InMemoryControlStore()),
            telegram=telegram,
        )
    )

    denied = client.post("/api/v1/telegram/webhook", json=_update("/whoami"))
    accepted = client.post(
        "/api/v1/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
        json=_update("/whoami", update_id=102),
    )

    assert denied.status_code == 403
    assert accepted.status_code == 204
    assert "Tenant: tenant-a" in sender.messages[-1].text


@pytest.mark.asyncio
async def test_outbound_telegram_failure_never_exposes_token_bearing_url() -> None:
    secret = "seeded-telegram-token-must-not-leak"

    async def fail(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, request=_request, text="provider failure")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        sender = HttpTelegramSender(secret, client)
        with pytest.raises(TelegramDeliveryError) as failure:
            await sender.send_message(123, "Sensitive reply must not enter an exception")

    rendered = repr(failure.value)
    assert secret not in rendered
    assert "Sensitive reply" not in rendered
    assert "api.telegram.org" not in rendered

"""Command-oriented Telegram Adapter over canonical memory and Agent Interfaces."""

from __future__ import annotations

import hmac
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field

from agent_memory_service.agents import AgentInvocation
from agent_memory_service.control import ControlModule
from agent_memory_service.governance import ProposeKnowledge
from agent_memory_service.memory import MemoryModule
from agent_memory_service.models import RecallQuery, RetainMemory, TenantSession
from agent_memory_service.telemetry import SafeTelemetry


class TelegramSender(Protocol):
    async def send_message(self, chat_id: int, text: str) -> None: ...


class TelegramDeliveryError(RuntimeError):
    """A content-safe outbound failure that never retains the token-bearing URL."""


class AgentRunCommands(Protocol):
    async def start_text(
        self,
        session: TenantSession,
        invocation: AgentInvocation,
    ) -> str: ...

    async def status_text(self, session: TenantSession, run_id: str) -> str: ...

    async def cancel_text(self, session: TenantSession, run_id: str) -> str: ...


class TelegramReply(BaseModel):
    model_config = ConfigDict(frozen=True)

    chat_id: int
    text: str


class _TelegramUser(BaseModel):
    id: int


class _TelegramChat(BaseModel):
    id: int
    type: str


class _TelegramMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    message_id: int
    from_user: _TelegramUser = Field(alias="from")
    chat: _TelegramChat
    text: str


class _TelegramUpdate(BaseModel):
    update_id: int
    message: _TelegramMessage


class RecordingTelegramSender:
    def __init__(self) -> None:
        self.messages: list[TelegramReply] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append(TelegramReply(chat_id=chat_id, text=text))


class HttpTelegramSender:
    def __init__(self, bot_token: str, client: httpx.AsyncClient | None = None) -> None:
        if not bot_token:
            raise ValueError("Telegram bot token is required")
        self._bot_token = bot_token
        self._client = client

    async def send_message(self, chat_id: int, text: str) -> None:
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        try:
            if self._client is None:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    response = await client.post(url, json={"chat_id": chat_id, "text": text})
            else:
                response = await self._client.post(
                    url,
                    json={"chat_id": chat_id, "text": text},
                )
        except httpx.HTTPError:
            raise TelegramDeliveryError("Telegram delivery failed") from None
        if response.is_error:
            raise TelegramDeliveryError("Telegram delivery failed")


class TelegramAdapter:
    """Translate authenticated direct-message commands without trusting usernames."""

    _HELP = (
        "Use /whoami, /recall <query>, /remember <text>, "
        "/propose <memory-id> | <claim>, "
        "/synthesize <memory-id>[,<memory-id>] | <request>, "
        "/status <run-id>, or /cancel <run-id>. "
        "Ordinary text is not stored or executed."
    )

    def __init__(
        self,
        control: ControlModule,
        memory: MemoryModule,
        sender: TelegramSender,
        *,
        webhook_secret: str,
        agent_runs: AgentRunCommands | None = None,
        telemetry: SafeTelemetry | None = None,
    ) -> None:
        if not webhook_secret:
            raise ValueError("Telegram webhook secret is required")
        self._control = control
        self._memory = memory
        self._sender = sender
        self._webhook_secret = webhook_secret
        self._agent_runs = agent_runs
        self._telemetry = telemetry

    async def handle_update(
        self,
        supplied_secret: str,
        payload: dict[str, object],
    ) -> TelegramReply:
        command = "help"
        try:
            if not hmac.compare_digest(supplied_secret, self._webhook_secret):
                raise PermissionError("Invalid Telegram webhook secret")
            update = _TelegramUpdate.model_validate(payload)
            if update.message.chat.type != "private":
                raise PermissionError("Telegram bot accepts direct messages only")
            external_id = str(update.message.from_user.id)
            session = self._control.resolve_channel_session("telegram", external_id)
            text = update.message.text.strip()
            command = _telegram_command(text)
            reply_text = await self._execute(
                session,
                update.update_id,
                update.message.chat.id,
                text,
            )
            reply = TelegramReply(chat_id=update.message.chat.id, text=reply_text)
            await self._sender.send_message(reply.chat_id, reply.text)
        except PermissionError:
            if self._telemetry is not None:
                self._telemetry.observe_telegram("rejected", command)
            raise
        except Exception:
            if self._telemetry is not None:
                self._telemetry.observe_telegram("failed", command)
                self._telemetry.set_dependency_health("telegram", False)
            raise
        if self._telemetry is not None:
            self._telemetry.observe_telegram("accepted", command)
            self._telemetry.set_dependency_health("telegram", True)
        return reply

    async def _execute(
        self,
        session: TenantSession,
        update_id: int,
        chat_id: int,
        text: str,
    ) -> str:
        command, separator, argument = text.partition(" ")
        argument = argument.strip() if separator else ""
        if command == "/whoami":
            return (
                f"Tenant: {session.tenant_id}\n"
                f"Agent: {session.actor_id}\n"
                f"User: {session.subject_user_id}"
            )
        if command == "/recall" and argument:
            result = await self._memory.recall(session, RecallQuery(query=argument))
            if not result.items:
                return "No matching memory was found."
            return "\n\n".join(item.content for item in result.items)
        if command == "/remember" and argument:
            item = await self._memory.retain(
                session,
                RetainMemory(
                    content=argument,
                    idempotency_key=f"telegram:{update_id}:remember",
                ),
            )
            return f"Remembered as {item.id}."
        if command == "/propose" and "|" in argument:
            memory_id, claim = (part.strip() for part in argument.split("|", maxsplit=1))
            if not memory_id or not claim:
                return self._HELP
            candidate = await self._memory.propose_knowledge(
                session,
                ProposeKnowledge(
                    claim=claim,
                    source_memory_ids=(memory_id,),
                    idempotency_key=f"telegram:{update_id}:propose",
                ),
            )
            return f"Knowledge Candidate {candidate.id} submitted for human review."
        if command == "/synthesize" and "|" in argument and self._agent_runs is not None:
            source_text, request = (part.strip() for part in argument.split("|", maxsplit=1))
            source_memory_ids = [
                memory_id.strip() for memory_id in source_text.split(",") if memory_id.strip()
            ]
            if not source_memory_ids or not request:
                return self._HELP
            return await self._agent_runs.start_text(
                session,
                AgentInvocation(
                    capability="knowledge_synthesis",
                    input={
                        "request": request,
                        "source_memory_ids": source_memory_ids,
                    },
                    idempotency_key=f"telegram:{update_id}:synthesize",
                    reply_context={
                        "channel": "telegram",
                        "destination": str(chat_id),
                    },
                ),
            )
        if command == "/status" and argument and self._agent_runs is not None:
            return await self._agent_runs.status_text(session, argument)
        if command == "/cancel" and argument and self._agent_runs is not None:
            return await self._agent_runs.cancel_text(session, argument)
        return self._HELP


class RuntimeAgentRunCommands:
    """Render content-safe run status for the Telegram Adapter."""

    def __init__(self, runtime: object) -> None:
        from agent_memory_service.agents import AgentRuntimeModule

        if not isinstance(runtime, AgentRuntimeModule):
            raise TypeError("Agent runtime is required")
        self._runtime = runtime

    async def start_text(
        self,
        session: TenantSession,
        invocation: AgentInvocation,
    ) -> str:
        run = await self._runtime.start_agent_run(session, invocation)
        return f"Agent Run {run.id}: {run.state.value}."

    async def status_text(self, session: TenantSession, run_id: str) -> str:
        run = self._runtime.status(session, run_id)
        return f"Agent Run {run.id}: {run.state.value}."

    async def cancel_text(self, session: TenantSession, run_id: str) -> str:
        run = self._runtime.cancel(session, run_id)
        return f"Agent Run {run.id}: {run.state.value}."


def _telegram_command(text: str) -> str:
    command = text.partition(" ")[0]
    return {
        "/whoami": "whoami",
        "/recall": "recall",
        "/remember": "remember",
        "/propose": "propose",
        "/synthesize": "synthesize",
        "/status": "status",
        "/cancel": "cancel",
    }.get(command, "help")

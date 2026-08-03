from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import timedelta
from typing import Any, cast

import pytest
from starlette.types import ASGIApp, Message, Scope

from agent_memory_service.auth import InMemoryTokenStore, TokenService
from agent_memory_service.body_limits import (
    ARCHIVE_IMPORT_BODY_LIMIT,
    DEFAULT_PUBLIC_BODY_LIMIT,
    TELEGRAM_BODY_LIMIT,
    public_request_body_limit,
)
from agent_memory_service.http import create_http_app
from agent_memory_service.mcp_server import create_memory_mcp_server
from agent_memory_service.memory import MemoryModule
from agent_memory_service.models import PrincipalKind, TenantSession
from agent_memory_service.stores.memory import InMemoryTenantMemoryRouter
from agent_memory_service.telegram import TelegramAdapter


class _RecordingTelegram:
    def __init__(self) -> None:
        self.calls = 0

    async def handle_update(self, secret: str, payload: dict[str, object]) -> None:
        del secret, payload
        self.calls += 1


def _platform() -> tuple[MemoryModule, TokenService, str]:
    memory = MemoryModule(InMemoryTenantMemoryRouter(["tenant-a"]))
    tokens = TokenService(InMemoryTokenStore())
    credential = tokens.issue(
        TenantSession(
            tenant_id="tenant-a",
            actor_id="user-alice",
            actor_kind=PrincipalKind.USER,
            roles=frozenset({"tenant_member"}),
        ),
        lifetime=timedelta(days=1),
    )
    return memory, tokens, credential.access_token


@pytest.mark.asyncio
async def test_chunked_telegram_json_is_rejected_before_endpoint_work() -> None:
    memory, tokens, _token = _platform()
    telegram = _RecordingTelegram()
    app = create_http_app(
        memory,
        tokens,
        telegram=cast(TelegramAdapter, telegram),
    )
    body = json.dumps({"padding": "x" * TELEGRAM_BODY_LIMIT}).encode()

    response = await _chunked_request(
        app,
        "/api/v1/telegram/webhook",
        body,
        headers=(
            (b"content-type", b"application/json"),
            (b"x-telegram-bot-api-secret-token", b"webhook-secret"),
        ),
    )

    assert response.status == 413
    assert response.json()["code"] == "payload_too_large"
    assert telegram.calls == 0


@pytest.mark.asyncio
async def test_chunked_public_api_body_over_two_mebibytes_returns_413() -> None:
    memory, tokens, token = _platform()
    app = create_http_app(memory, tokens)
    body = json.dumps(
        {
            "content": "x" * DEFAULT_PUBLIC_BODY_LIMIT,
            "idempotency_key": "oversized-retain",
        }
    ).encode()

    response = await _chunked_request(
        app,
        "/api/v1/memories",
        body,
        headers=(
            (b"authorization", f"Bearer {token}".encode()),
            (b"content-type", b"application/json"),
        ),
    )

    assert response.status == 413
    assert response.json() == {
        "code": "payload_too_large",
        "message": "Request body exceeds the allowed size",
    }


@pytest.mark.asyncio
async def test_standalone_mcp_rejects_chunked_body_over_two_mebibytes() -> None:
    memory, tokens, token = _platform()
    app = create_memory_mcp_server(memory, tokens).streamable_http_app()
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "padding": "x" * DEFAULT_PUBLIC_BODY_LIMIT,
        }
    ).encode()

    async with app.router.lifespan_context(app):
        response = await _chunked_request(
            app,
            "/mcp",
            body,
            headers=(
                (b"authorization", f"Bearer {token}".encode()),
                (b"accept", b"application/json, text/event-stream"),
                (b"content-type", b"application/json"),
                (b"mcp-protocol-version", b"2025-06-18"),
            ),
        )

    assert response.status == 413
    assert response.json()["code"] == "payload_too_large"


@pytest.mark.asyncio
async def test_archive_import_uses_larger_limit_for_chunked_body() -> None:
    memory, tokens, token = _platform()
    app = create_http_app(memory, tokens)
    body = b"invalid archive\n" + b"x" * DEFAULT_PUBLIC_BODY_LIMIT

    response = await _chunked_request(
        app,
        "/api/v1/memory-archives/private/import",
        body,
        headers=(
            (b"authorization", f"Bearer {token}".encode()),
            (b"content-type", b"application/x-ndjson"),
        ),
    )

    assert response.status == 400
    assert response.json()["code"] == "invalid_request"


def test_public_route_limits_are_exact_and_trailing_slash_safe() -> None:
    assert public_request_body_limit("/api/v1/telegram/webhook") == 256 * 1024
    assert public_request_body_limit("/api/v1/telegram/webhook/") == TELEGRAM_BODY_LIMIT
    assert public_request_body_limit("/api/v1/memory-archives/private/import") == 64 * 1024 * 1024
    assert (
        public_request_body_limit("/api/v1/memory-archives/tenant/import/")
        == ARCHIVE_IMPORT_BODY_LIMIT
    )
    assert public_request_body_limit("/api/v1/memories") == DEFAULT_PUBLIC_BODY_LIMIT
    assert public_request_body_limit("/mcp") == DEFAULT_PUBLIC_BODY_LIMIT


class _ASGIResponse:
    def __init__(self, messages: Iterable[Message]) -> None:
        collected = tuple(messages)
        start = next(message for message in collected if message["type"] == "http.response.start")
        self.status = int(start["status"])
        self.body = b"".join(
            message.get("body", b"")
            for message in collected
            if message["type"] == "http.response.body"
        )

    def json(self) -> dict[str, Any]:
        result = json.loads(self.body)
        assert isinstance(result, dict)
        return result


async def _chunked_request(
    app: ASGIApp,
    path: str,
    body: bytes,
    *,
    headers: tuple[tuple[bytes, bytes], ...],
) -> _ASGIResponse:
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": (*headers, (b"transfer-encoding", b"chunked")),
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    chunks = tuple(body[offset : offset + 64 * 1024] for offset in range(0, len(body), 64 * 1024))
    requests: list[Message] = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    ]
    sent: list[Message] = []

    async def receive() -> Message:
        if requests:
            return requests.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return _ASGIResponse(sent)

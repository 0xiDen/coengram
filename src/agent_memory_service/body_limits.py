"""Application-level request-body limits for every public ingress."""

from __future__ import annotations

from collections.abc import Callable
from tempfile import SpooledTemporaryFile

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MEBIBYTE = 1024 * 1024
DEFAULT_PUBLIC_BODY_LIMIT = 2 * MEBIBYTE
TELEGRAM_BODY_LIMIT = 256 * 1024
ARCHIVE_IMPORT_BODY_LIMIT = 64 * MEBIBYTE

_ARCHIVE_IMPORT_PATHS = frozenset(
    {
        "/api/v1/memory-archives/private/import",
        "/api/v1/memory-archives/tenant/import",
    }
)
_TELEGRAM_WEBHOOK_PATH = "/api/v1/telegram/webhook"


def public_request_body_limit(path: str) -> int:
    """Return the byte ceiling for one normalized public request path."""
    normalized = path.rstrip("/") or "/"
    if normalized in _ARCHIVE_IMPORT_PATHS:
        return ARCHIVE_IMPORT_BODY_LIMIT
    if normalized == _TELEGRAM_WEBHOOK_PATH:
        return TELEGRAM_BODY_LIMIT
    return DEFAULT_PUBLIC_BODY_LIMIT


class RequestBodyLimitMiddleware:
    """Validate the complete stream before dispatching any endpoint work.

    ``Content-Length`` is only an early-rejection optimization.  The wrapped
    receive callable always counts actual bytes into a small spooled buffer,
    which also covers requests with a missing length and HTTP chunked transfer
    encoding.  Only a validated stream is replayed to authentication, parsing,
    and endpoint code.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        limit_for_path: Callable[[str], int] = public_request_body_limit,
    ) -> None:
        self._app = app
        self._limit_for_path = limit_for_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        limit = self._limit_for_path(str(scope.get("path", "/")))
        declared_length = _content_length(scope)
        if declared_length is not None and declared_length > limit:
            await _send_too_large(scope, receive, send)
            return

        with SpooledTemporaryFile(max_size=MEBIBYTE, mode="w+b") as body:
            received = 0
            complete = False
            disconnected = False
            while not complete:
                message = await receive()
                if message["type"] == "http.disconnect":
                    disconnected = True
                    break
                if message["type"] != "http.request":  # pragma: no cover - ASGI invariant
                    continue
                chunk = message.get("body", b"")
                received += len(chunk)
                if received > limit:
                    await _send_too_large(scope, receive, send)
                    return
                body.write(chunk)
                complete = not message.get("more_body", False)

            body.seek(0)
            replayed_empty = False

            async def replay_receive() -> Message:
                nonlocal replayed_empty
                chunk = body.read(64 * 1024)
                if chunk:
                    more = body.tell() < received or not complete
                    return {"type": "http.request", "body": chunk, "more_body": more}
                if received == 0 and complete and not replayed_empty:
                    replayed_empty = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                if disconnected:
                    return {"type": "http.disconnect"}
                return await receive()

            await self._app(scope, replay_receive, send)


def _content_length(scope: Scope) -> int | None:
    values = [value for name, value in scope.get("headers", ()) if name == b"content-length"]
    if len(values) != 1:
        return None
    try:
        length = int(values[0])
    except ValueError:
        return None
    return length if length >= 0 else None


async def _send_too_large(scope: Scope, receive: Receive, send: Send) -> None:
    response = JSONResponse(
        status_code=413,
        content={
            "code": "payload_too_large",
            "message": "Request body exceeds the allowed size",
        },
    )
    await response(scope, receive, send)

"""Content-free authenticated upstream used by the local Caddy release check."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_TOKEN = "Bearer release-verification-token"
_AUTHORITATIVE_HEADERS = (
    "X-Tenant-ID",
    "X-Principal-ID",
    "X-Subject-User-ID",
    "X-Actor-ID",
)


class _GatewayHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        self._respond()

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length:
            self.rfile.read(content_length)
        self._respond()

    def log_message(self, _format: str, *args: object) -> None:
        del args

    def _respond(self) -> None:
        if any(self.headers.get(name) is not None for name in _AUTHORITATIVE_HEADERS):
            self._json(400, {"code": "authoritative_header_forwarded"})
            return
        if self.path == "/.well-known/oauth-protected-resource":
            self._json(
                200,
                {
                    "resource": "https://memory.example.com/mcp",
                    "surface": "oauth-protected-resource",
                },
            )
            return
        if self.headers.get("Authorization") != _TOKEN:
            self._json(401, {"code": "unauthorized"})
            return
        surface = "mcp" if self.path.startswith("/mcp") else "http"
        self._json(200, {"authenticated": True, "surface": surface})

    def _json(self, status: int, document: dict[str, object]) -> None:
        body = json.dumps(document, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    ThreadingHTTPServer(("0.0.0.0", 8080), _GatewayHandler).serve_forever()


if __name__ == "__main__":
    main()

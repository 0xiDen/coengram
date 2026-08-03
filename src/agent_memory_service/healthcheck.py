"""Small dependency-free TCP health check for the MCP container."""

from __future__ import annotations

import os
import socket


def main() -> None:
    host = "127.0.0.1"
    port = int(os.getenv("MCP_PORT", "8080"))
    with socket.create_connection((host, port), timeout=3):
        pass


if __name__ == "__main__":
    main()

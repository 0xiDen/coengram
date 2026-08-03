"""Run the authenticated HTTP, MCP, Agent, and Telegram gateway."""

from __future__ import annotations

import uvicorn

from agent_memory_service.platform import PlatformConfig, create_gateway_app


def main() -> None:
    config = PlatformConfig.from_env()
    uvicorn.run(
        create_gateway_app(config),
        host=config.host,
        port=config.port,
        access_log=False,
    )


if __name__ == "__main__":
    main()

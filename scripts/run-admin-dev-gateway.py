"""Run a local in-memory gateway for admin frontend smoke tests."""

from __future__ import annotations

from datetime import timedelta

import uvicorn

from agent_memory_service.auth import TokenService
from agent_memory_service.control import ControlModule, InMemoryControlStore
from agent_memory_service.http import create_http_app
from agent_memory_service.memory import MemoryModule
from agent_memory_service.stores.memory import InMemoryTenantMemoryRouter


def main() -> None:
    store = InMemoryControlStore()
    tokens = TokenService(store)
    control = ControlModule(store, tokens)
    control.create_tenant("tenant-a", "Product A")
    control.create_operator(
        "operator-dev",
        "Dev Operator",
        frozenset(
            {
                "operator_admin",
                "identity_admin",
                "tenant_provisioner",
                "tenant_support",
                "knowledge_admin",
                "token_admin",
                "audit_viewer",
            }
        ),
    )
    credential = control.issue_operator_access_token(
        "operator-dev",
        lifetime=timedelta(days=7),
    )
    app = create_http_app(
        MemoryModule(InMemoryTenantMemoryRouter(["tenant-a"])),
        tokens,
        control=control,
    )
    print("Admin dev Operator Access Token:", flush=True)
    print(credential.access_token, flush=True)
    uvicorn.run(app, host="127.0.0.1", port=8080, access_log=False)


if __name__ == "__main__":
    main()

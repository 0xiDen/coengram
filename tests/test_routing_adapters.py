from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from neo4j.exceptions import ServiceUnavailable

from agent_memory_service.auth import TokenService
from agent_memory_service.control import ControlModule, InMemoryControlStore
from agent_memory_service.routing import FileSecretReader, RoutedTenantMemoryRouter
from agent_memory_service.stores.memory import TenantMemoryUnavailable


def _control() -> ControlModule:
    store = InMemoryControlStore()
    control = ControlModule(store, TokenService(store))
    control.create_tenant("tenant-a", "Product A Backend")
    control.register_tenant_route(
        "tenant-a",
        neo4j_service_address="neo4j-tenant-a:7687",
        neo4j_secret_name="tenant-a/neo4j_password",
        tenant_database_name="tenant_tenant_a",
        tenant_database_role="tenant_tenant_a_rw",
        healthy=True,
    )
    return control


def test_file_secret_reader_rejects_escape_and_reads_only_regular_secret(tmp_path: Path) -> None:
    directory = tmp_path / "tenant-a"
    directory.mkdir()
    secret = directory / "neo4j_password"
    secret.write_text("strong-password\n", encoding="utf-8")
    reader = FileSecretReader(tmp_path)

    assert reader.read("tenant-a/neo4j_password") == "strong-password"
    with pytest.raises(ValueError, match="path"):
        reader.read("../another-tenant/neo4j_password")


def test_memory_router_uses_only_healthy_control_route(tmp_path: Path) -> None:
    directory = tmp_path / "tenant-a"
    directory.mkdir()
    (directory / "neo4j_password").write_text("strong-password\n", encoding="utf-8")
    router = RoutedTenantMemoryRouter(
        _control(),
        FileSecretReader(tmp_path),
        embedding_model="BAAI/bge-small-en-v1.5",
    )

    first = router.for_tenant("tenant-a")
    assert router.for_tenant("tenant-a") is first
    with pytest.raises(TenantMemoryUnavailable):
        router.for_tenant("tenant-unknown")


@pytest.mark.asyncio
async def test_cached_neo4j_store_translates_a_post_connect_outage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created_clients: list[object] = []

    class OutageShortTerm:
        unavailable = False

        async def get_conversation(self, _session_id: str, **_kwargs: object) -> object:
            if self.unavailable:
                raise ServiceUnavailable("sensitive Bolt endpoint detail")
            return SimpleNamespace(messages=[])

    class ConnectedClient:
        def __init__(self, settings: Any) -> None:
            self.settings = settings
            self.short_term = OutageShortTerm()
            created_clients.append(self)

        async def connect(self) -> None:
            pass

        async def close(self) -> None:
            pass

    directory = tmp_path / "tenant-a"
    directory.mkdir()
    (directory / "neo4j_password").write_text("strong-password\n", encoding="utf-8")
    monkeypatch.setattr("agent_memory_service.routing.MemoryClient", ConnectedClient)
    router = RoutedTenantMemoryRouter(
        _control(),
        FileSecretReader(tmp_path),
        embedding_model="BAAI/bge-small-en-v1.5",
    )
    store = router.for_tenant("tenant-a")

    assert await store.list_private("user-alice") == ()
    assert len(created_clients) == 1
    client = created_clients[0]
    assert isinstance(client, ConnectedClient)
    embedding = client.settings.embedding
    assert embedding.provider.value == "sentence_transformers"
    assert embedding.model == "BAAI/bge-small-en-v1.5"
    assert embedding.dimensions == 384
    client.short_term.unavailable = True

    with pytest.raises(TenantMemoryUnavailable, match="unavailable") as captured:
        await store.list_private("user-alice")
    assert "sensitive" not in str(captured.value)

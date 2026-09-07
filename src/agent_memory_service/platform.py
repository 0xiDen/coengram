"""Production composition root for the authenticated memory gateway."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI
from pydantic import SecretStr

from agent_memory_service.agents import (
    AgentRuntimeModule,
    KnowledgeSynthesisCapability,
    LangChainAnthropicProvider,
    ManagedActiveGraphStoreFactory,
    MemoryModuleKnowledgeSynthesisPort,
)
from agent_memory_service.auth import TokenService
from agent_memory_service.control import ControlModule
from agent_memory_service.http import create_http_app
from agent_memory_service.mcp_server import create_memory_mcp_server
from agent_memory_service.memory import MemoryModule
from agent_memory_service.pseudonyms import TelemetryPseudonymizer
from agent_memory_service.routing import (
    FileSecretReader,
    RoutedAgentRunRepository,
    RoutedGovernanceStore,
    RoutedTenantMemoryRouter,
)
from agent_memory_service.schema import (
    CONTROL_SCHEMA_REVISION,
    TENANT_SCHEMA_REVISION,
    SchemaRequirement,
    require_schema,
)
from agent_memory_service.stores.postgres_control import PostgresControlStore
from agent_memory_service.telegram import (
    HttpTelegramSender,
    RuntimeAgentRunCommands,
    TelegramAdapter,
)
from agent_memory_service.telemetry import (
    ContentSafeTracer,
    SafeTelemetry,
)


@dataclass(frozen=True, slots=True)
class PlatformConfig:
    host: str
    port: int
    public_url: str
    control_database_url: str = field(repr=False)
    tenant_credentials_dir: Path
    tenant_postgres_host: str
    tenant_postgres_port: int
    embedding_model: str
    telemetry_pseudonymizer: TelemetryPseudonymizer = field(repr=False)
    embedding_dimensions: int = 384
    telegram_bot_token: str | None = field(default=None, repr=False)
    telegram_webhook_secret: str | None = field(default=None, repr=False)
    anthropic_api_key: str | None = field(default=None, repr=False)
    verify_schema_on_startup: bool = True
    otel_endpoint: str | None = None

    @classmethod
    def from_env(cls) -> PlatformConfig:
        password = _required_secret("MEMORY_CONTROL_DATABASE_PASSWORD_FILE")
        host = os.getenv("MEMORY_CONTROL_DATABASE_HOST", "postgres")
        database = os.getenv("MEMORY_CONTROL_DATABASE_NAME", "memory_control")
        user = os.getenv("MEMORY_CONTROL_DATABASE_USER", "memory_control")
        try:
            port = int(os.getenv("MEMORY_GATEWAY_PORT", "8080"))
            postgres_port = int(os.getenv("MEMORY_CONTROL_DATABASE_PORT", "5432"))
            embedding_dimensions = int(os.getenv("MEMORY_EMBEDDING_DIMENSIONS", "384"))
        except ValueError as exc:
            raise RuntimeError("Configured ports must be integers") from exc
        if not 1 <= port <= 65535 or not 1 <= postgres_port <= 65535:
            raise RuntimeError("Configured ports must be between 1 and 65535")
        if embedding_dimensions < 1:
            raise RuntimeError("Embedding dimensions must be positive")
        control_database_url = (
            f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@"
            f"{host}:{postgres_port}/{quote(database, safe='')}"
        )
        public_host = _optional_secret("MEMORY_PUBLIC_HOST_FILE")
        public_url = (
            f"https://{public_host}"
            if public_host is not None
            else os.getenv("MEMORY_PUBLIC_URL", "http://127.0.0.1:8080")
        )
        return cls(
            host=os.getenv("MEMORY_GATEWAY_HOST", "0.0.0.0"),
            port=port,
            public_url=public_url,
            control_database_url=control_database_url,
            tenant_credentials_dir=Path(_required("MEMORY_TENANT_CREDENTIALS_DIR")),
            tenant_postgres_host=os.getenv("MEMORY_TENANT_DATABASE_HOST", host),
            tenant_postgres_port=postgres_port,
            embedding_model=os.getenv(
                "MEMORY_EMBEDDING_MODEL",
                os.getenv(
                    "COENGRAM_EMBEDDING_MODEL",
                    "BAAI/bge-small-en-v1.5",
                ),
            ),
            embedding_dimensions=embedding_dimensions,
            telemetry_pseudonymizer=TelemetryPseudonymizer.from_file(
                Path(_required("MEMORY_TELEMETRY_HMAC_KEY_FILE"))
            ),
            telegram_bot_token=_optional_secret("TELEGRAM_BOT_TOKEN_FILE"),
            telegram_webhook_secret=_optional_secret("TELEGRAM_WEBHOOK_SECRET_FILE"),
            anthropic_api_key=_optional_secret("ANTHROPIC_API_KEY_FILE"),
            verify_schema_on_startup=_boolean("MEMORY_VERIFY_SCHEMA_ON_STARTUP", default=True),
            otel_endpoint=os.getenv("MEMORY_OTEL_ENDPOINT", "").strip() or None,
        )


def create_gateway_app(config: PlatformConfig) -> FastAPI:
    """Build every public Adapter over one canonical authorization and domain seam."""

    control_store = PostgresControlStore(config.control_database_url)
    tokens = TokenService(control_store)
    control = ControlModule(control_store, tokens)
    secret_reader = FileSecretReader(config.tenant_credentials_dir)
    memory_router = RoutedTenantMemoryRouter(
        control,
        secret_reader,
        embedding_model=config.embedding_model,
        embedding_dimensions=config.embedding_dimensions,
    )
    governance = RoutedGovernanceStore(
        control,
        secret_reader,
        postgres_host=config.tenant_postgres_host,
        postgres_port=config.tenant_postgres_port,
    )
    memory = MemoryModule(
        memory_router,
        governance,
        erasures=governance,
        self_approval=control,
        commands=governance,
    )
    tracing = ContentSafeTracer(
        "memory-gateway",
        config.otel_endpoint,
        pseudonymizer=config.telemetry_pseudonymizer,
    )
    telemetry = SafeTelemetry(
        pseudonymizer=config.telemetry_pseudonymizer,
        tracer=tracing,
    )
    agent_runs = RoutedAgentRunRepository(governance)
    provider = LangChainAnthropicProvider(_anthropic_factory(config.anthropic_api_key))
    activegraph_stores = _activegraph_store_factory(governance)
    agents = AgentRuntimeModule(
        capabilities=(
            KnowledgeSynthesisCapability(MemoryModuleKnowledgeSynthesisPort(memory, memory_router)),
        ),
        provider=provider,
        repository=agent_runs,
        activegraph_store_factory=activegraph_stores,
        tracer=tracing,
    )
    telegram = _telegram_adapter(config, control, memory, agents, telemetry)
    app = create_http_app(
        memory,
        tokens,
        telegram=telegram,
        agents=agents,
        telemetry=telemetry,
        execute_agent_runs=False,
        control=control,
    )
    mcp_app = create_memory_mcp_server(
        memory,
        tokens,
        agents=agents,
        public_url=config.public_url,
        telemetry=telemetry,
    ).streamable_http_app()
    app.router.routes.extend(mcp_app.routes)
    tracing.instrument_fastapi(app)
    mcp_lifespan = mcp_app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            if config.verify_schema_on_startup:
                _require_platform_schemas(config, control, governance)
            telemetry.set_dependency_health("control_postgres", True)
            telemetry.set_dependency_health("tenant_postgres", True)
        except Exception:
            telemetry.set_dependency_health("control_postgres", False)
            telemetry.set_dependency_health("tenant_postgres", False)
            raise
        async with mcp_lifespan(mcp_app):
            try:
                yield
            finally:
                try:
                    activegraph_stores.close()
                finally:
                    await memory_router.close()
                    tracing.shutdown()

    app.router.lifespan_context = lifespan
    return app


def _telegram_adapter(
    config: PlatformConfig,
    control: ControlModule,
    memory: MemoryModule,
    agents: AgentRuntimeModule,
    telemetry: SafeTelemetry,
) -> TelegramAdapter | None:
    configured = (config.telegram_bot_token, config.telegram_webhook_secret)
    if configured == (None, None):
        return None
    if None in configured:
        raise RuntimeError("Telegram bot token and webhook secret must be configured together")
    assert config.telegram_bot_token is not None
    assert config.telegram_webhook_secret is not None
    return TelegramAdapter(
        control,
        memory,
        HttpTelegramSender(config.telegram_bot_token),
        webhook_secret=config.telegram_webhook_secret,
        agent_runs=RuntimeAgentRunCommands(agents),
        telemetry=telemetry,
    )


def _anthropic_factory(api_key: str | None) -> Callable[..., Any] | None:
    if api_key is None:
        return None

    def factory(**kwargs: Any) -> Any:
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(api_key=SecretStr(api_key), **kwargs)

    return factory


def _activegraph_store_factory(
    governance: RoutedGovernanceStore,
) -> ManagedActiveGraphStoreFactory:
    return ManagedActiveGraphStoreFactory(governance.database_url_for_tenant)


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return value


def _require_platform_schemas(
    config: PlatformConfig,
    control: ControlModule,
    governance: RoutedGovernanceStore,
) -> None:
    require_schema(
        SchemaRequirement(
            database_url=config.control_database_url,
            expected_revision=CONTROL_SCHEMA_REVISION,
            store_name="Control Store",
        )
    )
    for route in control.list_tenant_routes():
        require_schema(
            SchemaRequirement(
                database_url=governance.database_url_for_tenant(route.tenant_id),
                expected_revision=TENANT_SCHEMA_REVISION,
                store_name=f"Tenant Store {route.tenant_id}",
            )
        )


def _boolean(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise RuntimeError(f"{name} must be a boolean")


def _required_secret(name: str) -> str:
    value = _optional_secret(name)
    if value is None:
        raise RuntimeError(f"Required secret file variable {name} is not set")
    return value


def _optional_secret(name: str) -> str | None:
    path = os.getenv(name, "").strip()
    if not path:
        return None
    secret_path = Path(path)
    if secret_path.is_symlink() or not secret_path.is_file():
        raise RuntimeError(f"Secret file configured by {name} is not a regular file")
    value = secret_path.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"Secret file configured by {name} is empty")
    return value

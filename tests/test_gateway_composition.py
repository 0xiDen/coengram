from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from agent_memory_service.platform import PlatformConfig, create_gateway_app
from agent_memory_service.pseudonyms import TelemetryPseudonymizer

_TELEMETRY_KEY = b"0123456789abcdef0123456789abcdef"


def test_gateway_composes_health_http_mcp_and_agents(tmp_path: Path) -> None:
    config = PlatformConfig(
        host="127.0.0.1",
        port=8080,
        public_url="https://memory.example.com",
        control_database_url="postgresql://control:password@postgres/memory_control",
        tenant_credentials_dir=tmp_path,
        tenant_postgres_host="postgres",
        tenant_postgres_port=5432,
        embedding_model="BAAI/bge-small-en-v1.5",
        telemetry_pseudonymizer=TelemetryPseudonymizer(_TELEMETRY_KEY),
        verify_schema_on_startup=False,
    )
    app = create_gateway_app(config)
    paths = {getattr(route, "path", None) for route in app.routes}

    assert "/api/v1/memories" in paths
    assert "/api/v1/agent-runs" in paths
    assert "/mcp" in paths
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}


def test_platform_config_reads_control_password_from_file(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    password = tmp_path / "postgres_password"
    telemetry_key = tmp_path / "telemetry_hmac_key"
    password.write_text("p@ss/word\n", encoding="utf-8")
    telemetry_key.write_bytes(_TELEMETRY_KEY + b"\n")
    patch = monkeypatch
    assert hasattr(patch, "setenv")
    patch.setenv("MEMORY_CONTROL_DATABASE_HOST", "postgres")
    patch.setenv("MEMORY_CONTROL_DATABASE_NAME", "memory_control")
    patch.setenv("MEMORY_CONTROL_DATABASE_USER", "memory_control")
    patch.setenv("MEMORY_CONTROL_DATABASE_PASSWORD_FILE", str(password))
    patch.setenv("MEMORY_TELEMETRY_HMAC_KEY_FILE", str(telemetry_key))
    patch.setenv("MEMORY_TENANT_CREDENTIALS_DIR", str(tmp_path))

    config = PlatformConfig.from_env()

    assert "p%40ss%2Fword" in config.control_database_url
    assert "p@ss/word" not in repr(config)
    assert (
        config.telemetry_pseudonymizer.reference("tenant", "tenant-product-a")
        == "o_c9676991a3bd6a87"
    )
    assert _TELEMETRY_KEY.decode() not in repr(config)


def test_platform_config_reads_public_hostname_from_secret_file(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    password = tmp_path / "postgres_password"
    hostname = tmp_path / "memory_public_host"
    telemetry_key = tmp_path / "telemetry_hmac_key"
    password.write_text("control-password\n", encoding="utf-8")
    hostname.write_text("memory.example.com\n", encoding="utf-8")
    telemetry_key.write_bytes(_TELEMETRY_KEY + b"\n")
    patch = monkeypatch
    assert hasattr(patch, "setenv")
    patch.setenv("MEMORY_CONTROL_DATABASE_PASSWORD_FILE", str(password))
    patch.setenv("MEMORY_TELEMETRY_HMAC_KEY_FILE", str(telemetry_key))
    patch.setenv("MEMORY_TENANT_CREDENTIALS_DIR", str(tmp_path))
    patch.setenv("MEMORY_PUBLIC_HOST_FILE", str(hostname))

    config = PlatformConfig.from_env()

    assert config.public_url == "https://memory.example.com"

"""Static release checks for the production deployment boundary.

These tests deliberately use only the Python standard library. They can run before
Docker, Compose, or production secrets exist and protect the security properties that
would be easy to regress while the application image is still evolving.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
OBSERVABILITY = ROOT / "ops" / "observability"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def compose_service_blocks(compose: str) -> dict[str, str]:
    """Extract service blocks from the deliberately conventional Compose layout."""

    lines = compose.splitlines()
    services_start = lines.index("services:") + 1
    services_end = next(
        (
            index
            for index in range(services_start, len(lines))
            if lines[index] and not lines[index].startswith(" ")
        ),
        len(lines),
    )
    service_headers = [
        (index, match.group(1))
        for index in range(services_start, services_end)
        if (match := re.fullmatch(r"  ([a-z][a-z0-9_-]*):", lines[index]))
    ]
    blocks: dict[str, str] = {}
    for position, (start, name) in enumerate(service_headers):
        end = (
            service_headers[position + 1][0]
            if position + 1 < len(service_headers)
            else services_end
        )
        blocks[name] = "\n".join(lines[start:end])
    return blocks


def test_caddy_is_built_with_cloudflare_dns_module() -> None:
    dockerfile = read(DEPLOY / "caddy" / "Dockerfile")

    assert "RUN xcaddy build" in dockerfile
    assert "--with github.com/caddy-dns/cloudflare@${CADDY_CLOUDFLARE_COMMIT}" in dockerfile
    assert re.search(r"^ARG CADDY_VERSION=\d+\.\d+\.\d+$", dockerfile, re.MULTILINE)
    assert re.search(r"^ARG CADDY_CLOUDFLARE_COMMIT=[0-9a-f]{40}$", dockerfile, re.MULTILINE)
    assert dockerfile.count("@${CADDY_") == 3
    assert ":latest" not in dockerfile


def test_caddy_reads_cloudflare_token_from_a_secret_file() -> None:
    entrypoint = read(DEPLOY / "caddy" / "entrypoint.sh")
    caddyfile = read(DEPLOY / "caddy" / "Caddyfile")

    assert "/run/secrets/cloudflare_api_token" in entrypoint
    assert "export CLOUDFLARE_API_TOKEN" in entrypoint
    assert "/run/secrets/memory_public_host" in entrypoint
    assert "export MEMORY_PUBLIC_HOST" in entrypoint
    assert "{$MEMORY_PUBLIC_HOST}" in caddyfile
    assert "memory.example.com" not in caddyfile
    assert "dns cloudflare {env.CLOUDFLARE_API_TOKEN}" in caddyfile
    assert "output stdout" in caddyfile
    assert "format filter" in caddyfile
    assert "metrics" in caddyfile
    assert "admin off" in caddyfile
    assert "http://:9180" in caddyfile
    assert "metrics /metrics" in caddyfile
    assert "per_host" not in caddyfile


def test_caddy_drops_the_secret_hostname_from_access_and_runtime_telemetry() -> None:
    caddyfile = read(DEPLOY / "caddy" / "Caddyfile")
    gateway_routes = read(DEPLOY / "caddy" / "gateway-routes.caddy")

    for field in ("identifier", "identifiers", "domain", "domains", "host", "server_name"):
        assert f"{field} delete" in caddyfile
    assert "request>host delete" in gateway_routes
    assert "request>headers>Host delete" in gateway_routes
    assert "request>tls>server_name delete" in gateway_routes


def test_caddy_metrics_do_not_expose_the_admin_api() -> None:
    caddyfile = read(DEPLOY / "caddy" / "Caddyfile")
    alloy = read(ROOT / "ops" / "observability" / "alloy" / "config.alloy")

    assert "admin off" in caddyfile
    assert "http://:9180" in caddyfile
    assert '"caddy:9180"' in alloy
    assert "caddy:2019" not in alloy


def test_caddy_routes_only_to_the_authenticated_gateway() -> None:
    caddyfile = read(DEPLOY / "caddy" / "Caddyfile")
    gateway_routes = read(DEPLOY / "caddy" / "gateway-routes.caddy")
    upstreams = re.findall(r"^\s*reverse_proxy\s+([^\s{]+)", gateway_routes, re.MULTILINE)

    assert 'import "/etc/caddy/gateway-routes.caddy"' in caddyfile
    assert "import memory_gateway_routes" in caddyfile
    assert upstreams == ["gateway:8080"]
    for authoritative_header in (
        "X-Tenant-ID",
        "X-Principal-ID",
        "X-Subject-User-ID",
        "X-Actor-ID",
    ):
        assert f"header_up -{authoritative_header}" in gateway_routes


def test_caddy_exposes_oauth_metadata_and_bounds_every_public_request_body() -> None:
    gateway_routes = read(DEPLOY / "caddy" / "gateway-routes.caddy")

    assert "@oauth_metadata path /.well-known/oauth-protected-resource" in gateway_routes
    assert "@telegram path /api/v1/telegram/webhook" in gateway_routes
    assert (
        "@archive_import path /api/v1/memory-archives/private/import "
        "/api/v1/memory-archives/tenant/import"
    ) in gateway_routes
    assert "@mcp path /mcp /mcp/*" in gateway_routes
    assert "@api path /api/v1/*" in gateway_routes
    for limit in ("max_size 16KB", "max_size 256KB", "max_size 64MB"):
        assert limit in gateway_routes
    assert gateway_routes.count("max_size 2MB") == 2
    assert gateway_routes.count("import memory_gateway_proxy") == 5


def test_release_gate_builds_and_validates_the_actual_caddy_artifact() -> None:
    verifier = read(ROOT / "scripts" / "verify-caddy.sh")

    assert "docker build --quiet -f deploy/caddy/Dockerfile deploy/caddy" in verifier
    assert "list-modules" in verifier
    assert "dns.providers.cloudflare" in verifier
    assert "validate --config /etc/caddy/Caddyfile --adapter caddyfile" in verifier
    assert "0123456789abcdef0123456789abcdef01234567" in verifier
    assert "/run/secrets/memory_public_host:ro" in verifier
    assert "https://memory.example.com" in verifier
    assert "Caddyfile.test" in verifier
    assert "fake-ingress-gateway.py" in verifier
    assert "curl" in verifier
    assert "Authorization: Bearer release-verification-token" in verifier
    assert "unauthenticated HTTP" in verifier
    assert "authenticated HTTP" in verifier
    assert "authenticated MCP" in verifier
    assert "OAuth protected-resource metadata" in verifier
    assert '"413"' in verifier
    assert "infrastructure route" in verifier


def test_container_release_gate_exposes_restartable_fault_targets() -> None:
    verifier = read(ROOT / "scripts" / "verify-infrastructure.sh")

    assert 'RABBITMQ_TEST_CONTAINER="$rabbit_container"' in verifier
    assert 'NEO4J_TEST_CONTAINER="$neo4j_container"' in verifier
    assert "test_release_fault_recovery.py" in verifier
    assert '--rm --name "$rabbit_container"' not in verifier
    assert '--rm --name "$neo4j_container"' not in verifier


def test_only_caddy_is_public_while_operator_ports_are_loopback_only() -> None:
    compose = read(DEPLOY / "shared.compose.yaml")
    services = compose_service_blocks(compose)

    assert set(services) == {
        "alloy",
        "caddy",
        "docker-socket-proxy",
        "gateway",
        "grafana",
        "loki",
        "migrate-control",
        "mimir",
        "postgres",
        "postgres-exporter",
        "rabbitmq",
        "tempo",
        "worker",
    }
    assert "ports:" in services["caddy"]
    assert '"80:80"' in services["caddy"]
    assert '"443:443"' in services["caddy"]
    assert '"127.0.0.1:${POSTGRES_OPERATOR_PORT:-5432}:5432"' in services["postgres"]
    assert '"127.0.0.1:${GRAFANA_OPERATOR_PORT:-3000}:3000"' in services["grafana"]
    for name, block in services.items():
        if name not in {"caddy", "postgres", "grafana"}:
            assert "ports:" not in block, f"{name} must not publish a host port"


def test_control_migrations_complete_before_application_start() -> None:
    services = compose_service_blocks(read(DEPLOY / "shared.compose.yaml"))

    assert "alembic-control.ini upgrade head" in services["migrate-control"]
    assert "service_completed_successfully" in services["gateway"]
    assert "service_completed_successfully" in services["worker"]


def test_application_image_and_commands_are_explicit_deployment_inputs() -> None:
    compose = read(DEPLOY / "shared.compose.yaml")
    services = compose_service_blocks(compose)

    assert "MEMORY_PLATFORM_IMAGE:?" in compose
    assert "MEMORY_GATEWAY_COMMAND:?" in services["gateway"]
    assert "MEMORY_WORKER_COMMAND:?" in services["worker"]
    assert "build:" not in services["gateway"]
    assert "build:" not in services["worker"]
    assert "MEMORY_EMBEDDING_MODEL" in compose
    assert "HF_HOME: /models" in compose
    assert "/opt/coengram/models/bge-small-en-v1.5" in compose
    assert "embedding_models" not in compose
    assert "volumes: *application-volumes" in services["gateway"]
    assert "volumes: *application-volumes" in services["worker"]


def test_release_image_bakes_a_revision_pinned_offline_embedding_model() -> None:
    dockerfile = read(ROOT / "Dockerfile")

    assert "EMBEDDING_MODEL_REVISION=01d3c3cd65ac9dc6bd0d702ed913366e7931097b" in dockerfile
    assert "COENGRAM_EMBEDDING_MODEL=/opt/coengram/models/bge-small-en-v1.5" in dockerfile
    assert "HF_HUB_OFFLINE=1" in dockerfile
    assert "TRANSFORMERS_OFFLINE=1" in dockerfile
    assert "snapshot_download(" in dockerfile


def test_shared_credentials_are_file_backed_compose_secrets() -> None:
    compose = read(DEPLOY / "shared.compose.yaml")

    expected = {
        "cloudflare_api_token",
        "memory_public_host",
        "postgres_password",
        "postgres_admin_password",
        "rabbitmq_password",
        "telemetry_hmac_key",
        "anthropic_api_key",
        "telegram_bot_token",
        "telegram_webhook_secret",
        "grafana_admin_password",
    }
    for secret in expected:
        assert re.search(rf"^  {secret}:\n    file:", compose, re.MULTILINE)
        assert f"/run/secrets/{secret}" in compose

    assert "PASSWORD_FILE" in compose
    assert "API_KEY_FILE" in compose
    assert "BOT_TOKEN_FILE" in compose
    assert "WEBHOOK_SECRET_FILE" in compose


def test_telemetry_pseudonyms_use_one_file_backed_shared_hmac_key() -> None:
    production = read(DEPLOY / "shared.compose.yaml")
    production_services = compose_service_blocks(production)
    local = read(ROOT / "compose.yaml")
    setup = read(ROOT / "scripts" / "setup-dev-secrets.sh")
    operator_environment = read(DEPLOY / "operator.env.example")

    setting = "MEMORY_TELEMETRY_HMAC_KEY_FILE: /run/secrets/telemetry_hmac_key"
    assert setting in production
    assert setting in local
    for service in ("gateway", "worker"):
        assert "telemetry_hmac_key" in production_services[service]
    assert "telemetry_hmac_key" not in production_services["caddy"]
    assert 'ensure_secret "$destination/telemetry_hmac_key"' in setup
    assert "scripts/telemetry-reference.py" in setup
    assert "MEMORY_TELEMETRY_HMAC_KEY_FILE=/etc/coengram/secrets/telemetry_hmac_key" in (
        operator_environment
    )


def test_application_services_receive_only_their_required_external_secrets() -> None:
    services = compose_service_blocks(read(DEPLOY / "shared.compose.yaml"))

    assert "telegram_bot_token" in services["gateway"]
    assert "telegram_webhook_secret" in services["gateway"]
    assert "anthropic_api_key" not in services["gateway"]
    assert "anthropic_api_key" in services["worker"]
    assert "telegram_bot_token" not in services["worker"]
    assert "telegram_webhook_secret" not in services["worker"]
    assert "telemetry_hmac_key" in services["gateway"]
    assert "telemetry_hmac_key" in services["worker"]
    assert "postgres_admin_password" not in services["gateway"]
    assert "postgres_admin_password" not in services["worker"]
    assert "postgres_admin_password" not in services["migrate-control"]


def test_non_root_applications_receive_only_the_dedicated_tenant_secrets_group() -> None:
    compose = read(DEPLOY / "shared.compose.yaml")
    services = compose_service_blocks(compose)
    expected = (
        "${TENANT_SECRETS_GID:?Set TENANT_SECRETS_GID to the dedicated host secrets group ID}"
    )

    assert expected in services["gateway"]
    assert expected in services["worker"]
    assert "TENANT_SECRETS_GID" not in services["caddy"]
    assert "TENANT_SECRETS_GID" not in services["postgres"]
    assert "USER app" in read(ROOT / "Dockerfile")


def test_postgres_bootstrap_admin_is_separate_from_control_application_role() -> None:
    compose = read(DEPLOY / "shared.compose.yaml")
    postgres = compose_service_blocks(compose)["postgres"]
    initializer = read(DEPLOY / "postgres" / "init-control.sh")

    assert "POSTGRES_USER: ${POSTGRES_ADMIN_USER:-memory_admin}" in postgres
    assert "POSTGRES_PASSWORD_FILE: /run/secrets/postgres_admin_password" in postgres
    assert "POSTGRES_CONTROL_USER: ${POSTGRES_CONTROL_USER:-memory_control}" in postgres
    assert "target: control" in postgres
    assert "COPY --chmod=0444 init-control.sh" in read(DEPLOY / "postgres" / "Dockerfile")
    assert "ALTER DATABASE" in initializer
    assert "OWNER TO" in initializer
    assert "--set" not in initializer

    tenant_initializer = read(DEPLOY / "postgres" / "init-tenants.sh")
    assert "REVOKE CONNECT ON DATABASE" in tenant_initializer
    assert "FROM PUBLIC" in tenant_initializer
    assert "GRANT CONNECT ON DATABASE" in tenant_initializer


def test_postgres_init_scripts_are_built_as_source_only_files() -> None:
    dockerfile = read(DEPLOY / "postgres" / "Dockerfile")

    assert "FROM postgres:${POSTGRES_VERSION}@${POSTGRES_DIGEST}" in dockerfile
    assert re.search(r"^ARG POSTGRES_DIGEST=sha256:[0-9a-f]{64}$", dockerfile, re.MULTILINE)
    assert "COPY --chmod=0444 init-control.sh" in dockerfile
    assert "COPY --chmod=0444 init-tenants.sh" in dockerfile


def test_rabbitmq_uses_supported_file_secret_loading_and_versioned_topology_names() -> None:
    compose = read(DEPLOY / "shared.compose.yaml")
    rabbitmq = compose_service_blocks(compose)["rabbitmq"]
    rabbitmq_config = read(DEPLOY / "rabbitmq" / "rabbitmq.conf")
    entrypoint = read(DEPLOY / "rabbitmq" / "entrypoint.sh")
    worker = read(ROOT / "src" / "agent_memory_service" / "worker.py")

    assert "RABBITMQ_DEFAULT_PASS_FILE" not in rabbitmq
    assert "MEMORY_RABBITMQ_SEED_PASSWORD_FILE" in rabbitmq
    assert "/run/secrets/rabbitmq_password" in entrypoint
    assert "MEMORY_RABBITMQ_SEED_PASSWORD" in rabbitmq_config
    assert "default_vhost = memory" in rabbitmq_config
    assert "default_user_tags.administrator = false" in rabbitmq_config
    assert "administrator = true" not in rabbitmq_config
    assert "default_permissions.configure = ^memory[.]" in rabbitmq_config
    assert "MEMORY_AMQP_VHOST: memory" in compose
    assert "rabbitmq_prometheus" in read(DEPLOY / "rabbitmq" / "enabled_plugins")
    assert "MEMORY_AMQP_EXCHANGE: memory.events.v1" in compose
    assert "MEMORY_AMQP_GRAPH_QUEUE: memory.graph.apply.v1" in compose
    assert "MEMORY_AMQP_DEAD_LETTER_EXCHANGE: memory.dead-letter.v1" in compose
    assert "MEMORY_AMQP_DEAD_LETTER_QUEUE: memory.graph.dead-letter.v1" in compose
    for variable in (
        "MEMORY_AMQP_EXCHANGE",
        "MEMORY_AMQP_GRAPH_QUEUE",
        "MEMORY_AMQP_DEAD_LETTER_EXCHANGE",
        "MEMORY_AMQP_DEAD_LETTER_QUEUE",
    ):
        assert f'"{variable}"' in worker


def test_docker_socket_proxy_allows_only_alloy_discovery_and_log_endpoints() -> None:
    proxy_config = read(DEPLOY / "docker-api-proxy" / "Caddyfile")

    expected_image = (
        "caddy:2.10.2-alpine@"
        "sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d"
    )
    assert "admin off" in proxy_config
    assert "method GET HEAD" in proxy_config
    for allowed_path in (
        "_ping",
        "version",
        "containers/json",
        "networks",
        "containers/[0-9a-f]{64}/(?:json|logs)",
    ):
        assert allowed_path in proxy_config
    for forbidden_path in ("archive", "events", "exec", "stats", "images", "secrets"):
        assert forbidden_path not in proxy_config
    assert "respond 403" in proxy_config

    for compose_path in (ROOT / "compose.yaml", DEPLOY / "shared.compose.yaml"):
        compose = read(compose_path)
        services = compose_service_blocks(compose)
        proxy = services["docker-socket-proxy"]
        alloy = services["alloy"]

        assert compose.count("/var/run/docker.sock") == 2
        assert expected_image in proxy
        assert "/var/run/docker.sock:/var/run/docker.sock:ro" in proxy
        assert "docker-api-proxy/Caddyfile:" in proxy
        assert "/var/run/docker.sock" not in alloy
        assert "CONTAINERS:" not in proxy
        assert "POST:" not in proxy
        assert "ports:" not in proxy
        assert "read_only: true" in proxy
        assert "no-new-privileges:true" in proxy
        assert "condition: service_healthy" in alloy
        assert "docker-api" in proxy
        assert "docker-api" in alloy
        assert re.search(r"^  docker-api:\n    internal: true$", compose, re.MULTILINE)
        for name, block in services.items():
            if name != "docker-socket-proxy":
                assert "/var/run/docker.sock" not in block, name
            if name not in {"alloy", "docker-socket-proxy"}:
                assert "docker-api" not in block, name

    alloy_config = read(OBSERVABILITY / "alloy" / "config.alloy")
    assert alloy_config.count('host             = "http://docker-socket-proxy:2375"') == 2
    assert 'host       = "http://docker-socket-proxy:2375"' in alloy_config
    assert "unix:///var/run/docker.sock" not in alloy_config


def test_database_stdout_is_excluded_from_central_logs() -> None:
    alloy = read(OBSERVABILITY / "alloy" / "config.alloy")
    shared_services = compose_service_blocks(read(DEPLOY / "shared.compose.yaml"))
    tenant_services = compose_service_blocks(read(DEPLOY / "tenant.compose.yaml"))

    assert "caddy|gateway|worker|rabbitmq|postgres-exporter" in alloy
    assert "PostgreSQL" in alloy and "Neo4j stdout" in alloy
    assert 'memory.telemetry: "true"' not in shared_services["postgres"]
    assert 'memory.telemetry: "true"' not in tenant_services["neo4j"]
    assert 'memory.neo4j_health: "true"' in tenant_services["neo4j"]


def test_runtime_and_recovery_images_are_immutable_by_default() -> None:
    local = read(ROOT / "compose.yaml")
    shared = read(DEPLOY / "shared.compose.yaml")
    tenant = read(DEPLOY / "tenant.compose.yaml")
    shared_environment = read(DEPLOY / "shared.env.example")
    operator_environment = read(DEPLOY / "operator.env.example")
    restore = read(ROOT / "src" / "agent_memory_service" / "host_restore.py")
    backup = read(ROOT / "src" / "agent_memory_service" / "host_backup.py")
    cli = read(ROOT / "src" / "agent_memory_service" / "cli.py")
    infrastructure = read(ROOT / "scripts" / "verify-infrastructure.sh")

    local_services = compose_service_blocks(local)
    for name, block in local_services.items():
        if "image:" in block and "build:" not in block:
            assert re.search(r"image: [^\n]+@sha256:[0-9a-f]{64}", block), name

    for variable in (
        "POSTGRES_EXPORTER_DIGEST",
        "POSTGRES_DIGEST",
        "RABBITMQ_DIGEST",
        "ALLOY_DIGEST",
        "MIMIR_DIGEST",
        "LOKI_DIGEST",
        "TEMPO_DIGEST",
        "GRAFANA_DIGEST",
    ):
        assert re.search(rf"^{variable}=sha256:[0-9a-f]{{64}}$", shared_environment, re.MULTILINE)
        assert f"${{{variable}:-sha256:" in shared

    assert re.search(
        r"^MEMORY_PLATFORM_IMAGE=[^\n]+@sha256:[0-9a-f]{64}$",
        shared_environment,
        re.MULTILINE,
    )
    assert re.search(r"@\${NEO4J_DIGEST:-sha256:[0-9a-f]{64}}", tenant)
    assert re.search(r"^POSTGRES_DIGEST=sha256:[0-9a-f]{64}$", operator_environment, re.MULTILINE)
    assert re.search(r"^NEO4J_DIGEST=sha256:[0-9a-f]{64}$", operator_environment, re.MULTILINE)
    normalized_restore = re.sub(r'"\s*"', "", restore)
    normalized_backup = re.sub(r'"\s*"', "", backup)
    assert "postgres:17.6-alpine@sha256:" in normalized_restore
    assert "neo4j:5.26.28-community@sha256:" in normalized_restore
    assert "neo4j:5.26.28-community@sha256:" in normalized_backup
    assert 'neo4j_admin_image=f"neo4j:{neo4j_version}@{neo4j_digest}"' in cli
    assert re.search(r'^rabbit_image="[^\n]+@sha256:[0-9a-f]{64}"$', infrastructure, re.MULTILINE)
    assert re.search(r'^neo4j_image="[^\n]+@sha256:[0-9a-f]{64}"$', infrastructure, re.MULTILINE)
    caddy_verifier = read(ROOT / "scripts" / "verify-caddy.sh")
    docker_proxy_test = read(ROOT / "tests" / "integration" / "test_docker_api_proxy.py")
    assert re.search(r"python:3\.12-alpine@sha256:[0-9a-f]{64}", caddy_verifier)
    assert re.search(r"python:3\.12-alpine@sha256:[0-9a-f]{64}", docker_proxy_test)


def test_release_publication_order_and_dependency_update_scope_are_safe() -> None:
    ci = read(ROOT / ".github" / "workflows" / "ci.yml")
    release = read(ROOT / ".github" / "workflows" / "release.yml")
    dependabot = read(ROOT / ".github" / "dependabot.yml")

    assert 'echo "/usr/lib/postgresql/17/bin" >> "$GITHUB_PATH"' in ci
    assert '"/usr/lib/postgresql/17/bin/pg_dump" --version' in ci
    assert re.search(
        r"^  publish-pypi:\n(?:.*\n)*?    needs: \[build-python, publish-container\]$",
        release,
        re.MULTILINE,
    )
    for directory in ("/", "/deploy", "/deploy/caddy", "/deploy/postgres"):
        assert f"directory: {directory}\n" in dependabot


def test_tenant_neo4j_is_community_only_private_and_uniquely_addressed() -> None:
    compose = read(DEPLOY / "tenant.compose.yaml")
    services = compose_service_blocks(compose)
    neo4j = services["neo4j"]

    assert "5.26.28-community" in neo4j
    assert "ports:" not in neo4j
    assert "neo4j-${TENANT_ID}" in neo4j
    assert "memory.tenant_ref: ${TENANT_TELEMETRY_REF:?" in neo4j
    assert "memory.tenant_id" not in neo4j
    assert "/run/secrets/neo4j_password" in neo4j
    assert "NEO4J_AUTH:" not in neo4j
    assert re.search(r"^    external: true$", compose, re.MULTILINE)
    assert "name: memory-backplane" in compose


def test_tenant_secret_is_a_protected_file_input_not_an_environment_value() -> None:
    compose = read(DEPLOY / "tenant.compose.yaml")
    example = read(DEPLOY / "tenant.env.example")

    assert re.search(r"^  neo4j_password:\n    file:", compose, re.MULTILINE)
    assert "TENANT_SECRETS_DIR:?" in compose
    assert "NEO4J_PASSWORD=" not in example
    assert "TENANT_ID=" in example


def test_checked_tenant_manifest_example_is_secret_free_and_derived() -> None:
    from agent_memory_service.manifest import TenantManifest

    document = read(DEPLOY / "tenant.manifest.example.json")
    manifest = TenantManifest.import_json(document)

    assert manifest.database_name == "tenant_tenant_product_a_backend"
    assert manifest.neo4j_service_name == "neo4j-tenant-product-a-backend"
    for forbidden in ("password", "api_key", "access_token"):
        assert forbidden not in document.casefold()


def test_observability_retention_matches_the_approved_policy() -> None:
    mimir = read(OBSERVABILITY / "mimir" / "config.yaml")
    loki = read(OBSERVABILITY / "loki" / "config.yaml")
    tempo = read(OBSERVABILITY / "tempo" / "config.yaml")

    assert "compactor_blocks_retention_period: 720h" in mimir  # 30 days
    assert "retention_period: 336h" in loki  # 14 days
    assert "block_retention: 168h" in tempo  # 7 days
    assert "retention_enabled: true" in loki


def test_alloy_centralizes_selected_logs_metrics_and_traces() -> None:
    alloy = read(OBSERVABILITY / "alloy" / "config.alloy")

    assert 'values = ["memory.telemetry=true"]' in alloy
    assert "loki.source.docker" in alloy
    assert "prometheus.remote_write" in alloy
    assert 'url = "http://mimir:9009/api/v1/push"' in alloy
    assert '{ "__address__" = "worker:9090", "job" = "worker" }' in alloy
    assert '{ "__address__" = "postgres-exporter:9187", "job" = "postgres" }' in alloy
    assert "prometheus.exporter.blackbox" in alloy
    assert 'values = ["memory.neo4j_health=true"]' in alloy
    assert 'target_label  = "tenant_ref"' in alloy
    assert 'replacement   = "$1:7687"' in alloy
    assert "otelcol.receiver.otlp" in alloy
    assert 'endpoint = "tempo:4317"' in alloy
    assert "otelcol.processor.batch" in alloy


def test_postgres_exporter_uses_a_dedicated_monitor_role_and_file_secret() -> None:
    compose = read(DEPLOY / "shared.compose.yaml")
    services = compose_service_blocks(compose)
    init = read(DEPLOY / "postgres" / "init-control.sh")

    exporter = services["postgres-exporter"]
    assert "postgres-exporter:${POSTGRES_EXPORTER_VERSION:-v0.19.1}" in exporter
    assert "DATA_SOURCE_PASS_FILE: /run/secrets/postgres_exporter_password" in exporter
    assert "DATA_SOURCE_NAME" not in exporter
    assert "ports:" not in exporter
    assert "GRANT pg_monitor" in init
    assert "postgres_exporter_password" in init


def test_grafana_dashboard_covers_iteration_one_operational_signals() -> None:
    dashboard = json.loads(
        read(OBSERVABILITY / "grafana" / "dashboards" / "platform-overview.json")
    )
    rendered = json.dumps(dashboard, sort_keys=True)

    for metric in (
        "memory_authentication_total",
        "memory_authentication_duration_seconds",
        "memory_dependency_healthy",
        "pg_up",
        "probe_success",
        "memory_agent_runs_total",
        "memory_agent_budget_exhaustions_total",
        "memory_outbox_oldest_claim_seconds",
        "rabbitmq_queue_messages_ready",
        "memory_worker_cycles_total",
        "memory_telegram_updates_total",
        "memory_unused_access_tokens",
    ):
        assert metric in rendered


def test_systemd_templates_schedule_exact_tenant_backup_and_isolated_restore() -> None:
    backup_service = read(DEPLOY / "systemd" / "coengram-backup@.service")
    backup_timer = read(DEPLOY / "systemd" / "coengram-backup@.timer")
    restore_service = read(DEPLOY / "systemd" / "coengram-restore-drill@.service")
    restore_timer = read(DEPLOY / "systemd" / "coengram-restore-drill@.timer")

    assert "backup create --tenant-id %i" in backup_service
    assert "backup status --tenant-id %i" in backup_service
    assert "backup retention-plan --tenant-id %i" in backup_service
    assert "backup retention-apply --tenant-id %i --confirm %i" in backup_service
    assert "Persistent=yes" in backup_timer
    assert "restore-latest-drill --tenant-id %i" in restore_service
    assert "--target-id restore-drill-%i-quarterly" in restore_service
    assert "--confirm-target restore-drill-%i-quarterly" in restore_service
    assert "Persistent=yes" in restore_timer
    for service in (backup_service, restore_service):
        assert "User=memory-operator" in service
        assert "NoNewPrivileges=yes" in service
        assert "ProtectSystem=strict" in service


def test_memoryctl_wires_the_production_restore_executor_and_workspace() -> None:
    cli = read(ROOT / "src" / "agent_memory_service" / "cli.py")
    operator_environment = read(DEPLOY / "operator.env.example")

    assert "HostRestoreDrillExecutor(" in cli
    assert "DockerCanonicalRestoreVerifier(" in cli
    assert "restore_drill=restore_executor" in cli
    assert 'os.getenv("MEMORY_RESTORE_WORKSPACE_DIR"' in cli
    assert "MEMORY_RESTORE_WORKSPACE_DIR=" in operator_environment
    assert "MEMORY_PLATFORM_IMAGE=" in operator_environment


def test_alloy_uses_only_opaque_tenant_and_stable_service_labels() -> None:
    alloy = read(OBSERVABILITY / "alloy" / "config.alloy")

    assert "__meta_docker_container_name" not in alloy
    assert "__meta_docker_container_label_memory_tenant_id" not in alloy
    assert "__meta_docker_container_label_memory_tenant_ref" in alloy
    assert 'target_label  = "tenant_ref"' in alloy


def test_grafana_provisions_all_three_data_sources_and_a_valid_dashboard() -> None:
    datasources = read(
        OBSERVABILITY / "grafana" / "provisioning" / "datasources" / "datasources.yaml"
    )
    dashboard_path = OBSERVABILITY / "grafana" / "dashboards" / "platform-overview.json"
    dashboard = json.loads(read(dashboard_path))

    for name, source_type in (("Mimir", "prometheus"), ("Loki", "loki"), ("Tempo", "tempo")):
        assert f"- name: {name}" in datasources
        assert f"type: {source_type}" in datasources
    assert "datasourceUid: loki" in datasources
    assert "datasourceUid: mimir" in datasources
    assert dashboard["uid"] == "memory-platform-overview"
    assert len(dashboard["panels"]) >= 9
    titles = {panel["title"] for panel in dashboard["panels"]}
    assert {
        "Authenticated request rate",
        "Agent Run progress",
        "PostgreSQL operations",
        "RabbitMQ queue depth",
        "Shadow-limit signals",
        "Tenant Neo4j operations",
        "Worker failures and retries",
    } <= titles


def test_internal_network_is_shared_by_name_and_not_public() -> None:
    shared = read(DEPLOY / "shared.compose.yaml")
    tenant = read(DEPLOY / "tenant.compose.yaml")

    assert re.search(
        r"^  backplane:\n    name: memory-backplane\n    internal: true\n    attachable: true$",
        shared,
        re.MULTILINE,
    )
    assert re.search(
        r"^  backplane:\n    name: memory-backplane\n    external: true$",
        tenant,
        re.MULTILINE,
    )

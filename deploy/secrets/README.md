# Runtime secrets

This directory is only a local layout example. Never commit secret values.

The shared Compose project expects these mode-`0600` files by default:

- `cloudflare_api_token`
- `memory_public_host` (the bare TLS hostname; no scheme, port, or path)
- `postgres_password` (least-privilege Control Store application role)
- `postgres_admin_password` (host/bootstrap database administrator; never mounted into applications)
- `postgres_exporter_password` (dedicated `pg_monitor` metrics role; mounted only into the exporter)
- `rabbitmq_password`
- `telemetry_hmac_key` (shared HMAC key for stable, non-enumerable telemetry references)
- `anthropic_api_key`
- `telegram_bot_token`
- `telegram_webhook_secret`
- `grafana_admin_password`

Generate `rabbitmq_password` with a high-entropy base64url alphabet without `=`
padding; the RabbitMQ entrypoint rejects characters that could alter its interpolated
configuration.

Generate `telemetry_hmac_key` once with `openssl rand -hex 32`. Gateway, worker, and
host-side `coengramctl` must read that same protected file through
`MEMORY_TELEMETRY_HMAC_KEY_FILE`; never place the key value in an environment variable.
Changing the key intentionally changes every telemetry reference, so rotate it only as
one coordinated observability-boundary operation.

Each directory below `tenants/` is named with an immutable Tenant identifier and
contains mode-`0640` `neo4j_password` and `postgres_password` files. Directories use
mode `0750`; both are group-owned by the dedicated numeric `TENANT_SECRETS_GID`. The Tenant
Compose project consumes the former; the trusted application services consume both
through their read-only credential directory after server-side Tenant routing. In production, set
`MEMORY_SECRETS_DIR` and `TENANT_SECRETS_DIR` to protected host paths outside the
repository. Secret creation and rotation are performed by the operator workflow;
Compose never generates or prints them.

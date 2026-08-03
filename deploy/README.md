# CoEngram production Compose layout

The production topology has one shared Compose project and one Compose project for
each Tenant. Only Caddy publishes public host ports. PostgreSQL and Grafana bind to
`127.0.0.1` only so host-side `coengramctl` and an SSH tunnel can reach them. RabbitMQ,
the telemetry stores, application services, and every Neo4j Community instance remain
on Docker networks and are not routed by Caddy.

## Operator prerequisites

Outside this repository, configure the proxied Cloudflare DNS record for
the public hostname stored in the protected `memory_public_host` secret file, the host firewall, host disk encryption, SSH access, and
remote backup storage. Create protected secret files rather than placing credentials
in an environment file. The Cloudflare token needs only Zone Read and DNS Edit for the
relevant zone.

Install Docker Engine with Compose v2, the pinned PostgreSQL 17 client (`pg_dump`),
`age`, and the released `coengramctl` Python 3.12 package on the operator host. The
`memory-operator` service account needs read access to protected inputs, write access
only beneath `/var/lib/coengram`, and host-side Docker access for exact
per-Tenant operations. It must not be a general interactive administrator account.

Database, broker, and graph passwords seed stateful services on their first start.
Replacing a file alone is not rotation: the operator workflow must update the service
credential and its clients during the documented overlap procedure before retiring the
old value.

Use [the credential rotation runbook](../docs/runbooks/credential-rotation.md) for
Principal tokens, PostgreSQL, RabbitMQ, per-Tenant Neo4j, age identities, and external
provider secrets.

PostgreSQL uses three deliberately separate shared credentials. `memory_admin` is the
first-boot/host provisioning administrator and its `postgres_admin_password` is never
mounted into gateway, worker, or migrations. `memory_control` owns only the
`memory_control` database and applications receive only its `postgres_password`.
`memory_metrics` has only `pg_monitor` and uses `postgres_exporter_password`, mounted
only into the internal PostgreSQL exporter. Every Tenant database has another,
distinct Tenant role credential.

On Linux, Compose bind-mounted file secrets retain host ownership and mode. The custom
PostgreSQL entrypoint therefore copies only the database secrets into a root-owned
runtime directory, changes each copy to `postgres:postgres` mode `0400`, and then hands
control to the upstream entrypoint. Secret values remain file-backed and the protected
host files can stay owner-only mode `0600`.

## Shared services

Copy `shared.env.example` to an operator-owned environment file. Create a dedicated
`memory-secrets` host group, add `memory-operator` to it, and set both
`TENANT_SECRETS_GID` and `MEMORY_TENANT_SECRETS_GID` to its numeric GID. Tenant
directories must be group-owned mode `0750` and their files mode `0640`; gateway and
worker receive that supplementary group but no other host credentials. Keep the
shared service credential files themselves owner-only mode `0600`. Point
`MEMORY_PLATFORM_IMAGE` at the released application's `sha256` digest—not only its
tag—and set the gateway and worker commands exposed by that image. Version and digest
settings for upstream images must always be updated together. Create every file listed
in `secrets/README.md` with
mode `0600`, or set `MEMORY_SECRETS_DIR` and `TENANT_SECRETS_DIR` to equivalent
protected host directories. Alloy never mounts the Docker socket. The shared project
mounts it only into a dedicated Docker API proxy whose image is pinned by immutable digest.
The proxy is reachable only by Alloy on the internal `docker-api` network and publishes
no host port. Exact method and path matching permits only API negotiation, container
and network discovery, container inspection, and container log reads. Archive/file
reads, stats, images, secrets, exec, and every mutation are rejected before reaching
the Docker socket. Treat changes to its image, endpoint allowlist, network, or socket
mount as security-boundary changes.

Create `telemetry_hmac_key` once with `openssl rand -hex 32`. The shared Compose project
mounts that one file into gateway and worker, while host-side `coengramctl` reads the
same file path from `MEMORY_TELEMETRY_HMAC_KEY_FILE` in `operator.env`. Tenant lifecycle
commands derive `TENANT_TELEMETRY_REF` themselves; do not calculate it with an unkeyed
hash or copy the HMAC key into an environment variable.

Validate before applying:

```sh
docker compose --env-file /protected/path/shared.env \
  -f deploy/shared.compose.yaml config --quiet
```

Then start the shared project with the same arguments. The one-shot `migrate-control`
service applies Alembic before the gateway or worker starts. The `memory-backplane`
network is created by this project and is marked internal. Grafana is available only
on the configured loopback operator port; use a short-lived SSH tunnel rather than
adding it to the public Caddy routes.

Alloy scrapes the gateway, worker, RabbitMQ, Caddy, and the dedicated PostgreSQL
exporter. Neo4j's native metrics surface is Enterprise-only, so this Community-only
deployment discovers only containers labelled `memory.neo4j_health=true` and performs
a Bolt TCP black-box probe. PostgreSQL and Neo4j stdout are not shipped to Loki because
their error diagnostics may contain statement parameters or stored values; exporter
metrics and health probes keep them visible without crossing that content boundary.
The probe and collected logs retain only server-derived opaque labels; raw Tenant IDs
and container names are not promoted.

The gateway or worker declares the versioned `memory.events.v1` exchange, durable
`memory.graph.apply.v1` queue, `memory.dead-letter.v1` exchange, and durable
`memory.graph.dead-letter.v1` queue idempotently when it establishes AMQP topology.
RabbitMQ boot-time definition import is intentionally not used because on a blank node
it suppresses creation of the configured seed User and virtual host.

## Telegram bot onboarding

Create the Telegram bot with BotFather outside CoEngram, then place its token in the
protected `telegram_bot_token` file and generate an independent high-entropy value for
`telegram_webhook_secret`. A channel binding always resolves a Telegram sender to an
explicit User → Agent Delegation in exactly one Tenant; the sender cannot choose a
Tenant or identity in a message.

Provision the identities and relationship from the operator host (substitute your
immutable IDs and the numeric Telegram sender ID):

```sh
coengramctl principal create --id user-alice --kind user --name "Alice"
coengramctl principal create --id agent-telegram --kind agent --name "Telegram Agent"
coengramctl membership grant --tenant-id tenant-product-a-backend \
  --principal-id user-alice --role tenant_member
coengramctl membership grant --tenant-id tenant-product-a-backend \
  --principal-id agent-telegram --role tenant_member
coengramctl delegation create --id delegation-alice-telegram \
  --tenant-id tenant-product-a-backend --agent-id agent-telegram \
  --subject-user-id user-alice
coengramctl channel bind --id channel-alice-telegram --channel telegram \
  --external-id 123456789 --delegation-id delegation-alice-telegram
```

Before registering the webhook, a protected operator session can briefly call
Telegram's `getUpdates` method after the User sends the bot a message and read
`message.from.id`; do not persist or publish the response. Once the shared stack is
healthy, register the only accepted webhook path using the protected host and secret
files:

```sh
bot_token="$(tr -d '\r\n' < /protected/path/telegram_bot_token)"
webhook_secret="$(tr -d '\r\n' < /protected/path/telegram_webhook_secret)"
public_host="$(tr -d '\r\n' < /protected/path/memory_public_host)"
curl --fail --silent --show-error \
  "https://api.telegram.org/bot${bot_token}/setWebhook" \
  --data-urlencode "url=https://${public_host}/api/v1/telegram/webhook" \
  --data-urlencode "secret_token=${webhook_secret}"
unset bot_token webhook_secret public_host
```

Telegram supplies the secret as `X-Telegram-Bot-Api-Secret-Token`; CoEngram rejects a
missing or incorrect value before resolving the channel binding. Disable or remove a
binding immediately when access changes, and rotate both bot and webhook secrets after
suspected exposure.

The application identity is tagless and restricted to the private `memory` vhost and
resources named `memory.*`; it is not a broker administrator and cannot use the
management UI/API. Host operators administer RabbitMQ only through protected,
container-local `rabbitmqctl`. After fixing the dependency that produced a poison
delivery, schedule one exact authoritative retry:

```sh
coengramctl outbox redrive --tenant-id tenant-product-a-backend \
  --event-id EVENT_ID --operator-id OPERATOR_ID \
  --confirm tenant-product-a-backend
```

The command reopens only that unprocessed PostgreSQL outbox event, writes a content-free
operator audit record, acknowledges the matching dead letter, and lets the normal relay
publish it again. Projection remains idempotent if recovery is interrupted.

## Tenant Memory Store

For each Tenant, render a dedicated environment file based on `tenant.env.example`.
`TENANT_ID` is the immutable control-plane identifier. Its protected directory must
contain `neo4j_password` and the Tenant database role's `postgres_password`; that exact
directory is also beneath the shared application services' read-only Tenant credential
directory.

After the shared project has created `memory-backplane`, validate and start a Tenant:

```sh
docker compose --env-file /protected/path/tenant.env \
  -f deploy/tenant.compose.yaml config --quiet
docker compose --env-file /protected/path/tenant.env \
  -f deploy/tenant.compose.yaml up -d
```

The generated service alias is `neo4j-<TENANT_ID>`. Only the authenticated gateway and
worker may resolve a Tenant from a server-derived Tenant Session and use that alias;
callers never provide it. The Community image requires no runtime plugin download, so
the Tenant project remains entirely on the internal backplane.

## Declarative Tenant provisioning

Copy `tenant.manifest.example.json` outside the repository and edit only its secret-free
declarations. Run `coengramctl` on the server host so Docker access stays out of the
gateway and worker. Point its database settings at the PostgreSQL loopback port and its
Tenant credentials directory at the same protected directory mounted by the shared
application:

```sh
export MEMORY_CONTROL_DATABASE_HOST=127.0.0.1
export MEMORY_CONTROL_DATABASE_PORT=5432
export MEMORY_CONTROL_DATABASE_NAME=memory_control
export MEMORY_CONTROL_DATABASE_USER=memory_control
export MEMORY_CONTROL_DATABASE_PASSWORD_FILE=/protected/memory/postgres_password
export MEMORY_TELEMETRY_HMAC_KEY_FILE=/protected/memory/telemetry_hmac_key
export MEMORY_POSTGRES_ADMIN_USER=memory_admin
export MEMORY_POSTGRES_ADMIN_PASSWORD_FILE=/protected/memory/postgres_admin_password
export MEMORY_TENANT_DATABASE_HOST=127.0.0.1
export MEMORY_TENANT_CREDENTIALS_DIR=/protected/memory/tenants
export MEMORY_REPOSITORY_ROOT=/opt/coengram

coengramctl tenant plan --manifest /protected/manifests/product-a.json
coengramctl tenant apply --manifest /protected/manifests/product-a.json \
  --confirm tenant-product-a-backend
```

`plan` is read-only. `apply` records an inactive Tenant and fail-closed route, creates
missing group-owned mode-`0640` credentials, starts the exact Tenant Compose project, creates its
distinct PostgreSQL role/database, applies PostgreSQL and Neo4j migrations, verifies
health/routing/isolation, and activates only after every gate passes. A failed step is
persisted; rerunning the identical manifest resumes after the last completed boundary.
A changed manifest cannot resume partially applied infrastructure.

## Revocation and credential rotation

Administration is fail-closed and dependency-aware. These commands update the Control
Store atomically; a disabled Principal or Membership, removed role, revoked Delegation,
or revoked token fails on its next authentication check. Revoking a Delegation also
disables its Channel Bindings and credentials. Token rotation creates the replacement
and shortens the old token in one transaction; overlap may not exceed 24 hours.

```sh
coengramctl token rotate --token-id TOKEN_ID --overlap-minutes 15
coengramctl membership revoke-role --tenant-id TENANT_ID \
  --principal-id PRINCIPAL_ID --role knowledge_curator
coengramctl delegation revoke --id DELEGATION_ID
coengramctl principal disable --id PRINCIPAL_ID
```

The rotate response displays the replacement token once. Store it in the authorized
client before the overlap expires; do not put it in a manifest, log, or shell history.

## Active Tenant database migrations

Runtime services verify the exact Control and every active Tenant PostgreSQL Alembic
head before serving. Upgrade the application image and migrate the Control Store first,
then create a current encrypted recovery point and migrate active Tenants through the
persisted host workflow:

```sh
coengramctl backup create --tenant-id tenant-product-a-backend
coengramctl migration plan --tenant-id tenant-product-a-backend
coengramctl migration apply --tenant-id tenant-product-a-backend \
  --confirm tenant-product-a-backend
```

The plan targets code-defined PostgreSQL and Neo4j revisions and resolves only a
healthy, canonical active route. Before either database can be changed, apply verifies
an exact Tenant backup receipt under `MEMORY_RECOVERY_EVIDENCE_DIR`, the referenced
manifest SHA-256, manifest scope/route, completeness, and the 24-hour RPO. The recovery
gate is a persisted first step under a versioned workflow ID, so an older ungated
migration record cannot satisfy it. Apply then persists each completed step, runs the
registered Neo4j migration, and rechecks health, routing, and isolation. If the gate
fails, create a new backup and use `coengramctl migration resume`; no database migration
has run. For any other dependency failure, fix that exact dependency and resume with
the same Tenant ID and exact confirmation. Resume skips only the persisted prefix; it
never scans or mutates another Tenant.

## Recovery and retirement

Copy `operator.env.example` to `/etc/coengram/operator.env`, keep the file
secret-free, and create the protected paths/files it references. Age recipient and
identity files must be regular mode-`0600` files; the identity stays on the recovery
host. The backup operator publishes only encrypted artifacts plus content-free
manifests and recovery-evidence receipts.

The repository provides hardened per-Tenant systemd templates. After reviewing the
paths, service account, Docker group, and binary location for this host, install and
enable them for each immutable Tenant ID:

```sh
sudo install -m 0644 deploy/systemd/coengram-backup@.service \
  deploy/systemd/coengram-backup@.timer \
  deploy/systemd/coengram-restore-drill@.service \
  deploy/systemd/coengram-restore-drill@.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now coengram-backup@tenant-product-a-backend.timer
sudo systemctl enable --now coengram-restore-drill@tenant-product-a-backend.timer
```

Nightly backup reports RPO health, previews retention, and then applies only the exact
backup IDs selected by the 7-daily/4-weekly policy. Quarterly restore uses the latest
valid recovery point and a non-routed `restore-drill-*` target. Remote replication of
the encrypted artifact directory and remote storage credentials remain host/operator
owned outside this repository.

Follow [the backup/restore runbook](../docs/runbooks/backup-restore.md) for manual
commands, failure handling, and drill evidence. Follow
[the decommission runbook](../docs/runbooks/decommission.md) for the persisted
two-operator retirement workflow. Request first suspends access and credentials, a
distinct operator confirms exact Tenant ID plus recovery evidence, and final
destruction is unavailable until the 30-day grace period ends. Each exact resource
deletion is resumable and final state retains only a content-free tombstone.

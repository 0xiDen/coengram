# Credential rotation runbook

## Invariants

Keep secret values out of argv, environment files, manifests, logs, tickets, and shell
history. Stage shared-service replacements as regular mode-`0600` files owned by the
`memory-operator` account. Validate the exact dependent services before revoking the
old credential. A service credential without native overlap requires a short announced
maintenance window; never fake overlap by leaving an undocumented administrator
credential active.

Per-Tenant PostgreSQL and Neo4j replacements are the exception: keep them mode `0640`
inside mode-`0750` Tenant directories, group-owned by the dedicated secrets GID that is
the only supplementary group granted to gateway and worker.

## Principal Access Tokens

Create an atomic replacement with at most 24 hours of overlap:

```console
coengramctl token rotate --token-id TOKEN_ID --overlap-minutes 15
```

The replacement is displayed once. Configure the exact Claude, MCP, HTTP, or automation
client, verify an authenticated `/api/v1` or MCP call, then revoke the old token early
if the overlap is no longer required. A suspected token is revoked immediately with
`coengramctl token revoke`; do not rotate it with overlap.

## Cloudflare, Anthropic, Telegram, and Grafana

These providers issue replacement credentials outside the repository. Write the new
value to a sibling protected file, atomically replace the configured secret file, and
recreate only its consumers:

- Cloudflare token: Caddy;
- Anthropic key: worker;
- Telegram bot token or webhook secret: gateway; and
- Grafana administrator password: Grafana, using Grafana's supported administrator
  reset procedure before changing the mounted bootstrap file.

Verify Caddy can reload and renew through DNS-01, an explicitly enabled Anthropic smoke
run can authenticate, Telegram accepts the configured webhook secret, or Grafana login
works as applicable. Revoke the old provider credential only after the check. Creating
Cloudflare records/proxy mode, rotating through BotFather, and provider account policy
remain operator-owned external actions.

## PostgreSQL application roles

PostgreSQL gives one password to each application role, so this is a coordinated
maintenance rotation rather than a dual-password overlap:

1. Take and verify a current encrypted recovery point.
2. Resolve the exact Control or Tenant role from the canonical route; never use a role
   wildcard.
3. In a protected interactive `psql` administrator session, use `\password ROLE` so
   neither old nor new password appears in argv or SQL history.
4. Atomically replace only that role's protected password file.
5. Recreate the exact gateway/worker consumers. For a Tenant role, also run routing,
   schema, isolation, retain, and recall checks for that Tenant.
6. If validation fails, keep ingress suspended for that exact Tenant and repair the
   credential mapping; do not restore a broad shared credential.

The least-privilege Control Store role rotation affects gateway, worker, host
`coengramctl`, and the migration job together. The separate `memory_admin` bootstrap
credential is used only by host provisioning and database administration and must
never be copied into an application secret mount. A Tenant role rotation affects only
that Tenant's routed data access and host backup source.

The `memory_metrics` role is separate again: rotate its password in the same protected
administrator session, replace only `postgres_exporter_password`, recreate only
`postgres-exporter`, and verify `pg_up == 1`. Confirm the role remains non-superuser,
cannot create roles or databases, cannot bypass row-level security, and is only a
member of `pg_monitor` before closing the maintenance window.

## RabbitMQ

Use RabbitMQ's administrator interface over the private operator path to create a new
least-privilege application user without putting its password on the command line.
Grant only the platform virtual host permissions, atomically replace the protected
password file and configured username, recreate gateway/worker consumers, and confirm
the versioned exchange, queues, publisher confirmations, and consumer acknowledgements.
Delete the old broker user after the old connections have drained.

## Tenant Neo4j

Rotate one Tenant at a time. Suspend its route, take a verified recovery point, and stop
only that Tenant's projection/recall traffic. Change the `neo4j` password in a protected
interactive `cypher-shell` session, atomically replace that Tenant's
`neo4j_password`, recreate its exact Compose service and trusted application consumers,
then run graph health, routing, isolation, retain, and recall checks before reactivation.
Never restart or rewrite every Tenant credential as one bulk operation.

## Telemetry HMAC key

Treat `telemetry_hmac_key` as one shared observability-boundary credential. Routine
rotation is intentionally avoided because changing it changes every Tenant, token, run,
request, and message reference. If compromise requires rotation, generate a mode-`0600`
replacement, atomically replace the file used by shared Compose and host-side
`coengramctl`, recreate gateway and worker together, and regenerate each running Tenant
Compose label through the host lifecycle command before resuming monitoring. Keep only
the old and new opaque-reference epochs in operational notes; never record either key.

## Age recovery identities

Generate the new X25519 identity on the protected recovery host. Add its recipient to
the recipients file before making it primary, assign a new non-secret
`MEMORY_BACKUP_AGE_KEY_ID`, and create/restore a test recovery point. Keep the old
identity offline until every backup encrypted only to the old recipient has expired or
been re-encrypted through an approved offline process. Removing the old identity before
retention expiry makes those recovery points unusable.

## Closeout

For every rotation, record only credential type, opaque/exact resource identifier,
operators, timestamps, validation results, and retirement status. Do not record secret
values or token verifiers. Confirm captured telemetry contains none of the seeded old
or new canary values.

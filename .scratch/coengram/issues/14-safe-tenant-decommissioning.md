# 14. Safe Tenant decommissioning

Status: resolved

Blocked by: 02. Declarative Tenant lifecycle and hard isolation; 13. Encrypted backup
and verified restore.

## What to build

Add a deliberate Tenant decommissioning state machine that cannot destroy data in one
command. An initial request suspends access and verifies backup or export; a separate
operator must confirm the immutable Tenant identifier; a 30-day grace period permits
recovery before final removal of the Tenant graph project, volume, PostgreSQL database,
roles, and domain routing leaves only a content-free tombstone.

## Acceptance criteria

- [x] Decommissioning begins with a non-destructive request that records the immutable
      Tenant identifier, Actor, reason, timing, and required backup/export policy.
- [x] Requesting decommissioning immediately suspends Tenant Sessions, blocks new token
      issuance, and revokes active Tenant credentials without deleting data.
- [x] The workflow verifies a recent valid backup or explicitly accepted export policy
      before it can enter the grace period.
- [x] A separate operator confirmation must include the exact immutable Tenant
      identifier; request and confirmation cannot be combined in one command or Actor
      action.
- [x] Final destruction is forbidden until 30 days after confirmed entry into the grace
      period, and current state plus eligible date are visible through the operator CLI.
- [x] An authorized cancellation during the grace period restores service only after
      routes, schemas, isolation, credentials, and health checks pass and new tokens are
      issued deliberately.
- [x] Finalization removes the Tenant Compose project, Neo4j volume and credentials,
      Tenant PostgreSQL database and role, domain routes, and remaining Tenant-scoped
      secret files.
- [x] Finalization does not remove shared PostgreSQL, RabbitMQ, Caddy, observability, or
      any other Tenant's containers, volumes, databases, roles, routes, or secrets.
- [x] After finalization, only a content-free tombstone and operator audit remain; no
      memory, candidate, Agent Run, prompt, embedding, message text, or credentials
      survive in live domain stores.
- [x] Repeated request, confirmation, cancellation, and finalization operations are
      idempotent and failed steps resume safely without broad destructive commands.
- [x] State-machine and integration tests cover every allowed and forbidden transition,
      wrong-ID confirmation, same-Actor confirmation, missing backup, early finalize,
      cancellation, partial failure, retry, and other-Tenant non-impact.
- [x] Operator documentation names exactly what is removed, the recovery window, and
      which backup artifact remains externally under operator retention.

## Comments

_No comments yet._

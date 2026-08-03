# Tenant decommission runbook

## Safety invariant

Tenant decommissioning is a persisted two-operator protocol, not a cleanup
script. Requesting it deletes nothing. Confirmation and finalization cannot be
combined. The confirmer must be different from the requester, must type the exact
tenant ID, and starts a 30-day grace period only after recovery evidence passes.

Never use a broad command such as a system-wide Docker prune, an unscoped Compose
volume removal, a PostgreSQL cluster/database wildcard, or recursive removal of a
shared secret root.

## 1. Request and suspend

Operator A resolves the immutable tenant resources from the control store and
reviews the request document:

- tenant ID, reason, requester, request time, and backup/export policy;
- exact Compose project and tenant Neo4j volume;
- exact tenant PostgreSQL database and role;
- exact route ID; and
- exact tenant secret reference.

`DecommissionService.request` persists the request before side effects, suspends
sessions, blocks new token issuance, and revokes active user/agent credentials.
It performs no deletion. If a suspension step fails, retry the same request ID
and identical payload; completed steps are not repeated.

```console
coengramctl decommission request --input /protected/decommission/request.json
```

## 2. Verify recovery and confirm

Select either a complete, integrity-verified backup no more than 24 hours old or
an encrypted export whose policy Operator B explicitly accepts. Verify that the
referenced artifact exists and is readable outside the live tenant. The external
artifact is not transferred into the deletion workflow.

Operator B submits a separate confirmation containing the request ID, exact
tenant ID, their actor ID, evidence ID, verification time, and key-independent
integrity result. The service rejects Operator A, the wrong tenant ID, missing or
stale backup, incomplete/corrupt evidence, future timestamps, and unaccepted
exports. A successful confirmation persists `grace_ends_at = confirmed_at + 30
days`.

The evidence ID must have an exact content-free receipt named `<artifact_id>.json`
under `MEMORY_RECOVERY_EVIDENCE_DIR`. Repeat the immutable tenant ID outside the
version-1 JSON document:

```console
coengramctl decommission confirm \
  --input /protected/decommission/confirmation.json \
  --confirm-tenant-id tenant-product-a-backend
```

The tenant remains suspended during grace. No destructive adapter is called
before the deadline.

## 3a. Cancel during grace

Cancellation is allowed only before `grace_ends_at` and before finalization. Run
all five read-only readiness checks against the exact tenant: route, schema,
isolation, backend credentials, and service health. Only after all pass may the
adapter reactivate the tenant.

Reactivation deliberately issues no user or agent token. An operator must use the
normal audited token-issuance flow to mint replacement credentials after the
cancelled state is persisted. A failed readiness check leaves the tenant
suspended in grace and records the failed action for retry.

```console
coengramctl decommission cancel \
  --input /protected/decommission/cancellation.json \
  --confirm-tenant-id tenant-product-a-backend
```

## 3b. Finalize after grace

At or after the exact grace deadline, finalization removes these tenant-owned
targets in persisted, idempotent steps:

1. exact tenant Compose project;
2. exact tenant Neo4j volume;
3. exact tenant PostgreSQL database and database role;
4. exact tenant route;
5. exact tenant secret files/reference; and
6. tenant domain records from the application stores.

It must not stop or delete shared PostgreSQL, the control database, Caddy,
RabbitMQ, observability services, the gateway, worker, or any other tenant's
Compose project, database, graph volume, route, credentials, or records.

After each successful step the state store commits that step. On failure, correct
the exact dependency and retry the same request; already committed steps are
skipped. Do not compensate with a broader delete. When all steps pass, the active
request is atomically replaced with a content-free tombstone containing request
ID, tenant ID, the two operator IDs, destruction time, and status. The reason,
resource names, evidence metadata, and all tenant domain content are removed.

```console
coengramctl decommission finalize \
  --input /protected/decommission/finalization.json \
  --confirm-tenant-id tenant-product-a-backend
```

## External recovery artifact and audit

The encrypted backup/export remains in external storage after tenant destruction.
Its 7-daily/4-weekly retention and any longer legal policy continue independently;
decommission finalization never deletes it. The content-free tombstone and
operator audit events prove the transition without retaining memory content.

Before closing the operation, verify that the tombstone is readable, the old
route and credentials fail closed, the exact tenant stores no longer exist, the
external recovery artifact still exists, and a sampled different tenant remains
healthy and recallable.

# 02. Declarative Tenant lifecycle and hard isolation

Status: resolved

Blocked by: 01. Authenticated Private Memory tracer.

## What to build

Let an operator declaratively create and maintain hard-isolated Tenants. Applying a
secret-free Tenant Manifest plans and resumes provisioning of a dedicated Neo4j
Community instance and a tenant-specific PostgreSQL database and role, then activates
the Tenant only after schema, routing, health, and isolation checks succeed.

## Acceptance criteria

- [x] A versioned, secret-free Tenant Manifest represents the Tenant, Principals,
      Memberships, roles, and policies and validates before any mutation.
- [x] The operator CLI can plan and idempotently apply a Tenant Manifest and reports
      every proposed infrastructure and control-state change.
- [x] Provisioning state is durable, failed work is visible, and reapplying safely
      resumes from the last completed step without destructive rollback.
- [x] Each Tenant receives one Neo4j Community instance, distinct credentials, a
      distinct persistent volume, and a generated service address with no direct public
      port.
- [x] One shared PostgreSQL instance hosts the content-free Control Store and one
      immutable-ID-named Tenant Operations Store database with a distinct role per
      Tenant.
- [x] The Control Store contains routing and provisioning records but no Memory Item,
      candidate, prompt, model, or Agent Run content.
- [x] Alembic manages control and Tenant PostgreSQL schemas, and versioned idempotent
      graph migrations manage required Neo4j constraints and indexes.
- [x] Migration plan, backup prerequisite, apply, resume, and schema-compatibility
      checks are exposed through the operator CLI; application startup rejects
      incompatible schemas.
- [x] A Tenant becomes active only after database, graph, migration, route, health, and
      cross-Tenant isolation checks pass.
- [x] Two real Tenant stacks prove correct routing and non-observation, including during
      missing, failed, or unhealthy routes; no request can fall back to another Tenant.
- [x] Provisioning invokes Compose from the host and does not mount a Docker socket into
      an application container.
- [x] State-machine and integration tests cover fresh apply, no-op apply, partial
      failure, resume, forbidden transitions, migration mismatch, and isolation.

## Comments

_No comments yet._

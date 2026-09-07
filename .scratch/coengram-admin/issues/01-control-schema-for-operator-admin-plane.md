# 01. Control schema for Operator admin plane

Status: ready-for-agent

Blocked by: None.

## What to build

Add Control Store schema for Operators, Operator Access Tokens, Admin Sessions,
Operator Audit Events, and Provisioning Jobs.

## Acceptance criteria

- [ ] A control Alembic migration after `0007_backup_barriers` creates content-safe
      tables for Operators, Operator Access Tokens, Admin Sessions, Operator Audit
      Events, Provisioning Jobs, and job attempts/status history.
- [ ] Operator and Operator Role constraints support `operator_admin`, `identity_admin`,
      `tenant_provisioner`, `tenant_support`, `knowledge_admin`, `token_admin`, and
      `audit_viewer`.
- [ ] Token/session tables store only non-reversible verifiers and metadata, never
      one-time credential secrets.
- [ ] Operator Audit Events can store actor, role context, action, target identifiers,
      request identity, timestamp, and safe before/after metadata.
- [ ] Provisioning Jobs model idempotency keys, Tenant Manifest fingerprints, claim
      state, attempts, step progress, failure, cancellation, cleanup eligibility, and
      cleanup completion.
- [ ] `CONTROL_SCHEMA_REVISION` is updated and compatibility checks fail closed on old
      schema revisions.
- [ ] Migration tests cover upgrade shape and constraints.

## Comments

No comments yet.

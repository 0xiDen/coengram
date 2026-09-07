# 06. Provisioning Jobs and Operator Service

Status: ready-for-agent

Blocked by: 01. Control schema for Operator admin plane; 03. Admin Sessions and CSRF;
04. Structured Operator Audit Events.

## What to build

Add async Provisioning Jobs requested through the admin API and executed by a separate
Operator Service using Control Store polling and claiming.

## Acceptance criteria

- [ ] `tenant_provisioner` and `operator_admin` can validate/plan Tenant Manifests and
      create/apply Provisioning Jobs.
- [ ] Provisioning Jobs require client idempotency keys and enforce one active job per
      Tenant Manifest fingerprint.
- [ ] Operator Service polls, claims, heartbeats, executes, and releases jobs without
      exposing Docker or host mutation authority to the gateway.
- [ ] Job status exposes step timeline, attempts, completed steps, failed step, failure
      code, cancellation state, heartbeat/staleness, and final result.
- [ ] Failed jobs retry on the same job and append attempt history.
- [ ] Running jobs support cooperative cancellation between provisioning steps.
- [ ] Never-active Provisioning Cleanup can remove Tenant secrets, Tenant PostgreSQL
      DB/role, Neo4j container/project/volume, routing, inactive control records, and
      provisioning state after two-step confirmation.
- [ ] Cleanup is forbidden once a Tenant has ever reached active and does not replace
      active Tenant decommissioning.
- [ ] Tests cover idempotency, fingerprint uniqueness, claiming races, retry,
      cancellation, cleanup confirmation, and active-Tenant cleanup denial.

## Comments

No comments yet.


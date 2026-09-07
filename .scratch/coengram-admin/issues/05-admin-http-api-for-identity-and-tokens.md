# 05. Admin HTTP API for identity and tokens

Status: ready-for-agent

Blocked by: 02. Operator auth and bootstrap CLI; 03. Admin Sessions and CSRF; 04.
Structured Operator Audit Events.

## What to build

Expose `/api/v1/admin/*` routes for dashboard data, Tenants, Operators, Principals,
Tenant Memberships, Principal Access Tokens, and Operator Access Tokens.

## Acceptance criteria

- [ ] Admin routes are mounted under `/api/v1/admin/*` and authenticated through Admin
      Sessions.
- [ ] Dashboard returns counts, expiring tokens, unused token warnings using the current
      30-day threshold, failed Provisioning Jobs, and recent Operator Audit Events.
- [ ] Tenant list/status visibility is allowed to `tenant_provisioner`,
      `tenant_support`, `identity_admin`, `audit_viewer`, and `operator_admin`.
- [ ] Principal and Tenant Membership visibility is allowed to `identity_admin`,
      `tenant_support`, and `operator_admin`.
- [ ] Principal and Tenant Membership mutation is allowed to `identity_admin` and
      `operator_admin`.
- [ ] Principal Access Token metadata is visible to `token_admin`, `identity_admin`,
      `tenant_support`, and `operator_admin`.
- [ ] Principal Access Token mutation is allowed to `token_admin`, `identity_admin`, and
      `operator_admin`.
- [ ] Operator Access Token metadata is visible to `token_admin`, `operator_admin`, and
      the same Operator viewing their own tokens.
- [ ] Operator Access Token mutation is allowed to `token_admin` and `operator_admin`.
- [ ] User creation wizard API creates a User, grants Tenant Membership, and optionally
      issues a one-time Access Token in one audited command.
- [ ] HTTP tests cover authorization matrix, one-time token reveal, validation failures,
      and no-secret metadata responses.

## Comments

No comments yet.


# 08. Admin frontend workflows

Status: ready-for-agent

Blocked by: 05. Admin HTTP API for identity and tokens; 06. Provisioning Jobs and
Operator Service; 07. Admin frontend scaffold.

## What to build

Implement slice-1 admin pages and workflows against the backend admin API.

## Acceptance criteria

- [ ] Dashboard shows counts, action queue, expiring tokens, unused token warnings,
      failed/stalled Provisioning Jobs, and recent Operator Audit Events.
- [ ] Tenants page lists Tenants and route/provisioning status.
- [ ] Tenant provisioning wizard builds a Tenant Manifest, validates/plans it, applies
      it, and links to job detail.
- [ ] Identity page supports Principal CRUD, Tenant Membership management, and the User
      creation wizard with optional token issuance.
- [ ] Tokens page lists Principal and Operator token metadata and supports issue,
      rotate, and revoke flows with one-time reveal.
- [ ] Operators page manages Operators and Operator Roles with last-admin protection
      surfaced clearly.
- [ ] Provisioning Jobs page shows status, attempts, step timeline, retry,
      cancellation, degraded Operator Service state, and cleanup confirmation for
      eligible never-active jobs.
- [ ] Audit page lists and filters Operator Audit Events.
- [ ] UI never displays raw Private Memory content or retained token secrets outside
      one-time reveal modals.

## Comments

No comments yet.

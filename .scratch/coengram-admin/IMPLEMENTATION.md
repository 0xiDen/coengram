# Implementation Plan: CoEngram Admin Slice 1

Status: implemented-locally-with-validation-blockers

## Goal

Build the first vertical admin slice: Operator authentication, role-based admin API,
 structured audit, async Tenant provisioning jobs, and a Chakra UI admin panel that
exercises the flow end to end.

## Backend Modules

Create a deep admin/control Module rather than spreading checks through route handlers.
The route layer should authenticate an Admin Session, pass commands into Modules, and
return typed Pydantic views. The Control Store remains the content-free authority for
Operators, Operator Access Tokens, Admin Sessions, Operator Audit Events, and
Provisioning Jobs.

Expected additions:

- `agent_memory_service.operator_auth`: Operator identity, Operator Access Token
  issuance/authentication/rotation, Admin Session cookies, CSRF token validation.
- `agent_memory_service.admin`: Operator role authorization, dashboard/query views, and
  command orchestration over `ControlModule`.
- `agent_memory_service.operator_audit`: structured Operator Audit Event models and
  writers.
- `agent_memory_service.operator_provisioning`: Provisioning Job model, store,
  retry/cancel/cleanup commands, and Operator Service runner.
- `agent_memory_service/admin_http.py`: `/api/v1/admin/*` route adapter.
- `agent_memory_service/operator_service.py`: host-side process entrypoint.

## Control Schema

Add a new control migration after `0007_backup_barriers` with tables for:

- `control.operators`
- `control.operator_access_tokens`
- `control.admin_sessions`
- `control.operator_audit_events`
- `control.provisioning_jobs`
- `control.provisioning_job_attempts` or structured attempt data, depending on the
  cleanest query shape

Stored values must be content-safe:

- Store token/session verifiers only, never secrets.
- Store CSRF verifier or session-bound token metadata, never raw Operator Access Tokens.
- Store safe target identifiers and before/after metadata, never Private Memory content.

Update `CONTROL_SCHEMA_REVISION` after migration and keep startup fail-closed.

## Admin API

All routes live under `/api/v1/admin/*`.

Authentication:

- `POST /api/v1/admin/session` exchanges an Operator Access Token for an Admin Session
  cookie and returns current Operator/roles plus CSRF token.
- `GET /api/v1/admin/session` returns current session context.
- `DELETE /api/v1/admin/session` revokes the Admin Session.

Dashboard:

- `GET /api/v1/admin/dashboard` returns counts, expiring tokens, unused token warnings,
  failed/stalled Provisioning Jobs, and recent Operator Audit Events.

Operators:

- CRUD/disable Operators and grant/update Operator Roles.
- List/issue/rotate/revoke Operator Access Tokens with one-time reveal only on issue or
  rotation.
- Prevent removal or disablement of the final active `operator_admin`.

Tenants and provisioning:

- List Tenants and routing/provisioning status.
- Validate/plan a Tenant Manifest.
- Create/apply Provisioning Jobs with idempotency key and manifest fingerprint safety.
- Show job status, attempts, completed steps, failed step, cancellation state, cleanup
  eligibility, and Operator Service heartbeat/staleness.
- Retry failed jobs, request cooperative cancellation, and perform never-active
  Provisioning Cleanup after two-step confirmation.

Identity and Principal tokens:

- List/create/update/disable Principals.
- List/grant/update/disable Tenant Memberships.
- List/issue/rotate/revoke Principal Access Tokens with one-time reveal only on issue or
  rotation.
- Create User wizard endpoint may compose User creation, Membership grant, and optional
  token issuance in one audited command.

Audit:

- List/filter Operator Audit Events for `audit_viewer` and `operator_admin`.

## Frontend

Add `/admin` as a Vite React TypeScript app using Chakra UI. Use generated OpenAPI
types/schemas plus hand-written fetch hooks.

Pages:

- Login
- Dashboard
- Tenants
- Tenant provisioning wizard
- Identity
- User creation wizard
- Tokens
- Operators
- Provisioning Jobs
- Audit

UI shape:

- Dense tables, filters, tabs, drawers, and modals for recurring operations.
- Guided wizards for Tenant provisioning and User creation.
- One-time token reveal modal with copy/download affordance.
- Provisioning Job detail view with step timeline, retries, cancellation, cleanup
  eligibility, and degraded Operator Service state.

## Caddy and Dev

Use Vite dev proxy for local development. Production builds should be served by Caddy
at `/admin` while `/api/v1/*` continues to proxy to the gateway. Keep the gateway as
the single browser-facing API.

## Verification

Backend:

- Unit tests for Operator Access Token issuance/authentication/rotation/revocation.
- Unit tests for role checks and last-`operator_admin` lockout protection.
- Admin Session and CSRF tests.
- Audit tests proving safe metadata and no token/private content leakage.
- Provisioning Job store tests for idempotency, fingerprint uniqueness, claiming,
  retries, cancellation, cleanup eligibility, and never-active cleanup restrictions.
- HTTP route tests with FastAPI `TestClient`.

Frontend:

- Vitest/React Testing Library for auth, route guards, token reveal, provisioning wizard,
  and job detail state.
- Playwright smoke against local stack.
- Final full-stack smoke on `alex@10.0.0.11`.

## Git Notes

When commits or pushes are requested, configure author identity with:

```sh
git config user.name "0xiDen"
git config user.email "0xiden@proton.me"
```

When pushing, use:

```sh
GIT_SSH_COMMAND='ssh -i ~/.ssh/zxiDen' git push
```

## Local Implementation Notes

Completed locally:

- Operator Access Tokens, Admin Sessions, CSRF validation, Operator Roles, and
  structured Operator Audit Events.
- Admin HTTP routes for Tenants, Operators, Operator Tokens, Principals, Principal
  Tokens, Provisioning Jobs, and Audit.
- Control Store migration and Postgres adapter support for Operator admin records and
  Provisioning Jobs.
- Operator Service runner and console entry point for claiming queued Provisioning Jobs.
- React/Vite/Chakra admin frontend served in development by Vite and in production by
  Caddy at `/admin/`.

Validation blockers:

- Full-suite Docker proxy integration cannot run until Docker is reachable locally.
- SSH full-stack validation on `alex@10.0.0.11` is blocked by `No route to host`.

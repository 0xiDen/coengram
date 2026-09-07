# 09. Local and SSH full-stack tests

Status: ready-for-agent

Blocked by: 05. Admin HTTP API for identity and tokens; 06. Provisioning Jobs and
Operator Service; 08. Admin frontend workflows.

## What to build

Add local automated verification and final full-stack smoke validation on
`alex@10.0.0.11`.

## Acceptance criteria

- [ ] Backend unit and HTTP tests cover Operator auth, Admin Sessions, CSRF, role matrix,
      audit, token lifecycle, identity workflows, and Provisioning Jobs.
- [ ] Frontend Vitest/React Testing Library tests cover login, navigation guards,
      dashboard states, token reveal, User creation, and provisioning job states.
- [ ] Playwright smoke runs against local backend/frontend.
- [ ] A documented SSH smoke checklist runs on `alex@10.0.0.11` after local tests pass.
- [ ] Smoke tests cover login, Tenant list, User creation with one-time token,
      Provisioning Job plan/apply/status, cancellation or retry test path, and audit
      visibility.

## Comments

No comments yet.


# 07. Admin frontend scaffold

Status: ready-for-agent

Blocked by: 03. Admin Sessions and CSRF.

## What to build

Add a Vite React TypeScript admin app under `/admin` using Chakra UI, generated API
types, and hand-written hooks.

## Acceptance criteria

- [ ] The frontend package has Vite, React, TypeScript, Chakra UI, routing, lint/test
      scripts, and a local dev proxy to the backend.
- [ ] OpenAPI-derived TypeScript types or schemas are generated as part of frontend
      tooling while UI hooks remain hand-written.
- [ ] Layout uses hybrid ops-console navigation: Dashboard, Tenants, Identity, Tokens,
      Operators, Provisioning Jobs, and Audit.
- [ ] Route guards use Admin Session state and Operator Roles to show allowed pages.
- [ ] Login page exchanges Operator Access Token for Admin Session and stores only
      session-visible state/CSRF token client-side.
- [ ] Vitest setup covers route guard and auth hook basics.

## Comments

No comments yet.

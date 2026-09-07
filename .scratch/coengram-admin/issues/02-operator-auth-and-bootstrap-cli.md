# 02. Operator auth and bootstrap CLI

Status: ready-for-agent

Blocked by: 01. Control schema for Operator admin plane.

## What to build

Create Operator identity and Operator Access Token issuance, authentication, revocation,
rotation, and production bootstrap through `coengramctl`.

## Acceptance criteria

- [ ] Operators are separate from Principals and do not create Tenant Sessions.
- [ ] `coengramctl operator create` creates an Operator with Operator Roles.
- [ ] `coengramctl operator token issue` shows the Operator Access Token exactly once.
- [ ] Operator Access Token authentication validates prefix, verifier, expiry,
      revocation, Operator active state, and non-empty role set.
- [ ] Operator Access Token rotation uses the same bounded-overlap model as Principal
      Access Tokens, with a maximum 24-hour overlap.
- [ ] `token_admin` and `operator_admin` can manage Operator Access Tokens through the
      Module, while CLI bootstrap remains available for first production setup.
- [ ] The final active `operator_admin` cannot be disabled or stripped of
      `operator_admin`.
- [ ] Unit tests cover issue, authenticate, rotate, revoke, no-role denial, and
      last-admin protection.

## Comments

No comments yet.


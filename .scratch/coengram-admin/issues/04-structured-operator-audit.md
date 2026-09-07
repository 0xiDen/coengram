# 04. Structured Operator Audit Events

Status: ready-for-agent

Blocked by: 01. Control schema for Operator admin plane; 02. Operator auth and bootstrap
CLI.

## What to build

Add a structured Operator Audit Event writer and reader used by admin Modules and HTTP
routes.

## Acceptance criteria

- [ ] Every admin mutation records an Operator Audit Event with Operator ID, role
      context, action, target identifiers, request ID, timestamp, and safe before/after
      metadata where useful.
- [ ] Audit metadata excludes Operator Access Token secrets, Admin Session secrets,
      CSRF secrets, and raw Private Memory content.
- [ ] `audit_viewer` and `operator_admin` can list/filter Operator Audit Events.
- [ ] Slice 1 retains Operator Audit Events indefinitely.
- [ ] Tests prove audit creation for identity, token, session, provisioning, retry,
      cancellation, cleanup, and failed authorization paths where appropriate.

## Comments

No comments yet.

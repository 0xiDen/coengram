# 01. Authenticated Private Memory tracer

Status: resolved

Blocked by: None — can start immediately.

## What to build

Build the narrow first product path: an operator creates a Tenant, User, Membership, and
opaque tenant-bound Access Token; the User authenticates through typed HTTP and retains
and recalls only their own Private Memory through the Memory Module Interface. This
slice establishes the deep Module and its public result contract without exposing
storage identifiers or upstream Agent Memory tools.

## Acceptance criteria

- [x] Python 3.12 application code defines a typed Memory Module Interface for retaining
      and recalling Private Memory, with success, authorization, validation, conflict,
      pending, and dependency-unavailable results represented explicitly.
- [x] An operator CLI creates a Tenant, User, Tenant Membership, and tenant-bound opaque
      Access Token and displays the working token only once.
- [x] Access Tokens have high entropy and the Control Store retains only a lookup-safe
      identifier and non-reversible verifier, never the working credential.
- [x] Authentication derives the Tenant, Actor, roles, and private owner scope from the
      token; request bodies cannot select authoritative tenant, user, graph, or database
      identifiers.
- [x] A typed HTTP client can retain an explicit Memory Item and recall it in a later
      request using the same credential.
- [x] Selective retention accepts explicit retention and typed durable preferences,
      constraints, or outcomes, while rejecting wholesale transcript retention by
      default.
- [x] Recall distinguishes an empty result from an unavailable Tenant Memory Store and
      never silently falls back to another route.
- [x] Memory Items carry stable identifiers, timestamps, provenance, and confidence.
- [x] A second User in the same Tenant cannot inspect or recall the first User's Private
      Memory through success results, errors, identifiers, or counts.
- [x] Unit and contract tests exercise the Memory Module Interface and typed HTTP path
      without asserting private repository classes, raw tables, or Cypher statements.
- [x] Existing self-hosted Neo4j Agent Memory smoke behavior remains covered as a
      lower-level Adapter check.

## Comments

_No comments yet._

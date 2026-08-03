# 03. Principal scopes, Delegation, and RBAC

Status: resolved

Blocked by: 02. Declarative Tenant lifecycle and hard isolation.

## What to build

Complete the iteration-1 identity and authorization model for human Users, autonomous
Agents, and delegated Agents. Every credential resolves to one Tenant Session; roles
govern administration and curation, and Delegation grants one Agent access to exactly
one Subject User without hiding the Agent's identity as Actor.

## Acceptance criteria

- [x] Principals are explicitly typed as User or Agent and receive access only through
      Tenant Memberships.
- [x] A Principal may belong to several Tenants, but every Access Token binds exactly one
      Membership and creates exactly one active Tenant Session.
- [x] Human roles implement Tenant Administrator, Knowledge Curator, and Tenant Member;
      Agents have non-administrative permissions only.
- [x] An autonomous Agent can read and write its own Private Memory and read published
      Tenant Knowledge, but has no Subject User scope.
- [x] A delegated-Agent token binds one Agent, one Tenant, one Delegation, and one Subject
      User; request data cannot switch the Subject User.
- [x] A delegated Agent can access its own Private Memory, its bound Subject User's
      Private Memory, and published Tenant Knowledge, while user-derived data is never
      silently copied into the Agent's private scope.
- [x] Audit records identify the Agent as Actor and the User as Subject User for
      delegated operations.
- [x] User and delegated-Agent tokens default to 90 days, autonomous-Agent tokens to 30
      days, with tenant policy allowed to shorten but not exceed platform maxima.
- [x] Token rotation supports a deliberate overlap window; revocation is immediate;
      expiry, rotation, last-use, and unused-for-30-days signals are observable.
- [x] The operator CLI can create, list, update, and revoke Principals, Memberships,
      Delegations, roles, and tokens without exposing a public administration API.
- [x] Authorization tests cover every User, autonomous-Agent, delegated-Agent, role,
      expiry, rotation, revocation, and Actor/Subject User combination.
- [x] At least two Tenants and two Users prove non-observation through memory operations,
      errors, identifiers, counts, and degraded routing.

## Comments

_No comments yet._

# PRD: CoEngram Admin Plane and Panel

Status: planned

## Problem Statement

CoEngram already has a secure Tenant-scoped memory system, operator CLI, and host-side
provisioning path, but it lacks a browser admin panel and typed admin API for day-to-day
operation. Operators need to create Tenants, manage Principals, Tenant Memberships,
Delegations, token lifecycles, Knowledge Candidates, Tenant Knowledge, content-free
Private Memory metadata, and knowledge graph visibility without bypassing CoEngram's
privacy and tenancy contracts.

The current `mem1` Access Token model authenticates Principals into exactly one Tenant
Session. That is the correct shape for memory operations, but it is not enough for
deployment-wide administration such as creating Tenants, issuing Operator Access Tokens,
reviewing Operator Audit Events, or requesting Tenant provisioning. The admin panel must
therefore add an Operator admin plane without turning Tenant Administrators into global
superusers or exposing Private Memory content.

## Solution

Add a role-based Operator admin plane. An Operator is a deployment-wide administrative
identity, separate from a Tenant Principal. Operators authenticate with Operator Access
Tokens, then exchange them for short-lived browser Admin Sessions. Admin routes live
under `/api/v1/admin/*`, require role checks, require server-issued CSRF tokens for
mutating browser requests, and write structured Operator Audit Events.

The React admin frontend will be a Vite application using Chakra UI. It will be served
by Caddy under `/admin` in production and use a Vite dev proxy locally. The UI style is
a hybrid ops console: dense tables and filters for everyday administration, guided
wizards for risky workflows such as Tenant provisioning and User creation.

Tenant creation will remain manifest-driven and resumable. The browser-facing gateway
will create Provisioning Jobs in the Control Store; a separate Operator Service will
poll and claim those jobs, perform host-side work, and update job status. This preserves
the existing rule that the shared gateway does not receive direct Docker or host mutation
authority. Failed or canceled never-active Tenants may be cleaned up through an explicit
two-step Provisioning Cleanup flow.

Admin visibility into Private Memory is content-free. Operators may see metadata,
counts, lifecycle state, timestamps, and aggregate visualizations, but raw Private
Memory content remains private. Support mode is modeled as an audited view of Tenant
Member metadata, roles, tokens, Delegations, and content-free Private Memory metadata,
not literal impersonation.

## Agreed Decisions

- Admin authority is hybrid: Operators handle deployment-wide actions; Tenant
  Administrator and Knowledge Curator roles continue to govern tenant-scoped memory
  actions.
- Operators are separate from Principals.
- Operator Access Tokens are one-time-revealed, revocable, expiring credentials stored
  in the Control Store as non-reversible verifiers.
- Admin Sessions use httpOnly cookies, expire after 30 minutes idle or 8 hours absolute,
  and require CSRF tokens for mutating admin requests.
- Initial Operator Roles are `operator_admin`, `identity_admin`, `tenant_provisioner`,
  `tenant_support`, `knowledge_admin`, `token_admin`, and `audit_viewer`.
- `operator_admin` manages Operators and Operator Roles, with last-admin lockout
  prevention.
- `token_admin` and `operator_admin` can manage Operator Access Tokens.
- `token_admin`, `identity_admin`, and `operator_admin` can issue, rotate, and revoke
  Principal Access Tokens; `tenant_support` can inspect token metadata only.
- Principal Access Token defaults stay as they are for slice 1: User tokens up to 90
  days, autonomous Agent tokens up to 30 days, delegated Agent tokens up to 90 days.
- Future policy work should add Tenant-configurable token expiry presets, configurable
  unused-token warning thresholds, and configurable Operator Audit Event retention.
- Provisioning is plan/apply/status through async Provisioning Jobs, executed by an
  Operator Service using Control Store polling and claiming.
- Repeated provisioning requests use both client idempotency keys and Tenant Manifest
  fingerprint uniqueness.
- Failed Provisioning Jobs can be retried on the same job.
- Provisioning Jobs support cooperative cancellation between steps.
- Provisioning Cleanup is available for never-active Tenants only and requires a
  two-step UI confirmation.
- The first slice excludes support mode, knowledge admin, and graph UI to keep the
  vertical slice testable.

## Slice 1 Scope

Slice 1 delivers the first vertical admin experience:

- Operator schema, Operator Access Tokens, Operator Role enforcement, and bootstrap CLI.
- Admin Session and CSRF flow.
- Structured Operator Audit Events.
- Admin APIs for dashboard counts/action queue, Tenants, Operators, Principals, Tenant
  Memberships, Principal tokens, Operator tokens, Tenant Manifest planning, Provisioning
  Jobs, retries, cancellation, and never-active cleanup.
- Operator Service polling and host execution path for Provisioning Jobs.
- Vite React Chakra UI at `/admin` with Dashboard, Tenants, Identity, Tokens, Operators,
  Provisioning Jobs, and Audit pages.
- Local unit/integration tests plus final full-stack smoke on `alex@10.0.0.11`.

## Later Slices

Slice 2 should add Delegations/support mode, including content-free Private Memory
metadata and delegated token management.

Slice 3 should add Knowledge Candidate full lifecycle controls, Knowledge Candidate
Revisions, Tenant Knowledge Revision, Tenant Knowledge Deprecation, retry of failed
publication, and archive/hide flows.

Slice 4 should add hybrid knowledge graph visualization using Tenant Operations Store
state for governance/private metadata and Neo4j projection data for published Tenant
Knowledge graph shape.

## Out of Scope for Slice 1

- Literal User impersonation.
- Admin access to raw Private Memory content.
- OIDC/SSO.
- Observability dashboards embedded in the admin panel.
- Cleanup for active Tenants; active Tenant removal stays with the existing
  decommissioning model.

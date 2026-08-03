# Tickets: CoEngram — Iteration 1

These tracer-bullet tickets build the product specified in [PRD.md](PRD.md). Work the
frontier: a ticket is ready to implement when every ticket named in its **Blocked by**
field is complete.

Iteration 1 status: all tickets resolved and covered by the provider-neutral release gate.

## 01. Authenticated Private Memory tracer

**What to build:** An operator can create a User and tenant-bound Access Token, and the
User can authenticate through typed HTTP to retain and recall only their own Private
Memory through the core Memory Module Interface.

**Blocked by:** None — can start immediately.

## 02. Declarative Tenant lifecycle and hard isolation

**What to build:** An operator can declaratively provision and migrate isolated Tenants,
each with its own Neo4j Community instance and PostgreSQL database, and activate a
Tenant only after routing and isolation checks pass.

**Blocked by:** 01. Authenticated Private Memory tracer.

## 03. Principal scopes, Delegation, and RBAC

**What to build:** Users, autonomous Agents, and delegated Agents receive server-derived
Tenant Sessions with correct private-memory scopes, human roles, auditable Actor and
Subject User attribution, and revocable credentials.

**Blocked by:** 02. Declarative Tenant lifecycle and hard isolation.

## 04. Reviewed knowledge publication

**What to build:** A Principal can propose a privacy-safe Knowledge Candidate, a human
Curator can review it, and approved Tenant Knowledge becomes recallable only after
reliable outbox, RabbitMQ, and idempotent Neo4j publication.

**Blocked by:** 03. Principal scopes, Delegation, and RBAC.

## 05. Correction and approved erasure

**What to build:** A User can supersede an owned Memory Item and request its erasure;
only a separate Tenant Administrator approval removes content while leaving a
content-free audit tombstone.

**Blocked by:** 04. Reviewed knowledge publication.

## 06. Portable memory archives

**What to build:** Principals and Tenant Administrators can export and import
checksummed, versioned Memory Archives without leaking private content or bypassing
review for shared knowledge.

**Blocked by:** 05. Correction and approved erasure.

## 07. MCP and Claude Code integration

**What to build:** Claude Code can use authenticated streamable HTTP MCP tools whose
intent-oriented behavior and authorization match typed HTTP without exposing storage
selectors, raw Cypher, or upstream Agent Memory tools.

**Blocked by:** 03. Principal scopes, Delegation, and RBAC; 04. Reviewed knowledge
publication; 05. Correction and approved erasure.

## 08. ActiveGraph runtime compatibility tracer

**What to build:** Agent developers can construct a tenant-scoped ActiveGraph Runtime
through one reusable Module, persist and replay deterministic Agent Run events, enforce
budgets, and load a versioned Pack against a proven pinned dependency.

**Blocked by:** 03. Principal scopes, Delegation, and RBAC.

## 09. Knowledge Synthesis Agent

**What to build:** A User can start, inspect, and cancel a budgeted Knowledge Synthesis
Agent Run that uses explicitly selected Private Memory, detects duplicate or conflicting
Tenant Knowledge, and submits—but never approves—a Knowledge Candidate.

**Blocked by:** 04. Reviewed knowledge publication; 08. ActiveGraph runtime
compatibility tracer.

## 10. Telegram invocation Adapter

**What to build:** An operator-bound Telegram direct-message User can safely invoke
explicit memory and Agent commands through the same canonical Interfaces, while
unauthenticated, group, or free-form messages cannot mutate memory.

**Blocked by:** 07. MCP and Claude Code integration; 09. Knowledge Synthesis Agent.

## 11. Production ingress and Compose topology

**What to build:** The platform can run on one production server behind Cloudflare-aware
Caddy at a hostname supplied through a protected secret file, with shared and per-Tenant Compose projects,
file-backed secrets, and no public infrastructure ports.

**Blocked by:** 02. Declarative Tenant lifecycle and hard isolation; 07. MCP and Claude
Code integration.

## 12. Central observability and shadow limits

**What to build:** Operators can diagnose ingress, memory, queue, graph, database, and
Agent Run behavior through Alloy, Mimir, Loki, Tempo, and Grafana while telemetry stays
content-free and workload limits remain informative rather than rejecting requests.

**Blocked by:** 04. Reviewed knowledge publication; 08. ActiveGraph runtime
compatibility tracer; 11. Production ingress and Compose topology.

## 13. Encrypted backup and verified restore

**What to build:** Operators can create encrypted nightly PostgreSQL and per-Tenant
Neo4j backups, apply retention, restore a Tenant into isolated targets, and verify the
accepted recovery objectives through public Interfaces.

**Blocked by:** 02. Declarative Tenant lifecycle and hard isolation; 04. Reviewed
knowledge publication; 08. ActiveGraph runtime compatibility tracer.

## 14. Safe Tenant decommissioning

**What to build:** Operators can suspend and eventually remove a Tenant only through a
two-step, grace-period-protected workflow that verifies backup or export and leaves a
content-free tombstone.

**Blocked by:** 02. Declarative Tenant lifecycle and hard isolation; 13. Encrypted
backup and verified restore.

## 15. Release tracer and deployment handoff

**What to build:** Maintainers have one provider-neutral release command that proves all
iteration-1 behavior—including same-Tenant sharing and cross-Tenant non-observation—and
a production deployment runbook ready for operator-supplied server details and secrets.

**Blocked by:** 06. Portable memory archives; 09. Knowledge Synthesis Agent; 10.
Telegram invocation Adapter; 11. Production ingress and Compose topology; 12. Central
observability and shadow limits; 13. Encrypted backup and verified restore; 14. Safe
Tenant decommissioning.

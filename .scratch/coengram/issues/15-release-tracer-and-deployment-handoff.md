# 15. Release tracer and deployment handoff

Status: resolved

Blocked by: 06. Portable memory archives; 09. Knowledge Synthesis Agent; 10. Telegram
invocation Adapter; 11. Production ingress and Compose topology; 12. Central
observability and shadow limits; 13. Encrypted backup and verified restore; 14. Safe
Tenant decommissioning.

## What to build

Turn every iteration-1 promise into one provider-neutral release gate and deployment
handoff. The end-to-end tracer proves private retention, reviewed same-Tenant sharing,
Agent synthesis, Telegram and Claude-compatible invocation, durable publication,
observability, portability, recovery, and other-Tenant non-observation. The runbook then
guides an operator from validated artifacts to the first server deployment without
inventing SSH access or production secrets.

## Acceptance criteria

- [x] One provider-neutral `make verify` command runs formatting/linting, static typing,
      unit tests, contract tests, and required container-backed integration suites from
      a developer host or generic CI runner.
- [x] The release gate runs Memory Module contracts, authorization/delegation/RBAC,
      two-Tenant isolation, every state machine, transport parity, ActiveGraph replay,
      LangChain construction, Telegram, Caddy, observability leakage, migration,
      archive, backup, and restore tests.
- [x] The end-to-end tracer creates Alice, Bob, a Curator, an Agent, and a second Tenant;
      each receives only explicitly authorized Memberships, roles, Delegations, and
      credentials.
- [x] Alice retains Private Memory, invokes Knowledge Synthesis with selected evidence,
      and receives a durable Agent Run whose candidate remains unpublished initially.
- [x] A human Curator approves the candidate; PostgreSQL/outbox/RabbitMQ/worker
      publication completes; Bob and the Agent then recall the resulting Tenant
      Knowledge while Alice's source remains private.
- [x] The second Tenant cannot observe the memory, candidate, review, knowledge, Agent
      Run, identifiers, counts, errors, timing-insensitive result shapes, exports, or
      degraded routes from the first Tenant.
- [x] Fault injection stops PostgreSQL, RabbitMQ, and individual Tenant Neo4j services
      at key boundaries and proves fail-closed routing, explicit unavailability,
      pending operations, recovery, idempotency, and no duplicate publication.
- [x] The tracer exercises authenticated typed HTTP, MCP/Claude-compatible
      configuration, and Telegram commands without requiring live Claude, Anthropic, or
      Telegram network calls.
- [x] Release output identifies every suite, duration, skip, failure, and required
      operator-only smoke check; mandatory gates cannot be silently skipped.
- [x] A deployment runbook validates host prerequisites, protected secret files,
      Compose rendering, custom Caddy modules, Cloudflare-proxied DNS prerequisite,
      migrations, Tenant apply, ingress authentication, dashboards, backup, and restore.
- [x] The runbook clearly separates repository actions from operator-owned DNS record
      management, firewalling, disk encryption, SSH provisioning, and remote backup
      storage.
- [x] A deployment checklist requests target SSH details, explicit host paths, and
      operator-created secrets only when actual server deployment begins and never
      extracts or invents credentials.
- [x] Product and operator documentation matches the canonical domain language and all
      accepted ADRs, includes degraded-mode behavior, and makes Community-only and
      single-server/no-HA boundaries explicit.

## Comments

_No comments yet._

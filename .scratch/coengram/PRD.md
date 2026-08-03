# PRD: CoEngram — Iteration 1

Status: implemented

## Problem Statement

Engineering teams need durable memory that their human users, Claude instances, and
workflow agents can use without sending that memory to a hosted memory provider. The
current repository proves that Neo4j Agent Memory can run against a local Neo4j
Community instance, but it has no application authentication, no separation between
users or engineering teams, no reviewed path from private experience to shared
knowledge, no durable agent runtime, and no production ingress or operational stack.

Exposing the current MCP service would let callers choose storage-level identifiers and
would publish direct Neo4j and Agent Memory operations without enforcing the intended
security model. A user could lose context, an Agent could be confused with the User it
represents, and information could be recalled outside the engineering team that owns it.
The system also lacks a consistent way to survive a PostgreSQL, RabbitMQ, or Neo4j
failure while publishing knowledge across stores.

The first production iteration must therefore make memory private-by-default,
tenant-isolated, authenticated, reviewable, portable, auditable, observable, and usable
through Claude Code, typed service integrations, ActiveGraph Packs, LangChain, an
operator CLI, and a command-oriented Telegram bot. It must remain entirely self-hosted
and use only Neo4j Community capabilities.

## Solution

Build a self-hosted CoEngram deployment behind a hostname supplied through a protected
secret file. Caddy terminates TLS using its Cloudflare DNS plugin and
proxies only to an authenticated gateway; the gateway derives the Principal, Tenant,
roles, optional Delegation, and Subject User from an opaque Bearer Access Token. Callers
never provide authoritative tenant, user, graph, or database selectors.

The central Memory Module is a deep Module. Its Interface expresses memory intentions:
recall, retain, propose knowledge, review knowledge, correct memory, request or review
erasure, import, and export. Its Implementation owns authorization, scope selection,
provenance, state transitions, audit, and cross-store consistency. MCP and typed HTTP
are transport Adapters over the same Interface, providing high Leverage from one
security Seam while keeping storage details local to the Module.

Each Tenant owns one isolated Neo4j Community container, credentials, and persistent
volume. A shared PostgreSQL instance has a content-free control database and one
database with a distinct role per Tenant. PostgreSQL is authoritative for commands,
governance, audit, ActiveGraph events, and the transactional outbox. RabbitMQ carries
durable outbox notifications; an idempotent worker applies committed changes to the
correct Tenant Memory Store. Recall only observes fully published state.

Private Memory can belong to a User or Agent. A delegated Agent remains the Actor but
may use its single bound Subject User's Private Memory, its own Private Memory, and
published Tenant Knowledge. User-derived content is never silently copied into the
Agent's scope. Users and Agents may produce Knowledge Candidates, but only a human
Knowledge Curator can approve Promotion. Reviewers see the distilled candidate and safe
provenance—not the source Private Memory.

Each fenced Agent Run transition first commits to an application-owned event snapshot
in the Tenant Operations Store, then projects into ActiveGraph's native durable event
stream. A valid missing ActiveGraph suffix is repaired idempotently; divergence fails
closed. ActiveGraph owns native Pack execution and replay over that projection. A shared
Agent Runtime Module loads versioned, native ActiveGraph Packs. The first Pack is the
Knowledge Synthesis Agent: it accepts explicitly selected Private Memory, recalls Tenant
Knowledge, detects duplication or conflicts, and submits a Knowledge Candidate without
approving it. LangChain provides a deterministic fake model Adapter for tests and an
Anthropic Adapter for production. The exact default model is `claude-sonnet-5` and is
recorded on every run.

Claude Code uses authenticated streamable HTTP MCP. Typed HTTP, MCP, CLI, and Telegram
are Adapters into canonical memory commands and Agent Invocations. Telegram direct
messages use operator-created Channel Bindings and command-oriented interactions;
external usernames, headers, or message text never select identity or Tenant.

The system ships as Docker Compose definitions for a full local instance, shared
production services, and one parameterized Tenant stack per Tenant. It includes a
custom Caddy build, file-backed Compose secrets, resumable declarative provisioning,
migrations, encrypted backup workflows, and centralized Grafana Alloy, Mimir, Loki,
Tempo, and Grafana observability. DNS record management, Cloudflare proxy configuration,
host firewalling, host disk encryption, and remote backup-storage provisioning remain
operator prerequisites outside this repository.

## User Stories

1. As a User, I want to authenticate with an opaque Access Token, so that a memory client cannot impersonate another Principal.
2. As a User with several Tenant Memberships, I want each credential bound to one active Tenant, so that accidental cross-team recall is impossible.
3. As a User, I want to retain an explicit memory privately, so that it is available in a later session.
4. As a User, I want durable preferences, constraints, and outcomes retained selectively, so that useful continuity does not require storing every transcript.
5. As a User, I want recall to combine my Private Memory with published Tenant Knowledge, so that answers reflect both personal and engineering-team context.
6. As a User, I want an unavailable memory service reported explicitly, so that absence of results is never confused with an infrastructure failure.
7. As a User, I want to inspect the memory items I own, so that I understand what the system will recall.
8. As a User, I want to correct an owned Memory Item by superseding it, so that history remains auditable without continuing to recall stale content.
9. As a User, I want to export my Private Memory as a portable archive, so that I am not locked into one deployment.
10. As a User, I want to import a Private Memory archive into my private scope, so that moving data never makes it shared accidentally.
11. As a User, I want to request erasure of an owned Memory Item, so that sensitive or incorrect content can enter a controlled removal workflow.
12. As a User, I want approved erasure to remove content from storage and recall, so that the system honors the decision while keeping a content-free audit tombstone.
13. As a User, I want to select Private Memory as evidence for a Knowledge Candidate, so that durable team lessons can be proposed deliberately.
14. As a User, I want my source Private Memory to stay private after Promotion, so that sharing a distilled lesson does not expose my original context.
15. As a User, I want candidate reviewers to see safe provenance rather than my private source, so that review does not bypass privacy boundaries.
16. As a Tenant Member, I want to read published Tenant Knowledge, so that reviewed engineering knowledge is available to everyone in my team.
17. As a Tenant Member, I want unpublished or rejected candidates excluded from ordinary recall, so that untrusted drafts do not become facts.
18. As a Knowledge Curator, I want to inspect, approve, or reject Knowledge Candidates, so that shared knowledge is intentional and trustworthy.
19. As a Knowledge Curator, I want duplicate and conflict information alongside a candidate, so that I can avoid contradictory shared knowledge.
20. As a one-human Tenant Administrator, I want to self-approve a candidate, so that a small Tenant can operate without inventing a second person.
21. As a Tenant Administrator, I want to manage Principals, Memberships, roles, Delegations, and Access Tokens, so that access remains explicit and revocable.
22. As a Tenant Administrator, I want to approve or reject Erasure Requests, so that iteration-1 removal has the required higher approval.
23. As a Tenant Administrator, I want to export Tenant Knowledge and governance history without private memories, so that Tenant data is portable without leaking personal context.
24. As a Tenant Administrator, I want imported shared knowledge to become reviewable candidates, so that an archive cannot bypass Promotion.
25. As a Tenant Administrator, I want configurable token lifetimes within platform maxima, so that operational policy can be stricter without weakening the system.
26. As a Tenant Administrator, I want unused-token and quota signals, so that I can understand workload before enforcing limits.
27. As an operator, I want an idempotent Tenant Manifest apply command with a plan, so that provisioning is declarative and reviewable.
28. As an operator, I want failed Tenant provisioning to resume safely, so that partial infrastructure work does not require destructive rollback.
29. As an operator, I want a Tenant activated only after database, graph, schema, routing, and isolation checks pass, so that incomplete Tenants cannot serve traffic.
30. As an operator, I want secret-free Tenant Manifests, so that configuration can be versioned safely.
31. As an operator, I want generated high-entropy credentials stored in protected files, so that Compose can mount secrets without putting them in environment files or source control.
32. As an operator, I want tokens displayed only at issuance or rotation time and stored as non-reversible verifiers, so that control data cannot reveal working credentials.
33. As an operator, I want overlapping token rotation and immediate revocation, so that clients can migrate without a service interruption while compromised tokens stop immediately.
34. As an operator, I want migration plans and resumable migration application, so that PostgreSQL and Neo4j schema changes are controlled across Tenants.
35. As an operator, I want incompatible application startup to fail on schema mismatch, so that old code cannot silently corrupt new state.
36. As an operator, I want nightly encrypted PostgreSQL and per-Tenant Neo4j backups, so that the accepted 24-hour recovery-point objective is achievable.
37. As an operator, I want quarterly restore drills, so that the accepted four-hour per-Tenant recovery objective is demonstrated rather than assumed.
38. As an operator, I want Tenant decommissioning to suspend access, verify export or backup, require a second explicit confirmation, and wait through a grace period, so that data is not destroyed accidentally.
39. As an operator, I want only a content-free tombstone after final decommissioning, so that removal can be audited without retaining domain content.
40. As an operator, I want quota thresholds logged and measured without returning `429` in iteration 1, so that policy can be based on observed workloads.
41. As an operator, I want Caddy to obtain and renew the public certificate with a Cloudflare API token, so that the MCP endpoint remains encrypted without exposing the server address through an unproxied record.
42. As an operator, I want no public Neo4j Bolt, Neo4j Browser, PostgreSQL, RabbitMQ, or observability ports, so that only intended ingress is externally reachable.
43. As an operator, I want metrics, logs, and traces correlated by opaque identifiers, so that incidents can be diagnosed across all shared and Tenant services.
44. As an operator, I want prompts, memory content, model input or output, Access Tokens, and credentials excluded from telemetry, so that observability does not become a second memory store.
45. As an operator, I want provisioned Grafana data sources and dashboards, so that authentication, Agent Runs, queues, workers, databases, Caddy, and shadow quotas share one operational view.
46. As an Agent, I want my own authenticated Principal and Private Memory, so that I can retain reusable operating lessons without pretending to be a User.
47. As an autonomous Agent, I want Tenant Knowledge and my own Private Memory without any Subject User access, so that I can operate safely without delegation.
48. As a delegated Agent, I want access to exactly one Subject User per Access Token, so that a request cannot switch the User whose memory I can read.
49. As a delegated Agent, I want audit events to identify both me as Actor and the User as Subject User, so that actions remain attributable.
50. As an Agent, I want to propose but never approve Tenant Knowledge, so that automation cannot promote its own output into trusted shared context.
51. As a Claude Code user, I want a checked example MCP configuration using a Bearer token from an environment variable, so that I can connect without embedding a secret in project configuration.
52. As a Claude Code user, I want small intent-oriented memory tools, so that I cannot accidentally run raw Cypher or bypass governance.
53. As a service developer, I want a typed HTTP Interface with the same behavior as MCP, so that ActiveGraph and other code integrations do not depend on tool-discovery semantics.
54. As a service developer, I want idempotency keys on state-changing commands and Agent Invocations, so that retries do not duplicate memory or runs.
55. As an agent developer, I want a reusable Agent Runtime Module, so that new ActiveGraph Packs inherit durable stores, budgets, model recording, tools, telemetry, and replay policy through one construction seam.
56. As an agent developer, I want a deterministic recorded model provider, so that Agent Runs can be tested and replayed without live billing or network nondeterminism.
57. As an agent developer, I want an Anthropic LangChain Adapter with a pinned exact model, so that production synthesis is reproducible and auditable.
58. As a User, I want to invoke the Knowledge Synthesis Agent with explicitly selected memories, so that it cannot silently mine my entire private history.
59. As a User, I want an Agent Run identifier and status, so that asynchronous synthesis is observable and resumable.
60. As a User, I want to cancel a running synthesis, so that I retain control of model and tool work.
61. As a User, I want a budget-exhausted Agent Run to stop with a recorded resumable failure, so that spending and runaway behavior are bounded.
62. As an ActiveGraph operator, I want every run limited to three model calls, ten tool calls, one hundred events, two minutes, and USD 0.25 by default, so that iteration-1 agents have an explicit safety envelope.
63. As an ActiveGraph operator, I want run events retained in the Tenant Operations Store, so that runs can be inspected, replayed, and audited independently of memory.
64. As an ActiveGraph operator, I want model ID and settings recorded per run, so that results can be attributed to an exact execution configuration.
65. As a Telegram User, I want an operator to bind my numeric Telegram identity to one User, Tenant Membership, and Agent Delegation, so that the bot knows my authorized context without trusting a username.
66. As a Telegram User, I want direct-message commands for identity, recall, retention, proposal, synthesis, run status, and cancellation, so that chat interaction remains explicit.
67. As a Telegram User, I want ordinary text to receive guidance rather than being silently stored or executed, so that casual messages do not mutate memory.
68. As a Telegram operator, I want webhook secret validation and direct-message-only processing, so that spoofed or group traffic cannot invoke the bot.
69. As a system maintainer, I want PostgreSQL to commit commands, audit, and outbox records atomically, so that RabbitMQ or Neo4j outages cannot lose accepted work.
70. As a system maintainer, I want RabbitMQ delivery acknowledged only after idempotent Neo4j application, so that redelivery is safe.
71. As a system maintainer, I want failed messages visible in a dead-letter queue and retryable from authoritative PostgreSQL state, so that poison events do not disappear.
72. As a system maintainer, I want Promotion state to move through draft, review, publishing, and published states, so that recall never exposes partially applied shared knowledge.
73. As a system maintainer, I want tenant routing to fail closed, so that missing or unhealthy routing can never fall back to a different Tenant Memory Store.
74. As a system maintainer, I want one provider-neutral verification command, so that every release gate can run locally or in any future CI system.
75. As a system maintainer, I want an end-to-end tracer proving same-Tenant sharing and other-Tenant non-observation, so that the primary security promise is executable.

## Implementation Decisions

- The system uses Python 3.12 for application Modules and a custom Caddy image built with Go and the Cloudflare DNS provider plugin.
- Neo4j Agent Memory remains an internal Adapter over self-hosted Bolt. No caller receives direct Bolt credentials, raw Cypher, graph export, or the upstream MCP tool surface.
- Each Tenant receives one Neo4j Community instance, one credential set, and one volume. This is the hard tenant-isolation boundary required because Community Edition supplies one standard database per instance.
- One shared PostgreSQL instance hosts `memory_control` and one immutable-ID-named database per Tenant. Each Tenant database has a distinct role and separates memory governance from ActiveGraph events by schema.
- The Control Store contains identities, Tenants, Memberships, Delegations, Access Token verifiers, routing, provisioning state, and content-free operator audit. It contains no memory, candidate, prompt, or Agent Run content.
- Opaque Access Tokens contain sufficient random entropy, are bound to one Membership, and are stored only as non-reversible verifiers with a lookup-safe identifier. User and delegated-Agent tokens default to 90 days; autonomous-Agent tokens default to 30 days. Rotation permits an overlap window and revocation is immediate.
- Request authorization produces a Tenant Session. All authoritative Tenant, Actor, role, Subject User, database, and Neo4j routing values come from that session, never from request-selected identifiers.
- The Memory Module is the primary Interface and test Seam. It exposes typed recall, retention, correction, candidate, review, erasure, import, and export commands. Its result types distinguish success, accepted/pending work, authorization denial, validation failure, conflict, and explicit dependency unavailability.
- MCP exposes `memory_recall`, `memory_retain`, `knowledge_propose`, `knowledge_review`, `memory_correct`, and `memory_request_erasure`, plus the Agent Invocation tools `knowledge_synthesize`, `agent_run_status`, and `agent_run_cancel`.
- Typed HTTP represents the same operations as versioned schemas and additionally supports the generalized `start_agent_run(capability, input)` contract. Only registered capability schemas are callable; clients cannot supply arbitrary Pack names.
- State-changing HTTP, MCP, CLI, and messaging commands accept or derive idempotency keys. Repeated commands return the original operation or Agent Run rather than creating a duplicate.
- Private Memory ownership is a Principal identifier within a Tenant Memory Store. A delegated Agent can read its own private scope, its bound Subject User's private scope, and published Tenant Knowledge; autonomous Agents have no Subject User scope.
- Private retention is selective. Explicit retention is always eligible; automatic retention is restricted to durable preferences, constraints, and outcomes. Raw transcripts are not retained wholesale by default.
- A correction creates a superseding Memory Item. Superseded content remains auditable but is excluded from normal recall.
- Erasure requires a request and a separate Tenant Administrator decision. Approved execution removes domain content from PostgreSQL and Neo4j and leaves only a content-free tombstone.
- A Knowledge Candidate contains a distilled claim, confidence, safe provenance references, proposer, and duplicate/conflict information. It never grants a reviewer access to the source Private Memory.
- Promotion is human-only. A Tenant Administrator can self-approve only when the Tenant has exactly one human member. Agents can propose but cannot review or administer.
- PostgreSQL is authoritative for state-changing memory commands, Promotion state, governance, audit, and an outbox record committed in the same transaction.
- RabbitMQ is the only iteration-1 broker. The outbox relay publishes durable messages; a worker acknowledges only after idempotently applying the event to the routed Tenant Memory Store. Failed delivery retries and eventually enters a dead-letter queue without losing PostgreSQL truth.
- Shared knowledge becomes recallable only in `published` state after successful graph application. Failures remain visible and retryable. Recall never treats an unavailable graph as an empty result and never falls back across Tenants.
- A Memory Archive is versioned JSON Lines with stable identifiers, provenance, supersedence, and checksums. Principal exports contain only owned Private Memory. Tenant exports contain published Tenant Knowledge and governance data but no Principal Private Memory. Shared imports become Knowledge Candidates.
- The application-owned Tenant snapshot is the fenced write-ahead authority for accepted Agent Run transitions, leases, and recovery. ActiveGraph receives a native durable projection used for Pack execution and replay; a valid missing suffix is repaired from the snapshot, while divergence fails closed. Neo4j serves the idempotent recall projection of PostgreSQL-authoritative memory state. LangChain owns model/tool integration but no durable workflow state.
- The Agent Runtime Module constructs ActiveGraph Runtime instances with the Tenant event store, Frame, Budget, exact model provider, registered tools, metrics, replay policy, and loaded Packs.
- Agent capabilities are versioned ActiveGraph Packs. The `knowledge_synthesis` Pack accepts an explicit request and selected Private Memory references, recalls Tenant Knowledge, detects duplication and conflict, produces a candidate, and never approves it. Reusable Agent lessons may enter the Agent's own Private Memory, but User-derived content does not.
- Agent Invocation is channel-neutral and asynchronous. Starting returns an Agent Run identifier; status and cancellation are separate operations. A reply context lets channel Adapters deliver the eventual outcome without entering the core behavior.
- The deterministic fake or recorded provider is the default in tests. The production LangChain Adapter calls Anthropic using a file-backed secret. `claude-sonnet-5` is the default exact model ID, and the model ID and all material settings are recorded per Agent Run.
- Default per-run budgets are three model calls, ten tool calls, one hundred events, two minutes, and USD 0.25. Exhaustion records a budget event and leaves a resumable failed run.
- Claude Code is the first verified MCP client. Its example uses streamable HTTP at `/mcp` and expands a Bearer token from `MEMORY_MCP_TOKEN` without checking secrets into source control.
- Telegram is an iteration-1 Adapter. It accepts direct messages only, validates Telegram's webhook secret, resolves numeric sender identity through an operator-managed Channel Binding, and supports `/whoami`, `/recall`, `/remember`, `/propose`, `/synthesize`, `/status`, and `/cancel`. `/synthesize` starts the channel-neutral Knowledge Synthesis Agent with explicit selected-memory references and persists only channel plus numeric destination as reply context. Starting returns the Agent Run identifier immediately; status and cancellation remain separate. Ordinary text returns help and performs no mutation.
- The root Compose model remains the full local and one-Tenant integration environment. Production uses one shared Compose model for Caddy, gateway, workers, PostgreSQL, RabbitMQ, Alloy, Mimir, Loki, Tempo, and Grafana, plus one parameterized Compose project for every Tenant Neo4j instance.
- Tenant containers join a private shared network under unique generated service addresses. Neo4j, PostgreSQL, RabbitMQ, and observability endpoints publish no public host ports in production.
- Caddy serves the secret-supplied hostname, obtains certificates with the Cloudflare DNS provider and a file-backed API token, emits JSON access logs and metrics, and proxies to the gateway. Cloudflare record creation and proxy mode are outside the repository.
- `coengramctl tenant apply` validates a secret-free Tenant Manifest, shows a plan, records provisioning state, generates protected secrets, invokes host-side Compose, creates the Tenant database and role, applies PostgreSQL and Neo4j migrations, verifies routing and isolation, and activates only on success. It resumes failed work and never mounts the Docker socket into an application container.
- Alembic manages the control database and every Tenant database. Versioned, idempotent graph migrations manage Neo4j indexes and constraints. Planning, backup checks, apply, resume, and schema-compatibility checks are available through the operator CLI.
- File-backed Compose secrets are used for generated database and token material and operator-supplied Cloudflare, Anthropic, Telegram, and backup credentials. Environment files contain non-secret settings only. Secret loading is behind an Adapter suitable for later Vault or SOPS integration.
- Tenant decommissioning first suspends access and revokes credentials, verifies backup or export requirements, records a request, requires a separate operator confirmation containing the immutable Tenant ID, waits 30 days, and then removes the Tenant Compose project, graph volume, and PostgreSQL database. Request and destructive completion cannot be one command.
- Backups run nightly. PostgreSQL dumps and stopped-per-Tenant Neo4j Community dumps are encrypted, retain seven daily and four weekly generations, and target a 24-hour recovery-point and four-hour per-Tenant recovery-time objective. Restore drills run quarterly.
- Alloy collects telemetry from Caddy and all application and infrastructure services. Mimir retains metrics for 30 days, Loki logs for 14 days, and Tempo traces for seven days. Grafana is operator-only and uses provisioned data sources and dashboards.
- Telemetry includes only opaque Tenant, Principal, run, operation, and correlation identifiers. It excludes Access Tokens, prompts, Memory Item content, Knowledge Candidate content, model input and output, Telegram text, and database credentials. Detailed Agent Run audit stays inside the Tenant event store; traces keep only shape and timing.
- Shadow limits are 120 requests per minute per token, 600 per Tenant, ten concurrent embedding or search operations, and two concurrent synthesis runs. Iteration 1 records utilization and would-have-limited events but never returns `429`.
- Domain data is retained indefinitely until approved erasure or Tenant decommissioning. Superseded items, candidate reviews, governance audit, and ActiveGraph events remain; only operational telemetry uses time-based deletion in iteration 1.
- The release command is provider-neutral and runs on a developer host or future CI runner without embedding a specific CI vendor configuration.

## Testing Decisions

- Tests verify externally visible behavior through the Memory Module Interface, transport Adapters, `coengramctl`, Agent Invocation Interface, and complete Compose ingress. They do not mock or assert private repository classes, raw tables, Cypher statements, queue implementation calls, or framework internals.
- The Memory Module contract suite is the highest and primary Seam. Every storage Adapter must pass the same recall, retain, supersede, candidate, review, erasure, import, and export examples.
- Authorization contract tests prove User, autonomous Agent, and delegated Agent scopes; role permissions; token expiry, rotation, and revocation; server-derived Tenant selection; and Actor versus Subject User audit attribution.
- Isolation tests use at least two Tenants and two Users. They assert non-observation through the public Interface, including error bodies, identifiers, counts, timing-insensitive result shapes, imports, exports, Agent Runs, and degraded routing.
- State-machine tests cover Promotion, correction, Erasure Request, provisioning, migrations, and decommissioning, including retries and forbidden transitions.
- Outbox integration tests use real PostgreSQL and RabbitMQ containers. They prove atomic command/outbox commit, broker outage recovery, duplicate publication, worker crash before acknowledgement, idempotent graph application, retry, dead-letter behavior, and `published` visibility.
- Neo4j integration tests use separate real Community instances to prove correct Tenant routing, idempotent writes, superseded-item filtering, published-only knowledge, explicit unavailability, and no fallback.
- Transport parity tests run the same behavior examples through typed HTTP and MCP, including authentication failures and redacted error handling.
- Claude compatibility tests validate the checked MCP configuration and exercise authenticated streamable HTTP using a test token; no live Claude session is required for the default suite.
- ActiveGraph Pack tests load `knowledge_synthesis` into a fresh Runtime with no global side effects, use recorded model and tool fixtures, assert typed events and budgets through public runtime inspection, and prove deterministic replay.
- LangChain contract tests validate Anthropic request construction, exact model recording, structured response parsing, failure mapping, and budget accounting without live model billing. A separately enabled smoke check may use a real operator-supplied key.
- Telegram tests validate webhook authentication, numeric identity binding, direct-message restriction, command parsing, idempotency, reply context, and absence of mutation for ordinary text without calling Telegram's live API.
- Caddy tests validate the built module list and rendered configuration, and black-box ingress tests prove the public hostname path requires authentication while internal services are not routed publicly.
- Manifest and Memory Archive round-trip tests use canonical fixtures with fixed checksums. They prove tokens and secrets are absent, private content cannot enter Tenant exports, and imported shared knowledge requires review.
- Backup and restore tests create representative Tenant data in both stores, perform the supported backup workflow, restore into isolated targets, and verify it only through public recall, audit, and Agent Run interfaces.
- The release tracer creates Alice, Bob, a Curator, an Agent, and a second Tenant; retains Alice's Private Memory; synthesizes and approves a candidate; waits for outbox publication; lets Bob and the Agent recall Tenant Knowledge; and proves the second Tenant cannot observe it.
- Fault-injection tests stop PostgreSQL, RabbitMQ, and individual Tenant Neo4j containers at key command boundaries, checking fail-closed routing, explicit unavailability, pending operation identifiers, resumption, and absence of duplicate published knowledge.
- Observability tests assert correlation fields and would-have-limited metrics while scanning captured logs and traces for forbidden secrets and domain content.
- The provider-neutral release gate runs linting, static typing, unit and contract tests, Compose integration tests, the end-to-end tracer, migration checks, manifest/archive round trips, Caddy validation, and backup/restore verification.
- Existing upstream Agent Memory smoke behavior is retained as a lower-level Adapter check, but it is not accepted as proof of platform authorization or isolation.

## Out of Scope

- OIDC, SSO, SCIM, identity-provider deployment, and browser login.
- Neo4j Enterprise features, a shared Neo4j instance between Tenants, or a hosted memory service.
- High availability, clustering, multi-server orchestration, Kubernetes, and zero-downtime Neo4j Community backups.
- Cross-Tenant memory sharing, global knowledge, or a Principal selecting a Tenant in request data.
- Automatic Agent approval, automatic Promotion, or curator access to source Private Memory.
- User self-service hard deletion and immediate Tenant destruction.
- A public administration API, web administration interface, or Grafana access for Tenant users.
- Telegram group chats, multiple Tenant bindings for one Telegram identity, free-form message execution, or Telegram-managed identity.
- Redis, a second broker, and request rejection based on iteration-1 shadow quotas.
- A self-hosted generative model. Local embeddings remain self-hosted; Anthropic inference is an operator-configured external dependency.
- Cloudflare DNS record creation, Cloudflare proxy-mode management, server firewall configuration, host disk encryption, SSH provisioning, and creation or credentialing of remote backup storage.

## Further Notes

- The first deployment target is a single production server. The architecture preserves Interfaces for later secret management, SSO, queue scaling, and infrastructure orchestration without claiming iteration-1 high availability.
- Expected initial capacity is five Tenants, fifty Users and Agents per Tenant, twenty concurrent sessions system-wide, and one hundred thousand Memory Items per Tenant. These are observability baselines rather than enforced quotas.
- Neo4j Agent Memory's self-hosted Bolt backend is retained because it provides the desired local graph behavior. The platform compensates for upstream per-user scoping limitations with a Tenant-per-instance boundary and its own authorization-aware Interface.
- ActiveGraph is treated as an evolving dependency. The implementation pins a proven package version and validates Pack loading, event persistence, Budget behavior, and replay in an early compatibility slice before building the production Knowledge Synthesis capability.
- Actual server deployment requires the target's SSH details, host paths, and operator-created secret files. The repository will provide validation and runbooks without inventing or extracting those credentials.

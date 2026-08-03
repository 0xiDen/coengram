# 07. MCP and Claude Code integration

Status: resolved

Blocked by: 03. Principal scopes, Delegation, and RBAC; 04. Reviewed knowledge
publication; 05. Correction and approved erasure.

## What to build

Expose the Memory Module to Claude Code through authenticated streamable HTTP MCP while
preserving the same authorization and behavior as typed HTTP. The MCP surface is small
and intention-oriented, derives all identity and routing from the Bearer token, and
hides Bolt, Cypher, database selectors, graph exports, and upstream Agent Memory tools.

## Acceptance criteria

- [x] Streamable HTTP MCP is available at `/mcp` and rejects missing, malformed,
      expired, or revoked Bearer Access Tokens with redacted errors.
- [x] MCP exposes `memory_recall`, `memory_retain`, `knowledge_propose`,
      `knowledge_review`, `memory_correct`, and `memory_request_erasure` with versioned,
      typed argument and result schemas.
- [x] Tool arguments contain no authoritative Tenant, Principal, Subject User, graph,
      database, or raw storage selector; all scope derives from the Tenant Session.
- [x] Raw Cypher, Bolt credentials, graph export, arbitrary database operations, and the
      upstream Neo4j Agent Memory MCP tools are not reachable through public MCP.
- [x] State-changing tools require or derive idempotency keys and retries return the
      original operation.
- [x] Knowledge review and erasure behavior enforce human role restrictions exactly as
      the Memory Module Interface does.
- [x] The same contract examples pass through typed HTTP and MCP for successful,
      denied, invalid, conflict, pending, and dependency-unavailable behavior.
- [x] Error payloads do not reveal another Tenant or Principal's existence, identifiers,
      counts, routes, credentials, or memory content.
- [x] A checked Claude Code configuration example expands `MEMORY_MCP_TOKEN` into the
      Authorization header without embedding a credential in source control.
- [x] Claude compatibility tests validate the example and exercise authenticated
      streamable HTTP using a test token without requiring a live Claude session.
- [x] Public documentation explains intended tool semantics, selective retention,
      pending publication, explicit dependency failures, and user confirmation
      responsibilities.

## Comments

_No comments yet._

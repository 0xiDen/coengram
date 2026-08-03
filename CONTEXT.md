# CoEngram

An authenticated memory system that lets human users and software agents retain and share relevant knowledge inside an engineering team's private context.

## Language

**Tenant**:
A server-enforced application security and memory-context boundary corresponding to an engineering team, such as the backend team for a product. Knowledge cannot cross tenant boundaries through supported requests unless an explicit future sharing mechanism permits it. The shared gateway, worker, and Docker host remain deployment-wide trust anchors in iteration 1.
_Avoid_: Team, workspace, organization

**Principal**:
An authenticated actor making a memory request. A principal is either a User or an Agent and receives access through Tenant Membership rather than choosing a tenant in tool arguments.
_Avoid_: Client, caller

**User**:
A human Principal who uses agents and owns personal memories within an authorized Tenant.
_Avoid_: End user, person account

**Agent**:
A non-human Principal, such as a Claude instance or workflow agent, with its own authenticated identity and non-administrative tenant-scoped permissions.
_Avoid_: Bot, MCP client

**Tenant Membership**:
The many-to-many authorization relationship that permits a Principal to operate inside a Tenant. A Principal may have several memberships, but each authenticated session selects exactly one.
_Avoid_: Team assignment, tenant tag

**Tenant Session**:
An authenticated Principal's access context bound to exactly one active Tenant. Memory operations derive the Tenant from this server-validated context rather than accepting an arbitrary tenant identifier from an agent.
_Avoid_: Tenant header, selected team

**Access Token**:
A revocable credential issued to one Principal for one Tenant Membership. An Agent token is either autonomous or bound to exactly one Delegation and Subject User; request arguments cannot change that scope.
_Avoid_: API key, tenant token

**Tenant Manifest**:
A versioned, secret-free representation of Tenants, Principals, Tenant Memberships, roles, and policies for validation, export, and idempotent import. Access Tokens are reissued after import rather than included in a manifest.
_Avoid_: Configuration export, tenant backup

**Tenant Memory Store**:
The isolated Neo4j Community instance and persistent graph owned by exactly one Tenant. Principals never select a store directly; their Tenant Session determines it.
_Avoid_: Tenant database, graph shard

**Control Store**:
The content-free PostgreSQL database containing global identity, Tenant Membership, Delegation, credential, routing, and operator-audit records.
_Avoid_: Admin database, auth database

**Tenant Operations Store**:
The PostgreSQL database owned by exactly one Tenant for Promotion workflow, tenant audit, provenance, fenced Agent Run snapshots and leases, and the native ActiveGraph event projection. It has tenant-specific credentials and no access to another Tenant's database.
_Avoid_: Tenant Postgres, event database

**Agent Run**:
One auditable execution of an agent goal whose events determine run state and can be replayed. Durable outcomes may become memory, but execution control remains outside memory.
_Avoid_: Session, workflow, trace

**Agent Invocation**:
An authenticated request to start an Agent capability with typed input, idempotency, and a response context. Invocation channels such as MCP, HTTP, CLI, schedules, or messaging adapt into the same command.
_Avoid_: Agent request, trigger, message

**Channel Binding**:
An operator-approved mapping from an external channel identity to one User, one Tenant Membership, and an Agent Delegation. In iteration 1, each Telegram direct-message identity has exactly one Channel Binding.
_Avoid_: Telegram mapping, linked account

**Knowledge Synthesis Agent**:
The first autonomous Agent, which turns explicitly selected Private Memory into a deduplicated, provenance-linked Knowledge Candidate for human review. It may propose but never approve Promotion.
_Avoid_: Curator agent, memory compiler

**Private Memory**:
A Principal's private conversations, preferences, and cross-run continuity. A delegated Agent may read its Subject User's Private Memory, but user-derived information remains owned by that User rather than being copied into the Agent's own scope.
_Avoid_: Personal memory, user memory, agent memory

**Memory Item**:
A durable, attributable unit of Private Memory or Tenant Knowledge with provenance, timestamps, and confidence. Corrections supersede an item rather than rewriting its history.
_Avoid_: Memory record, fact row

**Memory Archive**:
A versioned, scope-limited JSON Lines package with independently checked records and an aggregate archive checksum. A Principal's archive contains only its Private Memory, correction history, and content-free completed-erasure tombstones, while a Tenant archive contains Tenant Knowledge and privacy-safe governance records but no Principal's private content. Every record is decoded and validated before an import may mutate state; imported shared memory always becomes a reviewable Knowledge Candidate.
_Avoid_: Memory export, backup

**Tenant Knowledge**:
Reviewed facts, entities, conventions, and decisions intentionally shared for recall and learning by authorized Users and Agents in a Tenant.
_Avoid_: Team memory, shared memory

**Agent Record**:
An Agent's tool and reasoning history retained for audit and debugging but excluded from ordinary memory recall.
_Avoid_: Agent memory, trace memory

**Knowledge Candidate**:
A distilled claim proposed from Private Memory for possible inclusion in Tenant Knowledge, with provenance back to its private source but without exposing that source to reviewers.
_Avoid_: Suggested memory, draft knowledge

**Promotion**:
The human-reviewed transition by which an accepted Knowledge Candidate becomes Tenant Knowledge. Promotion copies an approved claim, records an audit trail, and never changes the visibility of its source Personal Memory.
_Avoid_: Upgrade, publish memory

**Knowledge Curator**:
A human User authorized to approve or reject Knowledge Candidates for a Tenant. A Tenant Administrator may act as Curator and may self-approve only when the Tenant has one human member.
_Avoid_: Reviewer, approver

**Tenant Administrator**:
A human User authorized to manage a Tenant's Principals, Tenant Memberships, Delegations, policies, and credential revocation.
_Avoid_: Owner, superuser

**Tenant Member**:
A User authorized to manage their own Private Memory, read Tenant Knowledge, and propose Knowledge Candidates inside a Tenant.
_Avoid_: Member, teammate

**Erasure Request**:
An audited request by a Principal to remove one of its Private Memory items from storage and recall. A human Tenant Administrator must approve it, and completion leaves only a content-free audit tombstone.
_Avoid_: Forget, delete memory

**Actor**:
The Principal that performed an operation. Audit records always identify the Actor even when an Agent is delegated to act for a User.
_Avoid_: Caller, executor

**Subject User**:
The User whose Private Memory a delegated Agent is authorized to access during a Tenant Session. Autonomous Agent sessions have no Subject User.
_Avoid_: Owner user, target user

**Delegation**:
A revocable authorization allowing an Agent to access one Subject User's Private Memory inside one Tenant, while preserving the Agent as the Actor. Each delegated Access Token binds exactly one Delegation.
_Avoid_: Impersonation, act-as header

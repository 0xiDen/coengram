# 05. Correction and approved erasure

Status: resolved

Blocked by: 04. Reviewed knowledge publication.

## What to build

Give Users control over incorrect or sensitive Private Memory without destroying audit
history casually. Correction supersedes an owned Memory Item. Erasure is an explicit,
audited request that needs a separate human Tenant Administrator decision before domain
content is removed from PostgreSQL and Neo4j and replaced by a content-free tombstone.

## Acceptance criteria

- [x] A Principal can inspect Memory Items it owns, including provenance, confidence,
      current/superseded status, and erasure state, but not another Principal's private
      content.
- [x] Correcting an owned Memory Item creates a new stable item linked by supersedence
      rather than overwriting the original.
- [x] Ordinary recall excludes superseded content and returns the current correction;
      historical audit remains attributable.
- [x] A Principal can create an idempotent Erasure Request only for a Memory Item it is
      authorized to own or act upon through Delegation.
- [x] Requesting erasure does not immediately remove or hide content in iteration 1.
- [x] Only a human Tenant Administrator can approve or reject an Erasure Request, and
      request and approval are separately attributed audit events.
- [x] Approved erasure removes domain content from PostgreSQL and the correct Tenant
      Memory Store and prevents all later ordinary recall.
- [x] Completion retains only a content-free tombstone sufficient to prove identifiers,
      decision, actor, and timing without retaining erased memory text or embeddings.
- [x] Graph or queue failure leaves erasure pending and retryable without reporting
      completion or returning erased content after successful application.
- [x] Agents cannot approve erasure, and a delegated Agent remains Actor while the bound
      User remains Subject User.
- [x] State-machine, idempotency, authorization, cross-Tenant isolation, queue-redelivery,
      and graph-application tests cover correction and every erasure transition.

## Comments

_No comments yet._
